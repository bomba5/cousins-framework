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

from cousin_lib import accounts, boot
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

    def runner(self, *, source="none", refuse_resume=False, resume_as=None, account=None):
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
        r = SdkRunner(self.home, client_factory=factory, account=account)
        self.addCleanup(lambda: r.stop(timeout=5))
        return r

    def one_turn(self, r, body="hi"):
        rec = r.enqueue(Item("operator:priya", "chat", body, sender="Priya"))
        # the runner closes a turn's rows, then appends the result and runs its post-turn
        # work (the session file, usage, mining, the proposal), then goes idle (#102)
        self.assertTrue(_wait(lambda: r.inbox.get(rec.inbox_id)["state"] == "done"
                              and r.state() == "idle"))

    def inits(self, r):
        return [e["payload"]["session_id"] for e in r.events() if e["kind"] == "session_init"]

    def bodies(self, r):
        return [(r.inbox.get(i) or {}).get("body") or "" for i in range(1, 30)]

    def session_file(self):
        return json.loads((self.home / "data" / "runner-session.json").read_text())

    def test_stop_and_start_keep_the_session_id(self):
        g0 = boot.read_generation(self.home)
        r1 = self.runner(); r1.start(); self.one_turn(r1); r1.stop(timeout=5)
        saved = json.loads((self.home / "data" / "runner-session.json").read_text())
        self.assertEqual(saved["session_id"], "s-live")
        r2 = self.runner(); r2.start(); self.one_turn(r2)
        self.assertEqual(self.inits(r2)[0], "s-live")          # the first init names the saved id
        self.assertEqual(r2.saved_session(), "s-live")
        kinds = [(e["kind"], e["payload"].get("subtype")) for e in r2.events()]
        self.assertIn(("system", "resumed"), kinds)
        self.assertNotIn(("system", "resume_failed"), kinds)
        self.assertEqual(boot.read_generation(self.home), g0)   # a deploy costs no generation

    def test_the_key_lane_resumes_from_the_store(self):
        # the account's kind decides the resume path (R12 folded into accounts)
        key = accounts.Account("metered", "anthropic-key", None, None, secret_value="k-test")
        r1 = self.runner(source="ANTHROPIC_API_KEY", account=key)
        r1.start(); self.one_turn(r1); r1.stop(timeout=5)
        self.assertEqual(json.loads((self.home / "data" / "runner-session.json").read_text())["lane"],
                         "key")
        r2 = self.runner(source="ANTHROPIC_API_KEY", account=key); r2.start()
        self.assertTrue(_wait(lambda: len(self.options) == 2))
        self.assertEqual(self.options[1].resume, "s-live")
        self.assertNotIn("resume", self.options[1].extra_args or {})

    def test_the_account_kind_decides_the_resume_path_whatever_the_init_says(self):
        # a key account whose init reports "none" still resumes store-backed;
        # the init's source is the CHECK that the account took effect (Task 16)
        key = accounts.Account("metered", "anthropic-key", None, None, secret_value="k-test")
        r1 = self.runner(account=key); r1.start(); self.one_turn(r1); r1.stop(timeout=5)
        self.assertEqual(self.session_file()["lane"], "login")     # the record, not the decider
        r2 = self.runner(account=key); r2.start()
        self.assertTrue(_wait(lambda: len(self.options) == 2))
        self.assertEqual(self.options[1].resume, "s-live")
        self.assertNotIn("resume", self.options[1].extra_args or {})

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
        digest = next(i for i in range(1, 30)
                      if "carry this too" in ((r.inbox.get(i) or {}).get("body") or ""))
        self.assertTrue(_wait(lambda: r.inbox.get(digest)["outcome"] == "delivered"))
        system = [e["payload"] for e in r.events() if e["kind"] == "system"]
        at = [p.get("subtype") for p in system].index("resume_failed")
        fresh = [p for p in system if p.get("subtype") == "fresh"]
        self.assertEqual(fresh, [{"subtype": "fresh", "digest": True}])    # exactly one
        self.assertIn(fresh[0], system[at + 1:])                           # after the failure

    def test_a_lane_change_on_the_same_session_rewrites_the_file(self):
        # the file says key, the CLI's init says login: the lane on file is a
        # record, rewritten to what the init said; the account's KIND picks the
        # resume path (a login resumes through the CLI's flag), never the file
        (self.home / "data" / "runner-session.json").write_text(
            json.dumps({"session_id": "s-live", "lane": "key", "generation": 0, "updated": 0}))
        host = accounts.Account(accounts.HOST, "claude-login", None, None, implicit=True)
        r = self.runner(account=host); r.start(); self.one_turn(r)
        self.assertIsNone(self.options[0].resume)                   # the kind, not the file's "key"
        self.assertEqual(self.options[0].extra_args["resume"], "s-live")
        self.assertTrue(_wait(lambda: self.session_file()["lane"] == "login"))
        self.assertEqual(self.session_file()["session_id"], "s-live")
        r.stop(timeout=5)
        r2 = self.runner(account=host); r2.start()
        self.assertTrue(_wait(lambda: len(self.options) == 2))
        self.assertIsNone(self.options[1].resume)
        self.assertEqual(self.options[1].extra_args["resume"], "s-live")

    def test_a_session_file_that_is_not_an_object_is_a_fresh_start(self):
        (self.home / "data" / "runner-session.json").write_text("null")
        r = self.runner(); r.start(); self.one_turn(r)
        self.assertIsNone(r.fatal)
        self.assertIn(("system", "fresh"),
                      [(e["kind"], e["payload"].get("subtype")) for e in r.events()])
        self.assertEqual(self.session_file()["session_id"], "s-live")

    def test_a_digest_turn_that_raises_does_not_end_the_worker(self):
        (self.home / "STATUS.md").write_text("## Open loops\n- x\n")
        r = self.runner()
        real = r._turn

        async def turn(first):
            if first["source"] == "boot":
                raise RuntimeError("digest turn broke")
            return await real(first)
        r._turn = turn
        r.start()
        errors = lambda: [e["payload"].get("error", "") for e in r.events() if e["kind"] == "error"]
        self.assertTrue(_wait(lambda: any("digest turn broke" in m for m in errors())))
        self.assertTrue(r.worker_alive())
        self.one_turn(r)                                             # the next row runs
        self.assertIsNone(r.fatal)

    def test_a_brand_new_cousin_gets_no_digest(self):
        r = self.runner(); r.start(); self.one_turn(r)
        self.assertFalse(any("STATE DIGEST" in b for b in self.bodies(r)))


if __name__ == "__main__":
    unittest.main()
