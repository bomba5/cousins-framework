"""The bridge guard: the Claude-subscription bridge cannot
come back through a config line, a plugin entry, a header or the
environment. One subtest per marker, in the config and in the env."""
import ast
import copy
import json
import pathlib
import sys
import unittest

from cousin_lib.runner import opencode_guard
from cousin_lib.runner.opencode_guard import BRIDGE_MARKERS, BridgeRefused, refuse_bridge
from tests._hermetic import HermeticCase

# What the runner renders, a local OpenAI-compatible endpoint included.
CLEAN_CONFIG = {
    "$schema": "https://opencode.ai/config.json",
    "model": "local/qwen3-coder", "small_model": "local/qwen3-coder",
    "disabled_providers": ["opencode"], "autoupdate": False, "share": "disabled",
    "permission": {"*": "allow"},
    "plugin": ["/srv/cf/plugins/opencode/cousin-policy.js"],
    "mcp": {"cousin": {"type": "remote", "url": "http://127.0.0.1:41233/mcp",
                       "headers": {"Authorization": "Bearer tok-per-run"}}},
    "provider": {"local": {"npm": "@ai-sdk/openai-compatible", "name": "local",
                           "options": {"baseURL": "http://127.0.0.1:11434/v1"},
                           "models": {"qwen3-coder": {"name": "qwen3-coder"}}},
                 "anthropic": {"options": {"baseURL": "https://api.anthropic.com/v1"}}},
}
CLEAN_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
    "HOME": "/srv/cf/.secrets/accounts/oc.opencode",
    "XDG_DATA_HOME": "/srv/cf/.secrets/accounts/oc.opencode/data",
    "OPENCODE_CONFIG": "/srv/cf/.secrets/accounts/oc.opencode/opencode.runner.json",
    "OPENCODE_DISABLE_AUTOUPDATE": "1", "OPENCODE_DISABLE_SHARE": "1",
    "OPENCODE_DISABLE_CLAUDE_CODE": "1", "OPENCODE_SERVER_PASSWORD": "pw-per-run",
}

# marker -> (a config that carries it, an environment that carries it),
# each placed where the bridge puts it (survey 8).
SAMPLES = {
    "opencode-with-claude": (
        {"plugin": ["opencode-with-claude"]},
        {"NODE_PATH": "/opt/lib/node_modules/opencode-with-claude"}),
    "meridian": (
        {"plugin": ["file:///opt/meridian/dist/index.js"]},
        {"PATH": "/usr/bin:/opt/meridian/bin"}),
    "rynfar": (
        {"plugin": ["@rynfar/opencode-scrub"]},
        {"NODE_PATH": "/opt/node_modules/@rynfar"}),
    "claude-max-proxy": (
        {"mcp": {"p": {"type": "local", "command": ["claude-max-proxy", "--port", "4000"]}}},
        {"PROXY_CMD": "claude-max-proxy"}),
    "CLAUDE_PROXY_": (
        {"mcp": {"p": {"type": "local", "command": ["node", "p.js"],
                       "environment": {"CLAUDE_PROXY_PORT": "4000"}}}},
        {"CLAUDE_PROXY_HOST": "127.0.0.1"}),
    "MERIDIAN_": (
        {"mcp": {"p": {"type": "local", "command": ["node", "p.js"],
                       "environment": {"MERIDIAN_PROFILES": "default"}}}},
        {"MERIDIAN_PASSTHROUGH": "1"}),
    "x-meridian": (
        {"provider": {"openai": {"options": {"headers": {"x-meridian-source": "opencode"}}}}},
        {"EXTRA_HEADERS": "x-meridian-source: opencode"}),
    r"://[^/?#\s]*:3456(?![0-9])": (
        {"provider": {"openai": {"options": {"baseURL": "http://127.0.0.1:3456/v1"}}}},
        {"OPENAI_BASE_URL": "http://127.0.0.1:3456/v1"}),
}


def _carries(marker, config=None, env=None):
    text = json.dumps(config) if config is not None else \
        " ".join("%s=%s" % kv for kv in env.items())
    return marker.search(text) is not None


class TestMarkers(HermeticCase):
    def test_every_marker_has_a_sample(self):
        self.assertEqual(set(SAMPLES), {m.pattern for m in BRIDGE_MARKERS})

    def test_every_bridge_marker_is_refused(self):
        for marker in BRIDGE_MARKERS:
            config, env = SAMPLES[marker.pattern]
            with self.subTest(marker=marker.pattern, where="config"):
                self.assertTrue(_carries(marker, config=config))
                with self.assertRaises(BridgeRefused) as cm:
                    refuse_bridge(config, CLEAN_ENV)
                self.assertIn("bridge", cm.exception.reason)
                self.assertIn("config", cm.exception.reason)
            with self.subTest(marker=marker.pattern, where="env"):
                self.assertTrue(_carries(marker, env=env))
                with self.assertRaises(BridgeRefused) as cm:
                    refuse_bridge(CLEAN_CONFIG, {**CLEAN_ENV, **env})
                self.assertIn("bridge", cm.exception.reason)
                self.assertIn(next(iter(env)), cm.exception.reason)

    def test_a_marker_is_found_whatever_its_case(self):
        with self.assertRaises(BridgeRefused):
            refuse_bridge({"plugin": ["Opencode-With-Claude"]}, {})
        with self.assertRaises(BridgeRefused):
            refuse_bridge({}, {"claude_proxy_port": "4000"})

    def test_the_reason_names_where_never_the_value(self):
        with self.assertRaises(BridgeRefused) as cm:
            refuse_bridge({"provider": {"x": {"options": {"apiKey": "sk-sekrit-meridian"}}}}, {})
        self.assertIn("provider.x.options.apiKey", cm.exception.reason)
        self.assertNotIn("sk-sekrit", cm.exception.reason)
        with self.assertRaises(BridgeRefused) as cm:
            refuse_bridge({}, {"SOME_TOKEN": "sk-sekrit-meridian"})
        self.assertIn("SOME_TOKEN", cm.exception.reason)
        self.assertNotIn("sk-sekrit", cm.exception.reason)
        self.assertEqual(str(cm.exception), cm.exception.reason)

    def test_port_3456_says_to_move_the_proxy(self):
        with self.assertRaises(BridgeRefused) as cm:
            refuse_bridge({"provider": {"local": {"options": {"baseURL": "http://localhost:3456"}}}},
                          {})
        self.assertIn("3456", cm.exception.reason)
        self.assertIn("move", cm.exception.reason)
        refuse_bridge({"provider": {"local": {"options": {"baseURL": "http://localhost:34567"}}}},
                      {})


class TestAnthropicLoopback(HermeticCase):
    LOOPBACK = ("http://127.0.0.1:8080", "http://localhost:4000/v1", "http://[::1]:4000",
                "http://127.9.9.9", "http://0.0.0.0:9000", "https://api.localhost/v1")

    def test_an_anthropic_provider_on_loopback_is_refused(self):
        for url in self.LOOPBACK:
            with self.subTest(url=url):
                with self.assertRaises(BridgeRefused) as cm:
                    refuse_bridge({"provider": {"anthropic": {"options": {"baseURL": url}}}}, {})
                self.assertIn("anthropic", cm.exception.reason)
                self.assertIn("loopback", cm.exception.reason)

    def test_the_anthropic_sdk_under_another_id_is_the_same_provider(self):
        with self.assertRaises(BridgeRefused) as cm:
            refuse_bridge({"provider": {"claude": {"npm": "@ai-sdk/anthropic",
                                                   "options": {"baseURL": "http://127.0.0.1:4000"}}}},
                          {})
        self.assertIn("provider.claude", cm.exception.reason)

    def test_anthropic_base_url_on_loopback_in_the_env_is_refused(self):
        with self.assertRaises(BridgeRefused) as cm:
            refuse_bridge(CLEAN_CONFIG, {**CLEAN_ENV, "ANTHROPIC_BASE_URL": "http://localhost:4000"})
        self.assertIn("ANTHROPIC_BASE_URL", cm.exception.reason)

    def test_a_remote_anthropic_url_and_a_local_openai_compatible_one_pass(self):
        refuse_bridge({"provider": {"anthropic": {"options": {"baseURL": "https://api.anthropic.com/v1"}},
                                    "local": {"npm": "@ai-sdk/openai-compatible",
                                              "options": {"baseURL": "http://localhost:11434/v1"}}}},
                      {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"})


class TestClean(HermeticCase):
    def test_a_clean_rendered_config_and_env_pass(self):
        self.assertIsNone(refuse_bridge(CLEAN_CONFIG, CLEAN_ENV))
        self.assertIsNone(refuse_bridge({}, {}))

    def test_the_inputs_are_not_touched(self):
        config, env = copy.deepcopy(CLEAN_CONFIG), dict(CLEAN_ENV)
        refuse_bridge(config, env)
        self.assertEqual((config, env), (CLEAN_CONFIG, CLEAN_ENV))

    def test_the_guard_imports_the_stdlib_only(self):
        tree = ast.parse(pathlib.Path(opencode_guard.__file__).read_text())
        mods = {n.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                for n in node.names}
        mods |= {node.module.split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module}
        self.assertTrue(mods)
        self.assertLessEqual(mods, set(sys.stdlib_module_names))


if __name__ == "__main__":
    unittest.main()
