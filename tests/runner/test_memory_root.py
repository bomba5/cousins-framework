"""The runner's memory reads use the runner's root, never the environment's,
and a raw memory is recalled by its topic."""
import asyncio
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib import memory, memory_search
from cousin_lib.runner import hooks
from cousin_lib.runner.state import StateMachine
from cousin_lib.runner.stream import EventStream
from tests._hermetic import HermeticCase

TOML = ('[cousin]\nslug = "wren"\nname = "Wren"\n'
        '[memory]\nproactive_recall = true\nrecall_keyword_only = true\n')


class RootCase(HermeticCase):
    """Two installs. The environment names install A, whose harness
    auto-memory holds a matching memory; the runner's root is install B,
    which declares no harness. A read that follows the environment
    surfaces A's memory."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        base = pathlib.Path(tmp.name)
        self.env_root = base / "a"
        (self.env_root / "config").mkdir(parents=True)
        harness = base / "a-harness"
        harness.mkdir()
        (harness / "quokka.md").write_text("# Quokka\nThe quokka ledger is audited by Mallory.\n")
        (self.env_root / "config" / "harness.toml").write_text(
            'auto_memory_dir = "%s"\n' % harness)
        self.root = base / "b"
        (self.root / "config").mkdir(parents=True)
        self.home = self.root / "cousins" / "wren"
        for sub in ("data", "memory", "notes"):
            (self.home / sub).mkdir(parents=True)
        (self.home / "cousin.toml").write_text(TOML)
        (self.home / "memory" / "ledgers.md").write_text(
            "# Ledgers\nThe quokka ledger closes monthly.\n")
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(self.env_root),
                                         "COUSIN_HOME": str(self.home)})
        p.start(); self.addCleanup(p.stop)

    def _prompt_context(self, prompt):
        stream = EventStream(self.home, "root-test")
        machine = StateMachine(on_change=lambda o, n, d: None)
        cbs = hooks.callbacks(self.home, slug="wren", root=self.root,
                              machine=machine, stream=stream)
        out = asyncio.run(cbs["UserPromptSubmit"](
            {"hook_event_name": "UserPromptSubmit", "session_id": "s",
             "transcript_path": "/dev/null", "cwd": str(self.home), "prompt": prompt}, None, {}))
        return out.get("hookSpecificOutput", {}).get("additionalContext", "")


class TestRootIsPassed(RootCase):
    def test_the_prompt_hook_recalls_from_the_runners_root_only(self):
        ctx = self._prompt_context("who audits the quokka ledger?")
        self.assertIn("Ledgers (memory:ledgers.md)", ctx)    # the runner's own memory
        self.assertNotIn("harness:", ctx)                     # never the environment's install

    def test_search_with_a_root_never_reads_the_environment(self):
        hits, _notice = memory_search.search("quokka ledger audited", home=self.home, root=self.root)
        self.assertEqual({h["collection"] for h in hits}, {"memory"})

    def test_a_search_with_record_false_leaves_no_trace(self):
        memory_search.search("quokka ledger audited", home=self.home, root=self.root, record=False)
        self.assertFalse((self.home / "memory" / ".recall-log.jsonl").exists())

    def test_search_without_a_root_still_discovers_it(self):
        """guard: the CLI and the tmux lane pass no root and keep today's
        discovery (it passes before and after this task)."""
        hits, _notice = memory_search.search("quokka ledger audited", home=self.home)
        self.assertIn("harness", {h["collection"] for h in hits})


class TestRawHitTitle(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        self.home = self.root / "cousins" / "wren"
        for sub in ("data", "memory", "notes"):
            (self.home / sub).mkdir(parents=True)
        (self.home / "cousin.toml").write_text(TOML)
        p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(self.root),
                                         "COUSIN_HOME": str(self.home)})
        p.start(); self.addCleanup(p.stop)

    def test_a_raw_hit_is_named_by_its_topic(self):
        memory.remember(self.home, "quokka audit cadence",
                        "The quokka audit runs every second Tuesday.")
        entries = memory_search.recall_entries(self.home, "when does the quokka audit run?")
        raw = [e for e in entries if "(raw:" in e]
        self.assertEqual(len(raw), 1, entries)
        self.assertTrue(raw[0].startswith("quokka audit cadence (raw:"), raw[0])

    def test_raw_entry_reads_back_the_entry_a_hit_names(self):
        memory.remember(self.home, "spare keys", "Toki keeps the spare keys.")
        hits, _notice = memory_search.search("spare keys", home=self.home, collection="raw")
        self.assertEqual(memory_search.raw_entry(hits[0]["path"])["topic"], "spare keys")
        self.assertIsNone(memory_search.raw_entry(str(self.home / "memory" / "nope.jsonl#1")))


if __name__ == "__main__":
    unittest.main()
