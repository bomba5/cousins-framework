"""The composed system prompt: complete, never truncated, byte-stable, root explicit."""
import contextlib
import datetime
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

from cousin_lib import boot, mcp_server, template_sync
from cousin_lib.runner import prompt
from tests._hermetic import HermeticCase

REPO = pathlib.Path(__file__).resolve().parents[2]
NOWHERE = {"FRAMEWORK_ROOT": "/nonexistent/framework-root"}
LAW = "".join("%d. Clause %d of the law, which is never cut.\n" % (i, i) for i in range(1, 400))
TEMPLATE = template_sync._template_text(REPO)
DOCTRINE = next(p for p in TEMPLATE.split("\n\n") if p.startswith("You are part of"))
INVARIANT = next(p for p in TEMPLATE.split("\n\n") if p.startswith("Invariant for every cousin"))



@contextlib.contextmanager
def _clock_at(epoch):
    """Every clock and counter a composer could read, moved to `epoch`.
    Patching time.time alone misses time.strftime(fmt), time.gmtime() and
    datetime.now(), which read the C clock directly and so saw no jump. A
    name a cousin_lib module bound at import (`from time import strftime`)
    is moved too: patching the time module cannot reach it."""
    gm, lt, strf = time.gmtime, time.localtime, time.strftime
    wall, wall_ns = time.time, time.time_ns
    mono, perf = time.monotonic, time.perf_counter
    shift = epoch - wall()
    ctime, asctime = time.ctime, time.asctime
    real_dt, real_date = datetime.datetime, datetime.date

    class _DateTime(real_dt):
        @classmethod
        def now(cls, tz=None):
            return real_dt.fromtimestamp(epoch, tz)

        @classmethod
        def utcnow(cls):
            return real_dt.fromtimestamp(epoch, datetime.timezone.utc).replace(tzinfo=None)

        @classmethod
        def today(cls):
            return real_dt.fromtimestamp(epoch)

    class _Date(real_date):
        @classmethod
        def today(cls):
            return real_date.fromtimestamp(epoch)

    moved = {
        "time.time": lambda: epoch,
        "time.time_ns": lambda: int(epoch) * 10**9,
        "time.gmtime": lambda secs=None: gm(epoch if secs is None else secs),
        "time.localtime": lambda secs=None: lt(epoch if secs is None else secs),
        "time.strftime": lambda fmt, t=None: strf(fmt, lt(epoch) if t is None else t),
        "time.ctime": lambda secs=None: ctime(epoch if secs is None else secs),
        "time.asctime": lambda t=None: asctime(lt(epoch) if t is None else t),
        "datetime.datetime": _DateTime,
        "datetime.date": _Date,
        "time.monotonic": lambda: mono() + shift,
        "time.perf_counter": lambda: perf() + shift,
    }
    real = {id(obj): moved["%s.%s" % (obj.__module__, obj.__name__)]
            for obj in (wall, wall_ns, gm, lt, strf, ctime, asctime, mono, perf, real_dt, real_date)}
    with contextlib.ExitStack() as stack:
        for target, value in moved.items():
            stack.enter_context(mock.patch(target, value))
        for name, module in list(sys.modules.items()):
            if name != "cousin_lib" and not name.startswith("cousin_lib."):
                continue
            for attr, value in list(vars(module).items()):
                if id(value) in real:
                    stack.enter_context(mock.patch.object(module, attr, real[id(value)]))
        yield

class PromptCase(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "wren"
        (self.home / "data").mkdir(parents=True)
        (self.home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n')
        (self.root / "config").mkdir()
        (self.root / "config" / "law.md").write_text(LAW)
        (self.root / "shared").mkdir()
        (self.root / "shared" / "rule_short.md").write_text("---\nkind: rule\n---\nBe brief.\n")
        (self.root / "shared" / "ref_map.md").write_text("---\ndescription: a map\n---\nx\n")
        (self.home / "self-portrait.md").write_text("# Portrait\n## Voice\nDry and exact.\n")
        (self.home / "CLAUDE.md").write_text(
            "# Wren - keeps the ledgers\n\n## Identity\n\nWren keeps the ledgers.\n\n" + DOCTRINE +
            "\n\n## Memory\n\nframework doctrine\n\n"
            "## Voice\n\n<!-- a template comment -->\n\nPlain.\n\n" + INVARIANT + "\n\n"
            "## Meetings\n\nmore doctrine\n\n"
            "## Append your cousin-specific sections below this line\n\n## Wren's rules\n\nNo guessing.\n")
        p = mock.patch.dict(os.environ, NOWHERE); p.start(); self.addCleanup(p.stop)
        self.registry = mcp_server.parse_registry(mcp_server.shipped_default_registry(REPO), "t")

    def compose(self, **kw):
        return prompt.compose_system_prompt(self.home, root=self.root, registry=self.registry,
                                            version="1.12.0", **kw)


class TestCompose(PromptCase):
    def test_sections_in_order_law_first(self):
        text = self.compose()
        order = [text.index(s) for s in ("1. Clause 1", "# Framework contract",
                                          "Wren keeps the ledgers", "Be brief.")]
        self.assertEqual(order, sorted(order))

    def test_the_root_is_the_one_given_never_the_environment(self):
        self.assertIn("Clause 1 of the law", self.compose())   # FRAMEWORK_ROOT points nowhere

    def test_the_law_appears_whole_however_long(self):
        self.assertGreater(len(LAW), boot.LAYER_BUDGETS["law"][1])  # longer than the packet allows
        self.assertIn(LAW.strip(), self.compose())
        self.assertNotIn("truncated", self.compose())

    def test_identity_keeps_the_authored_parts_and_the_title(self):
        text = self.compose()
        for kept in ("# Wren - keeps the ledgers", "Wren keeps the ledgers.", "Plain.",
                     "No guessing.", "Dry and exact."):
            self.assertIn(kept, text)

    def test_identity_drops_the_doctrine_the_template_and_comments(self):
        text = self.compose()
        for dropped in ("framework doctrine", "more doctrine", "a template comment",
                        " ".join(DOCTRINE.split())[:60], " ".join(INVARIANT.split())[:60]):
            self.assertNotIn(dropped, " ".join(text.split()))

    def test_a_reincarnated_role_reaches_the_prompt(self):
        from cousin_lib import lifecycle
        claude = self.home / "CLAUDE.md"
        claude.write_text(lifecycle.rewrite_role(claude.read_text(), name="Wren",
                                                 new_role="audits the audits"))
        self.assertIn("# Wren - audits the audits", self.compose())

    def test_the_law_and_every_rule_arrive_whole_on_every_lane(self):
        # the perimeter lives in the law and the operator rules: no lane may
        # budget them, however long they grow (a budget only ever applies to
        # the state digest)
        long_rule = "".join("Rule line %d stays.\n" % i for i in range(2000))
        self.assertGreater(len(long_rule), boot.LAYER_BUDGETS["shared"][1])
        (self.root / "shared" / "rule_long.md").write_text("---\nkind: rule\n---\n" + long_rule)
        block = prompt.compose_context_block(self.home, root=self.root, registry=self.registry,
                                             version="1.12.0")
        for lane, text in (("runner", self.compose()), ("tmux pane", block)):
            self.assertIn(LAW.strip(), text, lane)
            self.assertIn(long_rule.strip(), text, lane)
            self.assertIn("Be brief.", text, lane)
            self.assertNotIn("budget hit", text, lane)

    def test_rules_in_the_prompt_the_reference_index_not(self):
        text = self.compose()
        self.assertIn("Be brief.", text)
        self.assertNotIn("ref_map.md", text)

    def test_byte_identical_across_a_rollover_and_a_clock_jump(self):
        first = self.compose()
        boot.bump_generation(self.home)                                  # a rollover happened
        (self.home / "STATUS.md").write_text("## Open loops\n- new\n")  # state moved
        (self.home / "data" / "handoff.md").write_text("handoff\n")
        with _clock_at(4_102_444_800.0):                                 # years later, every clock
            second = self.compose()
        self.assertEqual(first.encode(), second.encode())

    def test_no_volatile_markers(self):
        text = self.compose()
        for needle in ("Generation:", "identity_hash", "memory_snapshot", "STALE WARNING",
                       "Open loops", str(self.home)):
            self.assertNotIn(needle, text)


class TestIdentity(PromptCase):
    def test_missing_identity_is_the_named_degraded_state(self):
        (self.home / "self-portrait.md").unlink(); (self.home / "CLAUDE.md").unlink()
        text, degraded = prompt.authored_identity(self.home, root=self.root)
        self.assertTrue(degraded)
        self.assertEqual(text, prompt.IDENTITY_ABSENT)
        self.assertIn(prompt.IDENTITY_ABSENT, self.compose())

    def test_a_claude_md_that_is_only_template_is_not_authored(self):
        (self.home / "self-portrait.md").unlink()
        (self.home / "CLAUDE.md").write_text("## Identity\n\n" + DOCTRINE + "\n")
        self.assertTrue(prompt.authored_identity(self.home, root=self.root)[1])

    def test_either_source_alone_is_not_degraded(self):
        (self.home / "CLAUDE.md").unlink()
        self.assertFalse(prompt.authored_identity(self.home, root=self.root)[1])

    def test_the_degraded_text_invents_no_persona(self):
        low = prompt.IDENTITY_ABSENT.lower()
        self.assertIn("plain professional register", low)
        self.assertIn("degraded", low)


class TestOption(PromptCase):
    def option(self):
        return prompt.system_prompt_option(self.home, root=self.root, registry=self.registry,
                                           version="1.12.0")

    def test_the_preset_carries_both_switches_and_never_the_text(self):
        opt = self.option()
        self.assertEqual(opt["type"], "preset"); self.assertEqual(opt["preset"], "claude_code")
        self.assertIs(opt["exclude_dynamic_sections"], True)
        self.assertIs(opt["snapshot"], True)
        self.assertNotIn("append", opt)                 # the SDK would put it on the argv
        self.assertNotIn(self.compose().strip()[:200], repr(opt))

    def test_the_text_is_written_to_the_private_file(self):
        self.option()
        path = prompt.system_prompt_path(self.home)
        self.assertEqual(path, self.home / "data" / "run" / "system-prompt.md")
        self.assertEqual(path.read_text(), self.compose())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_a_wider_file_or_directory_is_narrowed(self):
        run = self.home / "data" / "run"
        run.mkdir(parents=True, exist_ok=True)
        run.chmod(0o755)
        path = prompt.system_prompt_path(self.home)
        path.write_text("old")
        path.chmod(0o644)
        self.option()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(run.stat().st_mode & 0o777, 0o700)

    def test_every_call_rewrites_it_so_a_new_start_gets_the_new_persona(self):
        self.option()
        (self.home / "self-portrait.md").write_text("# Wren\n\nWren now audits the audits.\n")
        self.option()
        self.assertIn("Wren now audits the audits.",
                      prompt.system_prompt_path(self.home).read_text())

    def test_the_write_is_atomic_and_leaves_no_partial_file(self):
        self.option()
        path = prompt.system_prompt_path(self.home)
        before = path.read_text()
        (self.home / "self-portrait.md").write_text("# Wren\n\nchanged\n")
        with mock.patch.object(prompt.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.option()
        self.assertEqual(path.read_text(), before)      # the old file, whole
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), [path.name])


class TestRunnerLaneDoctrine(PromptCase):
    """Authored text written for the tmux lane reaches the runner's
    prompt as identity; the contract ahead of it overrides the CLI habit."""

    def test_an_identity_that_says_cousin_reply_is_overridden_by_the_contract(self):
        claude = self.home / "CLAUDE.md"
        claude.write_text(claude.read_text() + "\n## Chat\n\nReply path: "
                          "`cousin-reply --user Priya -m '...'`; log with `cousin-memory decide`.\n")
        text = " ".join(self.compose().split())
        told = text.index("A person on your chat surface is answered with the `reply` tool")
        self.assertLess(told, text.index("Reply path: `cousin-reply"))
        self.assertLess(text.index("through the `memory` tool"), text.index("log with `cousin-memory"))

    def test_the_tmux_lane_never_gets_the_runner_lane_section(self):
        flat = " ".join(TEMPLATE.split())
        self.assertNotIn("the `reply` tool", flat)
        self.assertIn("cousin-reply --user <their name> <<'REPLY'", TEMPLATE)


if __name__ == "__main__":
    unittest.main()
