"""The live test: the REAL `opencode serve`
through the real OpencodeRunner (runner_for, no server_factory), against
the loopback fake provider (tests/runner/_fake_provider.py). No
credentials: the account is an `endpoint` account pointed at the fake
provider, whose model is `local/m1`. No network: the models.dev fetch is
off (`[agent] opencode_models_fetch = false`) and the plugin dependency
is seeded, so the test also runs in a network namespace with
only loopback, as it was measured.

Opt in: COUSIN_LIVE_OPENCODE=1 and OPENCODE_BIN=<the pinned opencode>.
Each test starts one server in a throwaway root and stops it (the server's
process group is killed if a stop failed). Besides the runner's outcomes,
each test pins the raw event shapes the fake server models: the
first idle after an announced prompt ends a turn, an abort or a failure
sends session.error before a doubled idle pair, a prompt sent while busy
is answered by the same run. A mismatch here is the fake lying."""
import contextlib
import json
import os
import queue
import signal
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from cousin_lib.delivery import Item
from cousin_lib.runner import auth
from cousin_lib.runner.main import runner_for
from cousin_lib.runner.opencode import PLUGIN_DEPENDENCY, OpencodeRunner
from tests._hermetic import HermeticCase
from tests.runner._fake_provider import FakeProvider

BIN = os.environ.get("OPENCODE_BIN")
LIVE = os.environ.get("COUSIN_LIVE_OPENCODE") == "1" and bool(BIN)
BOOT_S = 90.0       # the health bound (60 s) plus the instance bootstrap
TURN_S = 60.0


def _wait(pred, timeout, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return False


def _op(body):
    return Item("operator:priya", "chat", body, sender="Priya")


def sig(event):
    """What the runner reads of one raw opencode event; None for the rest."""
    t, p = event.get("type"), event.get("properties") or {}
    if t == "message.updated":
        info = p.get("info") or {}
        s = "msg.%s" % info.get("role")
        if info.get("error"):
            s += ":error=%s" % info["error"].get("name")
        elif info.get("finish"):
            s += ":%s" % info["finish"]
        return s
    if t == "message.part.updated":
        part = p.get("part") or {}
        s = "part.%s" % part.get("type")
        if part.get("type") == "tool":
            s += ":%s" % (part.get("state") or {}).get("status")
        elif part.get("type") in ("text", "reasoning"):
            s += ":end" if (part.get("time") or {}).get("end") else ":open"
        return s
    if t == "session.status":
        return "status.%s" % (p.get("status") or {}).get("type")
    if t == "session.error":
        return "error.%s" % (p.get("error") or {}).get("name")
    if t == "session.idle":
        return t
    return None


class _Recording(queue.Queue):
    """The runner's event queue, keeping every raw event it was given."""

    def __init__(self):
        super().__init__()
        self.raw = []

    def put(self, item, block=True, timeout=None):
        self.raw.append(item)
        return super().put(item, block, timeout)


@unittest.skipUnless(LIVE, "set COUSIN_LIVE_OPENCODE=1 and OPENCODE_BIN=<the pinned opencode>")
class TestLiveOpencode(HermeticCase):
    def boot(self, script):
        tmp = tempfile.TemporaryDirectory(prefix="oc-live-")
        self.addCleanup(tmp.cleanup)
        self.provider = FakeProvider(script).start()
        self.addCleanup(self.provider.close)
        root = Path(tmp.name)
        self.home = root / "cousins" / "wren"
        for sub in ("data", "run", "memory"):
            (self.home / sub).mkdir(parents=True)
        (root / "config").mkdir()
        (root / "config" / "accounts.toml").write_text(
            '[accounts.lab]\nkind = "opencode"\nendpoint = "%s"\nendpoint_model = "m1"\n'
            % self.provider.url)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n\n[agent]\nrunner = "opencode"\n'
            'account = "lab"\nmodel = "local/m1"\nopencode_bin = "%s"\n'
            'opencode_models_fetch = false\n' % BIN)
        r = runner_for(self.home)
        self.assertIsInstance(r, OpencodeRunner)
        self.raw = r._events = _Recording()
        self.addCleanup(self._kill, r)
        r.start()
        self.assertTrue(_wait(lambda: r.opencode_session is not None or r.fatal, BOOT_S),
                        "no session within %ss" % BOOT_S)
        self.assertIsNone(r.fatal)
        self.runner = r
        return r

    def _kill(self, r):
        r.stop(timeout=15)
        server = r._server
        if server is not None and server.alive():      # never leave a server behind
            try:
                os.killpg(server.pid, signal.SIGKILL)
            except OSError:
                pass
        self.assertFalse(server is not None and server.alive(), "opencode serve still running")

    def outcome(self, receipt):
        row = self.runner.inbox.get(receipt.inbox_id)
        return row["outcome"] if row["state"] == "done" else None

    def done(self, receipt, timeout=TURN_S):
        self.assertTrue(_wait(lambda: self.outcome(receipt) is not None, timeout),
                        "row %d not closed; events: %s" % (receipt.inbox_id, self.kinds()))
        # the row closes just before its result is written
        self.assertTrue(_wait(lambda: any(receipt.inbox_id in x["inbox_ids"]
                                          for x in self.payloads("result")), 10))
        return self.outcome(receipt)

    def kinds(self):
        return [e["kind"] for e in self.runner.events()]

    def payloads(self, kind):
        return [e["payload"] for e in self.runner.events() if e["kind"] == kind]

    def sigs(self):
        return [s for s in (sig(e) for e in list(self.raw.raw)) if s]

    def settled_tail(self):
        """Wait until opencode has emitted its whole tail (the doubled
        idle after an abort or a failure comes after the runner settled)."""
        time.sleep(1.5)
        return self.sigs()

    # -- (1) one turn --------------------------------------------------------------
    def test_one_turn_is_answered(self):
        r = self.boot([("text", "Hello from the fake provider.")])
        a = r.enqueue(_op("say hello"))
        self.assertEqual(self.done(a), "delivered")
        self.assertIn({"text": "Hello from the fake provider."}, self.payloads("text"))
        result = self.payloads("result")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["inbox_ids"], [a.inbox_id])
        self.assertFalse(result[0]["is_error"])
        self.assertFalse(result[0]["interrupted"])
        # opencode stored exactly the text the runner sent (the echo is an exact match)
        self.assertEqual([u["echo_of"] for u in self.payloads("user")], [a.inbox_id])
        # the fake provider is the model's endpoint; the composed
        # system prompt reaches it, naming the tools as opencode does
        chats = self.provider.chats(aux=True)
        self.assertEqual({(c["path"], c["model"]) for c in chats},
                         {("/v1/chat/completions", "m1")})
        self.assertEqual([c["path"] for c in self.provider.requests if not c["chat"]], [])
        main = self.provider.chats()
        self.assertEqual(len(main), 1)
        self.assertIn("say hello", main[0]["last_user"])
        self.assertIn("cousin_reply", main[0]["system"])
        self.assertTrue(main[0]["completed"])
        # the raw shape: announced, answered, the first idle ends it
        s = self.sigs()
        self.assertLess(s.index("msg.user"), s.index("status.busy"))
        self.assertLess(s.index("part.text:end"), s.index("session.idle"))
        self.assertLess(s.index("msg.assistant:stop"), s.index("session.idle"))
        self.assertEqual(s.count("session.idle"), 1)
        # the plugin dependency was seeded, so opencode installed nothing
        lock = json.loads((r.account.data_dir / "config" / "opencode" / "package-lock.json")
                          .read_text())
        self.assertIn(PLUGIN_DEPENDENCY, lock["packages"][""]["dependencies"])
        self.assertEqual(os.listdir(r.account.data_dir / "config" / "opencode" / "node_modules"),
                         [])

    # -- (2) a framework tool over MCP -----------------------------------------------
    def test_a_framework_tool_call_reaches_the_runner_and_its_result_the_model(self):
        r = self.boot([("tool", "cousin_reply", {"text": "hello from the fake model"}),
                       ("text", "replied")])
        a = r.enqueue(_op("please reply"))
        self.assertEqual(self.done(a), "delivered")
        first, second = self.provider.chats()[:2]
        # the model really sees the cousin_* names
        self.assertIn("cousin_reply", first["tools"])
        self.assertIn("cousin_handoff", first["tools"])
        self.assertNotIn("mcp__cousin__reply", first["tools"])
        # the call reached the runner's own tool (in this process, on the live turn)
        tool = self.payloads("tool")
        self.assertEqual([(t["name"], t["input"]) for t in tool],
                         [("cousin_reply", {"text": "hello from the fake model"})])
        calls = self.payloads("tool_call")
        self.assertEqual([(c["tool"], c["is_error"]) for c in calls], [("reply", False)])
        results = self.payloads("tool_result")
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["is_error"])
        self.assertIn("replied to priya", results[0]["text"])
        with contextlib.closing(sqlite3.connect(self.home / "data" / "chat.db")) as db:
            rows = db.execute("SELECT chat_user, message FROM messages").fetchall()
        self.assertEqual(rows, [("priya", "hello from the fake model")])
        # and the model got the result back
        self.assertEqual(len(second["tool_results"]), 1)
        self.assertIn("replied to priya", second["tool_results"][0]["content"])
        self.assertIn({"text": "replied"}, self.payloads("text"))
        s = self.sigs()
        self.assertLess(s.index("part.tool:running"), s.index("part.tool:completed"))
        self.assertLess(s.index("msg.assistant:tool-calls"), s.index("msg.assistant:stop"))
        self.assertEqual(s.count("session.idle"), 1)

    # -- (3) interrupt mid reply ---------------------------------------------------------
    def test_an_interrupt_mid_reply_aborts_and_the_row_is_delivered(self):
        r = self.boot([("slow", 120, "partial answer that never ends")])
        a = r.enqueue(_op("talk slowly"))
        self.provider.wait(lambda reqs: any(q.get("chat") and not q.get("aux") for q in reqs),
                           TURN_S)
        self.assertTrue(_wait(lambda: any(e.get("type") == "message.part.delta"
                                          for e in list(self.raw.raw)), 10),
                        "the model's reply started streaming")
        t0 = time.monotonic()
        self.assertTrue(r.interrupt())
        self.assertEqual(self.done(a, 15), "delivered")      # the turn's first row
        self.assertTrue(_wait(lambda: r.state() == "idle", 15))
        took = time.monotonic() - t0
        self.assertLess(took, 10.0)
        result = self.payloads("result")
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["interrupted"])
        self.assertFalse(result[0]["is_error"])
        self.assertEqual(result[0]["inbox_ids"], [a.inbox_id])
        self.assertIn({"text": "partial ", "partial": True}, self.payloads("text"))
        # opencode closed the provider's stream
        self.assertTrue(_wait(lambda: self.provider.chats()[0].get("disconnected"), 10))
        s = self.settled_tail()
        err = s.index("error.MessageAbortedError")
        idle = s.index("session.idle")
        self.assertLess(err, idle)
        self.assertEqual(s.count("session.idle"), 2, "the doubled idle pair after an abort")
        self.assertIn("part.text:end", s[idle:], "the open part ends after the first idle")
        self.assertIn("msg.assistant:error=MessageAbortedError", s[idle:])
        self.assertEqual(len(self.payloads("result")), 1, "the late tail closes nothing")

    def test_an_abort_requeues_the_prompt_folded_behind_the_running_one(self):
        r = self.boot([("slow", 120, "the first answer, cut"), ("text", "the second, later")])
        a = r.enqueue(_op("first, slow"))
        self.provider.wait(lambda reqs: any(q.get("chat") and not q.get("aux") for q in reqs),
                           TURN_S)
        b = r.enqueue(_op("second, folded"))
        self.assertTrue(_wait(lambda: b.inbox_id in [u["echo_of"] for u in
                                                     self.payloads("user")], 10),
                        "opencode announced the folded prompt")
        self.assertTrue(r.interrupt())
        self.assertEqual(self.done(a, 15), "delivered")
        first = self.payloads("result")[0]
        self.assertTrue(first["interrupted"])
        self.assertEqual(first["inbox_ids"], [a.inbox_id])
        self.assertEqual(first["requeued"], [b.inbox_id], "the abort dropped it: requeued")
        # requeued, it runs as its own turn and is answered once
        self.assertEqual(self.done(b), "delivered")
        results = self.payloads("result")
        self.assertEqual([x["inbox_ids"] for x in results], [[a.inbox_id], [b.inbox_id]])
        self.assertFalse(results[1]["interrupted"])
        self.assertIn({"text": "the second, later"}, self.payloads("text"))
        lasts = [c["last_user"] for c in self.provider.chats()]
        self.assertEqual(len(lasts), 2)
        self.assertIn("second, folded", lasts[1])

    def test_an_interrupt_before_the_model_answers_delivers_the_row(self):
        """opencode's first prompt of a server spends seconds on its own setup
        before it calls the model: an abort then is one idle pair, no error,
        no answer (the fake's PREP step)."""
        r = self.boot([("text", "never asked")])
        a = r.enqueue(_op("stop me early"))
        self.assertTrue(_wait(lambda: a.inbox_id in [u["echo_of"] for u in
                                                     self.payloads("user")], TURN_S))
        self.assertTrue(r.interrupt())
        self.assertEqual(self.done(a, 15), "delivered")
        result = self.payloads("result")
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["interrupted"])
        self.assertFalse(result[0]["is_error"])
        s = self.settled_tail()
        if not self.provider.chats():           # the abort landed before the model call
            self.assertNotIn("error.MessageAbortedError", s)
            self.assertEqual(s.count("session.idle"), 1)
            self.assertNotIn("msg.assistant", [x.split(":")[0] for x in s])
        # the next turn runs normally
        b = r.enqueue(_op("now answer"))
        self.assertEqual(self.done(b), "delivered")

    # -- (4) a 401 from the provider -----------------------------------------------------
    def test_a_401_is_the_login_required_shape(self):
        r = self.boot([("status", 401, "Incorrect API key provided")])
        a = r.enqueue(_op("hello"))
        self.assertTrue(_wait(lambda: "auth" in self.kinds(), TURN_S), self.kinds())
        result = self.payloads("result")
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["is_error"])
        self.assertEqual(result[0]["auth"], auth.LOGIN)
        self.assertEqual(result[0]["inbox_ids"], [])
        self.assertEqual(result[0]["requeued"], [a.inbox_id])
        self.assertIn("401", result[0]["error"])
        event = self.payloads("auth")[0]
        self.assertEqual((event["account"], event["kind"], event["reason"]),
                         ("lab", "opencode", auth.LOGIN))
        self.assertIn(self.provider.url, event["action"])
        self.assertEqual(auth.read_login_required(self.home)["account"], "lab")
        self.assertTrue(r.login_required())
        self.assertEqual(r.state(), "errored")
        self.assertEqual(r.inbox.get(a.inbox_id)["state"], "queued", "kept for after the fix")
        self.assertEqual(len(self.provider.chats()), 1, "not retried (isRetryable false)")
        s = self.settled_tail()
        self.assertLess(s.index("error.APIError"), s.index("session.idle"))
        self.assertEqual(s.count("session.idle"), 2)
        self.assertIn("msg.assistant:error=APIError", s)

    # -- (5) midturn_fold, measured --------------------------------------------------------
    def test_a_second_operator_row_sent_while_busy_closes_in_the_same_turn(self):
        r = self.boot([("slow", 3, "the first answer"), ("text", "the second answer")])
        a = r.enqueue(_op("first"))
        self.provider.wait(lambda reqs: any(q.get("chat") and not q.get("aux") for q in reqs),
                           TURN_S)
        b = r.enqueue(_op("second, mid-turn"))
        self.assertEqual(self.done(a), "delivered")
        self.assertEqual(self.done(b, 10), "delivered")
        time.sleep(0.5)
        results = self.payloads("result")
        self.assertEqual(len(results), 1, "one result closes both rows")
        self.assertEqual(sorted(results[0]["inbox_ids"]), sorted([a.inbox_id, b.inbox_id]))
        self.assertEqual(len(self.payloads("turn_start")), 1)
        self.assertEqual([t["text"] for t in self.payloads("text")],
                         ["the first answer", "the second answer"])
        lasts = [c["last_user"] for c in self.provider.chats()]
        self.assertEqual(len(lasts), 2)
        self.assertIn("second, mid-turn", lasts[1])
        s = self.sigs()
        self.assertEqual(s.count("session.idle"), 1, "one run answered both")
        self.assertEqual(s.count("msg.assistant:stop"), 4, "two answers, two updates each")
        kinds = [(e["kind"], e["payload"]) for e in r.events()]
        echo_b = kinds.index(("user", next(p for k, p in kinds if k == "user"
                                           and p["echo_of"] == b.inbox_id)))
        self.assertLess(echo_b, kinds.index(("text", {"text": "the first answer"})),
                        "the second prompt was stored while the first was answered")


if __name__ == "__main__":
    unittest.main()
