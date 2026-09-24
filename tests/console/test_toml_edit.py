"""console/toml_edit: targeted edits of a cousin.toml that keep every
other byte, for every value shape an [agent] setting takes (strings,
integers, booleans, floats, lists of strings, dotted subtables), one key
or several at once, with a validate hook on the parsed result before the
atomic rename."""
import os
import stat
import tempfile
import tomllib
import unittest
from pathlib import Path

from cousin_lib.console import toml_edit

BASE = ('# my cousin\n'
        '[cousin]\n'
        'slug = "wren"   # keep me\n'
        'name = "Wren"\n'
        '\n'
        '[agent]\n'
        'runner = "sdk"\n'
        'shell_env = [\n'
        '  "LANG",\n'
        '  "TZ",\n'
        ']\n'
        '\n'
        '[agent.sessions]\n'
        'peer = "own"\n'
        '\n'
        '[chat]\n'
        'port = 8100\n')


class SetKey(unittest.TestCase):
    def test_a_float_is_written_and_reads_back(self):
        text = toml_edit.set_key(BASE, "agent", "rollover_at_percent", 72.5)
        self.assertEqual(tomllib.loads(text)["agent"]["rollover_at_percent"], 72.5)
        whole = toml_edit.set_key(BASE, "agent", "rollover_at_percent", 80.0)
        self.assertEqual(tomllib.loads(whole)["agent"]["rollover_at_percent"], 80.0)
        self.assertIsInstance(tomllib.loads(whole)["agent"]["rollover_at_percent"], float)

    def test_a_non_finite_float_is_refused(self):
        for bad in (float("inf"), float("nan")):
            with self.assertRaises(TypeError):
                toml_edit.set_key(BASE, "agent", "rollover_at_percent", bad)

    def test_a_list_of_strings_is_written(self):
        text = toml_edit.set_key(BASE, "agent", "env_allow", ["LANG", 'A"B'])
        self.assertEqual(tomllib.loads(text)["agent"]["env_allow"], ["LANG", 'A"B'])
        empty = toml_edit.set_key(BASE, "agent", "env_allow", [])
        self.assertEqual(tomllib.loads(empty)["agent"]["env_allow"], [])

    def test_a_list_of_anything_but_scalars_is_refused(self):
        with self.assertRaises(TypeError):
            toml_edit.set_key(BASE, "agent", "env_allow", [["nested"]])
        with self.assertRaises(TypeError):
            toml_edit.set_key(BASE, "agent", "env_allow", [{"a": 1}])

    def test_a_multi_line_value_is_replaced_whole(self):
        text = toml_edit.set_key(BASE, "agent", "shell_env", ["USER"])
        data = tomllib.loads(text)
        self.assertEqual(data["agent"]["shell_env"], ["USER"])
        self.assertNotIn('"TZ"', text)
        # everything around it kept, byte for byte
        self.assertTrue(text.startswith('# my cousin\n[cousin]\nslug = "wren"   # keep me\n'))
        self.assertTrue(text.endswith('\n[agent.sessions]\npeer = "own"\n\n[chat]\nport = 8100\n'))

    def test_removing_a_multi_line_value_removes_all_its_lines(self):
        text = toml_edit.set_key(BASE, "agent", "shell_env", None)
        self.assertNotIn("shell_env", text)
        self.assertNotIn('"LANG"', text)
        self.assertEqual(tomllib.loads(text)["agent"], {"runner": "sdk", "sessions": {"peer": "own"}})

    def test_a_dotted_subtable_is_edited_in_place(self):
        text = toml_edit.set_key(BASE, "agent.sessions", "meeting", "own")
        data = tomllib.loads(text)
        self.assertEqual(data["agent"]["sessions"], {"peer": "own", "meeting": "own"})
        self.assertEqual(data["agent"]["runner"], "sdk")

    def test_an_absent_dotted_subtable_is_created(self):
        text = toml_edit.set_key('[agent]\nrunner = "sdk"\n', "agent.sessions", "peer", "own")
        self.assertEqual(tomllib.loads(text)["agent"], {"runner": "sdk", "sessions": {"peer": "own"}})

    def test_a_key_that_is_not_a_bare_key_is_refused(self):
        for bad in ("a b", "a=b", "", "x\ny"):
            with self.assertRaises(ValueError):
                toml_edit.set_key(BASE, "agent", bad, "v")
        with self.assertRaises(ValueError):
            toml_edit.set_key(BASE, "agent]\n[x", "k", "v")

    def test_crlf_files_keep_their_line_endings(self):
        crlf = BASE.replace("\n", "\r\n")
        text = toml_edit.set_key(crlf, "agent", "model", "m")
        self.assertNotIn("\n", text.replace("\r\n", ""))
        self.assertEqual(tomllib.loads(text)["agent"]["model"], "m")


class WriteKeys(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.path = self.home / "cousin.toml"
        self.path.write_text(BASE)
        os.chmod(self.path, 0o640)

    def test_several_keys_in_one_write(self):
        parsed = toml_edit.write_keys(self.home, [
            ("agent", "model", "claude-x"),
            ("agent", "rollover_at_percent", 60.0),
            ("agent", "auto_start", False),
            ("agent.sessions", "peer", None),
            ("agent", "shell_env", ["LANG"]),
        ])
        data = tomllib.loads(self.path.read_text())
        self.assertEqual(parsed, data)
        self.assertEqual(data["agent"], {"runner": "sdk", "model": "claude-x",
                                         "rollover_at_percent": 60.0, "auto_start": False,
                                         "shell_env": ["LANG"], "sessions": {}})
        self.assertIn('slug = "wren"   # keep me\n', self.path.read_text())

    def test_a_mapping_of_table_key_pairs_is_accepted(self):
        toml_edit.write_keys(self.home, {("agent", "model"): "m", ("cousin", "role"): "r"})
        data = tomllib.loads(self.path.read_text())
        self.assertEqual((data["agent"]["model"], data["cousin"]["role"]), ("m", "r"))

    def test_the_hook_sees_the_parsed_result_and_can_refuse_it(self):
        seen = []

        def hook(parsed):
            seen.append(parsed["agent"].get("model"))
            if parsed["agent"].get("model") == "bad":
                raise ValueError("no bad models")
        toml_edit.write_keys(self.home, [("agent", "model", "good")], validate=hook)
        with self.assertRaises(ValueError):
            toml_edit.write_keys(self.home, [("agent", "model", "bad"),
                                             ("agent", "effort", "max")], validate=hook)
        self.assertEqual(seen, ["good", "bad"])
        data = tomllib.loads(self.path.read_text())
        self.assertEqual(data["agent"]["model"], "good")
        self.assertNotIn("effort", data["agent"])
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["cousin.toml"])

    def test_nothing_is_written_when_one_value_is_unsupported(self):
        before = self.path.read_bytes()
        with self.assertRaises(TypeError):
            toml_edit.write_keys(self.home, [("agent", "model", "m"),
                                             ("agent", "x", object())])
        self.assertEqual(self.path.read_bytes(), before)

    def test_a_text_that_does_not_round_trip_is_never_persisted(self):
        # a dotted key already in [agent] makes a second [agent.sessions]
        # header a redefinition: the parse refuses it, nothing is written
        self.path.write_text('[agent]\nrunner = "sdk"\nsessions.peer = "own"\n')
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            toml_edit.write_keys(self.home, [("agent.sessions", "meeting", "own")])
        self.assertEqual(self.path.read_bytes(), before)

    def test_the_files_mode_is_kept(self):
        toml_edit.write_keys(self.home, [("agent", "model", "m")])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)

    def test_write_key_is_write_keys_of_one(self):
        parsed = toml_edit.write_key(self.home, "agent", "rollover_at_percent", 55.5)
        self.assertEqual(parsed["agent"]["rollover_at_percent"], 55.5)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)


if __name__ == "__main__":
    unittest.main()
