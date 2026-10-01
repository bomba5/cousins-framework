"""Every lane branch in cousin_lib, classified for the
`tmux` runner kind, and a meta-test that pins the table: a new branch on
the runner lane (or a literal kind test) fails here until it is classified.

  kinds      defines or checks the kind list itself (delivery.RUNNER_KINDS,
             runner_for, the contract table, a --runner choice)
  transport  the inbox and the supervisor: right for tmux as it stands (the
             tmux kind is fed by the inbox and supervised like every runner)
  sdk-only   tests for the SDK kind by name: each with a named owner
  pane       the tmux pane itself (the supervisor's reap of a stopped tmux
             cousin's pane; a pane's liveness is otherwise TmuxRunner's)
  refusal    2.0.0's answer to a cousin with no runner kind: a call of
             delivery.lane_refusal or a site naming RefusedBackend

Sites are file plus the stripped line, so line moves do not break it. The
table is in file order."""
import collections
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BRANCH = re.compile(r"RUNNER_KINDS|runner_lane\(|InboxBackend\)|_runner_kind\(|_is_runner\("
                    r"|runner_cousins\(|_lane_homes\(|lane_refusal\(|RefusedBackend")
LITERAL = re.compile(r"""(runner"\)|_runner_kind\([^)]*\))\s*[!=]=\s*["'](sdk|fake|opencode|tmux)["']""")
# check's config_mismatches compares a migrated cousin's [agent] with its
# 1.x [runtime]: it goes with the legacy migration (not in the slim
# 2.0.0, which keeps that code unreached)
OWNERS = {"cousin_lib/migrate.py": "the legacy migration (check's config_mismatches)"}

SITES = (
    ('cousin_lib/agent_settings.py', 'The lanes are delivery.RUNNER_KINDS, read at call time and never copied', 'kinds'),
    ('cousin_lib/agent_settings.py', "added to RUNNER_KINDS later (as `tmux` was) gets the keys that name", 'kinds'),
    ('cousin_lib/agent_settings.py', 'return list(delivery.RUNNER_KINDS)', 'kinds'),
    ('cousin_lib/agent_settings.py', 'return runner if runner in delivery.RUNNER_KINDS else TMUX_LEGACY', 'kinds'),
    ('cousin_lib/agent_settings.py', 'if lane not in delivery.RUNNER_KINDS:', 'kinds'),
    ('cousin_lib/chat.py', 'return isinstance(delivery.backend_for(target.home), delivery.InboxBackend)', 'transport'),
    ('cousin_lib/chat.py', 'raise DeliveryRefused(delivery.lane_refusal(target.home))', 'refusal'),
    ('cousin_lib/chat.py', 'kind = delivery._runner_kind(c.home) or (', 'kinds'),
    ('cousin_lib/console/pane.py', 'if _runner_kind(home) != "tmux":', 'pane'),
    ('cousin_lib/console/proxy.py', 'return isinstance(delivery.backend_for(home), delivery.InboxBackend)', 'transport'),
    ('cousin_lib/console/routes_fleet.py', 'def _is_runner(config):', 'transport'),
    ('cousin_lib/console/routes_fleet.py', 'return isinstance(delivery.backend_for(config.home), delivery.InboxBackend)', 'transport'),
    ('cousin_lib/console/routes_fleet.py', 'if spawn.runner_lane(config.home):', 'transport'),
    ('cousin_lib/console/routes_fleet.py', 'chat = None if _is_runner(config) else chat_health(config)', 'transport'),
    ('cousin_lib/console/routes_fleet.py', 'if _is_runner(config):', 'transport'),
    ('cousin_lib/console/routes_fleet.py', 'from delivery.RUNNER_KINDS), `default_runner` (COUSIN_DEFAULT_RUNNER,', 'kinds'),
    ('cousin_lib/console/routes_fleet.py', 'if spawn.runner_lane(config.home):', 'transport'),
    ('cousin_lib/console/routes_fleet.py', 'raise HttpError(409, delivery.lane_refusal(config.home))', 'refusal'),
    ('cousin_lib/console/routes_fleet.py', 'if spawn.runner_lane(home):', 'transport'),
    ('cousin_lib/console/routes_fleet.py', '# The lane: `runner` (one of RUNNER_KINDS) and the `account` it runs', 'kinds'),
    ('cousin_lib/console/routes_fleet.py', 'if spawn.runner_lane(config.home):', 'transport'),
    ('cousin_lib/console/routes_fleet.py', 'if spawn.runner_lane(home):', 'transport'),
    ('cousin_lib/console/routes_mcp.py', 'def _is_runner(lane):', 'kinds'),
    ('cousin_lib/console/routes_mcp.py', 'if _is_runner(lane):', 'kinds'),
    ('cousin_lib/console/routes_mcp.py', '_write_registry(req, own, runner_lane=_is_runner(_lane(home)))', 'kinds'),
    ('cousin_lib/console/routes_migrate.py', 'return _runner_kind(home) or "tmux-legacy"', 'kinds'),
    ('cousin_lib/console/routes_migrate.py', 'from cousin_lib.delivery import RUNNER_KINDS, _runner_kind', 'kinds'),
    ('cousin_lib/console/routes_migrate.py', 'raise HttpError(400 if _runner_kind(home) in RUNNER_KINDS else 409,', 'refusal'),
    ('cousin_lib/console/routes_migrate.py', 'from cousin_lib.delivery import RUNNER_KINDS, _runner_kind, lane_refusal', 'kinds'),
    ('cousin_lib/console/routes_migrate.py', 'refusal = None if _runner_kind(home) in RUNNER_KINDS else lane_refusal(home)', 'refusal'),
    ('cousin_lib/console/routes_telegram.py', 'def _runner_lane(home):', 'transport'),
    ('cousin_lib/console/routes_telegram.py', 'return spawn.runner_lane(home)', 'transport'),
    ('cousin_lib/console/routes_telegram.py', 'if _runner_lane(home):', 'transport'),
    ('cousin_lib/console/routes_telegram.py', 'return delivery.lane_refusal(home)', 'refusal'),
    ('cousin_lib/console/tokens.py', 'return any(_runner_kind(c.home) in OWN_USAGE_KINDS', 'kinds'),
    ('cousin_lib/console/tokens.py', 'if _runner_kind(home) in USAGE_DB_KINDS:', 'kinds'),
    ('cousin_lib/console/tokens.py', 'if _runner_kind(home) == "tmux":', 'kinds'),
    ('cousin_lib/delivery.py', 'A cousin whose `[agent] runner` names a runner kind (`RUNNER_KINDS`)', 'kinds'),
    ('cousin_lib/delivery.py', '`failed`, and `lane_refusal(home)` is the one line every entry point', 'refusal'),
    ('cousin_lib/delivery.py', 'RUNNER_KINDS = ("sdk", "fake", "opencode", "tmux")', 'kinds'),
    ('cousin_lib/delivery.py', 'def _runner_kind(home):', 'kinds'),
    ('cousin_lib/delivery.py', 'def lane_refusal(home):', 'refusal'),
    ('cousin_lib/delivery.py', '% (slug, kind, ", ".join(RUNNER_KINDS)))', 'kinds'),
    ('cousin_lib/delivery.py', 'class RefusedBackend:', 'refusal'),
    ('cousin_lib/delivery.py', 'with `lane_refusal(home)`)."""', 'refusal'),
    ('cousin_lib/delivery.py', 'return lane_refusal(home)', 'refusal'),
    ('cousin_lib/delivery.py', 'kind; `RefusedBackend` for anything else (missing, unknown value,', 'refusal'),
    ('cousin_lib/delivery.py', 'if _runner_kind(home) in RUNNER_KINDS:', 'kinds'),
    ('cousin_lib/delivery.py', 'return RefusedBackend()', 'refusal'),
    ('cousin_lib/delivery.py', 'return isinstance(backend_for(home), InboxBackend)', 'kinds'),
    ('cousin_lib/delivery.py', 'if isinstance(backend_for(home), InboxBackend):', 'kinds'),
    ('cousin_lib/flip.py', 'from cousin_lib.delivery import RUNNER_KINDS, _runner_kind', 'kinds'),
    ('cousin_lib/flip.py', 'if _runner_kind(home) in RUNNER_KINDS:', 'transport'),
    ('cousin_lib/flip.py', 'result["error"] = lane_refusal(home)', 'refusal'),
    ('cousin_lib/lifecycle.py', 'from cousin_lib.delivery import RUNNER_KINDS, _runner_kind, lane_refusal', 'kinds'),
    ('cousin_lib/lifecycle.py', 'if _runner_kind(home) not in RUNNER_KINDS:', 'transport'),
    ('cousin_lib/lifecycle.py', 'result["error"] = lane_refusal(home)', 'refusal'),
    ('cousin_lib/lifecycle.py', 'from cousin_lib.delivery import RUNNER_KINDS, _runner_kind', 'kinds'),
    ('cousin_lib/lifecycle.py', 'if _runner_kind(home) in RUNNER_KINDS:', 'transport'),
    ('cousin_lib/lifecycle.py', 'from cousin_lib.delivery import RUNNER_KINDS, _runner_kind, lane_refusal', 'kinds'),
    ('cousin_lib/lifecycle.py', 'if _runner_kind(cfg.home) not in RUNNER_KINDS:', 'transport'),
    ('cousin_lib/lifecycle.py', 'result["error"] = lane_refusal(cfg.home)', 'refusal'),
    ('cousin_lib/loops.py', 'wait = not isinstance(delivery.backend_for(home), delivery.InboxBackend)', 'transport'),
    ('cousin_lib/mcp_server.py', 'return _runner_kind(pathlib.Path(home)) == "tmux"', 'kinds'),
    ('cousin_lib/meetings.py', 'wait = not isinstance(delivery.backend_for(home), delivery.InboxBackend)', 'transport'),
    ('cousin_lib/migrate.py', 'from cousin_lib.delivery import RUNNER_KINDS, lane_refusal  # the one list of runner kinds', 'kinds'),
    ('cousin_lib/migrate.py', 'lane_ok = runner not in RUNNER_KINDS', 'kinds'),
    ('cousin_lib/migrate.py', 'if agent.get("runner") != "sdk":', 'sdk-only'),
    ('cousin_lib/migrate.py', 'tmux_kind = _agent(home).get("runner") == "tmux"', 'kinds'),
    ('cousin_lib/migrate.py', 'if current not in RUNNER_KINDS:', 'kinds'),
    ('cousin_lib/migrate.py', 'checks.append(_check("kind", False, lane_refusal(home)))', 'refusal'),
    ('cousin_lib/migrate.py', 'if data is None or (kind not in RUNNER_KINDS and not worker):', 'transport'),
    ('cousin_lib/migrate.py', 'target["refused"] = lane_refusal(home)       # tidy is not a conversion', 'refusal'),
    ('cousin_lib/migrate.py', 'kind = _runner_kind(home)', 'kinds'),
    ('cousin_lib/migrate.py', 'if kind not in RUNNER_KINDS:', 'transport'),
    ('cousin_lib/migrate.py', 'return lane_refusal(home)', 'refusal'),
    ('cousin_lib/runner/contract_table.py', 'runner kind (`delivery.RUNNER_KINDS`), each cell IMPLEMENTED, PLUGIN or', 'kinds'),
    ('cousin_lib/runner/contract_table.py', 'from cousin_lib.delivery import RUNNER_KINDS', 'kinds'),
    ('cousin_lib/runner/contract_table.py', '# runner kind -> "module:Class"; every kind in delivery.RUNNER_KINDS has one', 'kinds'),
    ('cousin_lib/runner/contract_table.py', 'def cells(kinds=RUNNER_KINDS):', 'kinds'),
    ('cousin_lib/runner/contract_table.py', 'def render(kinds=RUNNER_KINDS):', 'kinds'),
    ('cousin_lib/runner/main.py', 'from cousin_lib.delivery import RUNNER_KINDS, lane_refusal', 'kinds'),
    ('cousin_lib/runner/main.py', 'KINDS = RUNNER_KINDS      # the runners runner_for builds, one list (delivery)', 'kinds'),
    ('cousin_lib/runner/main.py', 'raise RunnerError(lane_refusal(home))', 'refusal'),
    ('cousin_lib/schedule.py', 'delivery.InboxBackend)', 'transport'),
    ('cousin_lib/spawn.py', 'from cousin_lib.delivery import RUNNER_KINDS  # the one list of runner kinds', 'kinds'),
    ('cousin_lib/spawn.py', 'name); a runner must be one of RUNNER_KINDS. account None reads', 'kinds'),
    ('cousin_lib/spawn.py', '" name one of %s" % (TMUX_LEGACY, ", ".join(RUNNER_KINDS)))', 'kinds'),
    ('cousin_lib/spawn.py', 'if runner not in RUNNER_KINDS:', 'kinds'),
    ('cousin_lib/spawn.py', '% (runner_from, ", ".join(RUNNER_KINDS), runner))', 'kinds'),
    ('cousin_lib/spawn.py', 'def runner_lane(home):', 'transport'),
    ('cousin_lib/spawn.py', '"""The cousin runs on cousin-runner (`[agent] runner` is one of RUNNER_KINDS),', 'transport'),
    ('cousin_lib/spawn.py', 'return delivery._runner_kind(home) in RUNNER_KINDS', 'transport'),
    ('cousin_lib/spawn.py', 'if runner_lane(home):', 'transport'),
    ('cousin_lib/spawn.py', 'return {"worker": "no session", "note": delivery.lane_refusal(home)}', 'refusal'),
    ('cousin_lib/spawn.py', 'raise SpawnError(delivery.lane_refusal(home))', 'refusal'),
    ('cousin_lib/spawn.py', 'if not runner_lane(home) and \\', 'transport'),
    ('cousin_lib/spawn.py', '% delivery.lane_refusal(home)}', 'refusal'),
    ('cousin_lib/spawn.py', 'if not runner_lane(home):', 'transport'),
    ('cousin_lib/spawn.py', 'lane = delivery._runner_kind(home)', 'kinds'),
    ('cousin_lib/spawn.py', 'if runner_lane(home):', 'transport'),
    ('cousin_lib/spawn.py', 'raise SpawnError(delivery.lane_refusal(home))', 'refusal'),
    ('cousin_lib/spawn.py', 'return "tmux" if (data.get("agent") or {}).get("runner") == "tmux" else None', 'kinds'),
    ('cousin_lib/spawn.py', 'parser.add_argument("--runner", choices=RUNNER_KINDS,', 'kinds'),
    ('cousin_lib/spawn.py', 'on_runner = runner_lane(root / "cousins" / args.slug)', 'transport'),
    ('cousin_lib/spawn.py', 'why = delivery.lane_refusal(root / "cousins" / args.slug)', 'refusal'),
    ('cousin_lib/supervisor.py', 'delivery.RUNNER_KINDS gets a runner child, unless `[agent] auto_start = false`', 'transport'),
    ('cousin_lib/supervisor.py', 'from cousin_lib.delivery import RUNNER_KINDS, lane_refusal  # the one list of runner kinds', 'kinds'),
    ('cousin_lib/supervisor.py', 'return {"ok": False, "error": lane_refusal(self.root / "cousins" / slug)}', 'refusal'),
    ('cousin_lib/supervisor.py', 'if (_agent_table(home) or {}).get("runner") != "tmux":', 'pane'),
    ('cousin_lib/supervisor.py', 'wanted = runner_cousins(self.root)', 'transport'),
    ('cousin_lib/supervisor.py', 'lane = {Path(c.home).name for c in _lane_homes(self.root)}', 'transport'),
    ('cousin_lib/supervisor.py', '"""`[agent] runner` is one of RUNNER_KINDS (delivery._runner_kind\'s test)."""', 'transport'),
    ('cousin_lib/supervisor.py', 'return agent is not None and agent.get("runner") in RUNNER_KINDS', 'transport'),
    ('cousin_lib/supervisor.py', 'def _lane_homes(root):', 'transport'),
    ('cousin_lib/supervisor.py', 'def runner_cousins(root):', 'transport'),
    ('cousin_lib/supervisor.py', '(`[agent] runner` one of RUNNER_KINDS) whose `[agent] auto_start` is not', 'transport'),
    ('cousin_lib/supervisor.py', 'return [c for c in _lane_homes(root)', 'transport'),
    ('cousin_lib/supervisor.py', 'refused[entry.name] = lane_refusal(entry)', 'refusal'),
    ('cousin_lib/supervisor.py', 'specs += [runner_spec(c.home) for c in runner_cousins(root)]', 'transport'),
    ('cousin_lib/telegram.py', 'if not isinstance(delivery.backend_for(cfg.home), delivery.InboxBackend):', 'transport'),
    ('cousin_lib/telegram.py', 'raise TelegramConfigError(delivery.lane_refusal(cfg.home))', 'refusal'),
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
                         " gone ones): kinds, transport, sdk-only (with its owner), pane"
                         " or refusal")

    def test_the_classes_are_the_five_and_each_sdk_only_site_has_an_owner(self):
        for f, _line, cls in SITES:
            self.assertIn(cls, ("kinds", "transport", "sdk-only", "pane", "refusal"), f)
            if cls == "sdk-only":
                self.assertIn(f, OWNERS)


if __name__ == "__main__":
    unittest.main()
