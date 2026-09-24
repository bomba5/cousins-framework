"""[agent.sessions] and the side session's digest (phase 8): which thread
kinds get a session of their own, and what that session is told first."""
import os
import unittest
from unittest import mock

from cousin_lib.runner import prompt, sessions
from cousin_lib.runner.base import RunnerError
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home


def _home(case, table=""):
    home = temp_home(case)
    if table:
        with open(home / "cousin.toml", "a") as fh:
            fh.write("\n[agent.sessions]\n" + table)
    return home


class TestLoadMap(HermeticCase):
    def test_no_table_is_primary_only(self):
        home = _home(self)
        self.assertEqual(set(sessions.load_map(home).values()), {"primary"})
        self.assertEqual(sessions.side_kinds(home), ())

    def test_a_kind_mapped_own_gets_a_side_session(self):
        home = _home(self, 'peer = "own"\nmeeting = "primary"\nperson = "own"\n')
        self.assertEqual(sessions.side_kinds(home), ("person", "peer"))
        self.assertEqual(sessions.load_map(home)["meeting"], "primary")

    def test_an_unknown_kind_is_refused_by_name(self):
        home = _home(self, 'peers = "own"\n')
        with self.assertRaises(sessions.SessionsError) as ctx:
            sessions.load_map(home)
        self.assertIn("'peers' is not a thread kind", str(ctx.exception))

    def test_a_value_other_than_primary_or_own_is_refused(self):
        home = _home(self, 'peer = "side"\n')
        with self.assertRaisesRegex(sessions.SessionsError, "peer must be 'primary' or 'own'"):
            sessions.load_map(home)

    def test_operator_and_system_are_always_the_primarys(self):
        for kind in ("operator", "system"):
            with self.subTest(kind=kind):
                home = _home(self, '%s = "own"\n' % kind)
                with self.assertRaisesRegex(sessions.SessionsError, "belong to the primary"):
                    sessions.side_kinds(home)

    def test_a_non_table_is_refused(self):
        home = temp_home(self)
        with open(home / "cousin.toml", "a") as fh:
            fh.write('sessions = "own"\n')      # lands in [agent]: a string, not a table
        with self.assertRaisesRegex(sessions.SessionsError, "must be a table"):
            sessions.load_map(home)

    def test_the_error_is_a_runner_error_so_the_runner_exits_2(self):
        self.assertTrue(issubclass(sessions.SessionsError, RunnerError))


class TestSideDigest(HermeticCase):
    NOW = 1_900_000_000.0

    def setUp(self):
        super().setUp()
        self.home = temp_home(self)
        self.root = self.home.parent.parent
        os.environ["FRAMEWORK_ROOT"] = str(self.root / "no-such-install")   # must not be read

    def test_it_names_the_primarys_state_kinds_and_since(self):
        text = sessions.side_digest(self.home, root=self.root, slug="wren", kind="peer",
                                    primary={"state": "running", "thread_kinds": ["operator"],
                                             "since": self.NOW - 12 * 60}, now=self.NOW)
        self.assertTrue(text.startswith("SIDE SESSION: peer threads of wren\n"))
        self.assertIn("Primary session: running, in a turn on operator threads since", text)
        self.assertIn("(12 min)", text)
        self.assertIn("Do not call handoff", text)

    def test_an_idle_primary_and_one_that_did_not_say(self):
        idle = sessions.side_digest(self.home, root=self.root, slug="wren", kind="peer",
                                    primary={"state": "idle", "thread_kinds": [], "since": None})
        self.assertIn("Primary session: idle\n", idle)
        unknown = sessions.side_digest(self.home, root=self.root, slug="wren", kind="peer")
        self.assertIn("Primary session: not reported\n", unknown)

    def test_it_carries_the_cousins_own_activity_note(self):
        (self.home / "data" / "last-activity.txt").write_text(
            "2030-01-01T10:00:00: reconciling the ledger\n")
        text = sessions.side_digest(self.home, root=self.root, slug="wren", kind="peer")
        self.assertIn("Last recorded activity: 2030-01-01T10:00:00: reconciling the ledger", text)
        (self.home / "data" / "last-activity.txt").unlink()
        text = sessions.side_digest(self.home, root=self.root, slug="wren", kind="peer")
        self.assertIn("Last recorded activity: none recorded", text)

    def test_it_ends_with_the_generations_state_digest(self):
        (self.home / "STATUS.md").write_text("# Status - Wren\n\n## Open loops\n\n- [ ] ledger\n")
        text = sessions.side_digest(self.home, root=self.root, slug="wren", kind="peer")
        self.assertIn("STATE DIGEST FOR COUSIN: wren", text)
        self.assertIn("- [ ] ledger", text)

    def test_a_digest_that_cannot_be_built_is_said_not_raised(self):
        with mock.patch.object(prompt, "state_digest", side_effect=OSError("disk gone")):
            text = sessions.side_digest(self.home, root=self.root, slug="wren", kind="peer")
        self.assertIn("STATE DIGEST unavailable: OSError: disk gone", text)
        self.assertTrue(text.startswith("SIDE SESSION: peer threads of wren"))


if __name__ == "__main__":
    unittest.main()
