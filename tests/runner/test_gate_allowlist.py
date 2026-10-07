"""The gate's allow list, one row per surface and one test per row and
actor, driven through the real PreToolUse policy callback.

The actors:

- P, the primary session: a payload with no `agent_id`;
- S, a subagent or background pass: a payload carrying `agent_id`;
- F, a framework writer: `perimeter.assert_writable`, with no root.

TABLE is the test data. A row names its paths (relative to the
framework root, or absolute when the row is about a path outside it)
and the decision each actor gets for a WRITE. A write is tried with
every tool in WRITE_TOOLS: the path tools, and a Bash redirect, a `cp`
destination, `sed -i` and `tee`. A row whose decision changes changes
here, in one place, and the commit that changes it says why.

Three more checks keep the table honest:

- the mirror rule: every path in the table is read by both P and S with
  every tool in READ_TOOLS, and every one of those reads passes, so a
  later tightening cannot quietly refuse a read;
- coverage: every reason `perimeter.protected_reason` can give is
  produced by at least one row, so a new protected shape without a row
  here fails;
- the known gaps: Bash writes the scan does not see, pinned as allows,
  so the fix that closes one flips its test on purpose.
"""
import ast
import asyncio
import contextlib
import inspect
import io
import json
import os
import pathlib
import re
import tempfile
import textwrap
from unittest import mock

from cousin_lib import perimeter
from cousin_lib.runner import hooks, policy
from cousin_lib.runner import main as runner_main
from cousin_lib.runner import status
from cousin_lib.runner.fake import FakeRunner
from cousin_lib.runner.state import StateMachine
from cousin_lib.runner.stream import EventStream
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home

SLUG = "wren"           # the cousin whose session this is
OTHER = "sam"           # another cousin on the same install

ALLOW, REFUSE = "allow", "refuse"
NA = None               # the actor never writes this surface

# (row, paths, P write, S write, F write). Paths are relative to the
# framework root unless absolute.
TABLE = (
    ("A1", ("cousins/wren/memory/raw/2026-10-03.jsonl",
            "cousins/wren/memory/distilled/decisions.md",
            "cousins/wren/notes/plan.md",
            "cousins/wren/scratch/draft.md",
            "cousins/wren/STATUS.md",
            "cousins/wren/MEMORY.md",
            "cousins/wren/data/dreams/2026-10-03.md"), ALLOW, ALLOW, ALLOW),
    ("A2", ("cousins/wren/.self-portrait-candidate.md",), ALLOW, ALLOW, ALLOW),
    ("A3", ("cousins/wren/self-portrait.md",), ALLOW, REFUSE, REFUSE),
    ("A4", ("config/law.md",), ALLOW, REFUSE, REFUSE),
    ("A5", ("shared/reference_house-style.md",), ALLOW, REFUSE, REFUSE),
    ("A6", ("shared/proposed/wren__reference_h.md",), ALLOW, ALLOW, ALLOW),
    # G2: a subagent proposes only under its own name
    ("A7", ("shared/proposed/sam__reference_h.md",
            "shared/proposed/reference_h.md",
            "shared/proposed/nested/deep.md"), ALLOW, REFUSE, ALLOW),
    # G4: the tier's record is the framework's to append (shared_tier._audit)
    ("A8", ("shared/audit.jsonl",), ALLOW, REFUSE, ALLOW),
    # G3: what the next start reads is the operator's to change
    ("A9", ("cousins/wren/policy.toml",
            "cousins/wren/cousin.toml",
            "cousins/wren/.mcp.json",
            "cousins/wren/mcp-registry.toml",
            "cousins/wren/chat-hooks.json",
            "cousins/wren/.claude/settings.json",
            "cousins/wren/.claude/settings.local.json",
            "cousins/wren/.claude"), ALLOW, REFUSE, NA),
    # G1: another cousin's home is that cousin's
    ("A10", ("cousins/sam/memory/raw/2026-10-03.jsonl",
             "cousins/sam/STATUS.md",
             "cousins/sam/notes/plan.md",
             "cousins/sam/cousin.toml",
             "cousins/sam/policy.toml",
             "cousins/sam",
             "cousins/wren-b/STATUS.md"), ALLOW, REFUSE, ALLOW),
    ("A11", ("cousins/sam/self-portrait.md",), ALLOW, REFUSE, REFUSE),
    ("A12", ("/srv/other-repo/notes.md",
             "/srv/other-repo/shared/notes.md",
             "/srv/other-repo/config/law.md",
             "/srv/other-repo/cousins/sam/STATUS.md"), ALLOW, ALLOW, NA),
    ("A13", ("templates/shared/reference_rules.md",
             "templates/law.md"), ALLOW, ALLOW, NA),
)

# How each tool writes `p`: (label, tool name, tool_input).
WRITE_TOOLS = (
    ("Write", "Write", lambda p: {"file_path": p, "content": "x"}),
    ("Edit", "Edit", lambda p: {"file_path": p, "old_string": "a", "new_string": "b"}),
    ("MultiEdit", "MultiEdit", lambda p: {"file_path": p, "edits": [
        {"old_string": "a", "new_string": "b"}]}),
    ("redirect", "Bash", lambda p: {"command": "echo x > %s" % p}),
    ("cp destination", "Bash", lambda p: {"command": "cp /tmp/x %s" % p}),
    ("sed -i", "Bash", lambda p: {"command": "sed -i s/a/b/ %s" % p}),
    ("tee", "Bash", lambda p: {"command": "echo x | tee %s" % p}),
)

# How each tool reads `p`.
READ_TOOLS = (
    ("Read", "Read", lambda p: {"file_path": p}),
    ("Grep", "Grep", lambda p: {"pattern": "rule", "path": p}),
    ("Glob", "Glob", lambda p: {"pattern": os.path.basename(p),
                                "path": os.path.dirname(p)}),
    ("cat", "Bash", lambda p: {"command": "cat %s" % p}),
    ("grep -n", "Bash", lambda p: {"command": "grep -n rule %s" % p}),
    ("sed -n", "Bash", lambda p: {"command": "sed -n 1,20p %s" % p}),
    ("head", "Bash", lambda p: {"command": "head -n 5 %s" % p}),
    ("git diff --", "Bash", lambda p: {"command": "git diff -- %s" % p}),
    ("cp source", "Bash", lambda p: {"command": "cp %s /tmp/x" % p}),
)


class GateCase(HermeticCase):
    """A root with one cousin's home in it, and the real callbacks."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.R = pathlib.Path(tmp.name)
        self.home = self.R / "cousins" / SLUG
        (self.home / "data").mkdir(parents=True)
        self.stream = EventStream(self.home, SLUG)
        self.cbs = hooks.callbacks(self.home, slug=SLUG, root=self.R,
                                   machine=StateMachine(), stream=self.stream,
                                   policy=policy.Policy())

    def path(self, rel):
        return rel if rel.startswith("/") else str(self.R / rel)

    def decide(self, actor, tool, tool_input, cwd=None):
        """`(decision, reason)` for one call by `actor` ("P" or "S")."""
        payload = {"session_id": "s", "transcript_path": "/dev/null",
                   "cwd": cwd or str(self.home), "hook_event_name": "PreToolUse",
                   "tool_use_id": "t", "tool_name": tool, "tool_input": tool_input}
        if actor == "S":
            payload["agent_id"] = "sub-1"
        out = asyncio.run(self.cbs["PreToolUse:policy"](payload, "t", {}))
        if not out:
            return ALLOW, ""
        spec = out["hookSpecificOutput"]
        self.assertEqual(spec["permissionDecision"], "deny")
        return REFUSE, spec["permissionDecisionReason"]


def _write_test(row, paths, actor, expected):
    def test(self):
        for rel in paths:
            path = self.path(rel)
            for label, tool, make in WRITE_TOOLS:
                with self.subTest(path=rel, tool=label):
                    decision, reason = self.decide(actor, tool, make(path))
                    self.assertEqual(decision, expected, reason)
                    if decision == REFUSE:
                        # a refusal names the path it refused
                        self.assertIn("memory perimeter", reason)
                        self.assertIn(path, reason)
    test.__doc__ = "%s: a write by %s: %s" % (row, actor, expected)
    return test


def _framework_test(row, paths, expected):
    def test(self):
        for rel in paths:
            path = self.path(rel)
            with self.subTest(path=rel):
                if expected == REFUSE:
                    with self.assertRaises(perimeter.PerimeterRefused):
                        perimeter.assert_writable(path, writer="test")
                else:
                    self.assertEqual(perimeter.assert_writable(path, writer="test"), path)
    test.__doc__ = "%s: a framework writer is %s" % (row, expected)
    return test


def _read_test(row, paths, actor):
    def test(self):
        for rel in paths:
            path = self.path(rel)
            for label, tool, make in READ_TOOLS:
                with self.subTest(path=rel, tool=label):
                    decision, reason = self.decide(actor, tool, make(path))
                    self.assertEqual(decision, ALLOW, reason)
    test.__doc__ = "%s: every read by %s passes (the mirror rule)" % (row, actor)
    return test


class TestTheAllowList(GateCase):
    """One test per row and actor, generated from TABLE below."""


class TestTheMirrorRule(GateCase):
    """Reads always pass, for every actor, on every path in the table:
    the refused paths above all. Generated from TABLE below."""


for _row, _paths, _p, _s, _f in TABLE:
    _name = _row.lower()
    setattr(TestTheAllowList, "test_%s_primary_write" % _name, _write_test(_row, _paths, "P", _p))
    setattr(TestTheAllowList, "test_%s_subagent_write" % _name, _write_test(_row, _paths, "S", _s))
    if _f is not NA:
        setattr(TestTheAllowList, "test_%s_framework_write" % _name,
                _framework_test(_row, _paths, _f))
    setattr(TestTheMirrorRule, "test_%s_primary_reads" % _name, _read_test(_row, _paths, "P"))
    setattr(TestTheMirrorRule, "test_%s_subagent_reads" % _name, _read_test(_row, _paths, "S"))


class TestAnotherCousinsHome(GateCase):
    """Row A10 spelled out, for docs/reference/rules-inventory.md (law
    12): a background pass cannot put text in another cousin's memory,
    where that cousin's boot and recall would read it."""

    def test_a_subagent_cannot_write_into_another_cousins_memory(self):
        for rel in ("cousins/sam/MEMORY.md", "cousins/sam/memory/distilled/decisions.md",
                    "cousins/sam/memory/raw/2026-10-03.jsonl"):
            with self.subTest(path=rel):
                decision, reason = self.decide("S", "Write", {"file_path": self.path(rel)})
                self.assertEqual(decision, REFUSE)
                self.assertIn("sam's home", reason)
                self.assertIn("cousin-chat send sam", reason)
                # the primary session keeps today's behaviour
                self.assertEqual(self.decide("P", "Write", {"file_path": self.path(rel)})[0],
                                 ALLOW)


class TestOwnTools(GateCase):
    """A14: the cousin's own tools are not paths; the perimeter has
    nothing to say about them."""

    def test_the_cousin_tools_pass_for_both_actors(self):
        calls = (("mcp__cousin__memory", {"command": "remember", "fact": "x"}),
                 ("mcp__cousin__handoff", {"position": "p", "next_action": "n",
                                           "status": "s"}),
                 ("mcp__cousin__reply", {"text": "hi", "thread": "operator:ana"}))
        for actor in ("P", "S"):
            for tool, tool_input in calls:
                with self.subTest(actor=actor, tool=tool):
                    self.assertEqual(self.decide(actor, tool, tool_input)[0], ALLOW)


class TestCoverage(GateCase):
    """Every reason protected_reason can give is produced by a row.

    A reason is a module constant `_*_REASON` or a call to a module
    function `_*_reason(...)`: the first test holds protected_reason to
    that convention, the second matches every such producer against
    the reasons the table's S refusals actually got."""

    PRODUCER_CONST = re.compile(r"^_[A-Z0-9_]+_REASON$")
    PRODUCER_FUNC = re.compile(r"^_[a-z0-9_]+_reason$")

    def _returns(self, fn, seen=None):
        """Every returned expression of `fn`, following calls to the
        module's own helpers that are not producers."""
        seen = seen if seen is not None else set()
        if fn.__name__ in seen:
            return []
        seen.add(fn.__name__)
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        out = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Return):
                continue
            value = node.value
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) \
                    and not self.PRODUCER_FUNC.match(value.func.id) \
                    and inspect.isfunction(getattr(perimeter, value.func.id, None)):
                out.extend(self._returns(getattr(perimeter, value.func.id), seen))
            else:
                out.append(value)
        return out

    def test_protected_reason_returns_only_named_producers(self):
        for value in self._returns(perimeter.protected_reason):
            with self.subTest(line=ast.unparse(value) if value is not None else "None"):
                if value is None or (isinstance(value, ast.Constant) and value.value is None):
                    continue
                if isinstance(value, ast.Name):
                    self.assertRegex(value.id, self.PRODUCER_CONST)
                    continue
                self.assertIsInstance(value, ast.Call)
                self.assertRegex(value.func.id, self.PRODUCER_FUNC)

    def _producers(self):
        """name -> a regex matching every reason that producer gives."""
        out = {}
        for name, value in vars(perimeter).items():
            if self.PRODUCER_CONST.match(name) and isinstance(value, str):
                out[name] = re.compile("^%s$" % re.escape(value))
            elif self.PRODUCER_FUNC.match(name) and inspect.isfunction(value):
                marks = ["\x00%d\x00" % i
                         for i in range(len(inspect.signature(value).parameters))]
                text = re.escape(value(*marks))
                for mark in marks:
                    text = text.replace(re.escape(mark), ".*")
                out[name] = re.compile("^%s$" % text, re.S)
        return out

    def test_every_producer_is_named_by_a_row(self):
        producers = self._producers()
        self.assertTrue(producers)
        seen = {}
        for row, paths, _p, s, _f in TABLE:
            if s != REFUSE:
                continue
            for rel in paths:
                # what the gate asks for a subagent
                reason = perimeter.protected_reason(self.path(rel), root=str(self.R),
                                                    cwd=str(self.home), home=self.home,
                                                    slug=SLUG)
                hits = [n for n, rx in producers.items() if reason and rx.match(reason)]
                self.assertTrue(hits, "%s %s: %r matches no producer" % (row, rel, reason))
                for name in hits:
                    seen.setdefault(name, row)
        self.assertEqual(sorted(set(producers) - set(seen)), [],
                         "a protected shape with no row in TABLE")


class TestKnownGaps(GateCase):
    """Bash writes the scan does not see. The scan is best-effort by
    design (cousin_lib/perimeter.py, the module docstring: "It cannot see
    every Bash write"); these pin today's misses as allows, so the change
    that closes one flips its test on purpose rather than by accident."""

    def test_a_target_behind_a_variable_is_not_seen(self):
        command = "L=%s; cp /tmp/x $L" % self.path("config/law.md")
        self.assertEqual(self.decide("S", "Bash", {"command": command})[0], ALLOW)

    def test_a_glob_that_does_not_end_in_md_is_not_seen(self):
        command = "cp /tmp/x %s" % self.path("shared/reference_*")
        self.assertEqual(self.decide("S", "Bash", {"command": command})[0], ALLOW)

    def test_a_glob_that_ends_in_md_is_seen_by_its_text(self):
        # Not a gap: the literal `*.md` is a name directly under shared/,
        # so the shape matches before the shell would expand it.
        command = "cp /tmp/x %s" % self.path("shared/*.md")
        self.assertEqual(self.decide("S", "Bash", {"command": command})[0], REFUSE)

    def test_find_exec_is_not_seen(self):
        command = "find %s -name law.md -exec cp /tmp/x {} \\;" % self.R
        self.assertEqual(self.decide("S", "Bash", {"command": command})[0], ALLOW)

    def test_a_cd_then_a_relative_write_is_not_seen(self):
        for directory, name in (("shared", "a.md"), ("config", "law.md"),
                                ("cousins/sam", "STATUS.md")):
            command = "cd %s && echo x > %s" % (self.path(directory), name)
            with self.subTest(command=command):
                self.assertEqual(self.decide("S", "Bash", {"command": command})[0], ALLOW)


class TestTheGateKnowsWhoWrites(GateCase):
    """The rows that depend on the writer (its own home, another
    cousin's, its own proposals) need the cousin's own home and slug at
    the gate; the callbacks hand over theirs."""

    def test_the_callbacks_pass_their_home_and_slug_to_the_perimeter(self):
        with mock.patch.object(perimeter, "check_tool", return_value=None) as check:
            self.decide("S", "Write", {"file_path": self.path("cousins/wren/notes/a.md")})
        self.assertEqual(check.call_args.kwargs["slug"], SLUG)
        self.assertEqual(pathlib.Path(check.call_args.kwargs["home"]), self.home)
        self.assertEqual(pathlib.Path(check.call_args.kwargs["root"]), self.R)


class TestLanes(HermeticCase):
    """The gate runs on the sdk lane only. The opencode and tmux kinds
    say so at start, so the docs' claim and the running code agree."""

    def _start_as(self, kind):
        home = temp_home(self, runner="fake")
        stderr = io.StringIO()
        with mock.patch.object(FakeRunner, "kind", kind), \
                contextlib.redirect_stderr(stderr):
            rc = runner_main.runner_main(["--home", str(home), "--once"])
        self.assertEqual(rc, 0, stderr.getvalue())
        events = [json.loads(line) for line in
                  status.primary_stream(home).read_text().splitlines()]
        found = [e["payload"] for e in events if e["kind"] == "system"
                 and e["payload"].get("subtype") == "perimeter"]
        return stderr.getvalue(), found

    def test_an_ungated_kind_names_the_gap_at_start(self):
        for kind in ("opencode", "tmux"):
            with self.subTest(kind=kind):
                err, found = self._start_as(kind)
                self.assertIn("no perimeter on this lane", err)
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]["kind"], kind)
                self.assertIn("no perimeter on this lane", found[0]["line"])
                self.assertIn("the %s kind" % kind, found[0]["line"])

    def test_the_gated_lane_says_nothing(self):
        for kind in ("sdk", "fake"):
            with self.subTest(kind=kind):
                err, found = self._start_as(kind)
                self.assertNotIn("no perimeter", err)
                self.assertEqual(found, [])

    def test_every_kind_is_either_gated_or_named(self):
        # a new runner kind has to say which it is; `fake` is the tests'
        from cousin_lib.delivery import RUNNER_KINDS
        self.assertEqual(sorted(perimeter.UNGATED_KINDS),
                         sorted(k for k in RUNNER_KINDS if k not in ("sdk", "fake")))


class TestTheDocsTableMatchesTheGate(HermeticCase):
    """#249: docs/memory.md "Boundary or policy, at a glance" says, per
    kind of path, whether the gate refuses a subagent's write and whether
    the primary session is free. It must say what TABLE above enforces."""

    # the docs row's opening words -> the TABLE rows it summarises
    DOC_ROWS = {
        "Its own home": ("A1",),
        "Its own configuration": ("A9",),
        "Another cousin's home": ("A10",),
        "`config/law.md`": ("A4",),
        "A committed `self-portrait.md`": ("A3", "A11"),
        "A canonical `shared/*.md`": ("A5",),
        "A proposal in `shared/proposed/` not under its own name": ("A7",),
        "`shared/audit.jsonl`": ("A8",),
        "Anything outside the install": ("A12",),
    }

    def test_each_row_says_what_the_gate_does(self):
        root = pathlib.Path(__file__).resolve().parents[2]
        text = (root / "docs" / "memory.md").read_text()
        section = text[text.index("### Boundary or policy, at a glance"):]
        rows = {}
        for line in section.splitlines():
            if line.startswith("| ") and not line.startswith("| Path") and not line.startswith("|---"):
                cells = [c.strip() for c in line.strip("|").split("|")]
                rows[cells[0]] = cells
            elif rows and not line.startswith("|"):
                break
        table = {r[0]: r for r in TABLE}
        self.assertEqual(len(rows), len(self.DOC_ROWS))
        # a TABLE row the docs leave out must be one a subagent may write:
        # a new refusal with no docs row fails here
        covered = {rid for ids in self.DOC_ROWS.values() for rid in ids}
        for rid, _paths, _p, s_write, _f in TABLE:
            if rid not in covered:
                self.assertEqual(s_write, ALLOW, "%s is refused but has no row in the docs table" % rid)
        for label, ids in self.DOC_ROWS.items():
            cells = next(c for k, c in rows.items() if k.startswith(label))
            primary, sub_tool = cells[2], cells[3]
            for rid in ids:
                _, _paths, p_write, s_write, _f = table[rid]
                self.assertEqual(p_write, ALLOW, rid)
                self.assertTrue(primary.startswith("Policy"), (label, primary))
                self.assertEqual(sub_tool == "Refused", s_write == REFUSE, (label, rid, sub_tool))
            # the columns TABLE does not drive: reads are never stopped, a
            # subagent's Bash is best-effort wherever its tools are refused,
            # and opencode/tmux have no gate at all
            self.assertEqual(cells[1], "Policy", label)
            self.assertEqual(cells[4], "Best-effort" if sub_tool == "Refused" else "Policy", label)
            self.assertEqual(cells[5], "Policy", label)
