"""OpencodeRunner: rollover, context pressure and the wiring
(delivery.RUNNER_KINDS, runner.main.runner_for), against
the fake `opencode serve`. Never the opencode binary."""
import contextlib
import http.client
import io
import json
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from cousin_lib import boot, delivery
from cousin_lib.delivery import Item
from cousin_lib.runner import main as runner_main
from cousin_lib.runner.base import RunnerError
from cousin_lib.runner.opencode import OpencodeRunner
from cousin_lib.runner.opencode_http import OpencodeError
from tests.runner.test_opencode import ENDPOINT, Factory, OpencodeCase, _op, _wait

LIMITED = [{"id": "local", "name": "local", "source": "config", "env": [], "options": {},
            "key": "sk-testa-not-for-the-stream",
            "models": {"m1": {"id": "m1", "providerID": "local", "name": "m1",
                              "limit": {"context": 16, "output": 4}}}}]


def call_tool(runner, name, arguments):
    """One MCP tools/call on the runner's own server, as opencode makes it."""
    parts = urlsplit(runner._mcp.url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    conn.request("POST", parts.path, body=json.dumps({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": name, "arguments": arguments}}),
        headers={"Authorization": "Bearer %s" % runner._mcp.token,
                 "Content-Type": "application/json", "Accept": "application/json"})
    out = json.loads(conn.getresponse().read())
    conn.close()
    return out["result"]


def sessions_created(fake):
    return [q for q in fake.requests if q["method"] == "POST" and q["path"] == "/session"]


class TestRollover(OpencodeCase):
    def test_a_rollover_asks_for_the_handoff_then_starts_a_new_session_digest_first(self):
        r = self.started(self.runner([[("SLOW", 1.0), ("text", "handed over")]]))
        old, g0 = r.opencode_session, boot.read_generation(r.home)
        answers = []

        def handoff_when_asked():
            self.assertTrue(_wait(lambda: len(self.prompts()) == 1))
            answers.append(call_tool(r, "handoff", {
                "position": "mid widget", "next_action": "finish the widget",
                "status": "widget half done"}))

        import threading
        t = threading.Thread(target=handoff_when_asked)
        t.start()
        out = r.rollover("contract")
        t.join(10)
        self.assertTrue(out["ok"], out)
        self.assertFalse(answers[0]["isError"], answers)
        self.assertEqual((out["handoff"], out["generation"], out["old_session"]),
                         ("clean", g0 + 1, old))
        self.assertEqual(boot.read_generation(r.home), g0 + 1)
        self.assertNotEqual(r.opencode_session, old)
        self.assertEqual(out["new_session"], r.opencode_session)
        self.assertEqual(len(sessions_created(self.factory.fake)), 2)
        ask = self.prompts()[0]["body"]
        self.assertIn("Your generation is ending (contract)", ask["parts"][0]["text"])
        self.assertIn(old, self.prompts()[0]["path"])
        self.assertTrue(_wait(lambda: len(self.prompts()) == 2))
        first = self.prompts()[1]
        self.assertIn(r.opencode_session, first["path"])
        self.assertIn("STATE DIGEST FOR COUSIN: wren", first["body"]["parts"][0]["text"])
        self.assertIn("finish the widget", (r.home / "data" / "handoff.md").read_text())
        on_file = json.loads((r.home / "data" / "runner-session.json").read_text())
        self.assertEqual((on_file["session_id"], on_file["lane"]), (r.opencode_session, "opencode"))
        self.assertTrue((r.home / "data" / "generations" / ("gen-%04d" % g0)).is_dir())
        phases = [p["phase"] for p in self.payloads(r, "rollover")]
        self.assertEqual(phases[0], "start")
        self.assertIn("done", phases)
        # the handoff turn is the runner's own: no inbox row, no result
        self.assertTrue(_wait(lambda: len(self.payloads(r, "result")) == 1))
        self.assertTrue(_wait(lambda: r.state() == "idle"))
        rec = r.enqueue(_op("after the rollover"))
        self.assertTrue(_wait(lambda: self.outcome(r, rec) == "delivered"))
        self.assertIn(r.opencode_session, self.prompts()[-1]["path"])

    def test_the_digest_turn_after_a_rollover_is_guarded_too(self):
        """The rollover runs the new session's digest
        turn itself, so it gets the per-turn check: a config source written
        during the handoff turn stops it before its prompt."""
        r = self.started(self.runner([[("SLOW", 1.0), ("text", "no handoff")],
                                      [("text", "digest read")]]))
        g = Path(r.account.data_dir) / "config" / "opencode"

        def write_while_handing_off():
            self.assertTrue(_wait(lambda: len(self.prompts()) == 1))
            g.mkdir(parents=True, exist_ok=True)
            (g / "opencode.json").write_text("{}")
        import threading
        t = threading.Thread(target=write_while_handing_off)
        t.start()
        r.rollover("contract")
        t.join(10)
        self.assertTrue(_wait(lambda: r.fatal is not None, 8), "the digest turn ran unguarded")
        self.assertIn("opencode.json", r.fatal)
        self.assertEqual(len(self.prompts()), 1, "no digest prompt")
        # the digest row went back to the queue (durable: the next start runs
        # it), and nothing is left claimed
        self.assertTrue(_wait(lambda: r.inbox.pending() >= 1))
        self.assertEqual(r.inbox.unfinished(), r.inbox.pending())

    def test_a_handoff_the_model_never_writes_is_an_emergency_and_the_generation_moves(self):
        r = self.started(self.runner([[("text", "I would rather not")]]))
        g0 = boot.read_generation(r.home)
        out = r.rollover("contract")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["handoff"], "emergency")
        self.assertEqual(boot.read_generation(r.home), g0 + 1)
        handoff = (r.home / "data" / "handoff.md").read_text()
        self.assertIn("EMERGENCY HANDOFF", handoff)
        self.assertIn("the model finished its turn without calling handoff", handoff)
        self.assertIn("I would rather not", handoff)          # the session's tail
        emergency = [p for p in self.payloads(r, "rollover") if p.get("handoff") == "emergency"]
        self.assertTrue(emergency)

    def test_a_new_session_that_cannot_be_created_keeps_the_old_one(self):
        r = self.started(self.runner())
        old, g0 = r.opencode_session, boot.read_generation(r.home)
        with mock.patch.object(r._client, "create_session",
                               side_effect=OpencodeError("POST /session: HTTP 500 boom",
                                                         status=500)):
            out = r.rollover("contract")
        self.assertFalse(out["ok"])
        self.assertIn("HTTP 500 boom", out["reason"])
        self.assertEqual((r.opencode_session, boot.read_generation(r.home)), (old, g0))
        self.assertEqual(json.loads((r.home / "data" / "runner-session.json").read_text())
                         ["session_id"], old)
        # The state flips before its event row lands: wait on the events.
        states = lambda: [s["to"] for s in self.payloads(r, "state")]
        self.assertTrue(_wait(lambda: states()[-1:] == ["idle"]), states())
        self.assertEqual(states()[-3:], ["rolling_over", "errored", "idle"])
        rec = r.enqueue(_op("still here"))
        self.assertTrue(_wait(lambda: self.outcome(r, rec) == "delivered"))
        self.assertIn(old, self.prompts()[-1]["path"])

    def test_a_stop_during_the_handoff_requeues_the_flip_row(self):
        r = self.started(self.runner([[("HANG",)]]))
        rollover_row = r.inbox.put(Item("system", "flip", "contract", sender="runner"))
        self.assertTrue(_wait(lambda: r.state() == "rolling_over"))
        self.assertTrue(_wait(lambda: len(self.prompts()) == 1))
        r.stop(timeout=5)
        self.assertEqual(r.inbox.get(rollover_row)["state"], "queued")
        self.assertEqual(boot.read_generation(r.home), 0)


class TestPressure(OpencodeCase):
    def test_context_pressure_requests_a_rollover(self):
        """The last answer's tokens against the model's limit.context
        (GET /config/providers); 14 of 16 is over the 80% default."""
        r = self.started(self.runner(factory=Factory([[("text", "big")]], providers=LIMITED)))
        g0 = boot.read_generation(r.home)
        a = r.enqueue(_op("fill the context"))
        self.assertTrue(_wait(lambda: self.outcome(r, a) == "delivered"))
        self.assertTrue(_wait(lambda: boot.read_generation(r.home) == g0 + 1, 10))
        requested = [p for p in self.payloads(r, "rollover") if p["phase"] == "requested"]
        self.assertEqual(requested[0]["reason"], "context pressure 87%")
        stream = "".join(p.read_text() for p in (r.home / "data" / "stream").glob("*.jsonl"))
        self.assertNotIn("sk-testa-not-for-the-stream", stream)   # the answer's keys stay put

    def test_pressure_measures_the_last_answer_not_the_turns_sum(self):
        """Two answers of 14 tokens against a limit of 32: the turn spent 28
        (87%, its usage record), the context holds the last answer's 14."""
        roomy = json.loads(json.dumps(LIMITED))
        roomy[0]["models"]["m1"]["limit"]["context"] = 32
        r = self.started(self.runner(factory=Factory(
            [[("tool", "cousin_memory", {"command": "search"}, "found"), ("text", "done")]],
            providers=roomy)))
        a = r.enqueue(_op("two answers"))
        self.assertTrue(_wait(lambda: self.outcome(r, a) == "delivered"))
        self.assertTrue(_wait(lambda: self.payloads(r, "usage")))
        self.assertEqual(self.payloads(r, "usage")[0]["total"], 28)
        self.assertTrue(_wait(lambda: r.state() == "idle"))
        time.sleep(0.3)
        self.assertEqual([p for p in self.payloads(r, "rollover") if p["phase"] == "requested"],
                         [])
        self.assertEqual(r.inbox.open_rows("flip"), [])

    def test_no_limit_means_no_pressure_and_says_so_once(self):
        r = self.started(self.runner())
        for body in ("one", "two"):
            a = r.enqueue(_op(body))
            self.assertTrue(_wait(lambda: self.outcome(r, a) == "delivered"))
        off = [p for p in self.payloads(r, "system") if p.get("subtype") == "pressure_off"]
        self.assertEqual(len(off), 1)
        self.assertEqual(off[0]["model"], "local/m1")
        self.assertEqual(r.inbox.open_rows("flip"), [])


class TestWiring(OpencodeCase):
    def accounts_toml(self, text):
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "config" / "accounts.toml").write_text(text)

    LAB = '[accounts.lab]\nkind = "opencode"\nendpoint = "%s"\nendpoint_model = "m1"\n' % ENDPOINT

    def test_opencode_is_a_runner_kind_with_one_source(self):
        self.assertIn("opencode", delivery.RUNNER_KINDS)
        self.assertIs(runner_main.KINDS, delivery.RUNNER_KINDS)

    def test_runner_for_builds_the_opencode_runner_on_its_account(self):
        home = self.home(extra='account = "lab"\n')
        self.accounts_toml(self.LAB)
        (home / "policy.toml").write_text('deny_tools = ["WebFetch"]\n')
        r = runner_main.runner_for(home)
        self.assertIsInstance(r, OpencodeRunner)
        self.assertEqual((r.account.name, r.account.kind, r.model), ("lab", "opencode", "local/m1"))
        self.assertEqual(r.policy.deny_tools, ("WebFetch",))
        self.assertIsNone(r._thread)                      # built, not started

    def test_the_lanes_do_not_mix(self):
        home = self.home()                                # no account: the host's login
        with self.assertRaises(RunnerError) as err:
            runner_main.runner_for(home)
        self.assertIn('runs on a kind = "opencode" account only', str(err.exception))
        sdk_home = self.home(extra='account = "lab"\n')
        (sdk_home / "cousin.toml").write_text((sdk_home / "cousin.toml").read_text()
                                              .replace('runner = "opencode"', 'runner = "sdk"'))
        self.accounts_toml(self.LAB)
        with self.assertRaises(RunnerError) as err:
            runner_main.runner_for(sdk_home)
        self.assertIn('runs with runner = "opencode" only', str(err.exception))
        # a fake cousin too (runners.md says so; it returned
        # before the lane check)
        fake_home = self.home(extra='account = "lab"\n')
        (fake_home / "cousin.toml").write_text((fake_home / "cousin.toml").read_text()
                                               .replace('runner = "opencode"', 'runner = "fake"'))
        self.accounts_toml(self.LAB)
        with self.assertRaises(RunnerError) as err:
            runner_main.runner_for(fake_home)
        self.assertIn('runs with runner = "opencode" only, not runner = "fake"', str(err.exception))

    def test_a_cousin_without_a_model_or_on_the_bridge_exits_2(self):
        for model, needle in ((None, "[agent] model is required"),
                              ("meridian/opus", "Claude-subscription bridge")):
            with self.subTest(model=model):
                home = self.home(model=model, extra='account = "lab"\n')
                self.accounts_toml(self.LAB)
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    rc = runner_main.runner_main(["--home", str(home), "--once"])
                self.assertEqual(rc, 2)
                self.assertIn(needle, err.getvalue())
                self.assertFalse((home / "data" / "stream").exists()
                                 and any((home / "data" / "stream").iterdir()))

    def test_validate_is_refused_on_the_opencode_lane(self):
        home = self.home(extra='account = "lab"\n')
        self.accounts_toml(self.LAB)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = runner_main.runner_main(["--home", str(home), "--check-auth", "--validate"])
        self.assertEqual(rc, 2)
        self.assertIn("loggedIn=True", out.getvalue())
        self.assertIn("--validate", err.getvalue())


if __name__ == "__main__":
    unittest.main()
