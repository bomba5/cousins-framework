"""The tmux kind's prompt block, and
`reply` and `handoff` on the stdio `cousin-mcp` server, routed by
run/turn.json. The SDK kind's prompt and tools are unchanged."""
import json
import os
import pathlib
import sqlite3
import stat
import tempfile
import unittest
from unittest import mock

from cousin_lib import mcp_server
from cousin_lib.runner import contract, prompt, tmux_turn
from tests._hermetic import HermeticCase
from tests.runner.test_prompt import PromptCase

PANE = "an interactive Claude Code pane"


class TestContextBlock(PromptCase):
    def test_the_block_is_law_contract_and_rules_without_the_identity(self):
        block = prompt.compose_context_block(self.home, root=self.root, registry=self.registry,
                                             version="1.12.0")
        self.assertTrue(block.startswith("# Framework law\n\n"))
        self.assertIn("You run on %s," % PANE, block)
        self.assertIn("# Operator rules every cousin follows", block)
        self.assertNotIn("Wren keeps the ledgers", block)      # the identity stays in CLAUDE.md
        self.assertNotIn("Dry and exact", block)
        self.assertIn("mcp__cousin__reply", block)             # the stdio server is `cousin`

    def test_the_sdk_prompt_does_not_move(self):
        sdk = self.compose()
        block = prompt.compose_context_block(self.home, root=self.root, registry=self.registry,
                                             version="1.12.0")
        self.assertEqual(sdk.replace("You run on the SDK runner,", "You run on %s," % PANE)
                         .replace(self.identity(), ""), block)

    def identity(self):
        identity, _ = prompt.authored_identity(self.home, root=self.root)
        return identity.strip() + "\n\n"


def _home(case, kind="tmux"):
    tmp = tempfile.TemporaryDirectory(); case.addCleanup(tmp.cleanup)
    root = pathlib.Path(tmp.name)
    home = root / "cousins" / "wren"
    for sub in ("data", "run"):
        (home / sub).mkdir(parents=True)
    (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n'
                                      '[operator]\nname = "Priya"\n[agent]\nrunner = "%s"\n' % kind)
    (root / "config").mkdir()
    p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(root), "COUSIN_HOME": str(home)})
    p.start(); case.addCleanup(p.stop)
    return root, home


def _pane_session(home, session_id):
    (home / "run" / "tmux-session.json").write_text(json.dumps(
        {"session_id": session_id, "transcript_path": "/x", "source": "startup", "pid": 1,
         "ts": 0}))


class TestTurnFile(HermeticCase):
    def test_written_atomically_0600_and_read_back_only_for_the_panes_session(self):
        _root, home = _home(self)
        _pane_session(home, "s-1")
        self.assertEqual(tmux_turn.live(home), (False, ()))       # absent
        tmux_turn.write(home, session_id="s-1", turn_nonce="a1b2c3d4e5f6",
                        threads=["operator:priya"])
        path = home / "run" / "turn.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(path.read_text()), {
            "session_id": "s-1", "turn_nonce": "a1b2c3d4e5f6", "threads": ["operator:priya"]})
        self.assertEqual(tmux_turn.live(home), (True, ("operator:priya",)))
        _pane_session(home, "s-2")                                # another session: stale
        self.assertEqual(tmux_turn.live(home), (False, ()))
        _pane_session(home, "s-1")
        path.write_text("{not json")
        self.assertEqual(tmux_turn.live(home), (False, ()))
        tmux_turn.write(home, session_id="s-1", turn_nonce="a1b2c3d4e5f6", threads=["x"])
        tmux_turn.clear(home)
        self.assertFalse(path.exists())
        self.assertEqual(tmux_turn.live(home), (False, ()))
        tmux_turn.clear(home)                                     # idempotent


def _chat(home):
    db = home / "data" / "chat.db"
    if not db.exists():
        return []
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT chat_user, message FROM messages ORDER BY id").fetchall()


class TestStdioPaneTools(HermeticCase):
    def test_only_a_tmux_home_gets_reply_and_handoff(self):
        _root, home = _home(self)
        self.assertEqual(sorted(t["name"] for t in mcp_server.pane_tools(home)),
                         ["handoff", "reply"])
        _root2, sdk_home = _home(self, kind="sdk")
        self.assertEqual(mcp_server.pane_tools(sdk_home), [])

    def test_reply_goes_to_the_live_turns_thread(self):
        _root, home = _home(self)
        _pane_session(home, "s-1")
        text, is_error = mcp_server.call_pane_tool(home, "reply", {"text": "on it"})
        self.assertTrue(is_error)                                 # no live turn, no thread
        self.assertIn("no turn is live", text)
        tmux_turn.write(home, session_id="s-1", turn_nonce="a1b2c3d4e5f6",
                        threads=["operator:priya"])
        text, is_error = mcp_server.call_pane_tool(home, "reply", {"text": "on it"})
        self.assertFalse(is_error, text)
        self.assertEqual(_chat(home), [("priya", "on it")])
        tmux_turn.write(home, session_id="s-1", turn_nonce="a1b2c3d4e5f6",
                        threads=["operator:priya", "person:sam"])
        text, is_error = mcp_server.call_pane_tool(home, "reply", {"text": "which?"})
        self.assertTrue(is_error)
        self.assertIn("reply never guesses", text)            # the refusal names each thread's way

    def test_handoff_writes_the_handoff_file(self):
        _root, home = _home(self)
        text, is_error = mcp_server.call_pane_tool(home, "handoff", {
            "position": "mid ledger", "next_action": "close the ledger", "status": "half done"})
        self.assertFalse(is_error, text)
        self.assertIn("close the ledger", (home / "data" / "handoff.md").read_text())

    def test_a_non_tmux_home_refuses_them(self):
        _root, home = _home(self, kind="sdk")
        text, is_error = mcp_server.call_pane_tool(home, "reply", {"text": "x"})
        self.assertTrue(is_error)
        self.assertIn("tmux", text)


if __name__ == "__main__":
    unittest.main()
