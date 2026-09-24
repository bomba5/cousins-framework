"""The Claude-subscription bridge leaves no trace (phase 9 R13, R18).

The bridge is a third-party opencode plugin that starts a local proxy on a
Claude login; it hooks in through a plugin name, a provider baseURL on
port 3456, environment variables and request headers. None of that may
appear in what the framework ships: the code, the example config, the
plugins, the templates, the image and compose files, and the docs outside
docs/design/ (the design notes record the history). Two things name the
markers on purpose and are the only exclusions: the guard that refuses
them (cousin_lib/runner/opencode_guard.py) and its test, plus the one
docs section that tells an operator how to delete the bridge from a live
install. The scan reads the git-visible files, so a live root's own
config/ or cousins/ beside the checkout never decides the result.
"""
import os
import pathlib
import re
import subprocess
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]

# The wide pattern (Survey 8), as an ERE: the same string is the check in
# docs/migrating.md, so the runbook and this test cannot disagree.
BRIDGE_PATTERN = (r"opencode-with-claude|meridian|rynfar|claude-max-proxy|"
                  r"CLAUDE_PROXY_(PORT|HOST)|MERIDIAN_|x-meridian|127\.0\.0\.1:3456")
_BRIDGE = re.compile(BRIDGE_PATTERN, re.I)
# The master plan's test, word for word: grep -rniE "meridian|opencode-with-claude".
_LITERAL = re.compile(r"meridian|opencode-with-claude", re.I)

_ROOTS = ("cousin_lib", "config", "plugins", "templates", "docs")
_IMAGE_FILES = ("Dockerfile", ".dockerignore", "docker")
_LITERAL_ROOTS = ("cousin_lib", "config", "plugins", "docs")
_DESIGN = "docs/design/"
# Exactly these two files may name the markers: the guard and its test.
EXCLUDED_FILES = ("cousin_lib/runner/opencode_guard.py",
                  "tests/runner/test_opencode_guard.py")
# And exactly this one section: the operator's runbook for a live install.
EXCLUDED_SECTIONS = (("docs/migrating.md",
                      "## Remove the subscription bridge from a live install"),)
_RUNBOOK_FILE, _RUNBOOK_HEADING = EXCLUDED_SECTIONS[0]


def _visible(repo, paths):
    """The git-visible files (tracked, or untracked and not ignored) under
    `paths`, relative to `repo`; without git, every file under them."""
    try:
        out = subprocess.run(["git", "-C", str(repo), "ls-files", "-z", "--cached",
                              "--others", "--exclude-standard", "--"] + list(paths),
                             capture_output=True, check=True, timeout=60).stdout
        names = sorted(set(n for n in out.decode().split("\0") if n))
    except (OSError, subprocess.CalledProcessError):
        names = []
        for p in paths:
            base = repo / p
            if base.is_file():
                names.append(p)
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                for f in filenames:
                    names.append(str((pathlib.Path(dirpath) / f).relative_to(repo)))
        names.sort()
    return [n for n in names if (repo / n).is_file()]


def _without_section(text, heading):
    """`text` with the section under `heading` (to the next `## ` or the
    end) blanked, line numbers kept."""
    lines = text.split("\n")
    out, inside = [], False
    for line in lines:
        if line == heading:
            inside = True
        elif inside and line.startswith("## "):
            inside = False
        out.append("" if inside else line)
    return "\n".join(out)


def _section(text, heading):
    start = text.index(heading + "\n")
    end = text.find("\n## ", start + 1)
    return text[start:] if end < 0 else text[start:end]


def scan(repo, paths, pattern, *, excluded_files=EXCLUDED_FILES,
         excluded_sections=EXCLUDED_SECTIONS, skip_prefix=_DESIGN):
    """["path:line: text"] for every line matching `pattern`."""
    sections = dict(excluded_sections)
    hits = []
    for name in _visible(repo, paths):
        if name in excluded_files or name.startswith(skip_prefix):
            continue
        text = (repo / name).read_bytes().decode("utf-8", "replace")
        if name in sections:
            text = _without_section(text, sections[name])
        for n, line in enumerate(text.split("\n"), 1):
            if pattern.search(line):
                hits.append("%s:%d: %s" % (name, n, line.strip()[:120]))
    return hits


def _compose_files(repo):
    return sorted(p.name for p in repo.glob("compose*.yml"))


class TestNoBridge(unittest.TestCase):
    def test_the_wide_pattern_finds_nothing_in_what_the_framework_ships(self):
        paths = list(_ROOTS) + list(_IMAGE_FILES) + _compose_files(_REPO)
        hits = scan(_REPO, paths, _BRIDGE)
        self.assertEqual(hits, [], "\n".join(hits))

    def test_the_master_plans_grep_prints_nothing(self):
        # grep -rniE "meridian|opencode-with-claude" over cousin_lib, config,
        # plugins and docs outside docs/design/.
        hits = scan(_REPO, _LITERAL_ROOTS, _LITERAL)
        self.assertEqual(hits, [], "\n".join(hits))

    def test_the_exclusions_are_exactly_the_guard_its_test_and_the_runbook(self):
        self.assertEqual(EXCLUDED_FILES, ("cousin_lib/runner/opencode_guard.py",
                                          "tests/runner/test_opencode_guard.py"))
        self.assertEqual(EXCLUDED_SECTIONS, (("docs/migrating.md",
                                              "## Remove the subscription bridge"
                                              " from a live install"),))

    def test_an_excluded_file_still_needs_its_exclusion(self):
        # An exclusion whose file no longer names a marker is stale: drop it.
        for name in EXCLUDED_FILES:
            path = _REPO / name
            if path.exists():
                with self.subTest(name=name):
                    self.assertRegex(path.read_text(), _BRIDGE)

    def test_the_scan_reads_what_it_should(self):
        paths = list(_ROOTS) + list(_IMAGE_FILES) + _compose_files(_REPO)
        seen = _visible(_REPO, paths)
        for name in ("Dockerfile", ".dockerignore", "compose.yml", "compose.api-key.yml",
                     "compose.opencode.yml", "docker/entrypoint.sh",
                     "cousin_lib/accounts.py", "config/harness.toml.example",
                     "docs/install.md", "docs/migrating.md"):
            self.assertIn(name, seen)
        self.assertTrue(any(n.startswith("templates/") for n in seen))
        self.assertTrue(any(n.startswith(_DESIGN) for n in seen),
                        "docs/design/ is listed, then skipped by the scan")


class TestScanner(unittest.TestCase):
    """The scan finds every marker, however it is spelled, and honours
    exactly its exclusions."""

    MARKERS = ("plugin: [\"opencode-with-claude\"]", "import from \"@rynfar/meridian\"",
               "bin: claude-max-proxy", "CLAUDE_PROXY_PORT=3456", "CLAUDE_PROXY_HOST=x",
               "MERIDIAN_PROFILES=a", "x-meridian-source: opencode",
               "baseURL: http://127.0.0.1:3456", "The Meridian proxy")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_every_marker_is_found(self):
        for i, marker in enumerate(self.MARKERS):
            with self.subTest(marker=marker):
                self.write("docs/m%d.md" % i, "ok\n%s\n" % marker)
                hits = scan(self.root, ["docs/m%d.md" % i], _BRIDGE)
                self.assertEqual(hits, ["docs/m%d.md:2: %s" % (i, marker)])

    def test_clean_text_is_clean(self):
        self.write("docs/ok.md", "opencode serve on 127.0.0.1:4096, anthropic-key\n"
                                 "provider openai at http://127.0.0.1:8080/v1\n")
        self.assertEqual(scan(self.root, ["docs"], _BRIDGE), [])

    def test_the_exclusions_and_nothing_more(self):
        self.write("cousin_lib/runner/opencode_guard.py", "MARKERS = ['meridian']\n")
        self.write("cousin_lib/runner/other.py", "x = 'meridian'\n")
        self.write("docs/design/history.md", "meridian\n")
        self.write("docs/migrating.md",
                   "# Migrating\nclean\n## Remove the subscription bridge from a live"
                   " install\nnpm uninstall opencode-with-claude\n## Checking it\n"
                   "rynfar after the section\n")
        hits = scan(self.root, ["cousin_lib", "docs"], _BRIDGE)
        self.assertEqual(hits, ["cousin_lib/runner/other.py:1: x = 'meridian'",
                                "docs/migrating.md:6: rynfar after the section"])


class TestRunbook(unittest.TestCase):
    def setUp(self):
        text = (_REPO / _RUNBOOK_FILE).read_text()
        self.assertEqual(text.count("\n" + _RUNBOOK_HEADING + "\n"), 1)
        self.section = _section(text, _RUNBOOK_HEADING)
        self.flat = " ".join(self.section.split())

    def has(self, needle):
        self.assertTrue(" ".join(needle.split()) in self.flat, "%r not in the runbook" % needle)

    def test_it_removes_the_bridge_packages_and_their_tarballs(self):
        for needle in ("npm uninstall --offline --ignore-scripts --no-audit --no-fund"
                       " opencode-with-claude",
                       "vendor/opencode-with-claude-*.tgz", "vendor/rynfar-meridian-*.tgz",
                       "vendor/rynfar-meridian-plugin-opencode-scrub-*.tgz",
                       "package.json", "package-lock.json",
                       "find node_modules -mindepth 1 -maxdepth 1 -type d -empty -delete"):
            self.has(needle)

    def test_it_keeps_opencode_itself(self):
        self.has("opencode-ai-*.tgz")
        self.has("node_modules/.bin/opencode --version")

    def test_it_covers_cousin_configs_env_and_the_proxy(self):
        for needle in ("opencode.json", "3456", "CLAUDE_PROXY_", "MERIDIAN_",
                       "~/.config/meridian", "ss -ltnp"):
            self.has(needle)

    def test_the_check_is_this_tests_pattern(self):
        self.assertIn("BRIDGE='%s'" % BRIDGE_PATTERN, self.section)
        self.has('grep -rlIiE "$BRIDGE"')

    def test_it_says_what_it_breaks_and_who_runs_it(self):
        self.has("What it breaks")
        self.has("never by the framework")

    def test_generic_ascii_no_home_path(self):
        self.section.encode("ascii")
        self.assertNotIn("/home/", self.section)


if __name__ == "__main__":
    unittest.main()
