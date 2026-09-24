"""Phase 11 Task 1: every lane branch in cousin_lib, classified for the
`tmux` runner kind, and a meta-test that pins the table: a new branch on
the runner lane (or a literal kind test) fails here until it is classified.

  kinds      defines or checks the kind list itself (delivery.RUNNER_KINDS,
             runner_for, the contract table, a --runner choice)
  transport  the inbox and the supervisor: right for tmux as it stands (the
             tmux kind is fed by the inbox and supervised like every runner)
  sdk-only   tests for the SDK kind by name: each owned by a later task
  pane       the tmux pane itself (the supervisor's reap of a stopped tmux
             cousin's pane; a pane's liveness is otherwise TmuxRunner's, and
             the chat watchdog checks the runner process)

Sites are file plus the stripped line, so line moves do not break it."""
import collections
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BRANCH = re.compile(r"RUNNER_KINDS|runner_lane\(|InboxBackend\)|_runner_kind\(|_is_runner\("
                    r"|runner_cousins\(|_lane_homes\(")
LITERAL = re.compile(r"""(runner"\)|_runner_kind\([^)]*\))\s*[!=]=\s*["'](sdk|fake|opencode|tmux)["']""")
OWNERS = {"cousin_lib/migrate.py": "Task 11 (the kind switch)"}

SITES = (
    ('cousin_lib/migrate.py', 'tmux_kind = _agent(home).get("runner") == "tmux"', 'kinds'),
    ('cousin_lib/migrate.py', 'if current not in RUNNER_KINDS:', 'kinds'),
    ('cousin_lib/mcp_server.py', 'return _runner_kind(pathlib.Path(home)) == "tmux"', 'kinds'),
    ('cousin_lib/supervisor.py', 'if (_agent_table(home) or {}).get("runner") != "tmux":', 'pane'),
    ('cousin_lib/chat.py', 'return isinstance(delivery.backend_for(target.home), delivery.InboxBackend)', 'transport'),
    ('cousin_lib/chat_watchdog.py', 'def _default_runner_lane(home):', 'transport'),
    ('cousin_lib/chat_watchdog.py', 'return spawn.runner_lane(home)', 'transport'),
    ('cousin_lib/chat_watchdog.py', 'alive = runner_alive(home) if runner_lane(home) else has_tmux(config.tmux_session)', 'transport'),
    ('cousin_lib/console/proxy.py', 'return isinstance(delivery.backend_for(home), delivery.InboxBackend)', 'transport'),
    ('cousin_lib/console/routes_fleet.py', 'def _is_runner(config):', 'transport'),
    ('cousin_lib/console/routes_fleet.py', 'return isinstance(delivery.backend_for(config.home), delivery.InboxBackend)', 'transport'),
    ('cousin_lib/console/routes_fleet.py', 'chat = None if _is_runner(config) else chat_health(config)', 'transport'),
    ('cousin_lib/console/routes_fleet.py', 'if _is_runner(config):', 'transport'),
    ('cousin_lib/console/routes_fleet.py', 'if spawn.runner_lane(config.home):', 'transport'),
    ('cousin_lib/console/routes_fleet.py', 'if spawn.runner_lane(home):', 'transport'),
    ('cousin_lib/console/routes_fleet.py', 'if spawn.runner_lane(config.home):', 'transport'),
    ('cousin_lib/console/tokens.py', 'return any(_runner_kind(c.home) in OWN_USAGE_KINDS', 'kinds'),
    ('cousin_lib/console/tokens.py', 'if _runner_kind(home) == "tmux":', 'kinds'),
    ('cousin_lib/console/tokens.py', 'if _runner_kind(home) == "sdk":', 'kinds'),
    ('cousin_lib/delivery.py', 'RUNNER_KINDS = ("sdk", "fake", "opencode", "tmux")', 'kinds'),
    ('cousin_lib/delivery.py', 'def _runner_kind(home):', 'kinds'),
    ('cousin_lib/delivery.py', 'if _runner_kind(home) in RUNNER_KINDS:', 'kinds'),
    ('cousin_lib/delivery.py', 'return isinstance(backend_for(home), InboxBackend)', 'kinds'),
    ('cousin_lib/delivery.py', 'if isinstance(backend_for(home), InboxBackend):', 'kinds'),
    ('cousin_lib/flip.py', 'from cousin_lib.delivery import RUNNER_KINDS, _runner_kind', 'kinds'),
    ('cousin_lib/flip.py', 'if _runner_kind(home) in RUNNER_KINDS:', 'transport'),
    ('cousin_lib/lifecycle.py', 'from cousin_lib.delivery import RUNNER_KINDS, _runner_kind', 'kinds'),
    ('cousin_lib/lifecycle.py', 'if _runner_kind(home) in RUNNER_KINDS:', 'transport'),
    ('cousin_lib/loops.py', 'wait = not isinstance(delivery.backend_for(home), delivery.InboxBackend)', 'transport'),
    ('cousin_lib/meetings.py', 'wait = not isinstance(delivery.backend_for(home), delivery.InboxBackend)', 'transport'),
    ('cousin_lib/migrate.py', 'from cousin_lib.delivery import RUNNER_KINDS  # the one list of runner kinds (M6)', 'kinds'),
    ('cousin_lib/migrate.py', 'lane_ok = runner not in RUNNER_KINDS', 'kinds'),
    ('cousin_lib/migrate.py', 'if agent.get("runner") != "sdk":', 'sdk-only'),
    ('cousin_lib/runner/contract_table.py', 'runner kind (`delivery.RUNNER_KINDS`), each cell IMPLEMENTED, PLUGIN or', 'kinds'),
    ('cousin_lib/runner/contract_table.py', 'from cousin_lib.delivery import RUNNER_KINDS', 'kinds'),
    ('cousin_lib/runner/contract_table.py', '# runner kind -> "module:Class"; every kind in delivery.RUNNER_KINDS has one', 'kinds'),
    ('cousin_lib/runner/contract_table.py', 'def cells(kinds=RUNNER_KINDS):', 'kinds'),
    ('cousin_lib/runner/contract_table.py', 'def render(kinds=RUNNER_KINDS):', 'kinds'),
    ('cousin_lib/runner/main.py', 'from cousin_lib.delivery import RUNNER_KINDS', 'kinds'),
    ('cousin_lib/runner/main.py', 'KINDS = RUNNER_KINDS      # the runners runner_for builds, one list (delivery)', 'kinds'),
    ('cousin_lib/schedule.py', 'delivery.InboxBackend)', 'transport'),
    ('cousin_lib/server/app.py', 'delivery.InboxBackend)', 'transport'),
    ('cousin_lib/server/chat_api.py', 'if isinstance(delivery.backend_for(home), delivery.InboxBackend):', 'transport'),
    ('cousin_lib/spawn.py', 'from cousin_lib.delivery import RUNNER_KINDS  # the one list of runner kinds (M6)', 'kinds'),
    ('cousin_lib/spawn.py', 'if runner is not None and runner not in RUNNER_KINDS:', 'kinds'),
    ('cousin_lib/spawn.py', '% (runner_from, ", ".join(RUNNER_KINDS), runner))', 'transport'),
    ('cousin_lib/spawn.py', 'def runner_lane(home):', 'transport'),
    ('cousin_lib/spawn.py', 'return delivery._runner_kind(home) in RUNNER_KINDS', 'transport'),
    ('cousin_lib/spawn.py', 'if runner_lane(home):', 'transport'),
    ('cousin_lib/spawn.py', 'if runner_lane(home):', 'transport'),
    ('cousin_lib/spawn.py', 'parser.add_argument("--runner", choices=RUNNER_KINDS,', 'kinds'),
    ('cousin_lib/spawn.py', 'on_runner = runner_lane(root / "cousins" / args.slug)', 'transport'),
    ('cousin_lib/supervisor.py', 'from cousin_lib.delivery import RUNNER_KINDS  # the one list of runner kinds (M6)', 'kinds'),
    ('cousin_lib/supervisor.py', 'wanted = runner_cousins(self.root)', 'transport'),
    ('cousin_lib/supervisor.py', 'lane = {Path(c.home).name for c in _lane_homes(self.root)}', 'transport'),
    ('cousin_lib/supervisor.py', 'return agent is not None and agent.get("runner") in RUNNER_KINDS', 'transport'),
    ('cousin_lib/supervisor.py', 'def _lane_homes(root):', 'transport'),
    ('cousin_lib/supervisor.py', 'def runner_cousins(root):', 'transport'),
    ('cousin_lib/supervisor.py', 'return [c for c in _lane_homes(root)', 'transport'),
    ('cousin_lib/supervisor.py', 'specs += [runner_spec(c.home) for c in runner_cousins(root)]', 'transport'),
    ('cousin_lib/telegram.py', 'if isinstance(delivery.backend_for(cfg.home), delivery.InboxBackend):', 'transport'),
    ('cousin_lib/watch.py', 'if not isinstance(delivery.backend_for(home), delivery.InboxBackend):', 'transport'),
)


def current_sites():
    out = []
    for path in sorted((ROOT / "cousin_lib").rglob("*.py")):
        rel = str(path.relative_to(ROOT))
        for line in path.read_text().splitlines():
            if BRANCH.search(line) or LITERAL.search(line):
                out.append((rel, line.strip()))
    return out


class TestLaneBranchAudit(unittest.TestCase):
    def test_every_lane_branch_is_classified(self):
        table = collections.Counter((f, l) for f, l, _ in SITES)
        now = collections.Counter(current_sites())
        new = sorted((now - table).elements())
        gone = sorted((table - now).elements())
        self.assertEqual((new, gone), ([], []),
                         "classify the new sites in tests/test_lane_audit.py SITES (and drop the"
                         " gone ones): kinds, transport, sdk-only (with its owning task) or pane")

    def test_the_classes_are_the_four_and_each_sdk_only_site_has_an_owner(self):
        for f, _line, cls in SITES:
            self.assertIn(cls, ("kinds", "transport", "sdk-only", "pane"), f)
            if cls == "sdk-only":
                self.assertIn(f, OWNERS)


if __name__ == "__main__":
    unittest.main()
