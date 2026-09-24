"""Live proofs that the phase-4 switches took effect. Opt in: COUSIN_LIVE_SDK=1.
Each measures an EFFECT the switch alone can produce (phase 0 finding 7)."""
import json
import os
import pathlib
import unittest
import uuid
from unittest import mock

from cousin_lib import delivery
from cousin_lib.delivery import Item
from cousin_lib.runner.sdk import SdkRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home

MODEL = "claude-haiku-4-5-20251001"
# Long enough to be cached on its own (well past the minimum cacheable prefix).
# One nonce per test run, first in the law and shared by every home of the run
# (ruling W9-3): session A creates the block cold whatever an earlier run cached.
RUN_NONCE = uuid.uuid4().hex
LAW = "Run %s.\n" % RUN_NONCE + "".join(
    "%d. A clause of the law, long enough to be cached on its own.\n" % i for i in range(1, 700))
# Under the law's real token count: a conservative 6 characters per token.
LAW_TOKENS_LOWER_BOUND = len(LAW) // 6


def _op(body):
    return Item("operator:priya", "chat", body, sender="Priya")


def _results(r):
    return [e["payload"] for e in r.events() if e["kind"] == "result"]


def _texts(r, after=0):
    return " ".join(e["payload"]["text"] for e in r.events()
                    if e["kind"] == "text" and e["seq"] > after)


@unittest.skipUnless(os.environ.get("COUSIN_LIVE_SDK") == "1", "set COUSIN_LIVE_SDK=1")
class TestLivePrompt(HermeticCase):
    def _home(self, identity):
        home = temp_home(self, runner="sdk")
        root = home.parent.parent
        (root / "config").mkdir(exist_ok=True)
        (root / "config" / "law.md").write_text(LAW)
        (home / "CLAUDE.md").write_text("# Wren - tester\n\n## Identity\n\n%s\n" % identity)
        return home, root

    def _run(self, home, root, body):
        with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(root)}):
            r = SdkRunner(home, model=MODEL, idle_timeout_s=120)
            r.start()
            try:
                self.assertEqual(delivery.deliver(home, _op(body), wait=True, timeout=120),
                                 delivery.DELIVERED)
                return r, _results(r)[-1]
            finally:
                r.stop(timeout=30)

    def test_exclude_dynamic_sections_takes_the_cwd_out_of_the_cached_prefix(self):
        """Two homes, byte-identical prompts, DIFFERENT working directories.
        Phase 0 finding 3: with the cwd in the system prompt (before our
        block) the second session re-creates the whole block. With the
        switch working, the cwd is in the first user message instead and
        the second session reads the block from cache.

        The bar (ruling W9-5). A result's `usage` is the session's total over
        every model call of the turn (`num_turns`). A creates the appended
        block cold (the per-run nonce first in LAW makes sure of it, W9-3):
        the law plus the tail after it (the first user message, where the
        cwd is re-injected, and the turn's own messages). B reading the block
        from cache creates only its own tail; B re-creating it creates the
        law again. So the bar tests the mechanism, whether the law block was
        re-created: B's cumulative cache_creation must be under A's minus a
        lower bound on the law's tokens. Its margin scales with the law, not
        with the tail, so a turn with more tool calls does not eat it the way
        it would eat a halving. `read > 8000` says the block was read at all.

        `usage["iterations"]` is NOT a per-call breakdown: the CLI documents
        it as "per-iteration usage when the request ran several sampling
        iterations", the sampling passes of ONE request, and it had one entry
        on num_turns=4. Its figure reflects reuse inside the session and is
        near zero whether or not the switch worked, so it is printed for the
        record and never asserted."""
        home_a, root_a = self._home("Wren tests caching.")
        home_b, root_b = self._home("Wren tests caching.")
        _r, first = self._run(home_a, root_a, "Reply only OK.")
        _r, second = self._run(home_b, root_b, "Reply only OK.")
        created_a = (first["usage"] or {}).get("cache_creation_input_tokens") or 0
        created = (second["usage"] or {}).get("cache_creation_input_tokens") or 0
        read = (second["usage"] or {}).get("cache_read_input_tokens") or 0
        iterations = (second["usage"] or {}).get("iterations") or []
        last_iteration = (iterations[-1].get("cache_creation_input_tokens")
                          if iterations else None)
        print("\nREPORT cache: A created=%d; B created=%d read=%d over %s turns;"
              " B usage.iterations[-1] created=%s (informational)"
              % (created_a, created, read, second.get("num_turns"), last_iteration))
        self.assertGreater(read, 8000, "the appended block was not read from cache: %r" % second)
        self.assertLess(created, created_a - LAW_TOKENS_LOWER_BOUND,
                        "B re-created the law block A created cold (bound %d): %r vs %r"
                        % (LAW_TOKENS_LOWER_BOUND, second, first))

    def test_snapshot_keeps_the_recorded_prompt_across_a_resume(self):
        """Resume the SAME session after the identity file changed: with
        snapshot the session keeps the prompt it recorded first."""
        home, root = self._home("The code word is KESTREL.")
        self._run(home, root, "Reply only OK.")
        (home / "CLAUDE.md").write_text("# Wren - tester\n\n## Identity\n\nThe code word is PLOVER.\n")
        r, _res = self._run(home, root, "What is the code word in your instructions? One word.")
        text = _texts(r).upper()
        self.assertIn("KESTREL", text)
        self.assertNotIn("PLOVER", text)

    def _key(self):
        """The key lane needs a key: COUSIN_LIVE_API_KEY_FILE names a file
        holding one, authorised by the operator for the run. Absent: skip."""
        path = os.environ.get("COUSIN_LIVE_API_KEY_FILE")
        if not path:
            self.skipTest("set COUSIN_LIVE_API_KEY_FILE for the key-lane proof")
        return pathlib.Path(path).read_text().strip()

    def _run_lane(self, home, root, body, api_key=None, account=None):
        with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(root)}):
            extra = {"account": account} if account is not None else {}  # Task 14 adds account=
            r = SdkRunner(home, model=MODEL, idle_timeout_s=120, api_key=api_key, **extra)
            r.start()
            try:
                self.assertEqual(delivery.deliver(home, _op(body), wait=True, timeout=120),
                                 delivery.DELIVERED)
                return r
            finally:
                r.stop(timeout=30)

    def test_key_lane_resume_is_served_by_our_store_with_the_local_transcript_gone(self):
        """Finding 2 at the runner's level, on the lane where it applies
        (R12: the key lane's resume is store-backed). The harness's local
        transcript is deleted between the runners; the second resumes the
        same session id from data/sessions.db and remembers the first turn.
        Where the CLI wrote that transcript depends on the account: under
        ~/.claude before Task 14, under the ACCOUNT's config dir
        (<root>/data/accounts/<name>/projects/...) from Task 14 on, where a
        key account's CLAUDE_CONFIG_DIR points. Both are globbed, and at
        least one file must go: a deletion that found nothing would make
        this proof vacuous (the local file would serve the resume)."""
        key = self._key()
        home, root = self._home("Wren remembers.")
        self._run_lane(home, root, "Remember the word zebracorn and reply only OK.", api_key=key)
        saved = json.loads((home / "data" / "runner-session.json").read_text())
        self.assertEqual(saved["lane"], "key")
        config_dirs = [pathlib.Path.home() / ".claude", *(root / "data" / "accounts").glob("*")]
        deleted = []
        for config_dir in config_dirs:
            for path in (config_dir / "projects").glob("*/%s.jsonl" % saved["session_id"]):
                path.unlink()
                deleted.append(path)
        self.assertTrue(deleted, "no local transcript found to delete: the proof would be vacuous")
        if (root / "data" / "accounts").is_dir():             # from Task 14 on
            self.assertTrue(any(str(root / "data" / "accounts") in str(p) for p in deleted), deleted)
        r = self._run_lane(home, root, "What word did I ask you to remember? One word.", api_key=key)
        inits = [e["payload"]["session_id"] for e in r.events() if e["kind"] == "session_init"]
        self.assertEqual(set(inits), {saved["session_id"]})
        self.assertIn("zebracorn", _texts(r).lower())

    def test_login_lane_resume_through_the_clis_flag_keeps_the_session(self):
        """R12 on the login lane: the CLI's own --resume, reading its local
        transcript, which this test KEEPS (deleting it is the key lane's
        proof, and here would only produce the designed resume_failed)."""
        home, root = self._home("Wren remembers.")
        self._run_lane(home, root, "Remember the word kestrel and reply only OK.")
        saved = json.loads((home / "data" / "runner-session.json").read_text())
        if saved["lane"] != "login":
            self.skipTest("this machine's default lane is %s" % saved["lane"])
        r = self._run_lane(home, root, "What word did I ask you to remember? One word.")
        inits = [e["payload"]["session_id"] for e in r.events() if e["kind"] == "session_init"]
        self.assertEqual(inits[0], saved["session_id"])            # the first init proves it
        self.assertIn("kestrel", _texts(r).lower())

    def test_a_key_account_runs_its_tools(self):
        """1.18.2: a key account's turn runs a tool. With the CLI's
        CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1 set (phase 4 Task 14) the CLI forced
        the permission mode to `default` and the sandbox on every Bash call,
        so no tool ran and this proof was never run until a cousin moved to
        a key. An effect, not the option (finding 7): the Bash output itself.
        The key now reaches the command's environment, by design."""
        try:
            from cousin_lib import accounts
        except ImportError:
            self.skipTest("Task 14's accounts are not in yet")
        key = self._key()
        home, root = self._home("Wren runs a command when asked.")
        acc = accounts.Account("metered", "anthropic-key", None, None, secret_value=key)
        r = self._run_lane(home, root, "Run exactly this with your Bash tool: "
                           "echo ran-$((6*7)); env | grep -c '^PATH=' "
                           "and then reply only DONE.", account=acc)
        results = [e["payload"] for e in r.events() if e["kind"] == "tool_result"]
        self.assertTrue(results, "no tool call ran")
        self.assertFalse(results[0].get("is_error"), results[0].get("text"))
        self.assertEqual(results[0]["text"].split(), ["ran-42", "1"])
        self.assertFalse([e for e in r.events() if e["kind"] == "permission"])

    def test_report_whether_the_clis_auto_memory_reaches_the_first_user_message(self):
        """A REPORT, not a pass/fail on the behaviour (the controller's round-3
        ask): so a migrated test cousin does not run two memory systems
        unmeasured. A canary line is planted in the CLI's auto-memory for this
        cousin's project, one turn runs, and the first user entry in OUR store
        is searched for it. The test fails only if the canary went to the
        wrong directory (then the report would be wrong for the wrong reason)."""
        import re
        import shutil
        from cousin_lib.runner.session_store import SqliteSessionStore
        home, root = self._home("Wren tests memory.")
        project = pathlib.Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(home))
        (project / "memory").mkdir(parents=True, exist_ok=True)
        (project / "memory" / "MEMORY.md").write_text("- canary line: QUOKKA-7731 lives here\n")
        self.addCleanup(shutil.rmtree, project, True)
        self._run_lane(home, root, "Reply only OK.")
        sid = json.loads((home / "data" / "runner-session.json").read_text())["session_id"]
        self.assertTrue((project / ("%s.jsonl" % sid)).exists(),
                        "the CLI wrote this session elsewhere: the canary sat in the wrong project")
        entries, _ = SqliteSessionStore(home).entries_after(sid)
        first_user = next(e for e in entries if e.get("type") == "user")
        seen = "QUOKKA-7731" in json.dumps(first_user)
        print("\nREPORT auto-memory reaches the first user message: %s" % seen)
        (home.parent.parent / "live-report.json").write_text(json.dumps({"auto_memory_in_first_user_message": seen}))

    def test_the_switch_keeps_the_clis_auto_memory_out_with_a_control(self):
        """Phase 7 (R6). Phase 4 finding 8 measured that the CLI's auto-memory
        does not appear in the first user entry of our store, so the store
        cannot show whether the model saw it. The probe is the model's
        ANSWER, given without any tool (so the switched run cannot find the
        canary on disk). A canary line is planted in the auto-memory of this
        cousin's project, under the account's config directory
        (CLAUDE_CONFIG_DIR from the session env, else ~/.claude), and each
        run checks that the CLI wrote its session into THAT project: a canary
        in the wrong directory would read as "never seen". The CONTROL, with
        the switch removed, MUST answer the canary: otherwise the CLI never
        loads that memory on this lane and the switched run proves nothing.
        The switched run must not answer it."""
        import re
        import shutil
        from cousin_lib.runner import sdk as sdk_mod
        ask = ("Without using any tool: if your memory holds a canary line with a code,"
               " reply with the code only; if it holds none, reply NONE.")

        def answer(switch):
            home, root = self._home("Wren tests memory.")
            off = sdk_mod.AUTO_MEMORY_OFF if switch else {}
            with mock.patch.object(sdk_mod, "AUTO_MEMORY_OFF", off), \
                    mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(root)}):
                env = SdkRunner(home, model=MODEL).options().env
            config = pathlib.Path(env.get("CLAUDE_CONFIG_DIR") or pathlib.Path.home() / ".claude")
            project = config / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(home))
            (project / "memory").mkdir(parents=True, exist_ok=True)
            (project / "memory" / "MEMORY.md").write_text("- canary line: QUOKKA-7731 lives here\n")
            self.addCleanup(shutil.rmtree, project, True)
            with mock.patch.object(sdk_mod, "AUTO_MEMORY_OFF", off):
                r = self._run_lane(home, root, ask)
            sid = json.loads((home / "data" / "runner-session.json").read_text())["session_id"]
            self.assertTrue((project / ("%s.jsonl" % sid)).exists(),
                            "the CLI wrote this session elsewhere: the canary sat in the wrong project")
            return " ".join(e["payload"]["text"] for e in r.events() if e["kind"] == "text")

        self.assertIn("QUOKKA-7731", answer(switch=False),
                      "control: without the switch the model did not see its auto-memory,"
                      " so the switched run cannot prove anything")
        self.assertNotIn("QUOKKA-7731", answer(switch=True))


if __name__ == "__main__":
    unittest.main()
