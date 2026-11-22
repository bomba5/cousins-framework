"""served.served lists a real MCP server over stdio: a reworded tool and a
new parameter both move its digest (the CLI's own status shows neither)."""
import asyncio
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

from cousin_lib.runner import served

SERVER = '''
import sys
from mcp.server.fastmcp import FastMCP
m = FastMCP("probe")
import os
WORDS, EXTRA = sys.argv[1] + os.environ.get("PROBE_WORD", ""), sys.argv[2] == "1"
if EXTRA:
    @m.tool(description=WORDS)
    def drool(amount: float = 0.6, thick: float = 0.6) -> str:
        return "ok"
else:
    @m.tool(description=WORDS)
    def drool(amount: float = 0.6) -> str:
        return "ok"
m.run()
'''


class TestServedListing(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        (self.dir / "probe.py").write_text(SERVER)

    def listed(self, words, extra):
        spec = {"type": "stdio", "command": sys.executable, "args": [str(self.dir / "probe.py"), words, extra]}
        return asyncio.run(served.served({"probe": spec, "gone": {"command": "/nonexistent/server"}}, self.dir))

    def test_wording_and_parameters_move_the_digest_and_a_broken_server_is_left_out(self):
        base = self.listed("drool from the lip", "0")
        self.assertEqual(sorted(base), ["probe"])                     # "gone" cannot start: left out
        self.assertEqual(base, self.listed("drool from the lip", "0"))   # stable
        self.assertNotEqual(base, self.listed("drool from the lower lip", "0"))   # reworded
        self.assertNotEqual(base, self.listed("drool from the lip", "1"))         # a new parameter


    def test_a_variable_is_expanded_as_the_cli_does(self):
        # mcp_config passes ${NAME} through for the CLI to expand: the listing
        # must expand it too, or a server runs without its credential
        spec = lambda env: {"type": "stdio", "command": sys.executable,  # noqa: E731
                            "args": [str(self.dir / "probe.py"), "${PROBE_HEAD:-drool}", "0"], "env": env}
        with mock.patch.dict(os.environ, {"PROBE_SECRET": " real"}):
            ref = asyncio.run(served.served({"p": spec({"PROBE_WORD": "${PROBE_SECRET}"})}, self.dir))
            lit = asyncio.run(served.served({"p": spec({"PROBE_WORD": " real"})}, self.dir))
            wrong = asyncio.run(served.served({"p": spec({"PROBE_WORD": "${OTHER}"})}, self.dir))
        self.assertEqual(ref, lit)
        self.assertNotEqual(ref, wrong)


class TestExpand(unittest.TestCase):
    def test_names_and_defaults(self):
        from cousin_lib.runner.mcp_config import expand
        env = {"A": "1", "E": ""}
        self.assertEqual(expand("x${A}y", env), "x1y")
        self.assertEqual(expand("${B:-def}", env), "def")
        self.assertEqual(expand("${E:-def}", env), "")        # set, though empty: the CLI keeps it (2.1.321)
        self.assertEqual(expand("${B}", env), "")


if __name__ == "__main__":
    unittest.main()
