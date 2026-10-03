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
import os
import pathlib
import shutil
import tempfile
import unittest
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
            voice="Plain.")
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


class TestChatSurface(NullOperatorCase):
    def test_reply_requires_an_explicit_recipient(self):
        from cousin_lib.config import CousinConfig
        from cousin_lib.server import chat_api
        home = self._spawn()
        config = CousinConfig.load(home)
        # No configured operator means no default recipient: a reply
        # without one is refused, with one it works.
        with self.assertRaises(chat_api.BadRequest):
            chat_api.reply(config, {"message": "hi"})
        self.assertTrue(chat_api.reply(
            config, {"message": "hi", "reply_to_user": "Visitor"})["ok"])


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
