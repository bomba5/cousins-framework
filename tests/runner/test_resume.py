"""Restart with resume: stop and start keep the session; a lost resume is loud."""
import json
import os
import time
import unittest
from unittest import mock

try:
    import claude_agent_sdk  # noqa: F401
except ImportError:
    raise unittest.SkipTest("claude-agent-sdk not installed")

from cousin_lib.delivery import Item
from cousin_lib.runner.sdk import SdkRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home
from tests.runner.test_sdk import ScriptedClient, assistant, init_msg, result


def _wait(pred, timeout=10.0):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


class TestResume(HermeticCase):
    def setUp(self):
        super().setUp()
        self.home = temp_home(self)
        root = self.home.parent.parent
        (root / "config").mkdir(exist_ok=True)
        (root / "config" / "law.md").write_text("1. The law.\n")
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": "/nonexistent/framework-root"})
        p.start(); self.addCleanup(p.stop)
        self.options = []

    def runner(self, *, source="none", refuse_resume=False, resume_as=None):
        def factory(options):
            self.options.append(options)
            asked = options.resume or (options.extra_args or {}).get("resume")
            sid = (resume_as or "s-live") if asked else "s-live"
            client = ScriptedClient(options, [[init_msg(source=source, session=sid),
                                               assistant(text="ok"), result(session=sid)]
                                              for _ in range(4)])
            if refuse_resume and asked:
                async def boom(prompt=None):
                    raise RuntimeError("no such session")
                client.connect = boom
            return client
        r = SdkRunner(self.home, client_factory=factory)
        self.addCleanup(lambda: r.stop(timeout=5))
        return r

    def one_turn(self, r, body="hi"):
        rec = r.enqueue(Item("operator:priya", "chat", body, sender="Priya"))
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["state"] == "done"))

    def inits(self, r):
        return [e["payload"]["session_id"] for e in r.events() if e["kind"] == "session_init"]

    def bodies(self, r):
        return [(r.inbox.get(i) or {}).get("body") or "" for i in range(1, 30)]

    def test_stop_and_start_keep_the_session_id(self):
        r1 = self.runner(); r1.start(); self.one_turn(r1); r1.stop(timeout=5)
        saved = json.loads((self.home / "data" / "runner-session.json").read_text())
        self.assertEqual(saved["session_id"], "s-live")
        r2 = self.runner(); r2.start(); self.one_turn(r2)
        self.assertEqual(self.inits(r2)[0], "s-live")          # the first init names the saved id
        self.assertEqual(r2.saved_session(), "s-live")
        kinds = [(e["kind"], e["payload"].get("subtype")) for e in r2.events()]
        self.assertIn(("system", "resumed"), kinds)
        self.assertNotIn(("system", "resume_failed"), kinds)

    def test_the_key_lane_resumes_from_the_store(self):
        # the lane comes from the init's apiKeySource, never from being given a key (R12)
        r1 = self.runner(source="ANTHROPIC_API_KEY"); r1.start(); self.one_turn(r1); r1.stop(timeout=5)
        self.assertEqual(json.loads((self.home / "data" / "runner-session.json").read_text())["lane"],
                         "key")
        r2 = self.runner(source="ANTHROPIC_API_KEY"); r2.start()
        self.assertTrue(_wait(lambda: len(self.options) == 2))
        self.assertEqual(self.options[1].resume, "s-live")
        self.assertNotIn("resume", self.options[1].extra_args or {})

    def test_a_key_given_to_the_runner_does_not_decide_the_lane(self):
        r1 = self.runner(); r1.api_key = "k-ignored"            # the init still says "none"
        r1.start(); self.one_turn(r1); r1.stop(timeout=5)
        r2 = self.runner(); r2.start()
        self.assertTrue(_wait(lambda: len(self.options) == 2))
        self.assertEqual(self.options[1].extra_args["resume"], "s-live")

    def test_the_login_lane_resumes_through_the_clis_own_flag(self):
        r1 = self.runner(); r1.start(); self.one_turn(r1); r1.stop(timeout=5)
        r2 = self.runner(); r2.start()
        self.assertTrue(_wait(lambda: len(self.options) == 2))
        self.assertIsNone(self.options[1].resume)                   # no store materialization
        self.assertEqual(self.options[1].extra_args["resume"], "s-live")
        self.assertIsNotNone(self.options[1].session_store)         # the store still mirrors

    def test_a_restart_runs_no_start_hooks_and_no_digest(self):
        with open(self.home / "cousin.toml", "a") as fh:
            fh.write('\n[session]\nstart_hooks = ["echo start >> %s/hooks.log"]\n' % self.home)
        (self.home / "STATUS.md").write_text("## Open loops\n- x\n")
        r1 = self.runner(); r1.start(); self.one_turn(r1); r1.stop(timeout=5)
        r2 = self.runner(); r2.start(); self.one_turn(r2)
        self.assertEqual((self.home / "hooks.log").read_text().split(), ["start"])  # r1 only
        self.assertEqual(sum("STATE DIGEST" in b for b in self.bodies(r2)), 1)     # r1's only

    def test_a_connect_that_refuses_the_resume_starts_fresh_with_the_digest(self):
        (self.home / "data" / "runner-session.json").write_text(
            json.dumps({"session_id": "s-gone", "generation": 0, "updated": 0}))
        (self.home / "STATUS.md").write_text("## Open loops\n- carry this\n")
        r = self.runner(refuse_resume=True); r.start()
        self.assertTrue(_wait(lambda: any("carry this" in b for b in self.bodies(r))))
        kinds = [(e["kind"], e["payload"].get("subtype")) for e in r.events()]
        self.assertIn(("system", "resume_failed"), kinds)
        self.assertIsNone(r.fatal)

    def test_a_resume_that_comes_back_as_another_session_is_a_lost_resume(self):
        (self.home / "data" / "runner-session.json").write_text(
            json.dumps({"session_id": "s-live", "generation": 0, "updated": 0}))
        (self.home / "STATUS.md").write_text("## Open loops\n- carry this too\n")
        r = self.runner(resume_as="s-other"); r.start(); self.one_turn(r)
        failed = [e["payload"] for e in r.events()
                  if e["kind"] == "system" and e["payload"].get("subtype") == "resume_failed"]
        self.assertEqual((failed[0]["session_id"], failed[0]["got"]), ("s-live", "s-other"))
        self.assertTrue(_wait(lambda: any("carry this too" in b for b in self.bodies(r))))
        self.assertEqual(r.saved_session(), "s-other")

    def test_a_brand_new_cousin_gets_no_digest(self):
        r = self.runner(); r.start(); self.one_turn(r)
        self.assertFalse(any("STATE DIGEST" in b for b in self.bodies(r)))


if __name__ == "__main__":
    unittest.main()
