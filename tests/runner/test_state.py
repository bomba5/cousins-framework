"""Legal transitions only; every change is observable."""
import unittest

from cousin_lib.runner import state as st
from tests._hermetic import HermeticCase


class TestStateMachine(HermeticCase):
    def test_starts_idle_and_runs_a_turn(self):
        m = st.StateMachine()
        self.assertEqual(m.state, "idle")
        m.to("running"); m.to("idle")
        self.assertEqual(m.state, "idle")

    def test_illegal_transition_raises_and_keeps_state(self):
        m = st.StateMachine()
        with self.assertRaises(st.IllegalTransition):
            m.to("waiting_permission")
        self.assertEqual(m.state, "idle")

    def test_errored_is_reachable_from_every_non_terminal_state(self):
        for s in st.STATES:
            if s in ("stopped", "errored"):
                continue
            self.assertIn("errored", st.TRANSITIONS[s], s)

    def test_stopped_is_terminal(self):
        m = st.StateMachine()
        m.to("stopped")
        for s in st.STATES:
            with self.assertRaises(st.IllegalTransition):
                m.to(s)

    def test_on_change_sees_old_new_and_detail(self):
        seen = []
        m = st.StateMachine(on_change=lambda o, n, d: seen.append((o, n, d)))
        m.to("running", "turn 1")
        self.assertEqual(seen, [("idle", "running", "turn 1")])

    def test_unknown_state_is_illegal(self):
        with self.assertRaises(st.IllegalTransition):
            st.StateMachine().to("sleeping")


if __name__ == "__main__":
    unittest.main()
