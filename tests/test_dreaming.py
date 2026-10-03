"""Dreaming (cousin_lib/dreaming.py): the setting, when a pass is due, a
pass run under its budget with memory tools only, its record, undo, and
the loops worker. The memory side (dream_memory) is a fake here: this is
the harness's contract with it."""
import asyncio
import json
import os
import pathlib
import tempfile
import time
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

try:
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
except ImportError:                                  # pragma: no cover
    raise unittest.SkipTest("claude-agent-sdk not installed")

from cousin_lib import agent_settings, boot, dreaming, loops
from tests._hermetic import HermeticCase


class FakeOps:
    """dream_memory's contract, recorded."""

    def __init__(self, *, empty=False):
        self.calls = []
        self.empty = empty
        self.OPERATIONS = [{
            "name": "retire", "description": "retire one claim",
            "inputSchema": {"entry_id": str, "why": str}, "fn": self._retire}]

    def _retire(self, home, pass_id, args):
        if args.get("entry_id") == "L0-claim":
            raise ValueError("L0-L2 entries are never retired by a pass")
        return {"op": "retire", "topic": "kestrel", "entry_ids": [args["entry_id"]],
                "mark_id": "m-" + args["entry_id"], "why": args.get("why", "")}

    def slice_for(self, home, *, chars):
        self.calls.append(("slice", chars))
        return SimpleNamespace(text="the slice", through="e42", empty=self.empty)

    def prompt(self, piece):
        return "consolidate: " + piece.text

    def begin(self, home, pass_id):
        self.calls.append(("begin", pass_id))

    def commit(self, home, pass_id, through):
        self.calls.append(("commit", through))

    def release_stale(self, home, max_age):
        self.calls.append(("release_stale", max_age))

    def abandon(self, home, pass_id, reason):
        self.calls.append(("abandon", reason))

    def undo(self, home, pass_id, changes):
        return [{"op": "unretire", "entry_ids": c["entry_ids"]} for c in changes]


def _usage(n):
    return {"input_tokens": n, "output_tokens": 0}


class FakeClient:
    """A session that calls the dream server's tools through MCP, as the
    CLI would, then answers with the given usage."""

    def __init__(self, options, *, calls=(), usage=500, fail=None):
        self.options, self.calls, self.usage, self.fail = options, calls, usage, fail
        self.interrupted = False

    async def connect(self):
        if self.fail:
            raise self.fail

    async def query(self, prompt):
        self.prompt = prompt

    async def receive_response(self):
        from mcp.types import CallToolRequest, CallToolRequestParams
        server = self.options.mcp_servers["dream"]["instance"]
        self.results = []
        for name, args in self.calls:
            out = await server.request_handlers[CallToolRequest](CallToolRequest(
                method="tools/call", params=CallToolRequestParams(name=name, arguments=args)))
            self.results.append(out.root)
        yield AssistantMessage(content=[TextBlock("merged one duplicate")], model="sonnet",
                               usage=_usage(self.usage), message_id="m1")
        # the same API response arriving as a second message: counted once
        yield AssistantMessage(content=[TextBlock("done")], model="sonnet",
                               usage=_usage(self.usage), message_id="m1")
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                            num_turns=1, session_id="s", usage=_usage(self.usage))

    async def interrupt(self):
        self.interrupted = True

    async def disconnect(self):
        pass


class DreamCase(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        self.home = self.root / "cousins" / "wren"
        (self.home / "data").mkdir(parents=True)
        self.toml("")
        for name, value in (("for_cousin", object()), ("account_env", {})):
            p = mock.patch("cousin_lib.accounts." + name, return_value=value)
            p.start(); self.addCleanup(p.stop)

    def toml(self, agent_lines):
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\n\n[agent]\nrunner = "sdk"\n' + agent_lines)

    def run_pass(self, ops, **client_kw):
        clients = []

        def factory(options):
            clients.append(FakeClient(options, **client_kw))
            return clients[-1]
        end = dreaming.run_pass(self.home, self.root, trigger="manual", ops=ops,
                                client_factory=factory)
        return end, clients


class TestSetting(DreamCase):
    def test_off_unless_set_and_a_bad_value_is_off(self):
        self.assertEqual(dreaming.settings(self.home), {"mode": "off", "at": "03:00"})
        self.toml('dreaming = "sometimes"\ndreaming_at = "25:00"\n')
        self.assertEqual(dreaming.settings(self.home), {"mode": "off", "at": "03:00"})
        self.toml('dreaming = "nightly"\ndreaming_at = "02:30"\n')
        self.assertEqual(dreaming.settings(self.home), {"mode": "nightly", "at": "02:30"})

    def test_the_console_validates_it_and_needs_no_restart(self):
        self.assertEqual(agent_settings._check_value("dreaming", "rollover", "sdk", None),
                         "rollover")
        with self.assertRaisesRegex(ValueError, "one of off, nightly, rollover"):
            agent_settings._check_value("dreaming", "always", "sdk", None)
        with self.assertRaisesRegex(ValueError, "HH:MM"):
            agent_settings._check_value("dreaming_at", "3am", "sdk", None)
        self.assertFalse(agent_settings.needs_restart(["dreaming", "dreaming_at"]))
        self.assertTrue(agent_settings.needs_restart(["dreaming", "model"]))


class TestDue(DreamCase):
    def at(self, hhmm):
        h, m = (int(x) for x in hhmm.split(":"))
        return datetime.now().astimezone().replace(hour=h, minute=m, second=0, microsecond=0)

    def test_off_is_never_due(self):
        self.assertIsNone(dreaming.due(self.home, self.at("23:00")))

    def test_nightly_is_due_once_a_day_after_its_time(self):
        self.toml('dreaming = "nightly"\ndreaming_at = "03:00"\n')
        self.assertIsNone(dreaming.due(self.home, self.at("02:59")))
        self.assertEqual(dreaming.due(self.home, self.at("03:01")), "nightly")
        dreaming._write(self.home, {"pass_id": "p1", "event": "start",
                                    "started": self.at("03:01").timestamp()})
        self.assertIsNone(dreaming.due(self.home, self.at("05:00")))

    def test_rollover_is_due_on_a_request_newer_than_the_last_pass(self):
        self.toml('dreaming = "rollover"\n')
        self.assertIsNone(dreaming.due(self.home))
        boot.bump_generation(self.home)              # a rollover asks for a pass
        self.assertEqual(dreaming.due(self.home), "rollover")
        dreaming._write(self.home, {"pass_id": "p1", "event": "start",
                                    "started": time.time() + 1})
        self.assertIsNone(dreaming.due(self.home))

    def test_a_bump_asks_nothing_when_dreaming_is_not_rollover(self):
        self.toml('dreaming = "nightly"\n')
        boot.bump_generation(self.home)
        self.assertFalse((self.home / "data" / "dream.request").exists())


class TestPass(DreamCase):
    def test_a_pass_records_its_changes_and_commits_the_ledger(self):
        ops = FakeOps()
        end, (client,) = self.run_pass(ops, calls=[
            ("retire", {"entry_id": "abc", "why": "a newer claim says the barn"}),
            ("retire", {"entry_id": "L0-claim", "why": "no"})])
        self.assertEqual(end["result"], "done")
        self.assertEqual([c["entry_ids"] for c in end["changes"]], [["abc"]])
        self.assertTrue(client.results[1].isError)        # refused, told as a tool error
        self.assertIn(("commit", "e42"), ops.calls)
        # a dead pass's attempt is taken back before the slice
        self.assertEqual(ops.calls[0], ("release_stale", dreaming.PASS_TIMEOUT_S + 60))
        self.assertEqual(end["tokens"], 500)              # one response, counted once
        self.assertEqual(end["summary"], "done")
        self.assertEqual(client.prompt, "consolidate: the slice")
        # the session has no built-in tools: memory operations only
        self.assertEqual(client.options.tools, [])
        self.assertEqual(list(client.options.mcp_servers), ["dream"])
        # and the CLI attaches nothing else (the account's claude.ai connectors)
        self.assertIn("strict-mcp-config", client.options.extra_args)
        self.assertEqual(client.options.model, dreaming.DEFAULT_MODEL)
        (rec,) = dreaming.passes(self.home)
        self.assertEqual((rec["result"], rec["trigger"], len(rec["changes"])),
                         ("done", "manual", 1))

    def test_over_budget_interrupts_and_abandons(self):
        ops = FakeOps()
        end, (client,) = self.run_pass(ops, usage=dreaming.BUDGET_TOKENS + 1)
        self.assertEqual(end["result"], "budget")
        self.assertTrue(client.interrupted)
        self.assertIn(("abandon", "budget"), ops.calls)
        self.assertNotIn("commit", [c[0] for c in ops.calls])

    def test_cache_reads_count_at_their_billed_weight(self):
        self.assertEqual(dreaming._weighted({"input_tokens": 100, "output_tokens": 50,
                                             "cache_read_input_tokens": 10000,
                                             "cache_creation_input_tokens": 200}), 1350)

    def test_nothing_new_is_no_change_and_no_session(self):
        end, clients = self.run_pass(FakeOps(empty=True))
        self.assertEqual((end["result"], clients), ("no_change", []))

    def test_a_failure_is_a_recorded_error_and_abandons(self):
        ops = FakeOps()
        end, _ = self.run_pass(ops, fail=RuntimeError("the CLI died"))
        self.assertEqual(end["result"], "error")
        self.assertIn("the CLI died", end["error"])
        self.assertEqual(ops.calls[-1][0], "abandon")
        self.assertEqual(dreaming.passes(self.home)[0]["result"], "error")

    def test_a_pass_that_cannot_load_its_memory_side_is_recorded_once(self):
        self.toml('dreaming = "nightly"\ndreaming_at = "00:00"\n')
        with mock.patch.object(dreaming, "_memory_ops", side_effect=ImportError("no dream_memory")):
            end = dreaming.run_pass(self.home, self.root, trigger="nightly")
        self.assertEqual(end["result"], "error")
        self.assertIn("no dream_memory", end["error"])
        # recorded as today's attempt: the loops tick does not run it again
        self.assertEqual(dreaming.passes(self.home)[0]["result"], "error")
        self.assertIsNone(dreaming.due(self.home))

    def test_a_pass_that_died_without_an_end_shows_as_lost(self):
        dreaming._write(self.home, {"pass_id": "p0", "event": "start",
                                    "started": time.time() - dreaming.PASS_TIMEOUT_S - 120})
        self.assertEqual(dreaming.passes(self.home)[0]["result"], "lost")

    def test_a_lost_pass_is_read_and_undone_from_its_journal(self):
        ops = FakeOps()
        dreaming._write(self.home, {"pass_id": "p0", "event": "start",
                                    "started": time.time() - dreaming.PASS_TIMEOUT_S - 120})
        journal = self.home / "data" / "dreams" / "journal" / "p0.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_text(json.dumps({"op": "retire", "entry_ids": ["abc"],
                                       "mark_id": "m-abc"}) + "\n")
        rec = dreaming.passes(self.home)[0]
        self.assertEqual((rec["result"], len(rec["changes"])), ("lost", 1))
        out = dreaming.undo(self.home, "p0", by="ana", ops=ops)
        self.assertEqual(out, [{"op": "unretire", "entry_ids": ["abc"]}])

    def test_a_running_pass_is_never_undone(self):
        dreaming._write(self.home, {"pass_id": "p0", "event": "start", "started": time.time()})
        with self.assertRaisesRegex(ValueError, "still running"):
            dreaming.undo(self.home, "p0", ops=FakeOps())

    def test_undo_reverses_a_pass_once(self):
        ops = FakeOps()
        end, _ = self.run_pass(ops, calls=[("retire", {"entry_id": "abc", "why": "x"})])
        out = dreaming.undo(self.home, end["pass_id"], by="ana", ops=ops)
        self.assertEqual(out, [{"op": "unretire", "entry_ids": ["abc"]}])
        rec = dreaming.passes(self.home)[0]
        self.assertEqual(rec["undone_by"], "ana")
        with self.assertRaisesRegex(ValueError, "already undone"):
            dreaming.undo(self.home, end["pass_id"], ops=ops)
        with self.assertRaisesRegex(ValueError, "no dreaming pass"):
            dreaming.undo(self.home, "nope", ops=ops)


class TestLoopsWorker(DreamCase):
    def test_a_due_home_is_run_once_and_an_off_home_never(self):
        self.toml('dreaming = "nightly"\ndreaming_at = "00:00"\n')
        off = self.root / "cousins" / "kite"
        (off / "data").mkdir(parents=True)
        (off / "cousin.toml").write_text('[cousin]\nslug = "kite"\n')
        ran = []

        def run(home, root, trigger):
            ran.append((home.name, trigger))
            dreaming._write(home, {"pass_id": "p", "event": "start", "started": time.time()})
            return {"result": "no_change"}
        report = {"errors": []}
        loops.schedule_dreams(time.time(), report, homes=[("wren", self.home), ("kite", off)],
                              root=self.root, run=run, background=False)
        self.assertEqual(ran, [("wren", "nightly")])
        self.assertEqual(report["dreamed"], [("wren", {"result": "no_change"})])
        loops.schedule_dreams(time.time(), report, homes=[("wren", self.home)],
                              root=self.root, run=run, background=False)
        self.assertEqual(len(ran), 1)                     # once a day


if __name__ == "__main__":
    unittest.main()
