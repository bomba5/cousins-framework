"""The per-runner contract table (phase 9 Task 11, R19): the item list is
the suite's, every runner kind has a class and a contract module, each
cell comes from the class-level declarations, and
docs/reference/runners.md holds exactly what the module renders."""
import contextlib
import importlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cousin_lib.delivery import RUNNER_KINDS
from cousin_lib.runner import contract_table as ct
from cousin_lib.runner.fake import FakeRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home
from tests.runner.contract.suite import CONTRACT_ITEMS

REPO = Path(__file__).resolve().parents[2]


class Declares:
    UNSUPPORTED = ("midturn_fold",)
    PLUGIN_ITEMS = ("turn_events",)


class Unknown:
    UNSUPPORTED = ("teleport",)


class Both:
    UNSUPPORTED = ("peer_waits",)
    PLUGIN_ITEMS = ("peer_waits",)


class Silent:
    """A class that declares nothing at all: every item IMPLEMENTED."""


def _with(**classes):
    return mock.patch.dict(ct.RUNNER_CLASSES, {
        kind: "%s:%s" % (__name__, cls.__name__) for kind, cls in classes.items()})


class TestItems(unittest.TestCase):
    def test_the_items_are_the_suites_in_its_order(self):
        self.assertEqual(ct.item_names(), CONTRACT_ITEMS)
        self.assertTrue(all(what for _, what in ct.ITEMS))

    def test_every_runner_kind_has_a_class_and_a_contract_module(self):
        """A kind with no contract module would get a column the suite never ran."""
        self.assertEqual(sorted(ct.RUNNER_CLASSES), sorted(RUNNER_KINDS))
        for kind in RUNNER_KINDS:
            with self.subTest(kind=kind):
                self.assertTrue(callable(getattr(ct.runner_class(kind), "unsupported")))
                importlib.import_module("tests.runner.contract.test_%s" % kind)


class TestDeclarations(HermeticCase):
    def test_each_runner_declares_at_class_level_and_its_methods_answer_it(self):
        for kind in RUNNER_KINDS:
            cls = ct.runner_class(kind)
            with self.subTest(kind=kind):
                self.assertIsInstance(cls.__dict__.get("UNSUPPORTED"), tuple)
                self.assertIsInstance(cls.__dict__.get("PLUGIN_ITEMS"), tuple)
                bare = object.__new__(cls)            # no server, no session: nothing built
                self.assertEqual(bare.unsupported(), list(cls.UNSUPPORTED))
                self.assertEqual(bare.plugin_items(), list(cls.PLUGIN_ITEMS))
        r = FakeRunner(temp_home(self, runner="fake"))
        self.assertEqual((r.unsupported(), r.plugin_items()), ([], []))

    def test_a_declared_item_is_declared_a_plugin_item_plugin_the_rest_implemented(self):
        with _with(test=Declares, bare=Silent):
            table = ct.cells(("test", "bare"))
        self.assertEqual(table["test"]["midturn_fold"], ct.DECLARED)
        self.assertEqual(table["test"]["turn_events"], ct.PLUGIN)
        self.assertEqual(table["test"]["peer_waits"], ct.IMPLEMENTED)
        self.assertEqual(set(table["bare"].values()), {ct.IMPLEMENTED})
        with _with(test=Declares):
            text = ct.render(("test",))
        self.assertIn("| `midturn_fold` | ", text)
        self.assertTrue(text.splitlines()[0].endswith("| `test` |"))
        lines = text.splitlines()
        self.assertEqual(lines[1], "|---|---|---|")      # one column per kind
        self.assertEqual(len(lines), 2 + len(ct.ITEMS))
        self.assertTrue(all(line.count("|") == 4 for line in lines), lines)

    def test_an_unknown_or_doubled_declaration_is_refused(self):
        for cls, needle in ((Unknown, "names no contract item: teleport"),
                            (Both, "both unsupported and met by a plugin")):
            with self.subTest(cls=cls.__name__), _with(test=cls):
                with self.assertRaisesRegex(ct.TableError, needle):
                    ct.cells(("test",))

    def test_a_kind_with_no_class_is_refused(self):
        with self.assertRaisesRegex(ct.TableError, "has no class"):
            ct.runner_class("tmux")

    def test_the_shipped_runners_declare_nothing(self):
        """R14 (OPERATOR), measured in Task 7: opencode DECLARES no item and
        no item rides on a plugin (the plugin pack is the policy veto,
        which is not a contract item)."""
        for kind, column in ct.cells().items():
            with self.subTest(kind=kind):
                self.assertEqual(set(column.values()), {ct.IMPLEMENTED})


class TestPage(HermeticCase):
    def page(self):
        return (REPO / ct.PAGE).read_text()

    def test_the_page_holds_the_rendered_table(self):
        text = self.page()
        self.assertEqual(ct.splice(text, ct.render()), text,
                         "docs/reference/runners.md is stale: run"
                         " python3 -m cousin_lib.runner.contract_table --write")
        self.assertEqual(ct.main(["--check"]), 0)

    def test_check_fails_and_write_repairs_a_stale_page(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        stale = Path(tmp.name) / "runners.md"
        stale.write_text(self.page().replace("IMPLEMENTED |", "DECLARED |", 1))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(ct.main(["--check", "--page", str(stale)]), 1)
        self.assertIn("is stale", err.getvalue())
        self.assertEqual(ct.main(["--write", "--page", str(stale)]), 0)
        self.assertEqual(stale.read_text(), self.page())

    def test_the_markers_must_be_there_once(self):
        with self.assertRaisesRegex(ct.TableError, "exactly one"):
            ct.splice("# no table here\n", "| x |\n")
        with self.assertRaisesRegex(ct.TableError, "exactly one"):
            ct.splice(ct.END + "\n" + ct.BEGIN, "| x |\n")

    def test_the_page_is_linked_and_says_what_the_cells_mean(self):
        text = self.page()
        for needle in ("IMPLEMENTED", "PLUGIN", "DECLARED", "`sdk`", "`fake`", "`opencode`",
                       "apply_patch", "Known gaps"):
            self.assertIn(needle, text)
        self.assertIn("reference/runners.md", (REPO / "README.md").read_text()
                      + (REPO / "docs" / "configuration.md").read_text())


if __name__ == "__main__":
    unittest.main()
