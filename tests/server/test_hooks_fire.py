"""Chat-pattern hooks fired by a chat send (server/chat_api.send, what the
console, cousin-chat and the Telegram bridge call in-process).

Hooks run AFTER the message is stored and delivered, so the cousin sees
the chat line first and any inject line second, as its own delivery.
Every hook failure is invisible to the sender: the send is a 200 with
the message in the history whether the hook file is missing, malformed,
or its handler explodes.
"""
import json
import os
import pathlib
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from cousin_lib import chat_hooks
from cousin_lib.config import CousinConfig
from cousin_lib.server import chat_api


def _wait_for(path, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


class HooksServerCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "wren"
        self.home.mkdir(parents=True)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n')
        patcher = mock.patch.dict(os.environ, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ["PATH"] = "/usr/bin:/bin"
        chat_hooks._reported.clear()

    def _hooks(self, data):
        (self.home / "chat-hooks.json").write_text(
            data if isinstance(data, str) else json.dumps(data))

    def _boot(self, deliver=None):
        self.calls = []
        return SimpleNamespace(config=CousinConfig.load(self.home),
                               deliver=deliver or (lambda **kw: self.calls.append(kw)))

    def _send(self, server, message, user="Sam"):
        body = chat_api.send(server.config, {"user": user, "message": message},
                             deliver=server.deliver)
        return (200 if body.get("ok") else 500), body

    def _history(self, server, user="Sam"):
        return chat_api.history(self.home, {"user": user})["messages"]


class TestInjectHook(HooksServerCase):
    def test_inject_line_is_delivered_after_the_message_as_its_own_line(self):
        self._hooks([{"pattern": r"(?i)\bdebug\s+health\b", "user": "*",
                      "handler": "inject:[fw-hook] run the health check",
                      "desc": "nudge"}])
        server = self._boot()
        status, body = self._send(server, "please debug health now")
        self.assertEqual(status, 200)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0]["user"], "Sam")
        self.assertEqual(self.calls[0]["message"], "please debug health now")
        self.assertEqual(self.calls[1]["user"], chat_hooks.HOOK_SENDER)
        self.assertEqual(self.calls[1]["message"],
                         "[fw-hook] run the health check")
        self.assertEqual(self.calls[1]["message_id"], body["id"])
        self.assertEqual(self.calls[1]["attachments"], [])
        # The history holds what the operator said, never the hook line.
        self.assertEqual([m["message"] for m in self._history(server)],
                         ["please debug health now"])

    def test_user_filter_applies_on_the_send_path(self):
        self._hooks([{"pattern": "hi", "user": "Pat",
                      "handler": "inject:x"}])
        server = self._boot()
        self._send(server, "hi there", user="Sam")
        self.assertEqual(len(self.calls), 1)
        self._send(server, "hi there", user="pat")
        self.assertEqual(len(self.calls), 3)

    def test_no_delivery_seam_means_inject_is_a_noop(self):
        self._hooks([{"pattern": "hi", "handler": "inject:x"}])
        server = SimpleNamespace(config=CousinConfig.load(self.home), deliver=None)
        status, _ = self._send(server, "hi")
        self.assertEqual(status, 200)


class TestShellHook(HooksServerCase):
    def test_shell_handler_runs_with_the_message_in_its_env(self):
        marker = self.home / "data" / "hook-ran.txt"
        script = self.home / "scripts" / "on-ping.sh"
        script.parent.mkdir()
        script.write_text('#!/bin/sh\nmkdir -p "$COUSIN_HOME/data"\n'
                          'printf "%%s:%%s" "$COUSIN_HOOK_USER" '
                          '"$COUSIN_HOOK_MESSAGE" > "%s"\n' % marker)
        script.chmod(0o755)
        self._hooks([{"pattern": "ping", "handler": "shell:scripts/on-ping.sh"}])
        server = self._boot()
        status, _ = self._send(server, "ping please")
        self.assertEqual(status, 200)
        self.assertTrue(_wait_for(marker), "the shell hook never ran")
        self.assertEqual(marker.read_text(), "Sam:ping please")
        self.assertEqual(len(self.calls), 1)

    def test_script_outside_the_home_is_refused(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        marker = pathlib.Path(outside.name) / "ran"
        script = pathlib.Path(outside.name) / "evil.sh"
        script.write_text('#!/bin/sh\ntouch "%s"\n' % marker)
        script.chmod(0o755)
        self._hooks([{"pattern": ".", "handler": "shell:%s" % script}])
        server = self._boot()
        status, _ = self._send(server, "anything")
        self.assertEqual(status, 200)
        time.sleep(0.3)
        self.assertFalse(marker.exists())


class TestFailuresAreInvisible(HooksServerCase):
    def test_malformed_hooks_file_is_a_noop(self):
        self._hooks("{not json")
        server = self._boot()
        status, _ = self._send(server, "hello")
        self.assertEqual(status, 200)
        self.assertEqual(len(self.calls), 1)

    def test_missing_script_is_a_noop(self):
        self._hooks([{"pattern": ".", "handler": "shell:nope.sh"}])
        server = self._boot()
        status, _ = self._send(server, "hello")
        self.assertEqual(status, 200)

    def test_a_hook_that_raises_never_fails_the_send(self):
        self._hooks([{"pattern": ".", "handler": "inject:x"}])
        server = self._boot()
        with mock.patch.object(chat_hooks, "fire",
                               side_effect=RuntimeError("hook on fire")):
            status, _ = self._send(server, "hello")
        self.assertEqual(status, 200)
        self.assertEqual([m["message"] for m in self._history(server)],
                         ["hello"])

    def test_hooks_fire_after_the_message_is_delivered(self):
        # Ordering proof: the hook line is delivered after the message.
        seen = []

        def deliver(**kw):
            seen.append(kw["user"])

        self._hooks([{"pattern": ".", "handler": "inject:x"}])
        server = self._boot(deliver=deliver)
        self._send(server, "hello")
        self.assertEqual(seen, ["Sam", chat_hooks.HOOK_SENDER])


if __name__ == "__main__":
    unittest.main()
