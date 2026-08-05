"""Denylist loading and the placement property.

The real denylist must be neither version-controlled nor declaratively
managed. The loader enforces the property at runtime instead of trusting a
comment to outlive the next reorganisation.
"""
import pathlib
import tempfile
import unittest

from cousin_lib.gate.scanner import DenylistLocationError, load_denylist


class TestDenylistLoading(unittest.TestCase):
    def _dir(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return pathlib.Path(tmp.name)

    def test_loads_terms_skipping_comments_and_blanks(self):
        d = self._dir()
        f = d / "terms.txt"
        f.write_text("# household names\nzorblatt\n\nquibbleton\n")
        self.assertEqual(load_denylist(f), ["zorblatt", "quibbleton"])

    def test_refuses_a_path_inside_a_git_work_tree(self):
        d = self._dir()
        (d / ".git").mkdir()
        f = d / "cfg" / "terms.txt"
        f.parent.mkdir()
        f.write_text("zorblatt\n")
        with self.assertRaises(DenylistLocationError):
            load_denylist(f)

    def test_refuses_a_path_resolving_into_a_managed_store(self):
        d = self._dir()
        store = d / "store"
        store.mkdir()
        real = store / "terms.txt"
        real.write_text("zorblatt\n")
        link = d / "terms.txt"
        link.symlink_to(real)
        with self.assertRaises(DenylistLocationError):
            load_denylist(link, store_prefixes=(str(store),))


if __name__ == "__main__":
    unittest.main()
