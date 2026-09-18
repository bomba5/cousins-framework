"""The null-operator install: every v1 surface, no operator anywhere.

A zero-operator install is not a degraded configuration; it is a
framework with one subsystem absent - and this suite is the claim
made executable, because a documented seam with no test is a claim.
docs/cousins.md cites this file.

The pattern each test pins: the surface WORKS without an operator,
and wherever the operator's absence changes behavior, the change is
explicit (a required argument, a named degradation, a refusal with
remediation) - never a silently defaulted human being.
"""
import json
import os
import pathlib
import shutil
import tempfile
import unittest
import urllib.request
from unittest import mock

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class NullOperatorCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "templates").mkdir()
        shutil.copy(
            _REPO_ROOT / "templates" / "cousin-CLAUDE.template.md",
            self.root / "templates" / "cousin-CLAUDE.template.md")
        patcher = mock.patch.dict(os.environ, {
            "FRAMEWORK_ROOT": str(self.root),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def _spawn(self):
        from cousin_lib.spawn import create_cousin
        out = create_cousin(
            self.root, slug="wren", name="Wren", role="test cousin",
            voice="Plain.", port=8100)
        os.environ["COUSIN_HOME"] = str(out["home"])
        self.addCleanup(os.environ.pop, "COUSIN_HOME", None)
        return out["home"]


class TestSpawnAndIdentity(NullOperatorCase):
    def test_spawn_needs_no_operator_and_names_none(self):
        home = self._spawn()
        claude_md = (home / "CLAUDE.md").read_text()
        self.assertIn("[operator]", claude_md)  # points at config...
        cousin_toml = (home / "cousin.toml").read_text()
        self.assertNotIn("[operator]", cousin_toml)  # ...which is absent

    def test_boot_degrades_by_name_never_silently(self):
        from cousin_lib.boot import assemble
        home = self._spawn()
        packet = assemble("wren", home, generation=1)
        # No operator means no calibration - a NAMED degradation in
        # the packet header, not a silent gap and not a default human.
        self.assertIn("calibration", packet["degraded_sections"])
        self.assertIn("DEGRADED layers:", packet["text"])


class TestChatSurface(NullOperatorCase):
    def test_reply_requires_an_explicit_recipient(self):
        from cousin_lib.config import CousinConfig
        from cousin_lib.server.app import ChatServer
        home = self._spawn()
        config = CousinConfig.load(home)
        config.chat_port = 0
        server = ChatServer(config)
        server.start()
        self.addCleanup(server.stop)

        def post(path, payload):
            req = urllib.request.Request(
                "http://127.0.0.1:%d%s" % (server.port, path),
                data=json.dumps(payload).encode())
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status
            except urllib.error.HTTPError as err:
                return err.code

        # No configured operator means no default recipient: a reply
        # without one is a 400, with one it works.
        self.assertEqual(
            post("/api/wren_reply", {"message": "hi"}), 400)
        self.assertEqual(
            post("/api/wren_reply",
                 {"message": "hi", "reply_to_user": "Visitor"}), 200)


class TestMemoryAndLifecycle(NullOperatorCase):
    def test_memory_surface_is_operator_free(self):
        import contextlib
        import io

        from cousin_lib.memory import memory_main
        self._spawn()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(
                memory_main(["decide", "t", "d", "why"]), 0)
            self.assertEqual(
                memory_main(["activity", "running the null suite"]), 0)
            self.assertEqual(memory_main(["search", "anything"]), 0)

    @unittest.skipUnless(shutil.which("tmux"),
                         "tmux is a documented prerequisite; not installed")
    def test_flip_dry_run_needs_no_operator(self):
        from cousin_lib.flip import flip
        from tests._fakes import agent_on_path
        agent_on_path(self, self.root)
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "config" / "agent-cmd").write_text("my-agent\n")
        self._spawn()
        result = flip("wren", dry_run=True)
        self.assertTrue(result["ok"], result)

    def test_shared_promotion_refuses_rather_than_defaulting(self):
        # With no operator there is nobody to default promotion to;
        # the tier refuses with remediation instead of inventing an
        # approver.
        from cousin_lib.shared_tier import PromoteRefused, promote, propose
        self._spawn()
        propose("norms.md", "x\n", slug="wren")
        with self.assertRaises(PromoteRefused):
            promote("norms.md", proposer="wren", by="Anyone")


if __name__ == "__main__":
    unittest.main()
