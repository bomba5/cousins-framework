"""MCP adapter: registry, schemas, argv assembly, the guards, approval.

Spec: docs/mcp-spec.md. The core is stdlib-only and tested here without
the SDK; the SDK is imported inside serve() only, and no test below
needs the extra installed. The load-bearing test is the hostile one:
content carrying backticks, $(...), a newline and an unbalanced quote
must reach the CLI's argv byte-identical, because "no shell on the
path" is the whole reason the adapter exists.

Bare command names are NEVER resolved through PATH in this suite: a
machine may carry another install of these CLIs first on PATH, and a
test that reached it would test the wrong framework. Every guard that
runs a shipped CLI resolves it through pyproject's scripts table to a
`python -c` invocation of this checkout.
"""
import contextlib
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

from cousin_lib import config as config_mod
from cousin_lib import mcp_server
from cousin_lib.mcp_server import RegistryError, ToolError

ROOT = pathlib.Path(__file__).resolve().parents[1]
SHIPPED = ROOT / "config" / "mcp-registry.toml.example"


def _write(tmp, text, name="registry.toml"):
    p = pathlib.Path(tmp) / name
    p.write_text(text)
    return p


def _script_argv(cli_name):
    """`cousin-x` -> an argv that runs its console-script target from
    this checkout, via pyproject's scripts table (no install, no PATH)."""
    scripts = tomllib.loads(
        (ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    module, func = scripts[cli_name].split(":")
    code = ("import sys; from %s import %s; sys.exit(%s(sys.argv[1:]))"
            % (module, func, func))
    return [sys.executable, "-c", code]


def _resolved_registry(root):
    """The shipped registry with every `cousin-x` command rewritten to
    a `python -c` invocation of this checkout."""
    text = SHIPPED.read_text()

    def repl(m):
        return "command = %s" % json.dumps(_script_argv(m.group(1)))
    text = re.sub(r'command = "(cousin-[a-z-]+)"', repl, text)
    text = re.sub(r'peers_from = \["cousin-chat", "list"\]',
                  "peers_from = %s"
                  % json.dumps(_script_argv("cousin-chat") + ["list"]),
                  text)
    path = pathlib.Path(root) / "resolved-registry.toml"
    path.write_text(text)
    return path


class RegistryCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def test_shipped_registry_loads_with_defaults_filled(self):
        reg = mcp_server.load_registry(SHIPPED)
        self.assertEqual(reg["ceiling"], 12)
        self.assertEqual(reg["timeout"], 120)
        self.assertEqual(reg["max_output"], mcp_server.DEFAULT_MAX_OUTPUT)
        self.assertEqual(set(reg["tools"]),
                         {"memory", "send", "job", "schedule"})
        self.assertTrue(reg["tools"]["memory"]["enabled"])
        self.assertEqual(reg["tools"]["memory"]["kind"], "command")

    def test_tool_without_command_is_rejected(self):
        p = _write(self.tmp, '[tools.x]\ndescription = "d"\n'
                             '[tools.x.commands.a]\nargv = ["a"]\n')
        with self.assertRaises(RegistryError) as cm:
            mcp_server.load_registry(p)
        self.assertIn("x", str(cm.exception))
        self.assertIn("command", str(cm.exception))

    def test_partial_placeholder_is_rejected(self):
        p = _write(self.tmp, '[tools.x]\ncommand = "c"\ndescription = "d"\n'
                             '[tools.x.commands.a]\nargv = ["a", "pre{q}"]\n')
        with self.assertRaises(RegistryError) as cm:
            mcp_server.load_registry(p)
        self.assertIn("pre{q}", str(cm.exception))

    def test_placeholder_must_name_a_declared_property(self):
        p = _write(self.tmp, '[tools.x]\ncommand = "c"\ndescription = "d"\n'
                             '[tools.x.commands.a]\nargv = ["a", "{nope}"]\n')
        with self.assertRaises(RegistryError) as cm:
            mcp_server.load_registry(p)
        self.assertIn("nope", str(cm.exception))

    def test_non_string_argv_element_is_rejected(self):
        p = _write(self.tmp, '[tools.x]\ncommand = "c"\ndescription = "d"\n'
                             '[tools.x.commands.a]\nargv = ["a", 3]\n')
        with self.assertRaises(RegistryError) as cm:
            mcp_server.load_registry(p)
        self.assertIn("not a string", str(cm.exception))

    def test_option_naming_an_undeclared_property_is_rejected(self):
        p = _write(self.tmp, '[tools.x]\ncommand = "c"\ndescription = "d"\n'
                             '[tools.x.commands.a]\nargv = ["a"]\n'
                             'options = { ghost = "--g" }\n')
        with self.assertRaises(RegistryError) as cm:
            mcp_server.load_registry(p)
        self.assertIn("ghost", str(cm.exception))

    def test_more_tools_than_the_ceiling_is_rejected(self):
        body = "ceiling = 1\n"
        for n in ("a", "b"):
            body += ('[tools.%s]\ncommand = "c"\ndescription = "d"\n'
                     '[tools.%s.commands.x]\nargv = ["x"]\n' % (n, n))
        with self.assertRaises(RegistryError) as cm:
            mcp_server.load_registry(_write(self.tmp, body))
        self.assertIn("ceiling", str(cm.exception))

    def test_disabled_tools_do_not_count_against_the_ceiling(self):
        body = "ceiling = 1\n"
        body += ('[tools.a]\ncommand = "c"\ndescription = "d"\n'
                 '[tools.a.commands.x]\nargv = ["x"]\n')
        body += ('[tools.b]\ncommand = "c"\ndescription = "d"\n'
                 'enabled = false\n[tools.b.commands.x]\nargv = ["x"]\n')
        reg = mcp_server.load_registry(_write(self.tmp, body))
        self.assertFalse(reg["tools"]["b"]["enabled"])

    def test_unknown_kind_is_rejected(self):
        p = _write(self.tmp, '[tools.x]\nkind = "magic"\ncommand = "c"\n'
                             '[tools.x.commands.a]\nargv = ["a"]\n')
        with self.assertRaises(RegistryError):
            mcp_server.load_registry(p)

    def test_unreadable_or_unparsable_file_is_a_registry_error(self):
        with self.assertRaises(RegistryError):
            mcp_server.load_registry(pathlib.Path(self.tmp) / "absent.toml")
        with self.assertRaises(RegistryError):
            mcp_server.load_registry(_write(self.tmp, "not = [toml"))

    def test_default_path_is_the_home_registry_then_the_shipped_default(self):
        home = pathlib.Path(self.tmp) / "home"
        home.mkdir()
        root = pathlib.Path(self.tmp) / "root"
        (root / "config").mkdir(parents=True)
        (root / "config" / "mcp-registry.toml.example").write_text(
            SHIPPED.read_text())
        env = {"COUSIN_HOME": str(home), "FRAMEWORK_ROOT": str(root)}
        self.assertEqual(mcp_server.default_registry_path(env),
                         root / "config" / "mcp-registry.toml.example")
        (root / "config" / "mcp-registry.toml").write_text(SHIPPED.read_text())
        self.assertEqual(mcp_server.default_registry_path(env),
                         root / "config" / "mcp-registry.toml")
        (home / "mcp-registry.toml").write_text(SHIPPED.read_text())
        self.assertEqual(mcp_server.default_registry_path(env),
                         home / "mcp-registry.toml")


class SchemaCase(unittest.TestCase):
    def setUp(self):
        self.reg = mcp_server.load_registry(SHIPPED)

    def test_command_tool_schema_has_command_enum_and_property_union(self):
        schema = mcp_server.build_schema("memory", self.reg["tools"]["memory"])
        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["required"], ["command"])
        self.assertEqual(sorted(schema["properties"]["command"]["enum"]),
                         ["activity", "decide", "recall", "search"])
        self.assertEqual(schema["properties"]["query"]["type"], "string")
        self.assertIn("search", schema["properties"]["query"]["description"])
        self.assertFalse(schema["additionalProperties"])

    def test_send_tool_schema_requires_to_and_text_and_has_no_command(self):
        schema = mcp_server.build_schema("send", self.reg["tools"]["send"])
        self.assertEqual(sorted(schema["required"]), ["text", "to"])
        self.assertNotIn("command", schema["properties"])

    def test_list_tools_skips_disabled_and_carries_schema(self):
        self.reg["tools"]["memory"]["enabled"] = False
        self.assertNotIn("memory",
                         [t["name"] for t in mcp_server.list_tools(self.reg)])
        self.reg["tools"]["memory"]["enabled"] = True
        tools = {t["name"]: t for t in mcp_server.list_tools(self.reg)}
        self.assertIn("memory", tools)
        self.assertIn("inputSchema", tools["memory"])

    def test_array_property_maps_to_items(self):
        tool = {"kind": "command", "command": "c", "description": "d",
                "properties": {"tag": {"type": "array", "items": "string"}},
                "commands": {"a": {"argv": ["a"], "options": {"tag": "--tag"}}}}
        mcp_server._validate_tool("t", tool)
        schema = mcp_server.build_schema("t", tool)
        self.assertEqual(schema["properties"]["tag"],
                         {"type": "array", "items": {"type": "string"},
                          "description": "(a)"})

    def test_opaque_types_fall_back_to_string(self):
        tool = {"kind": "command", "command": "c", "description": "d",
                "properties": {"when": {"type": "duration"}},
                "commands": {"a": {"argv": ["a", "{when}"]}}}
        mcp_server._validate_tool("t", tool)
        schema = mcp_server.build_schema("t", tool)
        self.assertEqual(schema["properties"]["when"]["type"], "string")


class CallAssemblyCase(unittest.TestCase):
    def setUp(self):
        self.tool = mcp_server.load_registry(SHIPPED)["tools"]["memory"]

    def test_placeholders_become_whole_elements_never_joined(self):
        argv, stdin = mcp_server.build_call(
            self.tool, "search", {"query": "a b `c`", "top": 3},
            resolve=lambda name: name)
        self.assertEqual(argv, ["cousin-memory", "search", "a b `c`",
                                "--top", "3"])
        self.assertIsNone(stdin)

    def test_missing_required_placeholder_is_a_tool_error(self):
        with self.assertRaises(ToolError) as cm:
            mcp_server.build_call(self.tool, "decide", {"topic": "t"},
                                  resolve=lambda name: name)
        self.assertIn("decision", str(cm.exception))

    def test_optional_placeholder_drops_when_absent(self):
        argv, _ = mcp_server.build_call(self.tool, "recall", {},
                                        resolve=lambda name: name)
        self.assertEqual(argv, ["cousin-memory", "recall"])

    def test_option_semantics(self):
        tool = {"kind": "command", "command": "c", "description": "d",
                "properties": {"flag": {"type": "boolean"},
                               "many": {"type": "array"},
                               "n": {"type": "integer"}},
                "commands": {"a": {"argv": ["a"],
                                   "options": {"flag": "--flag",
                                               "many": "--m", "n": "--n"}}}}
        mcp_server._validate_tool("t", tool)
        argv, _ = mcp_server.build_call(
            tool, "a", {"flag": True, "many": ["x", "y"], "n": 0},
            resolve=lambda name: name)
        self.assertEqual(argv, ["c", "a", "--flag", "--m", "x", "--m", "y",
                                "--n", "0"])
        argv, _ = mcp_server.build_call(tool, "a", {"flag": False},
                                        resolve=lambda name: name)
        self.assertEqual(argv, ["c", "a"])

    def test_stdin_join_with_separator(self):
        tool = {"kind": "command", "command": "c", "description": "d",
                "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
                "commands": {"x": {"argv": ["x", "--stdin"],
                                   "stdin": ["a", "b"], "stdin_sep": "---"}}}
        mcp_server._validate_tool("t", tool)
        argv, stdin = mcp_server.build_call(
            tool, "x", {"a": "one\nline", "b": "two"},
            resolve=lambda name: name)
        self.assertEqual(argv, ["c", "x", "--stdin"])
        self.assertEqual(stdin, b"one\nline\n---\ntwo\n")

    def test_single_stdin_property_is_passed_verbatim(self):
        tool = {"kind": "command", "command": "c", "description": "d",
                "properties": {"a": {"type": "string"}},
                "commands": {"x": {"argv": [], "stdin": ["a"]}}}
        mcp_server._validate_tool("t", tool)
        _, stdin = mcp_server.build_call(
            tool, "x", {"a": "exact $(x) `y` \"z"}, resolve=lambda name: name)
        self.assertEqual(stdin, b"exact $(x) `y` \"z")

    def test_missing_stdin_property_is_a_tool_error(self):
        tool = {"kind": "command", "command": "c", "description": "d",
                "properties": {"a": {"type": "string"}},
                "commands": {"x": {"argv": [], "stdin": ["a"]}}}
        mcp_server._validate_tool("t", tool)
        with self.assertRaises(ToolError):
            mcp_server.build_call(tool, "x", {}, resolve=lambda name: name)

    def test_unknown_command_is_a_tool_error(self):
        with self.assertRaises(ToolError):
            mcp_server.build_call(self.tool, "nope", {},
                                  resolve=lambda name: name)

    def test_a_value_outside_a_declared_enum_is_refused(self):
        # The enum is enforced by the adapter, not only advertised: a
        # value the registry leaves out on purpose never reaches argv.
        with self.assertRaises(ToolError) as cm:
            mcp_server.build_call(
                self.tool, "search", {"query": "q", "collection": "bogus"},
                resolve=lambda name: name)
        text = str(cm.exception)
        self.assertIn("bogus", text)
        self.assertIn("memory, notes, harness", text)

    def test_a_value_inside_the_enum_passes(self):
        argv, _ = mcp_server.build_call(
            self.tool, "search", {"query": "q", "collection": "notes"},
            resolve=lambda name: name)
        self.assertEqual(argv[-2:], ["--collection", "notes"])

    def test_array_elements_are_checked_against_an_item_enum(self):
        tool = {"kind": "command", "command": "c", "description": "d",
                "properties": {"k": {"type": "array", "enum": ["a", "b"]}},
                "commands": {"x": {"argv": ["{k}"]}}}
        mcp_server._validate_tool("t", tool)
        with self.assertRaises(ToolError):
            mcp_server.build_call(tool, "x", {"k": ["a", "z"]},
                                  resolve=lambda name: name)


class ResolveCase(unittest.TestCase):
    """A bare name resolves beside the running interpreter first, then
    on PATH; an absolute path is taken as given. The order matters on a
    machine carrying two installs: the one this interpreter belongs to
    wins over whatever is first on PATH."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        self.beside = self.tmp / "venv-bin"
        self.onpath = self.tmp / "path-bin"
        self.beside.mkdir()
        self.onpath.mkdir()

    def _exe(self, d, name):
        p = d / name
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)
        return p

    def test_beside_the_interpreter_wins_over_path(self):
        beside = self._exe(self.beside, "cousin-x")
        self._exe(self.onpath, "cousin-x")
        with mock.patch.object(sys, "executable", str(self.beside / "python")), \
                mock.patch.dict(os.environ, {"PATH": str(self.onpath)}):
            self.assertEqual(mcp_server.resolve_command("cousin-x"), str(beside))

    def test_path_is_the_fallback(self):
        onpath = self._exe(self.onpath, "cousin-x")
        with mock.patch.object(sys, "executable", str(self.beside / "python")), \
                mock.patch.dict(os.environ, {"PATH": str(self.onpath)}):
            self.assertEqual(mcp_server.resolve_command("cousin-x"), str(onpath))

    def test_absolute_path_is_taken_as_given(self):
        self.assertEqual(mcp_server.resolve_command("/opt/x/bin/tool"),
                         "/opt/x/bin/tool")

    def test_unresolved_name_is_returned_unchanged_and_reported(self):
        with mock.patch.object(sys, "executable", str(self.beside / "python")), \
                mock.patch.dict(os.environ, {"PATH": str(self.onpath)}):
            self.assertEqual(mcp_server.resolve_command("cousin-nope"),
                             "cousin-nope")
            self.assertEqual(mcp_server.unresolved(["cousin-nope"]),
                             ["cousin-nope"])


FAKE_CLI = r"""
import json, sys
out = {"argv": sys.argv[1:], "stdin": sys.stdin.read()}
open(sys.argv[0] + ".log", "w").write(json.dumps(out))
if "--fail" in sys.argv:
    sys.stderr.write("boom\n")
    sys.exit(3)
if "--hang" in sys.argv:
    import time
    time.sleep(5)
if "--big" in sys.argv:
    sys.stdout.write("x" * 5000)
    sys.exit(0)
print("ok")
"""

HOSTILE = "a `b` $(rm -rf /) \"unbalanced\nnewline ${HOME} ; & | > x 'q"


class RunCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        self.fake = self.tmp / "fake_cli.py"
        self.fake.write_text(FAKE_CLI)
        cmd = [sys.executable, str(self.fake)]
        self.tool = {"kind": "command", "command": cmd, "description": "d",
                     "properties": {"p": {"type": "string"},
                                    "body": {"type": "string"},
                                    "fail": {"type": "boolean"},
                                    "hang": {"type": "boolean"},
                                    "big": {"type": "boolean"}},
                     "commands": {"echo": {"argv": ["echo", "{p}"],
                                           "options": {"fail": "--fail",
                                                       "hang": "--hang",
                                                       "big": "--big"},
                                           "stdin": ["body"]}}}
        mcp_server._validate_tool("fake", self.tool)
        self.reg = {"ceiling": 12, "timeout": 2, "max_output": 1000,
                    "tools": {"fake": self.tool}}

    def _log(self):
        return json.loads((self.tmp / "fake_cli.py.log").read_text())

    def test_hostile_content_reaches_argv_and_stdin_byte_identical(self):
        text, is_error = mcp_server.call_tool(
            self.reg, "fake", {"command": "echo", "p": HOSTILE, "body": HOSTILE},
            os.environ)
        self.assertFalse(is_error, text)
        log = self._log()
        self.assertEqual(log["argv"], ["echo", HOSTILE])
        self.assertEqual(log["stdin"], HOSTILE)

    def test_nonzero_exit_is_a_tool_error_carrying_stderr(self):
        text, is_error = mcp_server.call_tool(
            self.reg, "fake",
            {"command": "echo", "p": "x", "body": "", "fail": True}, os.environ)
        self.assertTrue(is_error)
        self.assertIn("boom", text)
        self.assertIn("exit 3", text)

    def test_timeout_is_a_tool_error_not_a_hang(self):
        text, is_error = mcp_server.call_tool(
            self.reg, "fake",
            {"command": "echo", "p": "x", "body": "", "hang": True}, os.environ)
        self.assertTrue(is_error)
        self.assertIn("timed out", text)

    def test_output_is_capped_at_the_registry_limit_and_says_so(self):
        text, is_error = mcp_server.call_tool(
            self.reg, "fake",
            {"command": "echo", "p": "x", "body": "", "big": True}, os.environ)
        self.assertFalse(is_error, text)
        self.assertLess(len(text), 1200)
        self.assertIn("truncated", text)
        self.assertIn("5000", text)

    def test_missing_command_property_is_a_tool_error(self):
        text, is_error = mcp_server.call_tool(self.reg, "fake", {"p": "x"},
                                              os.environ)
        self.assertTrue(is_error)
        self.assertIn("command", text)

    def test_unknown_tool_is_a_tool_error(self):
        text, is_error = mcp_server.call_tool(self.reg, "nope", {}, os.environ)
        self.assertTrue(is_error)

    def test_no_stdin_body_means_devnull_not_the_parents_stdin(self):
        tool = dict(self.tool)
        tool["commands"] = {"echo": dict(self.tool["commands"]["echo"], stdin=[])}
        reg = dict(self.reg, tools={"fake": tool})
        text, is_error = mcp_server.call_tool(
            reg, "fake", {"command": "echo", "p": "x"}, os.environ)
        self.assertFalse(is_error, text)
        self.assertEqual(self._log()["stdin"], "")

    def test_unresolvable_command_is_a_tool_error_not_a_crash(self):
        tool = dict(self.tool, command="cousin-does-not-exist-anywhere")
        tool["commands"] = {"echo": {"argv": ["echo"]}}
        mcp_server._validate_tool("fake", tool)
        reg = dict(self.reg, tools={"fake": tool})
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp)}):
            text, is_error = mcp_server.call_tool(
                reg, "fake", {"command": "echo"}, os.environ)
        self.assertTrue(is_error)
        self.assertIn("cannot run", text)

    def test_no_code_path_passes_shell_true(self):
        src = (ROOT / "cousin_lib" / "mcp_server.py").read_text()
        self.assertNotIn("shell=True", src)
        self.assertNotIn("shell=", src)
        self.assertNotIn('" ".join(argv', src)


class SendCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        self.peer_cli = self.tmp / "peer.py"
        self.peer_cli.write_text(FAKE_CLI)
        self.op_cli = self.tmp / "op.py"
        self.op_cli.write_text(FAKE_CLI)
        lister = self.tmp / "list.py"
        lister.write_text(
            'print("wren         port=8100  (self)")\n'
            'print("kestrel      port=8101  ")\n'
            'print("")\n')
        self.tool = {"kind": "send", "description": "d",
                     "operators": ["Operator"], "errors_name_peers": True,
                     "peers_from": [sys.executable, str(lister)],
                     "properties": {"to": {"type": "string"},
                                    "text": {"type": "string"}},
                     "commands": {
                         "peer": {"command": [sys.executable, str(self.peer_cli)],
                                  "argv": ["send", "{to}", "{text}"]},
                         "operator": {"command": [sys.executable, str(self.op_cli)],
                                      "options": {"to": "--user"},
                                      "stdin": ["text"]}}}
        mcp_server._validate_tool("send", self.tool)
        self.reg = {"ceiling": 12, "timeout": 5, "max_output": 16000,
                    "tools": {"send": self.tool}}

    def test_discover_peers_takes_first_token_and_drops_self(self):
        self.assertEqual(mcp_server.discover_peers(self.reg, os.environ),
                         {"kestrel"})

    def test_known_slug_goes_to_the_peer_cli(self):
        peers = mcp_server.discover_peers(self.reg, os.environ)
        text, is_error = mcp_server.call_tool(
            self.reg, "send", {"to": "kestrel", "text": HOSTILE}, os.environ,
            peers)
        self.assertFalse(is_error, text)
        log = json.loads((self.tmp / "peer.py.log").read_text())
        self.assertEqual(log["argv"], ["send", "kestrel", HOSTILE])

    def test_operator_name_goes_to_the_reply_cli_via_stdin(self):
        text, is_error = mcp_server.call_tool(
            self.reg, "send", {"to": "Operator", "text": HOSTILE}, os.environ,
            {"kestrel"})
        self.assertFalse(is_error, text)
        log = json.loads((self.tmp / "op.py.log").read_text())
        self.assertEqual(log["argv"], ["--user", "Operator"])
        self.assertEqual(log["stdin"], HOSTILE)

    def test_unknown_destination_is_an_error_naming_what_is_known(self):
        text, is_error = mcp_server.call_tool(
            self.reg, "send", {"to": "kestrell", "text": "x"}, os.environ,
            {"kestrel"})
        self.assertTrue(is_error)
        self.assertIn("kestrel", text)
        self.assertIn("Operator", text)
        self.assertFalse((self.tmp / "peer.py.log").exists())
        self.assertFalse((self.tmp / "op.py.log").exists())

    def test_no_operator_configured_says_so_before_listing_peers(self):
        self.tool["operators"] = []
        text, is_error = mcp_server.call_tool(
            self.reg, "send", {"to": "Operator", "text": "x"}, os.environ,
            {"kestrel"})
        self.assertTrue(is_error)
        self.assertIn("no operator is configured", text)
        self.assertLess(text.index("no operator is configured"),
                        text.index("kestrel"))

    def test_errors_can_withhold_peer_names_and_give_the_count(self):
        self.tool["errors_name_peers"] = False
        text, is_error = mcp_server.call_tool(
            self.reg, "send", {"to": "nobody", "text": "x"}, os.environ,
            {"kestrel", "wren"})
        self.assertTrue(is_error)
        self.assertNotIn("kestrel", text)
        self.assertNotIn("wren", text)
        self.assertIn("2 known", text)

    def test_missing_to_is_a_tool_error(self):
        text, is_error = mcp_server.call_tool(
            self.reg, "send", {"text": "x"}, os.environ, {"kestrel"})
        self.assertTrue(is_error)
        self.assertIn("to", text)

    def test_send_kind_defaults_fill_on_load(self):
        p = _write(self.tmp,
                   '[tools.send]\nkind = "send"\ndescription = "d"\n'
                   '[tools.send.properties]\nto = { type = "string" }\n'
                   'text = { type = "string" }\n'
                   '[tools.send.commands.peer]\ncommand = "c"\n'
                   'argv = ["{to}", "{text}"]\n'
                   '[tools.send.commands.operator]\ncommand = "r"\n'
                   'options = { to = "--user" }\nstdin = ["text"]\n')
        reg = mcp_server.load_registry(p)
        self.assertEqual(reg["tools"]["send"]["operators"], [])
        self.assertTrue(reg["tools"]["send"]["errors_name_peers"])
        self.assertEqual(reg["tools"]["send"]["peers_from"],
                         ["cousin-chat", "list"])

    def test_send_kind_needs_both_delivery_commands(self):
        p = _write(self.tmp,
                   '[tools.send]\nkind = "send"\ndescription = "d"\n'
                   '[tools.send.properties]\nto = { type = "string" }\n'
                   '[tools.send.commands.peer]\ncommand = "c"\nargv = ["{to}"]\n')
        with self.assertRaises(RegistryError) as cm:
            mcp_server.load_registry(p)
        self.assertIn("operator", str(cm.exception))

    def test_peer_discovery_failure_is_empty_and_reported(self):
        self.tool["peers_from"] = [sys.executable, "-c", "import sys; sys.exit(3)"]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            peers = mcp_server.discover_peers(self.reg, os.environ)
        self.assertEqual(peers, set())
        self.assertIn("peer discovery failed", err.getvalue())


FAKE_JOB_CLI = r"""
import json, os, sys
log = os.environ["FAKE_JOB_CALLS"]
calls = json.loads(open(log).read()) if os.path.exists(log) else []
calls.append(sys.argv[1:])
open(log, "w").write(json.dumps(calls))
if sys.argv[1] == "start":
    if os.environ.get("FAKE_JOB_NO_ID"):
        print(json.dumps({"job_id": None, "log_path": None}))
    else:
        print(json.dumps({"job_id": 7, "log_path": os.environ["FAKE_JOB_LOGPATH"]}))
elif sys.argv[1] == "show":
    print(json.dumps({"id": 7, "status": os.environ.get("FAKE_JOB_STATUS", "running"),
                      "log_path": os.environ["FAKE_JOB_LOGPATH"], "exit_code": None}))
"""


class JobKindCase(unittest.TestCase):
    """A job-kind tool never blocks: gen returns a job handle (or refuses
    when the tracker gave none), status and result poll it, result
    carries the log tail and image bytes only on request."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        job_cli = self.tmp / "job.py"
        job_cli.write_text(FAKE_JOB_CLI)
        self.calls = self.tmp / "calls.json"
        self.joblog = self.tmp / "job-7.log"
        self.env = dict(os.environ, FAKE_JOB_CALLS=str(self.calls),
                        FAKE_JOB_LOGPATH=str(self.joblog))
        self.tool = {"kind": "job", "command": "/opt/fake/bin/cousin-image-fake",
                     "job_command": [sys.executable, str(job_cli)],
                     "description": "d",
                     "properties": {"prompt": {"type": "string"},
                                    "id": {"type": "integer"},
                                    "return_image": {"type": "boolean",
                                                     "optional": True}},
                     "commands": {"gen": {"argv": ["gen", "{prompt}"]}}}
        mcp_server._validate_tool("image", self.tool)
        self.reg = {"ceiling": 12, "timeout": 5, "max_output": 16000,
                    "tools": {"image": self.tool}}

    def _calls(self):
        return json.loads(self.calls.read_text())

    def test_job_kind_schema_offers_gen_status_result(self):
        schema = mcp_server.build_schema("image", self.tool)
        self.assertEqual(schema["properties"]["command"]["enum"],
                         ["gen", "result", "status"])

    def test_job_kind_needs_gen_and_an_id_property(self):
        bad = {"kind": "job", "command": "c", "properties": {},
               "commands": {"gen": {"argv": ["gen"]}}}
        with self.assertRaises(RegistryError):
            mcp_server._validate_tool("x", bad)

    def test_gen_starts_a_tracked_job_and_returns_its_handle(self):
        text, is_error = mcp_server.call_tool(
            self.reg, "image", {"command": "gen", "prompt": HOSTILE}, self.env)
        self.assertFalse(is_error, text)
        self.assertEqual(json.loads(text)["job_id"], 7)
        call = self._calls()[0]
        self.assertEqual(call[:2], ["start", "shell"])
        self.assertTrue(call[2].startswith("image gen"))
        self.assertEqual(call[3:5], ["--json", "--"])
        self.assertEqual(call[5:], ["/opt/fake/bin/cousin-image-fake", "gen",
                                    HOSTILE])

    def test_gen_refuses_when_the_tracker_returns_no_id(self):
        env = dict(self.env, FAKE_JOB_NO_ID="1")
        text, is_error = mcp_server.call_tool(
            self.reg, "image", {"command": "gen", "prompt": "x"}, env)
        self.assertTrue(is_error)
        self.assertIn("from a shell", text)
        self.assertIn("cousin-image-fake", text)

    def test_status_shows_the_job(self):
        text, is_error = mcp_server.call_tool(
            self.reg, "image", {"command": "status", "id": 7}, self.env)
        self.assertFalse(is_error, text)
        self.assertEqual(self._calls()[-1], ["show", "7", "--json"])
        self.assertEqual(json.loads(text)["status"], "running")

    def test_result_before_done_is_a_state_not_an_error(self):
        text, is_error = mcp_server.call_tool(
            self.reg, "image", {"command": "result", "id": 7}, self.env)
        self.assertFalse(is_error, text)
        self.assertIn("running", text)

    def test_result_after_done_returns_the_tail_and_the_image_only_on_request(self):
        asset = self.tmp / "asset.png"
        asset.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        self.joblog.write_text("provider said hello\n%s\n" % asset)
        env = dict(self.env, FAKE_JOB_STATUS="done")
        text, is_error, attachments = mcp_server.call_tool_rich(
            self.reg, "image", {"command": "result", "id": 7}, env)
        self.assertFalse(is_error, text)
        self.assertIn("job 7 done", text)
        self.assertIn("provider said hello", text)
        self.assertTrue(text.rstrip().endswith(str(asset)))
        self.assertEqual(attachments, [])
        text, is_error, attachments = mcp_server.call_tool_rich(
            self.reg, "image", {"command": "result", "id": 7,
                                "return_image": True}, env)
        self.assertFalse(is_error, text)
        self.assertEqual(attachments, [{"path": str(asset), "mime": "image/png"}])

    def test_result_of_a_failed_job_is_an_error_carrying_the_log(self):
        self.joblog.write_text("provider refused: quota\n")
        env = dict(self.env, FAKE_JOB_STATUS="failed")
        text, is_error = mcp_server.call_tool(
            self.reg, "image", {"command": "result", "id": 7}, env)
        self.assertTrue(is_error)
        self.assertIn("quota", text)

    def test_status_without_id_is_a_tool_error(self):
        text, is_error = mcp_server.call_tool(
            self.reg, "image", {"command": "status"}, self.env)
        self.assertTrue(is_error)
        self.assertIn("id", text)


class VersionRecordCase(unittest.TestCase):
    def test_record_is_a_bounded_map_of_version_to_last_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = mcp_server.record_client_version(
                tmp, "2025-11-25", now="2026-09-09T12:00:00+02:00")
            mcp_server.record_client_version(
                tmp, "2025-11-25", now="2026-09-09T13:00:00+02:00")
            mcp_server.record_client_version(
                tmp, "2026-07-28", now="2026-09-10T09:00:00+02:00")
            data = json.loads(p.read_text())
            self.assertEqual(data, {"2025-11-25": "2026-09-09T13:00:00+02:00",
                                    "2026-07-28": "2026-09-10T09:00:00+02:00"})
            self.assertEqual(p, pathlib.Path(tmp) / "data" / "mcp-client.json")

    def test_corrupt_record_is_reset_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / "data").mkdir()
            (pathlib.Path(tmp) / "data" / "mcp-client.json").write_text("[1,")
            p = mcp_server.record_client_version(tmp, "v", now="t")
            self.assertEqual(json.loads(p.read_text()), {"v": "t"})


def _main(argv, env=None):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env or {}, clear=env is not None), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = mcp_server.mcp_main(argv)
    return rc, out.getvalue(), err.getvalue()


class CliCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        self.fake = self.tmp / "fake.py"
        self.fake.write_text(FAKE_CLI)
        self.registry = _write(
            self.tmp,
            '[tools.t]\ncommand = %s\ndescription = "d"\n'
            '[tools.t.properties]\np = { type = "string" }\n'
            '[tools.t.commands.echo]\nargv = ["echo", "{p}"]\n'
            % json.dumps([sys.executable, str(self.fake)]))

    def test_list_tools_prints_schemas_without_the_sdk(self):
        rc, out, _ = _main(["--registry", str(SHIPPED), "--list-tools"])
        self.assertEqual(rc, 0)
        tools = json.loads(out)
        self.assertEqual(sorted(t["name"] for t in tools),
                         ["job", "memory", "schedule", "send"])

    def test_selftest_prints_every_tool_and_its_commands_without_the_sdk(self):
        with mock.patch.dict(sys.modules, {"mcp": None}):
            rc, out, err = _main(["--registry", str(self.registry), "--selftest"])
        self.assertEqual(rc, 0, err)
        self.assertIn("t", out)
        self.assertIn("echo", out)
        self.assertIn("1 tool", out)
        self.assertIn("cousin-framework[mcp]", out + err)
        self.assertIn("selftest ok", out)

    def test_selftest_fails_when_a_command_cannot_be_resolved(self):
        p = _write(self.tmp, '[tools.t]\ncommand = "cousin-nowhere-at-all"\n'
                             'description = "d"\n'
                             '[tools.t.commands.a]\nargv = ["a"]\n',
                   name="unresolved.toml")
        env = dict(os.environ, PATH=str(self.tmp))
        rc, out, err = _main(["--registry", str(p), "--selftest"], env=env)
        self.assertEqual(rc, 1)
        self.assertIn("cousin-nowhere-at-all", out + err)
        self.assertNotIn("selftest ok", out)

    def test_versions_without_sdk_says_how_to_get_it(self):
        with mock.patch.dict(sys.modules, {"mcp": None, "mcp.shared": None,
                                           "mcp.shared.version": None}):
            rc, out, err = _main(["--registry", str(self.registry), "--versions"])
        self.assertEqual(rc, 2)
        self.assertIn('pip install "cousin-framework[mcp]"', err)

    def test_serving_without_the_sdk_exits_2_with_the_remediation(self):
        with mock.patch.dict(sys.modules, {"mcp": None, "anyio": None}):
            rc, out, err = _main(["--registry", str(self.registry)])
        self.assertEqual(rc, 2)
        self.assertIn('pip install "cousin-framework[mcp]"', err)

    def test_bad_registry_is_exit_2_with_the_reason(self):
        p = _write(self.tmp, '[tools.x]\ndescription = "d"\n'
                             '[tools.x.commands.a]\nargv = ["a"]\n',
                   name="bad.toml")
        rc, _, err = _main(["--registry", str(p), "--list-tools"])
        self.assertEqual(rc, 2)
        self.assertIn("registry", err)

    def test_no_registry_anywhere_is_exit_2_naming_the_flag(self):
        rc, _, err = _main(["--list-tools"], env={"PATH": os.environ["PATH"]})
        self.assertEqual(rc, 2)
        self.assertIn("--registry", err)

    def test_call_runs_one_tool_from_the_shell(self):
        rc, out, _ = _main(["--registry", str(self.registry), "--call", "t",
                            json.dumps({"command": "echo", "p": HOSTILE})])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "ok")
        self.assertEqual(
            json.loads((self.tmp / "fake.py.log").read_text())["argv"],
            ["echo", HOSTILE])

    def test_call_with_non_json_arguments_is_exit_2(self):
        rc, _, err = _main(["--registry", str(self.registry), "--call", "t",
                            "{not json"])
        self.assertEqual(rc, 2)
        self.assertIn("JSON", err)

    def test_module_imports_without_the_sdk(self):
        src = (ROOT / "cousin_lib" / "mcp_server.py").read_text()
        top_level = [l for l in src.splitlines()
                     if l.startswith("import mcp") or l.startswith("from mcp")
                     or l.startswith("import anyio")]
        self.assertEqual(top_level, [])


class ApproveCase(unittest.TestCase):
    """`cousin-mcp approve <slug>` edits exactly the harness settings
    file config/harness.toml names, and nothing when it names none."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "testa"
        (self.home / "data").mkdir(parents=True)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "testa"\n[chat]\nport = 8100\n')
        (self.home / ".mcp.json").write_text("{}")
        self.settings = self.root / "harness-settings.json"
        (self.root / "config").mkdir()

    def _seam(self):
        (self.root / "config" / "harness.toml").write_text(
            'settings_file = "%s"\n' % self.settings)

    def test_harness_config_reads_settings_file(self):
        self._seam()
        cfg = config_mod.harness_config(self.root)
        self.assertEqual(cfg["settings_file"], str(self.settings))

    def test_harness_config_without_the_key_is_none(self):
        (self.root / "config" / "harness.toml").write_text("")
        self.assertIsNone(config_mod.harness_config(self.root)["settings_file"])

    def test_approve_adds_trust_and_the_server_and_keeps_the_rest(self):
        self._seam()
        self.settings.write_text(json.dumps({
            "theme": "dark",
            "projects": {"/elsewhere": {"enabledMcpjsonServers": ["other"]},
                         str(self.home): {"disabledMcpjsonServers": ["cousin"],
                                          "custom": 1}}}))
        rc, out, err = _main(["approve", "testa", "--root", str(self.root)])
        self.assertEqual(rc, 0, err)
        data = json.loads(self.settings.read_text())
        proj = data["projects"][str(self.home)]
        self.assertIs(proj["hasTrustDialogAccepted"], True)
        self.assertEqual(proj["enabledMcpjsonServers"], ["cousin"])
        self.assertNotIn("cousin", proj["disabledMcpjsonServers"])
        self.assertEqual(proj["custom"], 1)
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["projects"]["/elsewhere"],
                         {"enabledMcpjsonServers": ["other"]})
        self.assertIn(str(self.settings), out)

    def test_approve_is_idempotent(self):
        self._seam()
        self.settings.write_text("{}")
        for _ in range(2):
            rc, _, err = _main(["approve", "testa", "--root", str(self.root)])
            self.assertEqual(rc, 0, err)
        proj = json.loads(self.settings.read_text())["projects"][str(self.home)]
        self.assertEqual(proj["enabledMcpjsonServers"], ["cousin"])

    def test_approve_refuses_without_the_seam_and_touches_nothing(self):
        (self.root / "config" / "harness.toml").write_text("")
        rc, _, err = _main(["approve", "testa", "--root", str(self.root)])
        self.assertEqual(rc, 2)
        self.assertIn("settings_file", err)
        self.assertIn("enabledMcpjsonServers", err)
        self.assertFalse(self.settings.exists())

    def test_approve_refuses_a_settings_file_that_does_not_exist(self):
        self._seam()
        rc, _, err = _main(["approve", "testa", "--root", str(self.root)])
        self.assertEqual(rc, 2)
        self.assertIn(str(self.settings), err)
        self.assertFalse(self.settings.exists())

    def test_approve_refuses_an_unknown_slug_and_an_unprovisioned_home(self):
        self._seam()
        self.settings.write_text("{}")
        rc, _, err = _main(["approve", "nobody", "--root", str(self.root)])
        self.assertEqual(rc, 2)
        self.assertIn("nobody", err)
        (self.home / ".mcp.json").unlink()
        rc, _, err = _main(["approve", "testa", "--root", str(self.root)])
        self.assertEqual(rc, 2)
        self.assertIn(".mcp.json", err)
        self.assertEqual(self.settings.read_text(), "{}")

    def test_approve_refuses_an_unparsable_settings_file(self):
        self._seam()
        self.settings.write_text("{broken")
        rc, _, err = _main(["approve", "testa", "--root", str(self.root)])
        self.assertEqual(rc, 2)
        self.assertEqual(self.settings.read_text(), "{broken")


def _help_text(cli_name, subcommand):
    argv = _script_argv(cli_name) + ([subcommand] if subcommand else []) + ["--help"]
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    proc = subprocess.run(argv, capture_output=True, text=True, env=env,
                          timeout=60)
    return proc.stdout + proc.stderr


class ShippedRegistryGuards(unittest.TestCase):
    """The shipped registry against the shipped CLIs: every declared
    flag exists, the count is under the ceiling, no operator is named,
    every command is a bare console-script name that pyproject declares."""

    def setUp(self):
        self.reg = mcp_server.load_registry(SHIPPED)
        self.scripts = tomllib.loads(
            (ROOT / "pyproject.toml").read_text())["project"]["scripts"]

    def test_tool_count_is_under_the_ceiling(self):
        self.assertLessEqual(len(self.reg["tools"]), self.reg["ceiling"])

    def test_every_command_is_a_declared_console_script_by_bare_name(self):
        for name, tool in self.reg["tools"].items():
            for cmd_name, cmd in tool["commands"].items():
                first = cmd["command"][0]
                self.assertNotIn("/", first, "%s.%s names a path" % (name, cmd_name))
                self.assertIn(first, self.scripts,
                              "%s.%s: %s is not a declared script" % (name, cmd_name, first))
            if tool["kind"] == "send":
                self.assertIn(tool["peers_from"][0], self.scripts)

    def test_every_declared_flag_appears_in_the_cli_help(self):
        missing = []
        for name, tool in self.reg["tools"].items():
            for cmd_name, cmd in tool["commands"].items():
                cli = cmd["command"][0]
                first = cmd["argv"][0] if cmd["argv"] else ""
                literal = first if first and not first.startswith(("{", "-")) else None
                text = _help_text(cli, literal)
                flags = (list(cmd["options"].values())
                         + [e for e in cmd["argv"] if e.startswith("--")])
                for flag in flags:
                    if flag not in text:
                        missing.append("%s.%s: %s not in `%s %s --help`"
                                       % (name, cmd_name, flag, cli, literal or ""))
        self.assertEqual(missing, [])

    def test_default_registry_names_no_operator(self):
        self.assertEqual(self.reg["tools"]["send"]["operators"], [])

    def test_send_kind_commands_resolve_to_the_two_delivery_clis(self):
        cmds = self.reg["tools"]["send"]["commands"]
        self.assertEqual(cmds["peer"]["command"], ["cousin-chat"])
        self.assertEqual(cmds["operator"]["command"], ["cousin-reply"])

    def test_every_schema_builds(self):
        for tool in mcp_server.list_tools(self.reg):
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_job_start_shell_is_refused_before_anything_runs(self):
        # `job start` takes no command, so a shell row started here
        # would stay "running" forever. It is refused, with the two
        # paths that do launch and close a shell job.
        with mock.patch.object(mcp_server, "run_call",
                               side_effect=AssertionError("ran")):
            text, is_error = mcp_server.call_tool(
                self.reg, "job", {"command": "start", "kind": "shell",
                                  "title": "t"}, {})
        self.assertTrue(is_error)
        self.assertIn("shell", text)
        self.assertIn("run_in_background", text)
        self.assertIn("cousin-job start shell", text)


class ParityCase(unittest.TestCase):
    """The tool and the CLI against the same home: the same effect."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "testa"
        (self.home / "memory").mkdir(parents=True)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "testa"\n[chat]\nport = 8100\n')
        self.env = dict(os.environ, FRAMEWORK_ROOT=str(self.root),
                        COUSIN_HOME=str(self.home), PYTHONPATH=str(ROOT))
        self.reg = mcp_server.load_registry(_resolved_registry(self.root))

    def _cli(self, cli_name, *argv):
        proc = subprocess.run(_script_argv(cli_name) + list(argv), env=self.env,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def _decisions(self):
        path = self.home / "data" / "decisions.jsonl"
        rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        for r in rows:
            r.pop("timestamp", None)
            r.pop("id", None)
        path.unlink()
        return rows

    def test_decide_via_tool_equals_decide_via_cli(self):
        text, is_error = mcp_server.call_tool(
            self.reg, "memory",
            {"command": "decide", "topic": "T", "decision": HOSTILE,
             "reasoning": "R"}, self.env)
        self.assertFalse(is_error, text)
        via_tool = self._decisions()
        self._cli("cousin-memory", "decide", "T", HOSTILE, "R")
        via_cli = self._decisions()
        self.assertEqual(via_tool, via_cli)
        self.assertEqual(via_tool[0]["decision"], HOSTILE)

    def test_search_via_tool_equals_search_via_cli(self):
        (self.home / "memory" / "upkeep.md").write_text(
            "The espresso machine needs descaling every 200 shots.\n")
        text, is_error = mcp_server.call_tool(
            self.reg, "memory", {"command": "search", "query": "descaling"},
            self.env)
        self.assertFalse(is_error, text)
        self.assertEqual(text, self._cli("cousin-memory", "search", "descaling"))
        self.assertIn("descaling", text)

    def test_job_start_via_tool_equals_cli(self):
        text, is_error = mcp_server.call_tool(
            self.reg, "job", {"command": "start", "kind": "other",
                              "title": HOSTILE}, self.env)
        self.assertFalse(is_error, text)
        shown, is_error = mcp_server.call_tool(
            self.reg, "job", {"command": "show", "id": int(text.strip()),
                              "json": True}, self.env)
        self.assertFalse(is_error, shown)
        self.assertEqual(json.loads(shown)["title"], HOSTILE)

    def test_send_discovers_peers_through_the_shipped_list_command(self):
        (self.root / "cousins" / "kestrel").mkdir()
        (self.root / "cousins" / "kestrel" / "cousin.toml").write_text(
            '[cousin]\nslug = "kestrel"\n[chat]\nport = 8101\n')
        self.assertEqual(mcp_server.discover_peers(self.reg, self.env),
                         {"kestrel"})


if __name__ == "__main__":
    unittest.main()
