"""The keys 2.0.0 removes (ruling P10-2): one table, cousin_lib/removed_keys.
On 1.x, `cousin-migrate plan` warns about every one a cousin or the
install still carries, with the line to follow, so the operator can clean
up before upgrading; the plan stays ready (a warning, not a blocker). In
2.0.0 (phase 10b) the same table fails the load."""
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from cousin_lib import migrate, removed_keys
from tests._hermetic import HermeticCase
from tests.test_migrate import Live


def _root(case, toml, harness=None, agent_cmd=False):
    tmp = tempfile.TemporaryDirectory(); case.addCleanup(tmp.cleanup)
    root = pathlib.Path(tmp.name)
    home = root / "cousins" / "wren"
    (home / "data").mkdir(parents=True)
    (root / "config").mkdir()
    (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n' + toml)
    if harness is not None:
        (root / "config" / "harness.toml").write_text(harness)
    if agent_cmd:
        (root / "config" / "agent-cmd").write_text("claude --model {model}\n")
    p = mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(root)})
    p.start(); case.addCleanup(p.stop)
    return root, home


class TestTheTable(HermeticCase):
    def test_a_tmux_cousin_and_install_show_every_key_2_0_rejects(self):
        root, home = _root(self, '\n[chat]\nport = 8091\nhost = "127.0.0.1"\ntmux_session = "wren"\n',
                           harness='attention_patterns = ["Login"]\nbusy_patterns = ["esc"]\n'
                                   'input_mode = "paste"\ntranscripts_dir = "~/t"\n'
                                   'mcp_logs_dir = "~/m"\nsettings_file = "~/s.json"\n',
                           agent_cmd=True)
        found = {(f["where"], f["key"]) for f in removed_keys.scan(root, home)}
        self.assertEqual(found, {
            ("cousin.toml", "[agent] runner"), ("cousin.toml", "[chat] port"),
            ("cousin.toml", "[chat] host"), ("cousin.toml", "[chat] tmux_session"),
            ("config/harness.toml", "attention_patterns"), ("config/harness.toml", "busy_patterns"),
            ("config/harness.toml", "input_mode"), ("config/harness.toml", "transcripts_dir"),
            ("config/harness.toml", "mcp_logs_dir"), ("config/harness.toml", "settings_file"),
            ("config/agent-cmd", "config/agent-cmd")})
        for f in removed_keys.scan(root, home):
            self.assertTrue(f["line"].strip(), f)

    def test_a_clean_runner_cousin_shows_nothing(self):
        root, home = _root(self, '\n[agent]\nrunner = "sdk"\n',
                           harness='auto_memory_dir = "~/a"\n')
        self.assertEqual(removed_keys.scan(root, home), [])

    def test_runner_tmux_is_named(self):
        root, home = _root(self, '\n[agent]\nrunner = "tmux"\n')
        [f] = removed_keys.scan(root, home)
        self.assertEqual(f["key"], "[agent] runner")
        self.assertIn("tmux", f["line"])


class TestPlanWarns(HermeticCase):
    def test_the_plan_warns_and_stays_ready(self):
        root, home = _root(self, '\n[chat]\nport = 8091\n', agent_cmd=True)
        (root / "config" / "accounts.toml").write_text(
            '[accounts.team]\nkind = "claude-login"\nconfig_dir = "accounts/team"\n')
        p = migrate.plan(home, root=root, account="team", **Live().kw())
        self.assertTrue(p["ready"], p)
        keys = {w["key"] for w in p["warnings"]}
        self.assertIn("[chat] port", keys)
        self.assertIn("config/agent-cmd", keys)


if __name__ == "__main__":
    unittest.main()
