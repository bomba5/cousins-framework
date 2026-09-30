"""Configuration seam.

Every hardcoded root, port, and personal default in an earlier version
becomes a lookup here. Fail-loud rule: a missing COUSIN_HOME is an error
with a message, never a silent fallback to somebody's home directory.
"""
import pathlib
import re
import tempfile
import unittest
from unittest import mock

from cousin_lib.config import CousinConfig, MissingConfigError


class TestCousinConfig(unittest.TestCase):
    def _home(self, toml_text=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = pathlib.Path(tmp.name)
        if toml_text is not None:
            (home / "cousin.toml").write_text(toml_text)
        return home

    def test_loads_identity_and_chat_port_from_cousin_toml(self):
        home = self._home(
            '[cousin]\nslug = "wren"\nname = "Wren"\n[chat]\nport = 8100\n'
        )
        cfg = CousinConfig.load(home)
        self.assertEqual(cfg.slug, "wren")
        self.assertEqual(cfg.name, "Wren")
        self.assertEqual(cfg.chat_port, 8100)

    def test_missing_cousin_home_env_fails_loud(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(MissingConfigError):
                CousinConfig.from_env()

    def test_from_env_reads_cousin_home(self):
        home = self._home('[cousin]\nslug = "wren"\n[chat]\nport = 8100\n')
        with mock.patch.dict("os.environ", {"COUSIN_HOME": str(home)}):
            cfg = CousinConfig.from_env()
        self.assertEqual(cfg.slug, "wren")

    def test_operator_name_defaults_to_none_not_a_person(self):
        # The null-operator profile starts here: no configured operator
        # means no operator, never a default human.
        home = self._home('[cousin]\nslug = "wren"\n[chat]\nport = 8100\n')
        cfg = CousinConfig.load(home)
        self.assertIsNone(cfg.operator_name)

    def test_operator_name_from_config_when_present(self):
        home = self._home(
            '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n'
            '[operator]\nname = "Sam"\n'
        )
        cfg = CousinConfig.load(home)
        self.assertEqual(cfg.operator_name, "Sam")

    def test_tmux_session_defaults_to_slug(self):
        home = self._home('[cousin]\nslug = "wren"\n[chat]\nport = 8100\n')
        cfg = CousinConfig.load(home)
        self.assertEqual(cfg.tmux_session, "wren")

    def test_tmux_session_from_config_when_present(self):
        home = self._home(
            '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n'
            'tmux_session = "wren-main"\n'
        )
        cfg = CousinConfig.load(home)
        self.assertEqual(cfg.tmux_session, "wren-main")

    def test_memory_scope_defaults_to_private(self):
        # The privacy gate's deny-on-uncertainty starts here: unset
        # means private, never shared.
        home = self._home('[cousin]\nslug = "wren"\n[chat]\nport = 8100\n')
        self.assertEqual(CousinConfig.load(home).memory_scope, "private")

    def test_memory_scope_from_config(self):
        home = self._home(
            '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n'
            '[memory]\nscope = "both"\n'
        )
        # "both" is retired: read as its successor "shared"
        self.assertEqual(CousinConfig.load(home).memory_scope, "shared")

    def test_proactive_recall_defaults_on(self):
        # A colleague remembers without being asked unless told not to.
        home = self._home('[cousin]\nslug = "wren"\n[chat]\nport = 8100\n')
        self.assertIs(CousinConfig.load(home).proactive_recall, True)

    def test_proactive_recall_explicit_off(self):
        home = self._home(
            '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n'
            '[memory]\nproactive_recall = false\n'
        )
        self.assertIs(CousinConfig.load(home).proactive_recall, False)

    def test_heartbeat_default_is_one_value_everywhere(self):
        # The source shipped three different beat defaults across
        # code, template, and docs. One value, stated in the spec.
        home = self._home('[cousin]\nslug = "wren"\n[chat]\nport = 8100\n')
        self.assertEqual(CousinConfig.load(home).heartbeat_seconds, 3600)

    def test_heartbeat_from_config(self):
        home = self._home(
            '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n'
            '[heartbeat]\ncontext_beat_seconds = 600\n')
        self.assertEqual(CousinConfig.load(home).heartbeat_seconds, 600)

    def test_cousin_type_defaults_to_cousin(self):
        home = self._home('[cousin]\nslug = "wren"\n[chat]\nport = 8100\n')
        self.assertEqual(CousinConfig.load(home).type, "cousin")

    def test_worker_type_from_config(self):
        home = self._home(
            '[cousin]\nslug = "g"\ntype = "worker"\n[chat]\nport = 8100\n')
        self.assertEqual(CousinConfig.load(home).type, "worker")

    def test_flip_at_defaults_to_none_and_parses(self):
        home = self._home('[cousin]\nslug = "wren"\n[chat]\nport = 8100\n')
        self.assertIsNone(CousinConfig.load(home).flip_at)
        home = self._home(
            '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n'
            '[lifecycle]\nflip_at = "04:00"\n')
        self.assertEqual(CousinConfig.load(home).flip_at, "04:00")

    def test_missing_chat_port_fails_loud(self):
        home = self._home('[cousin]\nslug = "wren"\n')
        cfg = CousinConfig.load(home)
        with self.assertRaises(MissingConfigError):
            cfg.require_chat_port()


_REPO = pathlib.Path(__file__).resolve().parents[1]
# A model id as the docs and examples write one; claude-login and the like
# are account kinds, not models.
_MODEL_ID = re.compile(r"\bclaude-(?:opus|sonnet|haiku|fable)[a-z0-9.-]*(?:\[1m\])?")
# An effort list: `low` through `max` within one short run of words.
_EFFORT_RUN = re.compile(r"\blow\b.{0,60}?\bmax\b")


def _doc_files():
    """The user-facing docs and shipped examples; design notes and the
    changelog are history and keep the words of their day."""
    docs = [p for p in (_REPO / "docs").rglob("*.md") if "design" not in p.parts]
    return docs + sorted((_REPO / "config").glob("*.example")) + [_REPO / "README.md"]


class TestOneModelAndEffortList(unittest.TestCase):
    """#41: the model catalogue and the effort levels live once, in
    cousin_lib/config.py (DEFAULT_MODELS, EFFORT_LEVELS). Code imports them;
    a doc or example that lists them is checked against them here."""

    def test_no_code_copy(self):
        from cousin_lib.config import EFFORT_LEVELS
        # the effort run, not any low/medium/high (capsule.py's confidences)
        effort_literal = re.compile(r"[\"']high[\"'],\s*[\"']xhigh[\"']")
        pkg = _REPO / "cousin_lib"
        for path in sorted(pkg.rglob("*")):
            if path.suffix not in (".py", ".jsx", ".js") or path.name == "config.py" \
                    or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(_MODEL_ID.search(text), "%s names a model id" % path)
            self.assertIsNone(effort_literal.search(text), "%s copies the effort levels" % path)
        self.assertEqual(len(EFFORT_LEVELS), len(set(EFFORT_LEVELS)))

    def test_every_effort_list_in_the_docs_is_the_levels(self):
        from cousin_lib.config import EFFORT_LEVELS
        seen = 0
        for path in _doc_files():
            flat = " ".join(path.read_text(encoding="utf-8").split())
            for m in _EFFORT_RUN.finditer(flat):
                words = [w for w in re.findall(r"[a-z]+", m.group(0)) if w not in ("or", "and")]
                self.assertEqual(tuple(words), EFFORT_LEVELS, "%s: %r" % (path, m.group(0)))
                seen += 1
        self.assertGreater(seen, 0)

    def test_every_model_id_in_the_docs_is_in_the_catalogue(self):
        from cousin_lib.config import DEFAULT_MODELS
        seen = 0
        for path in _doc_files():
            for model in _MODEL_ID.findall(path.read_text(encoding="utf-8")):
                self.assertIn(model, DEFAULT_MODELS, "%s names %s" % (path, model))
                seen += 1
        self.assertGreater(seen, 0)

    def test_the_catalogue_is_described_not_counted(self):
        # the example said "a built-in list of three names" while the list
        # held seven: a doc points at DEFAULT_MODELS instead of sizing it
        for path in _doc_files():
            flat = " ".join(path.read_text(encoding="utf-8").split())
            self.assertIsNone(re.search(r"built-in list of \w+ names", flat), path)
        conf = (_REPO / "docs" / "configuration.md").read_text(encoding="utf-8")
        self.assertIn("DEFAULT_MODELS", conf)
        self.assertIn("EFFORT_LEVELS", conf)


if __name__ == "__main__":
    unittest.main()


class RecallKeywordOnlyKey(unittest.TestCase):
    def test_default_false_and_explicit_true(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n')
            self.assertFalse(CousinConfig.load(home).recall_keyword_only)
            (home / "cousin.toml").write_text(
                '[cousin]\nslug = "wren"\nname = "Wren"\n[memory]\nrecall_keyword_only = true\n')
            self.assertTrue(CousinConfig.load(home).recall_keyword_only)


class TestRuntimeModelAndEffort(unittest.TestCase):
    """cousin.toml [runtime] model and effort: per-cousin values the
    agent-cmd placeholders {model} and {effort} render from. Absent is
    None (the install default applies); an effort outside the four
    levels is loud, because a misspelt level would otherwise reach the
    agent binary as a flag it rejects at the far end of a spawn."""

    def _home(self, toml_text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = pathlib.Path(tmp.name)
        (home / "cousin.toml").write_text(toml_text)
        return home

    def test_absent_means_none_never_a_vendor_default(self):
        cfg = CousinConfig.load(
            self._home('[cousin]\nslug = "wren"\n[chat]\nport = 8100\n'))
        self.assertIsNone(cfg.model)
        self.assertIsNone(cfg.effort)

    def test_reads_both_from_runtime(self):
        cfg = CousinConfig.load(self._home(
            '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n'
            '[runtime]\nmodel = "some-model"\neffort = "low"\n'))
        self.assertEqual(cfg.model, "some-model")
        self.assertEqual(cfg.effort, "low")

    def test_effort_outside_the_levels_is_loud(self):
        from cousin_lib.config import EFFORT_LEVELS
        self.assertEqual(EFFORT_LEVELS,
                         ("low", "medium", "high", "xhigh", "max"))
        with self.assertRaises(MissingConfigError) as ctx:
            CousinConfig.load(self._home(
                '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n'
                '[runtime]\neffort = "ultra"\n'))
        self.assertIn("effort", str(ctx.exception))
        self.assertIn("ultra", str(ctx.exception))

    def test_xhigh_is_a_level(self):
        # Canary: the agent CLI's --effort takes low, medium, high,
        # xhigh, max. A catalogue without xhigh hides a level the
        # harness accepts, which the console user found 2026-09-18.
        cfg = CousinConfig.load(self._home(
            '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n'
            '[runtime]\neffort = "xhigh"\n'))
        self.assertEqual(cfg.effort, "xhigh")

    def test_default_model_catalogue_covers_the_current_family(self):
        # Canary: the spawn dialog offered three models while the
        # harness accepted six (2026-09-18). Every id here was probed
        # against the CLI; a fable [1m] id is absent on purpose because
        # the CLI silently served plain fable for it.
        from cousin_lib.config import DEFAULT_MODELS
        # claude-opus-5-5 probed live on five running cousins (2026-09-23).
        for model in ("claude-opus-5-5", "claude-fable-5-1", "claude-opus-5",
                      "claude-opus-5[1m]", "claude-sonnet-5",
                      "claude-sonnet-5[1m]", "claude-haiku-4-5-20251001"):
            self.assertIn(model, DEFAULT_MODELS)
        self.assertNotIn("claude-fable-5-1[1m]", DEFAULT_MODELS)
        self.assertEqual(DEFAULT_MODELS[0], "claude-opus-5-5")
        self.assertEqual(len(set(DEFAULT_MODELS)), len(DEFAULT_MODELS))
