"""The in-flight work tracker: the store, the library surface, the CLI.

The store is module-owned SQLite at <root>/data/tracker.db. The
console serves it; nothing in the library depends on a console. Real
databases, real processes for the concurrency proof; the only seams
are the root flag and the environment.
"""
import contextlib
import io
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from cousin_lib.tracker import (
    STATES,
    ItemNotFound,
    TrackerError,
    add,
    db_path,
    delete,
    format_change,
    history,
    list_items,
    set_state,
    show,
    tracker_main,
    update,
)


class TrackerCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        home = self.root / "cousins" / "testa"
        home.mkdir(parents=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "testa"\n[chat]\nport = 8100\n'
        )
        patcher = mock.patch.dict(os.environ, {
            "FRAMEWORK_ROOT": str(self.root),
            "COUSIN_HOME": str(home),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = tracker_main(argv)
        return rc, out.getvalue(), err.getvalue()


class TestStore(TrackerCase):
    def test_add_returns_the_full_item_shape(self):
        item = add("port the tracker", domain="framework",
                   tags=["phase4", "cli"], notes="task 2")
        self.assertEqual(
            set(item), {"id", "title", "domain", "state", "tags",
                        "owner", "notes", "created_at", "updated_at"})
        self.assertEqual(item["state"], "open")
        self.assertEqual(item["tags"], ["phase4", "cli"])
        self.assertEqual(item["owner"], "testa")
        self.assertEqual(item["created_at"], item["updated_at"])
        self.assertTrue(db_path(self.root).is_file())
        self.assertEqual(db_path(self.root),
                         self.root / "data" / "tracker.db")

    def test_owner_is_empty_without_a_cousin_identity(self):
        with mock.patch.dict(os.environ):
            del os.environ["COUSIN_HOME"]
            item = add("operator-added")
        self.assertEqual(item["owner"], "")
        self.assertEqual(add("named", owner="other")["owner"], "other")

    def test_states_are_the_five_and_nothing_else(self):
        self.assertEqual(STATES,
                         ("open", "active", "blocked", "done", "dropped"))
        with self.assertRaises(TrackerError):
            add("bad", state="in_progress")
        item = add("ok", state="blocked")
        self.assertEqual(show(item["id"])["state"], "blocked")

    def test_blank_title_is_refused(self):
        with self.assertRaises(TrackerError):
            add("   ")

    def test_update_changes_only_the_named_fields(self):
        item = add("draft", domain="a", tags=["x"], notes="n")
        changed = update(item["id"], title="final", tags=["y", "z"])
        self.assertEqual(changed["title"], "final")
        self.assertEqual(changed["tags"], ["y", "z"])
        self.assertEqual(changed["domain"], "a")
        self.assertEqual(changed["notes"], "n")
        self.assertEqual(changed["created_at"], item["created_at"])
        with self.assertRaises(TrackerError):
            update(item["id"])
        with self.assertRaises(TrackerError):
            update(item["id"], state="under_review")

    def test_set_state_walks_the_lifecycle_and_bumps_updated_at(self):
        item = add("walk")
        with mock.patch("cousin_lib.tracker._now",
                        return_value="2030-01-01T00:00:00+00:00"):
            moved = set_state(item["id"], "active")
        self.assertEqual(moved["state"], "active")
        self.assertEqual(moved["updated_at"], "2030-01-01T00:00:00+00:00")
        self.assertEqual(moved["created_at"], item["created_at"])
        with self.assertRaises(TrackerError):
            set_state(item["id"], "closed")
        self.assertEqual(set_state(item["id"], "done")["state"], "done")

    def test_missing_item_is_a_named_error(self):
        self.assertIsNone(show(404))
        with self.assertRaises(ItemNotFound):
            update(404, title="x")
        with self.assertRaises(ItemNotFound):
            set_state(404, "done")
        self.assertFalse(delete(404))
        self.assertTrue(issubclass(ItemNotFound, TrackerError))

    def test_ids_never_recycle(self):
        # Delete the highest id, then add: the new id must be higher,
        # never the freed one. A console link to #3 must never come to
        # mean a different item.
        first = add("a")["id"]
        second = add("b")["id"]
        self.assertTrue(delete(second))
        third = add("c")["id"]
        self.assertGreater(third, second)
        self.assertGreater(second, first)

    def test_list_filters_by_domain_state_tag_and_owner(self):
        add("infra open", domain="infra", tags=["net"])
        a = add("infra active", domain="infra", tags=["net", "vpn"])
        set_state(a["id"], "active")
        add("income", domain="income", tags=["lead"], owner="other")
        titles = lambda **kw: {i["title"] for i in list_items(**kw)}
        self.assertEqual(titles(domain="infra"),
                         {"infra open", "infra active"})
        self.assertEqual(titles(state="active"), {"infra active"})
        self.assertEqual(titles(tag="vpn"), {"infra active"})
        self.assertEqual(titles(tag="net"),
                         {"infra open", "infra active"})
        self.assertEqual(titles(owner="other"), {"income"})
        self.assertEqual(titles(domain="infra", tag="net", state="open"),
                         {"infra open"})
        self.assertEqual(titles(), {"infra open", "infra active", "income"})

    def test_list_puts_closed_items_last_and_recent_first(self):
        a = add("old open")
        b = add("done one")
        c = add("new open")
        set_state(b["id"], "done")
        with mock.patch("cousin_lib.tracker._now",
                        return_value="2030-01-01T00:00:00+00:00"):
            set_state(a["id"], "active")
        order = [i["title"] for i in list_items()]
        self.assertEqual(order, ["old open", "new open", "done one"])

    def test_explicit_root_wins_over_the_environment(self):
        other = self.root / "elsewhere"
        add("here")
        add("there", root=other)
        self.assertEqual([i["title"] for i in list_items(root=other)],
                         ["there"])
        self.assertEqual([i["title"] for i in list_items()], ["here"])
        self.assertTrue((other / "data" / "tracker.db").is_file())

    def test_tags_round_trip_as_a_list_not_a_string(self):
        item = add("tagged", tags=("one", "two"))
        self.assertEqual(show(item["id"])["tags"], ["one", "two"])
        conn = sqlite3.connect(db_path(self.root))
        raw = conn.execute("SELECT tags FROM items WHERE id=?",
                           (item["id"],)).fetchone()[0]
        conn.close()
        self.assertEqual(json.loads(raw), ["one", "two"])


class TestConcurrency(TrackerCase):
    def test_parallel_writers_from_separate_processes_all_land(self):
        # One connection per call and BEGIN IMMEDIATE: eight processes
        # each adding five items must produce forty distinct ids with
        # no "database is locked" escape.
        code = (
            "import sys\n"
            "from cousin_lib.tracker import add\n"
            "for i in range(5):\n"
            "    add('w%s-%d' % (sys.argv[1], i))\n"
        )
        procs = [
            subprocess.Popen([sys.executable, "-c", code, str(n)],
                             env=dict(os.environ),
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
            for n in range(8)
        ]
        failures = []
        for p in procs:
            _, err = p.communicate(timeout=60)
            if p.returncode != 0:
                failures.append(err.decode(errors="replace"))
        self.assertEqual(failures, [])
        items = list_items()
        self.assertEqual(len(items), 40)
        self.assertEqual(len({i["id"] for i in items}), 40)


class TestHistory(TrackerCase):
    def test_a_replaced_note_is_kept_with_who_and_when(self):
        item = add("port", notes="the spec")
        with mock.patch.dict(os.environ):
            del os.environ["COUSIN_HOME"]
            update(item["id"], notes="status: half done")
        changes = history(item["id"])
        self.assertEqual([c["field"] for c in changes], ["created", "notes"])
        self.assertEqual(changes[0]["who"], "testa")
        self.assertEqual((changes[1]["old"], changes[1]["new"],
                          changes[1]["who"]),
                         ("the spec", "status: half done", "operator"))
        self.assertTrue(changes[1]["at"])

    def test_every_editable_field_is_recorded_old_and_new(self):
        item = add("a", domain="d1", tags=["x"], owner="o1")
        update(item["id"], title="b", domain="d2", state="active",
               tags=["y"], owner="o2", notes="n", who="someone")
        changes = {c["field"]: (c["old"], c["new"], c["who"])
                   for c in history(item["id"])[1:]}
        self.assertEqual(changes, {
            "title": ("a", "b", "someone"),
            "domain": ("d1", "d2", "someone"),
            "state": ("open", "active", "someone"),
            "tags": (["x"], ["y"], "someone"),
            "owner": ("o1", "o2", "someone"),
            "notes": ("", "n", "someone"),
        })

    def test_an_unchanged_value_records_nothing(self):
        item = add("same", notes="n")
        update(item["id"], title="same", notes="n", state="open")
        self.assertEqual([c["field"] for c in history(item["id"])],
                         ["created"])

    def test_add_note_appends_a_dated_signed_line(self):
        item = add("port", notes="the spec")
        first = update(item["id"], add_note="  started  ", state="active")
        lines = first["notes"].split("\n")
        self.assertEqual(lines[0], "the spec")
        self.assertRegex(lines[1],
                         r"^\d{4}-\d\d-\d\d \d\d:\d\d UTC testa: started$")
        second = update(item["id"], add_note="blocked on disk", who="other")
        self.assertEqual(second["notes"].split("\n")[:2], lines)
        self.assertTrue(second["notes"].endswith(" UTC other: blocked on disk"))
        self.assertEqual(add("bare")["id"], 2)
        self.assertRegex(update(2, add_note="x")["notes"],
                         r"^\d{4}-\d\d-\d\d \d\d:\d\d UTC testa: x$")
        with self.assertRaises(TrackerError):
            update(item["id"], add_note="   ")
        with self.assertRaises(ItemNotFound):
            update(404, add_note="x")

    def test_notes_and_add_note_together_replace_then_append(self):
        item = add("port", notes="old")
        changed = update(item["id"], notes="new", add_note="line")
        self.assertTrue(changed["notes"].startswith("new\n"))
        self.assertTrue(changed["notes"].endswith("testa: line"))
        self.assertEqual(history(item["id"])[-1]["old"], "old")

    def test_add_tags_appends_inside_the_update(self):
        item = add("t", tags=["a"])
        self.assertEqual(update(item["id"], add_tags=["b", "a"])["tags"],
                         ["a", "b"])
        self.assertEqual(update(item["id"], tags=["c"],
                                add_tags=["d"])["tags"], ["c", "d"])

    def test_delete_keeps_the_trail_and_the_item(self):
        item = add("gone", notes="keep me")
        self.assertTrue(delete(item["id"], who="someone"))
        last = history(item["id"])[-1]
        self.assertEqual((last["field"], last["who"]), ("deleted", "someone"))
        self.assertEqual(json.loads(last["old"])["notes"], "keep me")
        self.assertEqual(history(404), [])

    def test_a_store_from_before_the_history_table_gains_it(self):
        path = db_path(self.root)
        path.parent.mkdir(parents=True)
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " title TEXT NOT NULL, domain TEXT NOT NULL DEFAULT '',"
            " state TEXT NOT NULL DEFAULT 'open',"
            " tags TEXT NOT NULL DEFAULT '[]',"
            " owner TEXT NOT NULL DEFAULT '',"
            " notes TEXT NOT NULL DEFAULT '',"
            " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO items (title, notes, created_at, updated_at)"
            " VALUES ('old item', 'old spec', 't0', 't0')")
        conn.commit()
        conn.close()
        self.assertEqual(show(1)["notes"], "old spec")
        self.assertEqual(history(1), [])
        update(1, notes="replaced")
        self.assertEqual([(c["field"], c["old"]) for c in history(1)],
                         [("notes", "old spec")])
        show(1)  # a second open of the migrated store is a no-op
        self.assertEqual(len(history(1)), 1)

    def test_format_change_shows_appends_and_full_replacements(self):
        at = "2030-01-02T03:04:05+00:00"
        base = {"at": at, "who": "w"}
        self.assertEqual(
            format_change(dict(base, field="state", old="open", new="done")),
            '2030-01-02 03:04 UTC w: state "open" -> "done"')
        self.assertEqual(
            format_change(dict(base, field="notes", old="a", new="a\nb")),
            "2030-01-02 03:04 UTC w: notes += b")
        text = format_change(dict(base, field="notes", old="spec\nmore",
                                  new="status"))
        self.assertIn("notes replaced; were:\n      spec\n      more", text)
        self.assertIn("now:\n      status", text)


class TestCli(TrackerCase):
    def test_add_prints_the_id_and_json_prints_the_envelope(self):
        rc, out, _ = self._main(["add", "map the tree", "--domain",
                                 "framework", "--tag", "a", "--tag", "b"])
        self.assertEqual(rc, 0)
        self.assertIn("#1", out)
        rc, out, _ = self._main(["add", "second", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["item"]["title"], "second")
        self.assertEqual(data["item"]["id"], 2)
        self.assertEqual(show(1)["tags"], ["a", "b"])

    def test_state_update_show_and_delete(self):
        self._main(["add", "walk"])
        rc, out, _ = self._main(["state", "1", "active"])
        self.assertEqual(rc, 0)
        self.assertEqual(show(1)["state"], "active")
        rc, out, _ = self._main(["update", "1", "--title", "walked",
                                 "--add-tag", "t1", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["item"]["tags"], ["t1"])
        rc, out, _ = self._main(["update", "1", "--tag", "only"])
        self.assertEqual(show(1)["tags"], ["only"])
        rc, out, _ = self._main(["show", "1", "--json"])
        self.assertEqual(json.loads(out)["item"]["title"], "walked")
        rc, out, _ = self._main(["show", "1"])
        self.assertIn("walked", out)
        rc, out, _ = self._main(["delete", "1", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {"ok": True, "deleted": 1})
        self.assertIsNone(show(1))

    def test_add_note_appends_and_notes_replaces(self):
        self._main(["add", "port", "--notes", "the spec"])
        rc, _, _ = self._main(["update", "1", "--add-note", "started",
                               "--state", "active"])
        self.assertEqual(rc, 0)
        notes = show(1)["notes"]
        self.assertTrue(notes.startswith("the spec\n"))
        self.assertRegex(notes, r"\n\d{4}-\d\d-\d\d \d\d:\d\d UTC testa: started$")
        rc, _, _ = self._main(["update", "1", "--notes", "replaced"])
        self.assertEqual((rc, show(1)["notes"]), (0, "replaced"))
        self.assertEqual(history(1)[-1]["old"], notes)
        self.assertEqual(self._main(["update", "1", "--add-note", " "])[0], 2)

    def test_show_history_prints_changes_oldest_first(self):
        self._main(["add", "port", "--notes", "the spec"])
        self._main(["state", "1", "active"])
        self._main(["update", "1", "--notes", "status"])
        rc, plain, _ = self._main(["show", "1"])
        self.assertNotIn("history", plain)
        rc, out, _ = self._main(["show", "1", "--history"])
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith(plain))
        tail = out[len(plain):]
        self.assertLess(tail.index('created "port"'),
                        tail.index('state "open" -> "active"'))
        self.assertLess(tail.index("active"), tail.index("notes replaced"))
        self.assertIn("      the spec", tail)
        rc, out, _ = self._main(["show", "1", "--history", "--json"])
        data = json.loads(out)
        self.assertEqual(data["item"]["notes"], "status")
        self.assertEqual([c["field"] for c in data["history"]],
                         ["created", "state", "notes"])
        rc, out, _ = self._main(["show", "1", "--json"])
        self.assertEqual(set(json.loads(out)), {"item"})

    def test_show_history_of_a_deleted_item_prints_the_trail_and_exits_one(self):
        self._main(["add", "gone", "--notes", "keep me"])
        self._main(["delete", "1"])
        rc, out, err = self._main(["show", "1", "--history"])
        self.assertEqual(rc, 1)
        self.assertIn("not found", err)
        self.assertIn("deleted; the item was:", out)
        self.assertIn("keep me", out)

    def test_list_filters_and_json_envelope(self):
        self._main(["add", "one", "--domain", "infra", "--tag", "x"])
        self._main(["add", "two", "--domain", "income"])
        rc, out, _ = self._main(["list", "--domain", "infra"])
        self.assertEqual(rc, 0)
        self.assertIn("one", out)
        self.assertNotIn("two", out)
        rc, out, _ = self._main(["list", "--tag", "x", "--json"])
        data = json.loads(out)
        self.assertEqual([i["title"] for i in data["items"]], ["one"])
        rc, out, _ = self._main(["list", "--state", "done"])
        self.assertIn("(no tracker items)", out)

    def test_missing_item_exits_one_and_bad_state_exits_two(self):
        rc, _, err = self._main(["show", "9"])
        self.assertEqual(rc, 1)
        self.assertIn("not found", err)
        self.assertEqual(self._main(["state", "9", "done"])[0], 1)
        self.assertEqual(self._main(["delete", "9"])[0], 1)
        self._main(["add", "x"])
        rc, _, err = self._main(["state", "1", "in_progress"])
        self.assertEqual(rc, 2)
        rc, _, err = self._main(["add", "y", "--state", "new"])
        self.assertEqual(rc, 2)
        rc, _, err = self._main(["update", "1"])
        self.assertEqual(rc, 2)

    def test_root_flag_and_missing_root_fail_loud(self):
        other = self.root / "other"
        rc, out, _ = self._main(["add", "over there", "--root", str(other)])
        self.assertEqual(rc, 0)
        self.assertEqual([i["title"] for i in list_items(root=other)],
                         ["over there"])
        # run from a directory that is not a checkout, which would
        # otherwise be the root (FrameworkConfig.resolve)
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.root)
        with mock.patch.dict(os.environ):
            del os.environ["FRAMEWORK_ROOT"]
            os.environ.pop("COUSIN_HOME", None)
            rc, _, err = self._main(["list"])
        self.assertEqual(rc, 2)
        self.assertIn("FRAMEWORK_ROOT", err)


if __name__ == "__main__":
    unittest.main()
