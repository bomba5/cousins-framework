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
import os
import pathlib
import shutil
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

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
        self.assertTrue(srv["command"].endswith("cousin-mcp"))
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


class TestAdapterCommand(unittest.TestCase):
    """.mcp.json names the cousin-mcp of the install that wrote it: a
    bare name would run whichever cousin-mcp is first on the agent's
    PATH, which on a machine with two installs is the other one."""

    def _bin(self, with_adapter):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bindir = pathlib.Path(tmp.name) / "bin"
        bindir.mkdir()
        if with_adapter:
            adapter = bindir / "cousin-mcp"
            adapter.write_text("#!/bin/sh\n")
            adapter.chmod(0o755)
        return bindir

    def test_the_adapter_beside_the_interpreter_is_named_absolutely(self):
        bindir = self._bin(True)
        with mock.patch.object(sys, "executable", str(bindir / "python3")):
            cfg = mcp_server.mcp_json("/h/testa", "testa", "/r")
        self.assertEqual(cfg["mcpServers"]["cousin"]["command"],
                         str(bindir / "cousin-mcp"))

    def test_no_adapter_beside_the_interpreter_falls_back_to_the_name(self):
        bindir = self._bin(False)
        with mock.patch.object(sys, "executable", str(bindir / "python3")):
            cfg = mcp_server.mcp_json("/h/testa", "testa", "/r")
        self.assertEqual(cfg["mcpServers"]["cousin"]["command"],
                         "cousin-mcp")

    def test_path_is_never_consulted(self):
        # The fallback is the bare name, not a PATH lookup: baking in
        # whichever wrapper PATH finds first is the bug.
        bindir = self._bin(False)
        other = self._bin(True)
        with mock.patch.object(sys, "executable", str(bindir / "python3")), \
                mock.patch.dict(os.environ, {"PATH": str(other)}):
            cfg = mcp_server.mcp_json("/h/testa", "testa", "/r")
        self.assertEqual(cfg["mcpServers"]["cousin"]["command"],
                         "cousin-mcp")


class TestProjectSettings(ProvisionCase):
    """Spawn gives the new home its own harness project settings: its
    hooks and the approval of its `cousin` server. An existing home is
    brought up to date with `cousin-spawn <slug> --repair-settings`."""

    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = spawn_main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_new_cousin_gets_project_settings(self):
        root = self._framework_root()
        home = self._create(root)["home"]
        data = json.loads((home / ".claude" / "settings.json").read_text())
        self.assertIn("cousin", data["enabledMcpjsonServers"])
        self.assertIn("Stop", data["hooks"])

    def test_repair_settings_applies_to_an_existing_home(self):
        root = self._framework_root()
        home = self._create(root)["home"]
        path = home / ".claude" / "settings.json"
        path.write_text(json.dumps({"model": "kept"}))
        rc, out, err = self._main(["testa", "--root", str(root),
                                   "--repair-settings"])
        self.assertEqual(rc, 0, err)
        data = json.loads(path.read_text())
        self.assertEqual(data["model"], "kept")
        self.assertIn("cousin", data["enabledMcpjsonServers"])
        self.assertIn("Stop", data["hooks"])
        self.assertIn(str(path), out)

    def test_repair_settings_is_idempotent(self):
        root = self._framework_root()
        home = self._create(root)["home"]
        path = home / ".claude" / "settings.json"
        first = path.read_text()
        rc, _out, err = self._main(["testa", "--root", str(root),
                                    "--repair-settings"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(path.read_text(), first)

    def test_repair_settings_on_an_unknown_slug_is_a_usage_error(self):
        root = self._framework_root()
        rc, _out, err = self._main(["ghost", "--root", str(root),
                                    "--repair-settings"])
        self.assertEqual(rc, 2)
        self.assertIn("ghost", err)
        self.assertFalse((root / "cousins" / "ghost").exists())

    def test_create_still_requires_role_and_voice(self):
        root = self._framework_root()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                spawn_main(["testa", "--root", str(root)])


class TestRelativeRoot(ProvisionCase):
    """A root given as a relative path (the documented `--root .` from
    inside the checkout) must never reach the files the harness reads:
    the agent runs with the cousin home as its working directory, where
    `cousins/<slug>` and `.` name nothing. And an existing .mcp.json
    written relative by an older spawn is repaired in place."""

    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = spawn_main(argv)
        return rc, out.getvalue(), err.getvalue()

    def _assert_absolute(self, home, root):
        root = pathlib.Path(os.path.abspath(root))
        entry = json.loads((home / ".mcp.json").read_text())[
            "mcpServers"]["cousin"]
        registry = pathlib.Path(
            entry["args"][entry["args"].index("--registry") + 1])
        self.assertTrue(registry.is_absolute(), entry)
        self.assertTrue(registry.is_file(), entry)
        self.assertEqual(pathlib.Path(entry["env"]["COUSIN_HOME"]),
                         root / "cousins" / "testa")
        self.assertEqual(pathlib.Path(entry["env"]["FRAMEWORK_ROOT"]), root)
        settings = json.loads(
            (home / ".claude" / "settings.json").read_text())
        commands = [h["command"] for groups in settings["hooks"].values()
                    for g in groups for h in g["hooks"]]
        self.assertTrue(commands)
        for command in commands:
            self.assertIn(str(root / "cousins" / "testa"), command)
            self.assertNotIn(" cousins/testa", command)
            self.assertNotIn("--root .", command)

    def test_spawn_main_with_root_dot_writes_absolute_paths(self):
        root = self._framework_root()
        with contextlib.chdir(root):
            rc, _out, err = self._main(["testa", "--root", ".", "--role",
                                        "x", "--voice", "v", "--port",
                                        "8100"])
        self.assertEqual(rc, 0, err)
        self._assert_absolute(root / "cousins" / "testa", root)

    def test_create_cousin_with_a_relative_root_returns_an_absolute_home(self):
        root = self._framework_root()
        with contextlib.chdir(root.parent):
            out = self._create(pathlib.Path(root.name))
        self.assertTrue(out["home"].is_absolute(), out["home"])
        self._assert_absolute(root / "cousins" / "testa", root)

    def test_repair_settings_rewrites_a_relative_mcp_json(self):
        root = self._framework_root()
        home = self._create(root)["home"]
        stale = json.loads((home / ".mcp.json").read_text())
        entry = stale["mcpServers"]["cousin"]
        entry["args"] = ["--registry", "cousins/testa/mcp-registry.toml"]
        entry["env"].update({"COUSIN_HOME": "cousins/testa",
                             "FRAMEWORK_ROOT": ".", "EXTRA": "kept"})
        stale["mcpServers"]["other"] = {"command": "elsewhere"}
        (home / ".mcp.json").write_text(json.dumps(stale))
        with contextlib.chdir(root):
            rc, out, err = self._main(["testa", "--root", ".",
                                       "--repair-settings"])
        self.assertEqual(rc, 0, err)
        self.assertIn(".mcp.json", out)
        self._assert_absolute(home, root)
        data = json.loads((home / ".mcp.json").read_text())
        self.assertEqual(data["mcpServers"]["other"],
                         {"command": "elsewhere"})
        self.assertEqual(data["mcpServers"]["cousin"]["env"]["EXTRA"],
                         "kept")
        first = (home / ".mcp.json").read_text()
        rc, _out, err = self._main(["testa", "--root", str(root),
                                    "--repair-settings"])
        self.assertEqual(rc, 0, err)
        self.assertEqual((home / ".mcp.json").read_text(), first)

    def test_repair_settings_writes_a_missing_mcp_json(self):
        root = self._framework_root()
        home = self._create(root)["home"]
        (home / ".mcp.json").unlink()
        rc, _out, err = self._main(["testa", "--root", str(root),
                                    "--repair-settings"])
        self.assertEqual(rc, 0, err)
        self._assert_absolute(home, root)

    def test_a_non_object_mcp_json_is_refused_not_clobbered(self):
        root = self._framework_root()
        home = self._create(root)["home"]
        (home / ".mcp.json").write_text("[1, 2]")
        rc, _out, err = self._main(["testa", "--root", str(root),
                                    "--repair-settings"])
        self.assertEqual(rc, 2)
        self.assertIn(".mcp.json", err)
        self.assertEqual((home / ".mcp.json").read_text(), "[1, 2]")


if __name__ == "__main__":
    unittest.main()
