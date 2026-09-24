"""Phase 9's close-out: the CHANGELOG entry, the version, the master
plan's row, exit criterion and locked interfaces, and the docs that say
what the opencode lane is and is not. Doc checks only: the lane's
behaviour is pinned by tests/runner/test_opencode*.py and the contract
suite (tests/runner/contract/test_opencode.py)."""
import inspect
import pathlib
import re
import tomllib
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
_PLAN = "docs/design/plans/agent-loop-runner-plan.md"
_VERSION = tomllib.loads((_REPO / "pyproject.toml").read_text())["project"]["version"]
# Phase 9's CHANGELOG entry is found by what it says, never by its number
# or its place: a release landing before or after it renumbers it.
_PHASE9_NEEDLE = "OpencodeRunner"


def _semver(text):
    return tuple(int(x) for x in text.split("."))


def _phase9_entry(text):
    """(version, body, the version of the entry right below it) of the
    CHANGELOG entry whose body names OpencodeRunner."""
    heads = list(re.finditer(r"^## (\d+\.\d+\.\d+) - .*$", text, re.M))
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[head.end():end]
        if _PHASE9_NEEDLE in body:
            below = heads[i + 1].group(1) if i + 1 < len(heads) else None
            return head.group(1), body, below
    raise AssertionError("no CHANGELOG entry names %s" % _PHASE9_NEEDLE)


def _section(text, heading):
    start = text.index(heading)
    level = heading.split(" ", 1)[0]
    nxt = re.search(r"^%s " % re.escape(level), text[start + len(heading):], re.M)
    return text[start:start + len(heading) + nxt.start()] if nxt else text[start:]


class TestPhase9Exit(unittest.TestCase):
    def read(self, rel):
        return (_REPO / rel).read_text()

    def has(self, needle, text, where):
        self.assertIn(needle, text, "%s does not say %r" % (where, needle))

    def test_the_changelog_entry_is_one_minor_above_the_last_and_says_what_shipped(self):
        version, entry, below = _phase9_entry(self.read("CHANGELOG.md"))
        self.assertGreaterEqual(_semver(_VERSION), _semver(version))
        top, last = _semver(version), _semver(below)
        self.assertEqual(top, (last[0], last[1] + 1, 0),
                         "%s is not one MINOR above %s" % (version, below))
        for needle in ('runner = "opencode"', 'kind = "opencode"', "plugins/opencode/cousin-policy.js",
                       "cousin-account login <name> --provider <id>", "--key-file",
                       "compose.opencode.yml", "--target opencode", "never carries Claude subscription",
                       "docs/reference/runners.md", "cousin_lib.runner.contract_table",
                       "21/21", "nothing DECLARED", "[agent.sessions]", "RUNNER_KINDS"):
            self.has(needle, entry, "the phase 9 CHANGELOG entry")

    def test_the_master_plan_marks_phase_9_done_at_this_version(self):
        text = self.read(_PLAN)
        row = next(line for line in text.splitlines()
                   if line.startswith("| 9 | OpencodeRunner and plugins |"))
        self.has("**DONE**", row, "the phase 9 row")
        self.has("v%s," % _phase9_entry(self.read("CHANGELOG.md"))[0], row, "the phase 9 row")
        self.assertRegex(row, r"v[0-9.]+, [0-9]+ tests")
        phase9 = _section(text, "## Phase 9")
        criteria = phase9[phase9.index("**Exit criteria**"):]
        self.assertEqual(criteria.count("- [x]"), 1, criteria)
        self.assertNotIn("- [ ]", criteria)
        self.has("compose.opencode.yml", phase9, "the phase 9 tasks")
        self.has("not a profile", phase9, "the phase 9 tasks")
        self.has("the policy veto", phase9, "the phase 9 tasks")

    def test_the_master_plan_locks_the_phase_9_interfaces_as_the_code_has_them(self):
        from cousin_lib import accounts
        from cousin_lib.runner import contract_table, opencode, opencode_guard, opencode_http
        section = _section(self.read(_PLAN), "## Interfaces locked across phases")
        for needle in ('RUNNER_KINDS = ("sdk", "fake", "opencode")',
                       'KINDS = ("claude-login", "claude-token", "anthropic-key", "opencode")',
                       "def render(registry, version, *, tool_name=None)",
                       "# cousin_lib/runner/opencode.py (P9)", "# cousin_lib/runner/opencode_http.py (P9)",
                       "# cousin_lib/runner/opencode_guard.py (P9, R13)",
                       "# cousin_lib/runner/contract_table.py (P9, R19)", "plugin_items()"):
            self.has(needle, section, "the locked interfaces")
        # every locked P9 callable is named with its real parameters, in order
        for fn in (opencode.OpencodeRunner.__init__, opencode.render_config, opencode.server_env,
                   opencode.opencode_bin, opencode.render_policy, opencode.seed_plugin_dependency,
                   opencode_http.OpencodeServer.__init__, opencode_http.OpencodeClient.__init__,
                   opencode_http.OpencodeClient.prompt_async, opencode_http.EventReader.__init__,
                   opencode_guard.refuse_bridge, accounts.check_lane, accounts.login_action,
                   accounts.read_api_key, accounts.store_api_key, accounts.opencode_login_flow,
                   contract_table.splice):
            with self.subTest(fn=fn.__qualname__):
                name = fn.__name__
                params = [p for p in inspect.signature(fn).parameters if p != "self"]
                start = section.index("def %s(" % name) if name != "__init__" else \
                    section.index("class %s" % fn.__qualname__.split(".")[0])
                window = section[start:start + 600]
                at = window.index("(")
                for param in params:                  # each after the one before it
                    found = re.compile(r"\b%s\b" % re.escape(param)).search(window, at)
                    self.assertIsNotNone(found, "%s: %s missing or out of order in %r"
                                         % (fn.__qualname__, param, window[:300]))
                    at = found.end()

    def test_the_docs_say_how_to_run_the_lane_and_what_it_does_not_do(self):
        ops = _section(self.read("docs/operations.md"), "## The container")
        for needle in ("compose.opencode.yml", "never carries a Claude subscription",
                       "reference/runners.md#known-gaps"):
            self.has(needle, ops, "operations.md, The container")
        design = _section(self.read("docs/design/agent-loop-runner.md"),
                          "### The compatibility layer")
        for needle in ("remote MCP server the runner itself serves", "veto only", "21/21"):
            self.has(needle, design, "the design's compatibility layer")
        sessions = _section(self.read("docs/configuration.md"), "### [agent.sessions]")
        self.has('`runner = "opencode"`', sessions, "configuration.md, [agent.sessions]")
        gaps = _section(self.read("docs/reference/runners.md"), "## Known gaps")
        for needle in ("apply_patch", "usage records", "--check-auth --validate",
                       "Image attachments", "cousin-spawn --runner opencode"):
            self.has(needle, gaps, "runners.md, Known gaps")


if __name__ == "__main__":
    unittest.main()
