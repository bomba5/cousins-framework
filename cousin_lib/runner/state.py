"""The runner's state machine. Every transition is legal by table and
reported through `on_change`, which the runner wires to the event
stream: a state nobody can see is a state nobody can act on."""
from cousin_lib.runner.base import RunnerError

STATES = ("idle", "running", "waiting_permission", "rate_limited",
          "rolling_over", "errored", "stopped")

TRANSITIONS = {
    "idle": {"running", "rolling_over", "stopped", "errored"},
    "running": {"idle", "waiting_permission", "rate_limited",
                "rolling_over", "errored", "stopped"},
    "waiting_permission": {"running", "idle", "errored", "stopped"},
    "rate_limited": {"running", "idle", "errored", "stopped"},
    "rolling_over": {"idle", "errored", "stopped"},
    "errored": {"idle", "stopped"},
    "stopped": set(),
}


class IllegalTransition(RunnerError):
    pass


class StateMachine:
    def __init__(self, on_change=None):
        self.state = "idle"
        self._on_change = on_change

    def to(self, new_state, detail=""):
        if new_state not in STATES:
            raise IllegalTransition("unknown state %r" % (new_state,))
        if new_state not in TRANSITIONS[self.state]:
            raise IllegalTransition("%s -> %s is not a legal transition"
                                    % (self.state, new_state))
        old, self.state = self.state, new_state
        if self._on_change is not None:
            self._on_change(old, new_state, detail)
