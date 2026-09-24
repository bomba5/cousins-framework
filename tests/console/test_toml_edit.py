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

    def test_control_characters_and_del_are_escaped(self):
        for v in ("x\x7f", "a\nb", "tab\there", 'q"uote'):
            text = toml_edit.set_key(BASE, "agent", "model", v)
            self.assertEqual(tomllib.loads(text)["agent"]["model"], v)

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


class KeepsWhatIsNotTheKey(unittest.TestCase):
    """Fix round 1 minors: an inline comment survives a replace; a key's
    name inside a multi-line string or a nested array is never taken for
    the key."""

    DOC = ('[agent]\nrunner = "sdk"\nmodel = "a"  # keep me\n'
           'prompt = """\nline\nmodel = "inside"\n[fake]\n"""\n'
           'x = [\n  [1, 2],\n]\n\n[other]\nk = 1\n')

    def test_an_inline_comment_is_kept(self):
        text = toml_edit.set_key(self.DOC, "agent", "model", "b")
        self.assertIn('model = "b"  # keep me\n', text)
        self.assertEqual(tomllib.loads(text)["agent"]["model"], "b")

    def test_a_key_only_inside_a_multi_line_string_is_not_touched(self):
        doc = '[agent]\nrunner = "sdk"\nprompt = """\nmodel = "inside"\n"""\n'
        self.assertEqual(toml_edit.set_key(doc, "agent", "model", None), doc)
        text = toml_edit.set_key(doc, "agent", "model", "m")
        data = tomllib.loads(text)
        self.assertEqual(data["agent"]["model"], "m")
        self.assertEqual(data["agent"]["prompt"], 'model = "inside"\n')

    def test_an_array_of_tables_ends_the_table_before_it(self):
        doc = '[agent]\nrunner = "sdk"\n\n[[loops]]\nname = "a"\n'
        text = toml_edit.set_key(doc, "agent", "model", "m")
        data = tomllib.loads(text)
        self.assertEqual(data["agent"]["model"], "m")
        self.assertEqual(data["loops"], [{"name": "a"}])

    def test_the_rest_of_the_document_is_unchanged(self):
        before = tomllib.loads(self.DOC)
        for value in ("b", None):
            text = toml_edit.set_key(self.DOC, "agent", "effort", "high")
            text = toml_edit.set_key(text, "agent", "model", value)
            after = tomllib.loads(text)
            self.assertEqual(after["agent"]["prompt"], before["agent"]["prompt"])
            self.assertEqual(after["agent"]["x"], [[1, 2]])
            self.assertEqual(after["other"], {"k": 1})
            self.assertEqual(after["agent"]["effort"], "high")


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


ROOTED = ('# a registry\n'
          'ceiling = 12   # tools\n'
          'deny = [\n'
          '  "a",\n'
          ']\n'
          '\n'
          '[tools.memory]\n'
          'command = "cousin-memory"\n')


class RootTable(unittest.TestCase):
    """table "" is the document's top level: the keys before any header
    (policy.toml's, a registry's ceiling)."""

    def test_a_top_level_key_is_replaced_in_place(self):
        out = toml_edit.set_key(ROOTED, "", "ceiling", 14)
        self.assertIn("ceiling = 14   # tools\n", out)
        self.assertEqual(tomllib.loads(out)["tools"], {"memory": {"command": "cousin-memory"}})

    def test_a_new_top_level_key_lands_before_the_first_table(self):
        out = toml_edit.set_key(ROOTED, "", "timeout", 60)
        data = tomllib.loads(out)
        self.assertEqual((data["timeout"], data["ceiling"]), (60, 12))
        self.assertNotIn("timeout", data["tools"]["memory"])
        self.assertLess(out.index("timeout"), out.index("[tools.memory]"))

    def test_a_file_that_starts_with_a_table_gets_the_key_on_top(self):
        out = toml_edit.set_key('[tools.x]\ncommand = "c"\n', "", "ceiling", 3)
        data = tomllib.loads(out)
        self.assertEqual((data["ceiling"], data["tools"]["x"]), (3, {"command": "c"}))

    def test_a_top_level_list_is_replaced_whole_and_removed(self):
        out = toml_edit.set_key(ROOTED, "", "deny", ["b"])
        self.assertEqual(tomllib.loads(out)["deny"], ["b"])
        out = toml_edit.set_key(ROOTED, "", "deny", None)
        self.assertNotIn("deny", tomllib.loads(out))
        self.assertIn("# a registry\n", out)

    def test_an_empty_text_takes_top_level_keys(self):
        out = toml_edit.set_key("", "", "outbound_filter", True)
        self.assertEqual(tomllib.loads(out), {"outbound_filter": True})

    def test_a_text_that_does_not_parse_is_refused_for_the_top_level(self):
        with self.assertRaises(ValueError):
            toml_edit.set_key("x = = 1\n", "", "ceiling", 1)


class WriteFile(unittest.TestCase):
    """write_file: write_keys for any TOML file (a registry, policy.toml)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "mcp-registry.toml"
        self.path.write_text(ROOTED)
        os.chmod(self.path, 0o600)

    def test_keys_of_any_file_and_its_mode_kept(self):
        parsed = toml_edit.write_file(self.path, [("", "ceiling", 4),
                                                  ("tools.memory", "enabled", False)])
        self.assertEqual((parsed["ceiling"], parsed["tools"]["memory"]["enabled"]), (4, False))
        self.assertEqual(tomllib.loads(self.path.read_text()), parsed)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_the_text_hook_sees_the_new_text_and_can_refuse_it(self):
        seen = []

        def hook(text):
            seen.append(text)
            raise ValueError("no")
        with self.assertRaises(ValueError):
            toml_edit.write_file(self.path, [("", "ceiling", 4)], validate_text=hook)
        self.assertIn("ceiling = 4", seen[0])
        self.assertEqual(self.path.read_text(), ROOTED)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["mcp-registry.toml"])

    def test_an_absent_file_is_created_only_when_asked(self):
        path = self.dir / "policy.toml"
        with self.assertRaises(FileNotFoundError):
            toml_edit.write_file(path, [("", "ask", [])])
        parsed = toml_edit.write_file(path, [("", "ask", ["Agent"])], initial="# new\n")
        self.assertEqual(parsed, {"ask": ["Agent"]})
        self.assertTrue(path.read_text().startswith("# new\n"))

    def test_fresh_starts_from_initial_over_a_file_that_does_not_parse(self):
        path = self.dir / "policy.toml"
        path.write_text("broken = = 1\n")
        os.chmod(path, 0o600)
        with self.assertRaises(ValueError):
            toml_edit.write_file(path, [("", "ask", [])])
        parsed = toml_edit.write_file(path, [("", "ask", [])], initial="# new\n", fresh=True)
        self.assertEqual(parsed, {"ask": []})
        self.assertNotIn("broken", path.read_text())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
