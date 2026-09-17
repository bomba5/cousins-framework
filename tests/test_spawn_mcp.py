"""`create_cousin` provisions the MCP adapter for every new cousin.

A `.mcp.json` alone is a registration the harness has not accepted;
approval is a separate, operator-run step (`cousin-mcp approve`) and is
tested with the adapter. Here: the two files spawn writes, their exact
shape, the operator filled from what spawn was told, and the rule that
an existing file is never overwritten.
"""
import contextlib
import io
import json
import pathlib
import shutil
import tempfile
import tomllib
import unittest

from cousin_lib import mcp_server
from cousin_lib.spawn import create_cousin, spawn_main

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SHIPPED = _REPO_ROOT / "config" / "mcp-registry.toml.example"


class ProvisionCase(unittest.TestCase):
    def _framework_root(self, with_example=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        (root / "templates").mkdir()
        shutil.copy(_REPO_ROOT / "templates" / "cousin-CLAUDE.template.md",
                    root / "templates" / "cousin-CLAUDE.template.md")
        if with_example:
            (root / "config").mkdir()
            shutil.copy(_SHIPPED, root / "config" / "mcp-registry.toml.example")
        return root

    def _create(self, root, **kw):
        args = dict(slug="testa", name="Testa", role="test cousin",
                    voice="Plain and helpful.", port=8100)
        args.update(kw)
        return create_cousin(root, **args)

    def test_new_cousin_gets_the_default_registry_with_no_operator(self):
        root = self._framework_root()
        home = self._create(root)["home"]
        reg = tomllib.loads((home / "mcp-registry.toml").read_text())
        default = tomllib.loads(_SHIPPED.read_text())
        self.assertEqual(reg, default)
        self.assertEqual(reg["tools"]["send"]["operators"], [])
        # and it is a registry the adapter accepts
        mcp_server.load_registry(home / "mcp-registry.toml")

    def test_operator_given_to_spawn_lands_in_cousin_toml_and_the_registry(self):
        root = self._framework_root()
        home = self._create(root, operator="Sam")["home"]
        cfg = tomllib.loads((home / "cousin.toml").read_text())
        self.assertEqual(cfg["operator"]["name"], "Sam")
        reg = tomllib.loads((home / "mcp-registry.toml").read_text())
        self.assertEqual(reg["tools"]["send"]["operators"], ["Sam"])
        # everything else is the framework default, key for key
        default = tomllib.loads(_SHIPPED.read_text())
        default["tools"]["send"]["operators"] = ["Sam"]
        self.assertEqual(reg, default)

    def test_operator_name_with_quotes_survives_the_toml_round_trip(self):
        root = self._framework_root()
        home = self._create(root, operator='Sam "Q" O\'Neil')["home"]
        reg = tomllib.loads((home / "mcp-registry.toml").read_text())
        self.assertEqual(reg["tools"]["send"]["operators"], ['Sam "Q" O\'Neil'])

    def test_mcp_json_points_the_harness_at_the_home_registry(self):
        root = self._framework_root()
        out = self._create(root)
        home = out["home"]
        cfg = json.loads((home / ".mcp.json").read_text())
        srv = cfg["mcpServers"]["cousin"]
        self.assertEqual(srv["type"], "stdio")
        self.assertEqual(srv["command"], "cousin-mcp")
        self.assertEqual(srv["args"],
                         ["--registry", str(home / "mcp-registry.toml")])
        self.assertEqual(srv["env"]["COUSIN_HOME"], str(home))
        self.assertEqual(srv["env"]["COUSIN_SLUG"], "testa")
        self.assertEqual(srv["env"]["FRAMEWORK_ROOT"], str(root))
        self.assertEqual(set(cfg), {"mcpServers"})

    def test_install_default_registry_overrides_the_shipped_example(self):
        root = self._framework_root()
        text = _SHIPPED.read_text().replace("ceiling = 12", "ceiling = 9")
        (root / "config" / "mcp-registry.toml").write_text(text)
        home = self._create(root)["home"]
        reg = tomllib.loads((home / "mcp-registry.toml").read_text())
        self.assertEqual(reg["ceiling"], 9)

    def test_root_without_the_example_falls_back_to_the_checkout_copy(self):
        # The existing spawn tests build a root that holds only the
        # template; provisioning must not turn them into failures.
        root = self._framework_root(with_example=False)
        home = self._create(root)["home"]
        self.assertTrue((home / "mcp-registry.toml").is_file())
        self.assertTrue((home / ".mcp.json").is_file())

    def test_provision_never_overwrites_an_existing_file(self):
        root = self._framework_root()
        home = self._create(root)["home"]
        (home / "mcp-registry.toml").write_text("ceiling = 1\n")
        (home / ".mcp.json").write_text('{"mine": true}')
        written = mcp_server.provision_mcp(home, root=root, slug="testa")
        self.assertEqual(written, [])
        self.assertEqual((home / "mcp-registry.toml").read_text(), "ceiling = 1\n")
        self.assertEqual((home / ".mcp.json").read_text(), '{"mine": true}')

    def test_provision_reports_what_it_wrote(self):
        root = self._framework_root()
        home = self._create(root)["home"]
        (home / ".mcp.json").unlink()
        written = mcp_server.provision_mcp(home, root=root, slug="testa")
        self.assertEqual(written, [home / ".mcp.json"])

    def test_spawn_main_takes_operator(self):
        root = self._framework_root()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = spawn_main(["testa", "--root", str(root), "--role", "x",
                             "--voice", "v", "--port", "8100",
                             "--operator", "Sam"])
        self.assertEqual(rc, 0, err.getvalue())
        reg = tomllib.loads(
            (root / "cousins" / "testa" / "mcp-registry.toml").read_text())
        self.assertEqual(reg["tools"]["send"]["operators"], ["Sam"])


if __name__ == "__main__":
    unittest.main()
