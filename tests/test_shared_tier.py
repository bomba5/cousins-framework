"""The shared memory tier: docs/memory.md as executable spec.

Every promise the doctrine page makes is a test here: one entry path,
no silent overwrites, reviewed promotion, and above all the boundary -
the proposing side and the promoting side are never the same
principal, and no configuration can express otherwise.
"""
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib.shared_tier import (
    NameRefused,
    PromoteRefused,
    diff_proposal,
    list_shared,
    plan_bulk_propose,
    promote,
    propose,
    read_shared,
    reject,
)


class TierCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        patcher = mock.patch.dict(os.environ,
                                  {"FRAMEWORK_ROOT": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _reviewers(self, names):
        (self.root / "config" / "shared-reviewers.json").write_text(
            json.dumps({"reviewers": names}))

    def _register_cousin(self, slug, name):
        home = self.root / "cousins" / slug
        home.mkdir(parents=True, exist_ok=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "%s"\nname = "%s"\n[chat]\nport = 8100\n'
            % (slug, name))

    def _audit_entries(self):
        path = self.root / "shared" / "audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text().splitlines()]


class TestProposeAndPromote(TierCase):
    def test_propose_lands_in_proposed_never_canonical(self):
        propose("norms.md", "be kind\n", slug="wren")
        self.assertTrue(
            (self.root / "shared" / "proposed" / "wren__norms.md")
            .exists())
        self.assertFalse((self.root / "shared" / "norms.md").exists())

    def test_second_proposal_needs_force_and_both_are_audited(self):
        propose("norms.md", "v1\n", slug="wren")
        with self.assertRaises(FileExistsError):
            propose("norms.md", "v2\n", slug="wren")
        propose("norms.md", "v2\n", slug="wren", force=True)
        kinds = [e["kind"] for e in self._audit_entries()]
        self.assertEqual(kinds, ["propose", "propose-overwrite"])

    def test_promotion_moves_to_canonical_and_audits_the_reviewer(self):
        self._register_cousin("wren", "Wren")
        self._reviewers(["Sam"])
        propose("norms.md", "be kind\n", slug="wren")
        promote("norms.md", proposer="wren", by="Sam")
        self.assertEqual(
            (self.root / "shared" / "norms.md").read_text(), "be kind\n")
        self.assertFalse(
            (self.root / "shared" / "proposed" / "wren__norms.md")
            .exists())
        last = self._audit_entries()[-1]
        self.assertEqual((last["kind"], last["actor"]),
                         ("promote", "Sam"))

    def test_reject_removes_the_proposal_with_a_reason_on_record(self):
        self._register_cousin("wren", "Wren")
        self._reviewers(["Sam"])
        propose("norms.md", "questionable\n", slug="wren")
        reject("norms.md", proposer="wren", by="Sam", reason="too vague")
        self.assertFalse(
            (self.root / "shared" / "proposed" / "wren__norms.md")
            .exists())
        last = self._audit_entries()[-1]
        self.assertEqual(last["kind"], "reject")
        self.assertEqual(last["reason"], "too vague")


class TestTheBoundary(TierCase):
    def test_display_name_cannot_launder_self_approval(self):
        # The hole: proposer is a slug, by is free text, and a cousin
        # whose display name differs from its slug is the NORMAL case.
        # Both sides must resolve to the same principal - the slug -
        # before either check runs.
        self._register_cousin("wren", "Sam")
        self._reviewers(["Sam"])
        propose("norms.md", "mine\n", slug="wren")
        with self.assertRaises(PromoteRefused) as ctx:
            promote("norms.md", proposer="wren", by="Sam")
        self.assertIn("own proposal", str(ctx.exception))

    def test_reviewer_matching_is_by_principal_not_raw_string(self):
        # One normalisation for one identity field: 'priya' in config
        # must accept by='Priya' - the allowlist and the self-check
        # read from the same resolved value.
        self._register_cousin("wren", "Wren")
        self._reviewers(["priya"])
        propose("norms.md", "x\n", slug="wren")
        promote("norms.md", proposer="wren", by="Priya")
        self.assertTrue((self.root / "shared" / "norms.md").exists())

    def test_self_approval_is_refused_even_when_config_allows_it(self):
        # The allowlist must not be able to express proposer==approver:
        # a boundary that config can switch off is not a boundary.
        self._register_cousin("wren", "Wren")
        self._reviewers(["Wren", "Sam"])
        propose("norms.md", "mine\n", slug="wren")
        with self.assertRaises(PromoteRefused) as ctx:
            promote("norms.md", proposer="wren", by="Wren")
        self.assertIn("own proposal", str(ctx.exception))
        self.assertFalse((self.root / "shared" / "norms.md").exists())

    def test_unlisted_reviewer_is_refused(self):
        self._register_cousin("wren", "Wren")
        self._reviewers(["Sam"])
        propose("norms.md", "x\n", slug="wren")
        with self.assertRaises(PromoteRefused):
            promote("norms.md", proposer="wren", by="Mallory")

    def test_absent_registry_refuses_rather_than_degrading(self):
        # With no cousins/ directory, name resolution is impossible and
        # _principal would silently fall back to the raw-string
        # comparison the resolution exists to replace - a security
        # check degrading to its weaker predecessor when its data
        # source is absent. Absence here is not a no-op: it changes
        # who can approve what. Refuse.
        self._reviewers(["Sam"])
        propose("norms.md", "x\n", slug="wren")
        # TierCase never created cousins/ - the registry is absent.
        with self.assertRaises(PromoteRefused) as ctx:
            promote("norms.md", proposer="wren", by="Sam")
        self.assertIn("registry", str(ctx.exception))

    def test_no_reviewer_config_refuses_with_remediation(self):
        # Not "anyone but the proposer": an implicit reviewer set is
        # the self-approval hole one step removed.
        self._register_cousin("wren", "Wren")
        propose("norms.md", "x\n", slug="wren")
        with self.assertRaises(PromoteRefused) as ctx:
            promote("norms.md", proposer="wren", by="Sam")
        self.assertIn("shared-reviewers.json", str(ctx.exception))


class TestPathShapedNames(TierCase):
    """A slug or file name is one bare entry name. A "../" slug would
    land the proposal outside shared/proposed/; a "../" file would read
    another cousin's home. Both are refused before any path is built."""

    BAD = ("../wren", "..", ".hidden", "a/b", "a\\b", "")

    def test_propose_refuses_a_path_shaped_slug_and_writes_nothing(self):
        for slug in self.BAD:
            with self.subTest(slug=slug):
                with self.assertRaises(NameRefused) as ctx:
                    propose("norms.md", "x\n", slug=slug)
                self.assertIn("slug", str(ctx.exception))
                self.assertIn("bare name", str(ctx.exception))
        self.assertFalse((self.root / "norms.md").exists())
        self.assertFalse((self.root / "shared" / "proposed").exists())
        self.assertEqual(self._audit_entries(), [])

    def test_propose_refuses_a_path_shaped_file(self):
        for file in ("../norms.md", "sub/norms.md", ".norms.md"):
            with self.subTest(file=file):
                with self.assertRaises(NameRefused):
                    propose(file, "x\n", slug="wren")

    def test_read_refuses_a_file_outside_shared(self):
        other = self.root / "cousins" / "sam"
        other.mkdir(parents=True)
        (other / "STATUS.md").write_text("private\n")
        (self.root / "shared").mkdir()
        for file in ("../cousins/sam/STATUS.md", "..\\x.md", ".x.md"):
            with self.subTest(file=file):
                with self.assertRaises(NameRefused) as ctx:
                    read_shared(file)
                self.assertIn("file", str(ctx.exception))

    def test_diff_refuses_a_path_shaped_file_or_slug(self):
        with self.assertRaises(NameRefused):
            diff_proposal("../cousins/sam/STATUS.md", "wren")
        with self.assertRaises(NameRefused):
            diff_proposal("norms.md", "../wren")

    def test_promote_and_reject_refuse_path_shaped_names(self):
        self._register_cousin("wren", "Wren")
        self._reviewers(["Sam"])
        with self.assertRaises(NameRefused):
            promote("../x.md", proposer="wren", by="Sam")
        with self.assertRaises(NameRefused):
            reject("norms.md", proposer="../wren", by="Sam")

    def test_a_bare_name_still_passes(self):
        propose("norms.md", "x\n", slug="wren")
        self.assertIn("x", diff_proposal("norms.md", "wren"))


class TestBulkPropose(TierCase):
    def _home(self, scope=None, files=()):
        home = self.root / "cousins" / "wren"
        (home / "memory").mkdir(parents=True)
        toml = '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n'
        if scope:
            toml += '[memory]\nscope = "%s"\n' % scope
        (home / "cousin.toml").write_text(toml)
        for name, body in files:
            (home / "memory" / name).write_text(body)
        return home

    def test_private_scope_is_ineligible_deny_on_uncertainty(self):
        home = self._home(scope=None, files=[
            ("project_thing.md", "shareable: true\n\nfact\n")])
        plan = plan_bulk_propose(home, "wren")
        self.assertFalse(plan["eligible"])

    def test_only_marked_project_reference_files_enter_the_plan(self):
        home = self._home(scope="shared", files=[
            ("project_alpha.md", "shareable: true\n\nalpha fact\n"),
            ("project_beta.md", "beta fact, unmarked\n"),
            ("feedback_ops.md", "shareable: true\n\nnever this\n"),
            ("reference_api.md", "shareable: true\n\napi fact\n"),
        ])
        plan = plan_bulk_propose(home, "wren")
        names = {p["fname"] for p in plan["propose"]}
        self.assertEqual(names, {"project_alpha.md", "reference_api.md"})
        skipped = {f for f, _ in plan["skipped"]}
        self.assertIn("project_beta.md", skipped)


class TestCli(TierCase):
    def _main(self, argv, stdin_text=None):
        import contextlib
        import io

        from cousin_lib.shared_tier import shared_main
        out, err = io.StringIO(), io.StringIO()
        stdin = io.StringIO(stdin_text or "")
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", stdin):
            rc = shared_main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_propose_promote_read_roundtrip(self):
        self._register_cousin("wren", "Wren")
        self._reviewers(["Sam"])
        rc, _, _ = self._main(
            ["propose", "norms.md", "--slug", "wren"],
            stdin_text="be kind\n")
        self.assertEqual(rc, 0)
        rc, out, _ = self._main(["list"])
        self.assertIn("wren__norms.md", out)
        rc, _, _ = self._main(
            ["promote", "norms.md", "--proposer", "wren", "--by", "Sam"])
        self.assertEqual(rc, 0)
        rc, out, _ = self._main(["read", "norms.md"])
        self.assertEqual(out, "be kind\n")

    def test_self_approval_refusal_reaches_the_exit_code(self):
        self._register_cousin("wren", "Wren")
        self._reviewers(["Wren"])
        self._main(["propose", "norms.md", "--slug", "wren"],
                   stdin_text="x\n")
        rc, _, err = self._main(
            ["promote", "norms.md", "--proposer", "wren", "--by", "Wren"])
        self.assertEqual(rc, 3)
        self.assertIn("own proposal", err)


    def test_a_path_shaped_name_is_a_usage_error_on_the_cli(self):
        rc, _, err = self._main(["propose", "norms.md", "--slug", "../x"],
                                stdin_text="x\n")
        self.assertEqual(rc, 2)
        self.assertIn("bare name", err)
        self.assertFalse((self.root / "x__norms.md").exists())
        for argv in (["read", "../cousins/sam/STATUS.md"],
                     ["diff", "../x.md", "--slug", "wren"]):
            rc, _, err = self._main(argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn("refused", err)


class TestMemoryCliWiring(TierCase):
    def test_propose_shared_is_dry_run_by_default(self):
        import contextlib
        import io

        from cousin_lib.memory import memory_main
        home = self.root / "cousins" / "wren"
        (home / "memory").mkdir(parents=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\n[chat]\nport = 8100\n'
            '[memory]\nscope = "shared"\n')
        (home / "memory" / "project_alpha.md").write_text(
            "shareable: true\n\nalpha\n")
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"COUSIN_HOME": str(home)}), \
                contextlib.redirect_stdout(out):
            rc = memory_main(["propose-shared"])
        self.assertEqual(rc, 0)
        self.assertIn("dry-run", out.getvalue())
        self.assertFalse((self.root / "shared" / "proposed").exists())
        with mock.patch.dict(os.environ, {"COUSIN_HOME": str(home)}), \
                contextlib.redirect_stdout(io.StringIO()):
            memory_main(["propose-shared", "--commit"])
        self.assertTrue(
            (self.root / "shared" / "proposed"
             / "wren__project_alpha.md").exists())


if __name__ == "__main__":
    unittest.main()
