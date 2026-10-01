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

    def test_the_retired_scripts_are_not_shipped(self):
        # no per-cousin chat server, so neither it nor its watchdog
        # ships, and their modules are gone; the auth-mode switch went
        # with the legacy lane's [runtime] auth, and cousin-ui's one
        # release as an alias of cousin-console is over
        scripts = self.pyproject["project"]["scripts"]
        for name in ("cousin-chat-server", "cousin-chat-watchdog", "cousin-auth",
                     "cousin-ui"):
            self.assertNotIn(name, scripts)
        from cousin_lib import ui
        self.assertFalse(hasattr(ui, "ui_main"))
        for module in ("server/app.py", "server/injection.py", "chat_watchdog.py"):
            self.assertFalse((_REPO_ROOT / "cousin_lib" / module).exists(), module)

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

    def test_the_commands_page_covers_every_shipped_cli(self):
        # Every console script, derived from pyproject, must appear in
        # docs/commands.md, so a new command cannot ship undocumented.
        import tomllib
        pyproject = tomllib.loads(
            (_REPO_ROOT / "pyproject.toml").read_text())
        guide = (_REPO_ROOT / "docs" / "commands.md").read_text()
        for cli in pyproject["project"]["scripts"]:
            self.assertIn(cli, guide,
                          "%s is missing from docs/commands.md" % cli)

    def test_every_doc_is_linked_from_somewhere(self):
        # A page nobody links is a page nobody finds. The README or
        # another page must link every file under docs/.
        import re
        docs = _REPO_ROOT / "docs"
        corpus = "\n".join(
            p.read_text() for p in
            [_REPO_ROOT / "README.md"] + list(docs.rglob("*.md")))
        for spec in docs.rglob("*.md"):
            referenced = re.search(
                r"\b%s\b" % re.escape(spec.name), corpus)
            self.assertTrue(referenced,
                            "%s is unreferenced" % spec.name)


class TestPerimeterHygiene(unittest.TestCase):
    """Behavioural, not textual: ask git what the real .gitignore DOES
    to real files, never what it looks like. A text-match test passes
    against a carve-out that git never evaluates and would fail on the
    correct form - it defends the appearance, not the behaviour."""

    def _ignored(self, gitignore_text, relpath, content="secret\n"):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / ".gitignore").write_text(gitignore_text)
            target = root / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            result = subprocess.run(
                ["git", "check-ignore", relpath],
                cwd=root, capture_output=True, text=True)
            return result.returncode == 0  # 0 = the path is ignored

    def setUp(self):
        self.gitignore = (_REPO_ROOT / ".gitignore").read_text()

    def test_a_secret_under_config_is_ignored(self):
        self.assertTrue(
            self._ignored(self.gitignore, "config/embedding.toml"),
            "a credential-bearing config file would stage")

    def test_a_private_home_under_cousins_is_ignored(self):
        self.assertTrue(
            self._ignored(self.gitignore,
                          "cousins/wren/cousin.toml"),
            "a live fleet's private home would stage")

    def test_the_root_shared_tier_is_ignored(self):
        # <root>/shared/ holds the shared memory tier and the hive
        # database with its bearer tokens: runtime state of one
        # install, and a live one under the checkout must not stage
        # (nor fail the self-gate as untracked content).
        for rel in ("shared/hive/hive.db", "shared/proposed/x.md"):
            self.assertTrue(self._ignored(self.gitignore, rel), rel)

    def test_an_example_config_is_NOT_ignored(self):
        # The carve-out that lets safe examples ship: it can only fire
        # if the ignore excludes config's CONTENTS, not the directory -
        # git does not descend into an excluded directory to reach a
        # re-include.
        self.assertFalse(
            self._ignored(self.gitignore,
                          "config/embedding.toml.example"),
            "example configs cannot be committed; the carve-out is"
            " shadowed by a directory-level ignore")


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


class ConsoleStaticPackaging(unittest.TestCase):
    def test_console_static_is_declared_package_data(self):
        import tomllib
        data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
        patterns = (data.get("tool", {}).get("setuptools", {})
                    .get("package-data", {}).get("cousin_lib", []))
        self.assertTrue(any("console_static" in p for p in patterns),
                        "cousin_lib/console_static/* is not declared as package-data;"
                        " a wheel would omit the console")
