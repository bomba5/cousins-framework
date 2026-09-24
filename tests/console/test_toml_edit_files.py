"""console/toml_edit beyond a cousin.toml (WP-F, the install config
editors): a top-level key (the root table, `table=""`), a whole table
removed, and the same checked atomic write over any TOML file, created
when absent."""
import os
import stat
import tempfile
import tomllib
import unittest
from pathlib import Path

from cousin_lib.console import toml_edit

HIVE = ('# config/hive.toml\n'
        '# the queen\n'
        'enabled = false   # off for now\n'
        '\n'
        '# where nodes reach it\n'
        'public_url = "http://192.0.2.10:8600"\n'
        '\n'
        '[extra]\n'
        'x = 1\n')

PEERS = ('# peers\n'
         '[peers.kestrel]\n'
         'url = "http://127.0.0.1:8085"\n'
         'reach = ["wren"]\n'
         '\n'
         '[peers.robin]\n'
         'url = "http://127.0.0.1:8086"\n')


class TopLevel(unittest.TestCase):
    def test_a_top_level_key_is_replaced_in_place_with_its_comment(self):
        text = toml_edit.set_key(HIVE, "", "enabled", True)
        self.assertIn('enabled = true   # off for now\n', text)
        self.assertEqual(text.replace("enabled = true", "enabled = false"), HIVE)

    def test_a_new_top_level_key_goes_above_the_first_table(self):
        text = toml_edit.set_key(HIVE, "", "checkin_seconds", 30)
        parsed = tomllib.loads(text)
        self.assertEqual(parsed["checkin_seconds"], 30)
        self.assertEqual(parsed["extra"], {"x": 1})
        self.assertLess(text.index("checkin_seconds"), text.index("[extra]"))

    def test_a_top_level_key_is_removed(self):
        text = toml_edit.set_key(HIVE, "", "public_url", None)
        self.assertNotIn("public_url", tomllib.loads(text))
        self.assertIn("# where nodes reach it", text)

    def test_a_file_of_comments_only_takes_the_key_at_its_end(self):
        text = toml_edit.set_key("# nothing yet\n#url = \"x\"\n", "", "url", "http://h:1/e")
        self.assertEqual(tomllib.loads(text), {"url": "http://h:1/e"})
        self.assertTrue(text.startswith("# nothing yet\n"))

    def test_an_empty_text_takes_the_key(self):
        self.assertEqual(tomllib.loads(toml_edit.set_key("", "", "a", 1)), {"a": 1})

    def test_a_key_named_like_a_table_key_is_left_alone(self):
        text = toml_edit.set_key(HIVE, "", "x", 5)
        parsed = tomllib.loads(text)
        self.assertEqual(parsed["x"], 5)
        self.assertEqual(parsed["extra"], {"x": 1})


class RemoveTable(unittest.TestCase):
    def test_a_whole_table_goes_and_the_rest_is_kept(self):
        text = toml_edit.remove_table(PEERS, "peers.kestrel")
        self.assertEqual(tomllib.loads(text),
                         {"peers": {"robin": {"url": "http://127.0.0.1:8086"}}})
        self.assertIn("# peers\n", text)

    def test_the_last_table_goes(self):
        text = toml_edit.remove_table(PEERS, "peers.robin")
        self.assertEqual(set(tomllib.loads(text)["peers"]), {"kestrel"})

    def test_an_absent_table_changes_nothing(self):
        self.assertEqual(toml_edit.remove_table(PEERS, "peers.none"), PEERS)

    def test_a_table_without_a_header_of_its_own_is_refused(self):
        for text in ('[peers]\nkestrel = {url = "x"}\n', '[peers]\nkestrel.url = "x"\n',
                     'peers.kestrel.url = "x"\n'):
            with self.assertRaises(ValueError, msg=text):
                toml_edit.remove_table(text, "peers.kestrel")

    def test_a_bad_name_is_refused(self):
        with self.assertRaises(ValueError):
            toml_edit.remove_table(PEERS, "peers.a b")


class WriteFile(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_any_toml_file_is_edited_and_its_mode_kept(self):
        path = self.dir / "hive.toml"
        path.write_text(HIVE)
        os.chmod(path, 0o640)
        parsed = toml_edit.write_file_keys(path, [("", "enabled", True),
                                                  ("extra", "y", "z")])
        self.assertTrue(parsed["enabled"])
        self.assertEqual(tomllib.loads(path.read_text())["extra"], {"x": 1, "y": "z"})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_a_missing_file_is_created_only_when_asked_and_owner_only(self):
        path = self.dir / "media.toml"
        with self.assertRaises(FileNotFoundError):
            toml_edit.write_file_keys(path, [("image", "url", "http://h/i")])
        toml_edit.write_file_keys(path, [("image", "url", "http://h/i")], create=True)
        self.assertEqual(tomllib.loads(path.read_text()), {"image": {"url": "http://h/i"}})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_a_refusing_validator_writes_nothing(self):
        path = self.dir / "hive.toml"
        path.write_text(HIVE)

        def refuse(parsed):
            raise ValueError("no")
        with self.assertRaises(ValueError):
            toml_edit.write_file_keys(path, [("", "enabled", True)], validate=refuse)
        self.assertEqual(path.read_text(), HIVE)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["hive.toml"])

    def test_a_table_removal_goes_through_the_same_write(self):
        path = self.dir / "peers.toml"
        path.write_text(PEERS)
        seen = []
        toml_edit.write_file_keys(path, [], remove_tables=["peers.robin"],
                                  validate=seen.append)
        self.assertEqual(set(tomllib.loads(path.read_text())["peers"]), {"kestrel"})
        self.assertEqual(set(seen[0]["peers"]), {"kestrel"})

    def test_write_keys_still_edits_a_cousin_toml(self):
        home = self.dir / "wren"
        home.mkdir()
        (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\n')
        toml_edit.write_keys(home, [("agent", "model", "m")])
        self.assertEqual(tomllib.loads((home / "cousin.toml").read_text())["agent"],
                         {"model": "m"})


if __name__ == "__main__":
    unittest.main()
