"""The framework version: one number in pyproject.toml, exposed as
cousin_lib.__version__, printed and bumped by cousin-version, served
by the console's public GET /api/version and shown in its top bar."""
import contextlib
import io
import pathlib
import re
import tempfile
import tomllib
import unittest

import cousin_lib
from cousin_lib import version
from tests.console._harness import ConsoleCase

_REPO = pathlib.Path(__file__).resolve().parents[1]

_PYPROJECT = """[build-system]
requires = ["setuptools>=68"]

[project]
name = "cousins-framework"
version = "0.1.9"   # the one number
description = "x"

[project.scripts]
cousin-version = "cousin_lib.version:version_main"

[tool.other]
version = "9.9.9"
"""


class TheVersion(unittest.TestCase):
    def test_dunder_version_is_the_pyproject_number(self):
        data = tomllib.loads((_REPO / "pyproject.toml").read_text())
        self.assertEqual(cousin_lib.__version__, data["project"]["version"])
        self.assertEqual(version.version(), data["project"]["version"])

    def test_this_branch_ships_a_real_semver(self):
        self.assertRegex(cousin_lib.__version__, r"^\d+\.\d+\.\d+$")
        self.assertNotEqual(cousin_lib.__version__, "0.0.1")

    def test_the_changelog_has_an_entry_for_it(self):
        text = (_REPO / "CHANGELOG.md").read_text()
        self.assertIn("## %s" % cousin_lib.__version__, text)

    def test_a_pyproject_for_another_project_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "pyproject.toml"
            path.write_text('[project]\nname = "other"\nversion = "1.0.0"\n')
            self.assertIsNone(version.pyproject_version(path))

    def test_unknown_attribute_still_raises(self):
        with self.assertRaises(AttributeError):
            cousin_lib.no_such_thing  # noqa: B018


class Bump(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = pathlib.Path(tmp.name) / "pyproject.toml"
        self.path.write_text(_PYPROJECT)

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = version.version_main(argv + ["--pyproject", str(self.path)])
        return rc, out.getvalue(), err.getvalue()

    def test_each_part(self):
        self.assertEqual(version.bumped("1.2.3", "patch"), "1.2.4")
        self.assertEqual(version.bumped("1.2.3", "minor"), "1.3.0")
        self.assertEqual(version.bumped("1.2.3", "major"), "2.0.0")
        with self.assertRaises(version.VersionError):
            version.bumped("1.2", "patch")

    def test_bump_edits_only_the_project_version(self):
        rc, out, _ = self.run_cli(["bump", "minor"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.split(" (")[0], "0.1.9 -> 0.2.0")
        self.assertEqual(self.path.read_text(),
                         _PYPROJECT.replace('"0.1.9"', '"0.2.0"'))
        rc, out, _ = self.run_cli([])
        self.assertEqual(out.strip(), "0.2.0")
        rc, out, _ = self.run_cli(["bump"])
        self.assertIn("0.2.0 -> 0.2.1", out)

    def test_a_malformed_version_is_rc_2_and_leaves_the_file(self):
        self.path.write_text('[project]\nname = "cousins-framework"\n'
                             'version = "one"\n')
        before = self.path.read_text()
        rc, _out, err = self.run_cli(["bump", "patch"])
        self.assertEqual(rc, 2)
        self.assertIn("MAJOR.MINOR.PATCH", err)
        self.assertEqual(self.path.read_text(), before)


class VersionRoute(ConsoleCase):
    def test_public_even_when_login_is_configured(self):
        from cousin_lib.console import auth
        auth.Users(self.root / "config" / "console-users.json") \
            .set_password("ana", "correct horse")
        self.serve()
        status, body = self.get("/api/version")
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], cousin_lib.__version__)
        self.assertIn("commit", body)
        self.assertEqual(set(body), {"version", "commit", "repo_url",
                                     "commit_url"})
        # and the rest stays behind the login
        self.assertEqual(self.get("/api/cousins")[0], 401)


class RepoUrl(unittest.TestCase):
    """The badge links to the repository the checkout came from. Canary:
    the version is a link to the repo."""

    def test_remote_forms_become_a_browsable_https_url(self):
        cases = {
            "git@github.com:kestrel/wren-kit.git": "https://github.com/kestrel/wren-kit",
            "https://github.com/kestrel/wren-kit.git": "https://github.com/kestrel/wren-kit",
            "https://github.com/kestrel/wren-kit": "https://github.com/kestrel/wren-kit",
            "ssh://git@git.example.org:2222/kestrel/wren-kit.git": "https://git.example.org/kestrel/wren-kit",
        }
        for remote, url in cases.items():
            with self.subTest(remote=remote):
                self.assertEqual(version.browse_url(remote), url)

    def test_credentials_in_the_remote_never_leak(self):
        url = version.browse_url("https://kestrel:s3cr3t-token@github.com/kestrel/wren-kit.git")
        self.assertEqual(url, "https://github.com/kestrel/wren-kit")
        self.assertNotIn("s3cr3t", url)

    def test_local_or_odd_remotes_give_no_link(self):
        for remote in ("/srv/git/wren-kit.git", "file:///srv/wren", "", None,
                       "../wren-kit.bundle"):
            with self.subTest(remote=remote):
                self.assertIsNone(version.browse_url(remote))

    def test_commit_url_appends_the_commit(self):
        self.assertEqual(version.commit_url("https://github.com/kestrel/wren-kit", "abc1234"),
                         "https://github.com/kestrel/wren-kit/commit/abc1234")
        self.assertIsNone(version.commit_url(None, "abc1234"))
        self.assertIsNone(version.commit_url("https://github.com/kestrel/wren-kit", None))


class TopBar(unittest.TestCase):
    def test_the_version_sits_next_to_the_unchanged_brand(self):
        app = (_REPO / "cousin_lib" / "console_static" / "app.jsx") \
            .read_text(encoding="utf-8")
        self.assertIn("/api/version", app)
        m = re.search(r'<span className="brand">.*?</span>\s*\n(.*)\n', app)
        self.assertIsNotNone(m)
        self.assertIn("version", m.group(1))
        self.assertIn("commit", m.group(1))
        # both are links when the route gives the urls, opened safely
        self.assertIn("repo_url", m.group(1))
        self.assertIn("commit_url", m.group(1))
        self.assertIn('rel="noopener noreferrer"', m.group(1))


if __name__ == "__main__":
    unittest.main()


class BadgeLinks(unittest.TestCase):
    """Links in the accent colour, and a link's
    tooltip is its address."""

    def test_links_are_accent_and_title_their_address(self):
        css = (_REPO / "cousin_lib" / "console_static" / "styles.css").read_text()
        self.assertRegex(css, r"(?m)^a \{ color: var\(--accent\); \}")
        app = (_REPO / "cousin_lib" / "console_static" / "app.jsx").read_text()
        self.assertIn("title={build.repo_url}", app)
        self.assertIn("title={build.commit_url}", app)
