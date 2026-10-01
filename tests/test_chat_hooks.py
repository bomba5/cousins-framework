"""Chat-pattern hooks: the pure evaluation and the two handler kinds.

<home>/chat-hooks.json lists {pattern, user, handler, desc}. Every
failure is a no-op by contract: a missing file, malformed JSON, a bad
regex, a missing script, a script outside the home and the framework
root. None of them may ever cost the message that triggered them; the
server test proves that end to end, this file proves each rule alone.
"""
import io
import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from cousin_lib import chat_hooks
from tests._hermetic import HermeticCase


def _wait_for(path, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return True
        time.sleep(0.05)
    return path.exists()


class HooksCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "testa"
        self.home.mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ["PATH"] = "/usr/bin:/bin"
        chat_hooks._reported.clear()

    def _write_hooks(self, data):
        (self.home / chat_hooks.HOOKS_FILENAME).write_text(
            data if isinstance(data, str) else json.dumps(data))

    def _script(self, name, body, home=None):
        path = (home or self.home) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
        return path


class TestLoadHooks(HooksCase):
    def test_missing_file_is_no_hooks(self):
        self.assertEqual(chat_hooks.load_hooks(self.home), [])

    def test_malformed_json_is_no_hooks(self):
        self._write_hooks("{not json")
        self.assertEqual(chat_hooks.load_hooks(self.home), [])

    def test_non_list_document_is_no_hooks(self):
        self._write_hooks({"pattern": "x", "handler": "inject:y"})
        self.assertEqual(chat_hooks.load_hooks(self.home), [])

    def test_malformed_entries_are_dropped_and_the_rest_kept(self):
        self._write_hooks([
            {"pattern": "p1", "handler": "inject:x"},
            {"pattern": "p2"},
            "not a dict",
            {"handler": "inject:y"},
            {"pattern": 3, "handler": "inject:z"},
            {"pattern": "p3", "handler": "shell:s"},
        ])
        out = chat_hooks.load_hooks(self.home)
        self.assertEqual([h["pattern"] for h in out], ["p1", "p3"])


class TestEvaluate(HooksCase):
    def test_wildcard_user_matches_anyone(self):
        hooks = [{"pattern": "hello", "handler": "inject:hi", "user": "*"}]
        self.assertTrue(chat_hooks.evaluate(hooks, "Sam", "hello world"))
        self.assertTrue(chat_hooks.evaluate(hooks, "Pat", "hello there"))

    def test_absent_user_key_means_wildcard(self):
        hooks = [{"pattern": "hi", "handler": "inject:x"}]
        self.assertTrue(chat_hooks.evaluate(hooks, "Anyone", "hi"))

    def test_user_filter_is_case_insensitive(self):
        hooks = [{"pattern": "hi", "handler": "inject:x", "user": "Sam"}]
        self.assertTrue(chat_hooks.evaluate(hooks, "SAM", "hi"))
        self.assertTrue(chat_hooks.evaluate(hooks, "sam", "hi"))
        self.assertFalse(chat_hooks.evaluate(hooks, "Pat", "hi"))

    def test_no_match_is_empty(self):
        hooks = [{"pattern": "wibble", "handler": "inject:x", "user": "*"}]
        self.assertEqual(chat_hooks.evaluate(hooks, "u", "nothing here"), [])

    def test_bad_regex_is_skipped_and_reported_once(self):
        hooks = [
            {"pattern": "[unclosed", "handler": "inject:x", "user": "*"},
            {"pattern": "ok", "handler": "inject:y", "user": "*"},
        ]
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            first = chat_hooks.evaluate(hooks, "u", "ok")
            second = chat_hooks.evaluate(hooks, "u", "ok again")
        self.assertEqual([h["pattern"] for h in first], ["ok"])
        self.assertEqual([h["pattern"] for h in second], ["ok"])
        report = err.getvalue()
        self.assertIn("[unclosed", report)
        self.assertEqual(report.count("[unclosed"), 1)

    def test_empty_message_and_user_do_not_raise(self):
        hooks = [{"pattern": "x", "handler": "inject:y", "user": "Sam"}]
        self.assertEqual(chat_hooks.evaluate(hooks, None, None), [])


class TestFireInject(HooksCase):
    def test_inject_calls_the_seam_with_the_text(self):
        seen = []
        matched = [{"pattern": "p", "handler": "inject:[fw-hook] reminder"}]
        chat_hooks.fire(matched, user="u", message="m", slug="testa",
                        home=self.home, inject=seen.append)
        self.assertEqual(seen, ["[fw-hook] reminder"])

    def test_no_inject_seam_is_a_noop(self):
        matched = [{"pattern": "p", "handler": "inject:x"}]
        chat_hooks.fire(matched, user="u", message="m", slug="testa",
                        home=self.home, inject=None)

    def test_failing_seam_never_raises(self):
        def boom(text):
            raise RuntimeError("pane gone")
        matched = [{"pattern": "p", "handler": "inject:x"},
                   {"pattern": "p", "handler": "inject:y"}]
        chat_hooks.fire(matched, user="u", message="m", slug="testa",
                        home=self.home, inject=boom)

    def test_unknown_handler_kind_is_a_noop(self):
        seen = []
        matched = [{"pattern": "p", "handler": "unknown:nothing"}]
        with mock.patch("subprocess.Popen") as popen:
            chat_hooks.fire(matched, user="u", message="m", slug="testa",
                            home=self.home, inject=seen.append)
        popen.assert_not_called()
        self.assertEqual(seen, [])


class TestFireShell(HooksCase):
    def test_script_runs_detached_with_the_documented_env(self):
        marker = self.home / "data" / "marker.txt"
        self._script("scripts/hook.sh",
                     'mkdir -p "$COUSIN_HOME/data"\n'
                     'printf "%%s|%%s|%%s|%%s|%%s\\n" "$COUSIN_HOOK_USER" '
                     '"$COUSIN_HOOK_MESSAGE" "$COUSIN_HOOK_PATTERN" '
                     '"$COUSIN_SLUG" "$COUSIN_HOME" > "%s"\n' % marker)
        matched = [{"pattern": "p.t", "handler": "shell:scripts/hook.sh"}]
        chat_hooks.fire(matched, user="Sam", message="pat the cat",
                        slug="testa", home=self.home, inject=None)
        self.assertTrue(_wait_for(marker), "the hook never ran")
        self.assertEqual(marker.read_text().strip(),
                         "Sam|pat the cat|p.t|testa|%s" % self.home)

    def test_script_sees_no_credential_from_the_server_env(self):
        # the server's own env may carry the account's auth variables
        # and other credential-shaped names; a hook runs without them, as
        # the runner strips them from the cousin's own tools
        from cousin_lib import accounts
        secret = {name: "leak-%d" % i for i, name in enumerate(accounts.AUTH_VARS)}
        secret.update({"SERVICE_API_KEY": "leak-k", "DEPLOY_TOKEN": "leak-t",
                       "DB_PASSWORD": "leak-p", "APP_SECRET_SALT": "leak-s"})
        os.environ.update(secret)
        os.environ["HARMLESS_SETTING"] = "kept"
        dump, done = self.home / "data" / "env.txt", self.home / "data" / "env.done"
        # env(1) and shell builtins only: the test PATH is /usr/bin:/bin
        self._script("scripts/env.sh",
                     'env > "%s"\necho done > "%s"\n' % (dump, done))
        matched = [{"pattern": "p", "handler": "shell:scripts/env.sh"}]
        chat_hooks.fire(matched, user="Sam", message="p", slug="testa",
                        home=self.home, inject=None)
        self.assertTrue(_wait_for(done), "the hook never ran")
        seen = dict(line.split("=", 1) for line in dump.read_text().splitlines()
                    if "=" in line)
        self.assertEqual(sorted(set(seen) & set(secret)), [])
        self.assertNotIn("leak-", dump.read_text())
        self.assertEqual(seen.get("HARMLESS_SETTING"), "kept")
        self.assertEqual(seen.get("COUSIN_HOOK_USER"), "Sam")
        self.assertIn(secret["ANTHROPIC_API_KEY"], os.environ.values())   # the server's own env is untouched

    def test_output_is_appended_to_the_hooks_log(self):
        self._script("h.sh", "echo out; echo err >&2\n")
        matched = [{"pattern": "p", "handler": "shell:h.sh"}]
        log = self.home / "data" / "chat-hooks.log"
        chat_hooks.fire(matched, user="u", message="m", slug="testa",
                        home=self.home, inject=None)
        self.assertTrue(_wait_for(log))
        chat_hooks.fire(matched, user="u", message="m", slug="testa",
                        home=self.home, inject=None)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if log.read_text().count("out") >= 2:
                break
            time.sleep(0.05)
        text = log.read_text()
        self.assertEqual(text.count("out"), 2)
        self.assertEqual(text.count("err"), 2)

    def test_missing_script_is_a_noop(self):
        matched = [{"pattern": "p", "handler": "shell:does-not-exist.sh"}]
        with mock.patch("subprocess.Popen") as popen:
            chat_hooks.fire(matched, user="u", message="m", slug="testa",
                            home=self.home, inject=None)
        popen.assert_not_called()

    def test_absolute_path_inside_the_home_runs(self):
        script = self._script("abs.sh", "true\n")
        matched = [{"pattern": "p", "handler": "shell:%s" % script}]
        with mock.patch("subprocess.Popen") as popen:
            chat_hooks.fire(matched, user="u", message="m", slug="testa",
                            home=self.home, inject=None)
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], [str(script)])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_path_under_the_framework_root_runs(self):
        os.environ["FRAMEWORK_ROOT"] = str(self.root)
        script = self._script("scripts/shared.sh", "true\n", home=self.root)
        matched = [{"pattern": "p", "handler": "shell:%s" % script}]
        with mock.patch("subprocess.Popen") as popen:
            chat_hooks.fire(matched, user="u", message="m", slug="testa",
                            home=self.home, inject=None)
        popen.assert_called_once()

    def test_path_outside_home_and_root_is_refused_with_a_report(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        script = self._script("evil.sh", "true\n",
                              home=pathlib.Path(outside.name))
        for handler in ("shell:%s" % script, "shell:../../../%s" % script.name):
            matched = [{"pattern": "p", "handler": handler}]
            err = io.StringIO()
            with mock.patch("subprocess.Popen") as popen, \
                    mock.patch("sys.stderr", err):
                chat_hooks.fire(matched, user="u", message="m", slug="testa",
                                home=self.home, inject=None)
            popen.assert_not_called()
            self.assertIn("refused", err.getvalue())

    def test_relative_traversal_out_of_the_home_is_refused(self):
        # cousins/testa/../../escape.sh resolves to <root>/escape.sh, which
        # is inside the framework root only when one is configured.
        script = self._script("escape.sh", "true\n", home=self.root)
        matched = [{"pattern": "p", "handler": "shell:../../escape.sh"}]
        with mock.patch("subprocess.Popen") as popen:
            chat_hooks.fire(matched, user="u", message="m", slug="testa",
                            home=self.home, inject=None)
        popen.assert_not_called()
        self.assertTrue(script.exists())

    def test_popen_failure_never_raises(self):
        self._script("h.sh", "true\n")
        matched = [{"pattern": "p", "handler": "shell:h.sh"}]
        with mock.patch("subprocess.Popen", side_effect=OSError("no fork")):
            chat_hooks.fire(matched, user="u", message="m", slug="testa",
                            home=self.home, inject=None)


class TestOnMessageToTheInbox(HermeticCase):
    def test_an_inject_hook_rides_the_send_paths_own_delivery_after_the_message(self):
        # The production closure, not a stand-in: the Telegram bridge's
        # runner lane stores the message, delivers it, then fires the
        # home's hooks through the same deliver seam.
        from cousin_lib import telegram
        from cousin_lib.runner.inbox import Inbox
        from tests.runner._home import temp_home
        home = temp_home(self, runner="fake")
        (home / "chat-hooks.json").write_text(json.dumps([
            {"pattern": "(?i)price check", "user": "*",
             "handler": "inject:[fw-hook] quote the tariff"}]))
        cfg = telegram.BridgeConfig(slug="wren", token="unused", operator_ids={42},
                                    operator_name={42: "Sam"}, port=0, home=home)
        telegram._default_chat_send(cfg, user="Sam", message="price check please")
        rows = Inbox(home).claim(limit=5)
        self.assertEqual([(r["source"], r["sender"]) for r in rows],
                         [("chat", "Sam"), ("hook", chat_hooks.HOOK_SENDER)])
        chat_row, hook_row = rows
        self.assertEqual(chat_row["body"], "price check please")
        self.assertEqual(hook_row["thread_id"], "system")
        self.assertEqual(hook_row["message_id"], chat_row["message_id"])
        self.assertIsNotNone(chat_row["message_id"])
        self.assertIn("quote the tariff", hook_row["body"])

    def test_no_match_puts_nothing(self):
        from cousin_lib import chat_hooks
        from cousin_lib.runner.inbox import Inbox
        from tests.runner._home import temp_home
        home = temp_home(self, runner="fake")
        (home / "chat-hooks.json").write_text(json.dumps([
            {"pattern": "never", "user": "*", "handler": "inject:x"}]))
        self.assertEqual(chat_hooks.on_message(home, user="Priya", message="hi", message_id=1,
                                               slug="wren", deliver=lambda **k: None), [])
        self.assertEqual(Inbox(home).pending(), 0)


if __name__ == "__main__":
    unittest.main()
