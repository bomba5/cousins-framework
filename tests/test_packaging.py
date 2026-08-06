"""Packaging coherence: what a stranger gets from a real install.

`pip install -e .` leaves the tree in place and hides missing data
declarations; a real wheel install includes only what pyproject says
to include. These tests guard the files that are NOT Python and would
therefore vanish from a wheel unless declared - the UI's static
console above all, which the daemon serves from beside its module.
"""
import pathlib
import tomllib
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class TestPackageData(unittest.TestCase):
    def setUp(self):
        self.pyproject = tomllib.loads(
            (_REPO_ROOT / "pyproject.toml").read_text())

    def _package_data(self):
        return (self.pyproject.get("tool", {}).get("setuptools", {})
                .get("package-data", {}))

    def test_ui_static_assets_exist_beside_the_module(self):
        static = _REPO_ROOT / "cousin_lib" / "ui_static"
        for name in ("index.html", "console.css", "console.js"):
            self.assertTrue((static / name).is_file(), name)

    def test_ui_static_is_declared_package_data(self):
        # Without this a wheel ships cousin_lib but not the console it
        # serves, and the UI 404s every page for anyone who pip
        # installed rather than cloning.
        patterns = self._package_data().get("cousin_lib", [])
        self.assertTrue(
            any("ui_static" in p for p in patterns),
            "cousin_lib/ui_static/* is not declared as package-data;"
            " a wheel would omit the web console")

    def test_every_console_script_target_imports(self):
        # A stranger's entry points must all resolve - a script
        # pointing at a moved or misnamed function fails only when they
        # run it.
        import importlib
        for script, target in \
                self.pyproject["project"]["scripts"].items():
            module_name, func = target.split(":")
            module = importlib.import_module(module_name)
            self.assertTrue(hasattr(module, func),
                            "%s -> %s missing" % (script, target))


class TestDocsCoherence(unittest.TestCase):
    def test_every_doc_the_readme_points_at_exists(self):
        import re
        readme = (_REPO_ROOT / "README.md").read_text()
        for ref in set(re.findall(r"docs/[a-z0-9-]+\.md", readme)):
            self.assertTrue((_REPO_ROOT / ref).is_file(),
                            "README points at missing %s" % ref)

    def test_every_spec_doc_is_referenced_somewhere(self):
        # A spec nobody links is a spec nobody finds. The README or
        # another doc must reach every contract under docs/.
        import re
        docs = _REPO_ROOT / "docs"
        corpus = "\n".join(
            p.read_text() for p in
            [_REPO_ROOT / "README.md"] + list(docs.glob("*.md")))
        for spec in docs.glob("*.md"):
            if spec.name == "provenance.md":
                continue  # the ledger references files, not vice versa
            referenced = re.search(
                r"\b%s\b" % re.escape(spec.name), corpus)
            self.assertTrue(referenced,
                            "%s is unreferenced" % spec.name)


class TestConfigSeamsDocumented(unittest.TestCase):
    def test_every_config_file_the_code_reads_is_documented(self):
        # The mechanism form of "no config seam undocumented": scan the
        # source for config/<file> references and require each in
        # docs/configuration.md. A new seam cannot ship unlisted.
        import re
        lib = _REPO_ROOT / "cousin_lib"
        found = set()
        pat = re.compile(
            r'"config",\s*"([a-z0-9.-]+)"'
            r'|"config"\s*/\s*"([a-z0-9.-]+)"')
        for py in lib.rglob("*.py"):
            for a, b in pat.findall(py.read_text()):
                found.add(a or b)
        self.assertTrue(found, "no config seams detected - check the scan")
        doc = (_REPO_ROOT / "docs" / "configuration.md").read_text()
        for name in found:
            self.assertIn(name, doc,
                          "config/%s is read by code but absent from"
                          " docs/configuration.md" % name)


if __name__ == "__main__":
    unittest.main()
