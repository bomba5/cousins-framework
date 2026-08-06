"""The shared memory tier: docs/memory-tiers.md as executable spec.

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
    PromoteRefused,
    list_shared,
    plan_bulk_propose,
    promote,
    propose,
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
    def test_self_approval_is_refused_even_when_config_allows_it(self):
        # The allowlist must not be able to express proposer==approver:
        # a boundary that config can switch off is not a boundary.
        self._reviewers(["Wren", "Sam"])
        propose("norms.md", "mine\n", slug="wren")
        with self.assertRaises(PromoteRefused) as ctx:
            promote("norms.md", proposer="wren", by="Wren")
        self.assertIn("own proposal", str(ctx.exception))
        self.assertFalse((self.root / "shared" / "norms.md").exists())

    def test_unlisted_reviewer_is_refused(self):
        self._reviewers(["Sam"])
        propose("norms.md", "x\n", slug="wren")
        with self.assertRaises(PromoteRefused):
            promote("norms.md", proposer="wren", by="Mallory")

    def test_no_reviewer_config_refuses_with_remediation(self):
        # Not "anyone but the proposer": an implicit reviewer set is
        # the self-approval hole one step removed.
        propose("norms.md", "x\n", slug="wren")
        with self.assertRaises(PromoteRefused) as ctx:
            promote("norms.md", proposer="wren", by="Sam")
        self.assertIn("shared-reviewers.json", str(ctx.exception))


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


if __name__ == "__main__":
    unittest.main()
