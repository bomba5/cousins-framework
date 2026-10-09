"""cousin-backup and the runner's own stores: the inbox (a database,
snapshotted by VACUUM INTO before every other database), the event
stream (one append-only JSONL per runner session under data/stream/)
and the session files a runner resumes from (data/runner-session*.json).

A snapshot taken mid-turn must restore to a home the runner recovers
from on its own: the claimed row goes back to queued, is answered, and
the stream reads back without a torn last line. The inbox goes first so
a row `done` in the copy never lacks the reply committed before it:
at-least-once, never lost.
"""
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

from cousin_lib import backup
from cousin_lib.delivery import Item
from cousin_lib.runner import main as runner_main
from cousin_lib.runner.inbox import Inbox
from cousin_lib.runner.stream import EventStream
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home

SESSION = "fake-0a1b2c3d"


def _tmpdir(case):
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    return Path(tmp.name)


def _restore(case, snap, slug="wren"):
    """A fresh home built from a snapshot the way an operator restores
    one: the snapshot's data/ plus a cousin.toml (backup never copies
    cousin.toml, docs/operations.md "Backups")."""
    home = _tmpdir(case) / "cousins" / slug
    home.mkdir(parents=True)
    shutil.copytree(snap / "data", home / "data")
    (home / "cousin.toml").write_text(
        '[cousin]\nslug = "%s"\nname = "%s"\n\n[agent]\nrunner = "fake"\n'
        % (slug, slug.capitalize()))
    return home


def _states(home):
    con = sqlite3.connect(home / "data" / "inbox.db")
    try:
        return dict(con.execute("SELECT id, state FROM inbox").fetchall())
    finally:
        con.close()


class TestRunnerStoresInTheSnapshot(HermeticCase):
    def setUp(self):
        self.home = temp_home(self, slug="wren", runner="fake")
        self.dest = _tmpdir(self)

    def test_the_inbox_is_in_the_snapshot(self):
        inbox_id = Inbox(self.home).put(
            Item("operator:priya", "chat", "hello", sender="Priya"))
        snap = backup.snapshot(self.home, self.dest)
        copy = Inbox(snap)  # Inbox(home) reads <home>/data/inbox.db
        self.assertEqual(copy.get(inbox_id)["body"], "hello")
        self.assertEqual(copy.get(inbox_id)["state"], "queued")

    def test_the_event_stream_is_in_the_snapshot(self):
        stream = EventStream(self.home, SESSION)
        stream.append("turn_start", {"inbox_ids": [1]})
        stream.append("text", {"text": "caf\u00e9"})
        snap = backup.snapshot(self.home, self.dest)
        copy = snap / "data" / "stream" / ("%s.jsonl" % SESSION)
        self.assertTrue(copy.is_file(), "no stream file in the snapshot")
        self.assertEqual(copy.read_bytes(), stream.path.read_bytes())
        self.assertEqual(list(EventStream(snap, SESSION).tail()),
                         list(stream.tail()))

    def test_a_partial_last_stream_line_is_not_copied(self):
        stream = EventStream(self.home, SESSION)
        stream.append("turn_start", {"inbox_ids": [1]})
        stream.append("text", {"text": "one"})
        complete = stream.path.read_bytes()
        # a writer caught mid-line, cut inside a two-byte UTF-8 character
        with open(stream.path, "ab") as f:
            f.write(b'{"seq": 3, "kind": "text", "payload": {"text": "caf\xc3')
        snap = backup.snapshot(self.home, self.dest)
        copy = snap / "data" / "stream" / ("%s.jsonl" % SESSION)
        self.assertTrue(copy.is_file(), "no stream file in the snapshot")
        self.assertEqual(copy.read_bytes(), complete)
        restored = EventStream(snap, SESSION)   # its __init__ reads the tail
        self.assertEqual([e["seq"] for e in restored.tail()], [1, 2])
        self.assertEqual(restored.append("text", {"text": "after"}), 3)
        # the source is never written to
        self.assertTrue(stream.path.read_bytes().startswith(complete))
        self.assertGreater(len(stream.path.read_bytes()), len(complete))

    def test_the_cut_finds_a_line_end_further_back_than_one_block(self):
        stream = EventStream(self.home, SESSION)
        stream.append("text", {"text": "one"})
        complete = stream.path.read_bytes()
        with open(stream.path, "ab") as f:
            f.write(b'{"seq": 2, "kind": "text", "payload": {"text": "long')
        with mock.patch.object(backup, "STREAM_BLOCK", 7):
            snap = backup.snapshot(self.home, self.dest)
        copy = snap / "data" / "stream" / ("%s.jsonl" % SESSION)
        self.assertTrue(copy.is_file(), "no stream file in the snapshot")
        self.assertEqual(copy.read_bytes(), complete)

    def test_only_the_allowlisted_non_db_files_are_added_from_data(self):
        # the allowlist: stream/*.jsonl plus backup.RUNNER_STATE
        data_dir = self.home / "data"
        EventStream(self.home, SESSION).append("text", {"text": "x"})
        (data_dir / "runner-session.json").write_text('{"session_id": "s1"}')
        for name in ("generation.txt", "extract-cursor.json",
                     "propose-cursor.json", "proposals.json"):
            (data_dir / name).write_text("{}")
        (data_dir / "decisions.jsonl").write_text("{}\n")
        (data_dir / "handoff.md").write_text("x\n")
        (data_dir / "runner-session.tmp").write_text("{}")  # a writer's temp
        (data_dir / "stream" / "notes.txt").write_text("x\n")
        (data_dir / "stream" / "old").mkdir()
        (data_dir / "stream" / "old" / "x.jsonl").write_text("{}\n")
        (data_dir / "sub").mkdir()
        (data_dir / "sub" / "runner-session.json").write_text("{}")
        snap = backup.snapshot(self.home, self.dest)
        data = snap / "data"
        self.assertEqual(
            sorted(p.relative_to(data).as_posix()
                   for p in data.rglob("*") if p.is_file()),
            ["extract-cursor.json", "generation.txt", "proposals.json",
             "propose-cursor.json", "runner-session.json",
             "stream/%s.jsonl" % SESSION])

    def test_the_runner_state_files_are_in_the_snapshot(self):
        # without them a restored home resets its generation and re-mines
        # turns it already mined (boot.py, runner/extract.py)
        data_dir = self.home / "data"
        state = {"generation.txt": "7\n",
                 "extract-cursor.json": json.dumps({"sess-1": 12}),
                 "propose-cursor.json": json.dumps({"sess-1": 9}),
                 "proposals.json": json.dumps({"sent": []})}
        for name, text in state.items():
            (data_dir / name).write_text(text)
        snap = backup.snapshot(self.home, self.dest)
        for name, text in state.items():
            with self.subTest(name=name):
                self.assertEqual((snap / "data" / name).read_text(), text)

    def test_the_runner_session_files_are_in_the_snapshot(self):
        # runner-session.json is the SDK runner's (runner/sdk.py); a kind
        # may keep its own runner-session-<kind>.json beside it
        data_dir = self.home / "data"
        plain = json.dumps({"session_id": "sess-1", "lane": "store"})
        kind = json.dumps({"session_id": "sess-2", "lane": "cli"})
        (data_dir / "runner-session.json").write_text(plain)
        (data_dir / "runner-session-fake.json").write_text(kind)
        snap = backup.snapshot(self.home, self.dest)
        self.assertEqual(
            (snap / "data" / "runner-session.json").read_text(), plain)
        self.assertEqual(
            (snap / "data" / "runner-session-fake.json").read_text(), kind)

    def _seed_order_home(self):
        data_dir = self.home / "data"
        Inbox(self.home).put(
            Item("operator:priya", "chat", "hello", sender="Priya"))
        for name in ("chat.db", "abc.db", "usage.db"):   # both sides of "inbox"
            con = sqlite3.connect(data_dir / name)
            con.execute("CREATE TABLE t (x)")
            con.commit()
            con.close()
        EventStream(self.home, SESSION).append("text", {"text": "x"})
        (data_dir / "runner-session.json").write_text("{}")
        return data_dir

    def _recorder(self, calls, data_dir, kind, real):
        def wrapper(src, dst):
            calls.append((kind, Path(src).relative_to(data_dir).as_posix()))
            return real(src, dst)
        return wrapper

    def test_the_inbox_is_snapshotted_before_every_other_database(self):
        # a turn commits its reply (chat.db) before it closes its row
        # (inbox.db): with the inbox copied first, a row `done` in the
        # copy always has its reply in the copy
        data_dir = self._seed_order_home()
        calls = []
        with mock.patch.object(backup, "_snapshot_db", self._recorder(
                calls, data_dir, "db", backup._snapshot_db)):
            backup.snapshot(self.home, self.dest)
        self.assertEqual([name for _, name in calls],
                         ["inbox.db", "abc.db", "chat.db", "usage.db"])

    def test_the_streams_come_between_the_inbox_and_the_other_databases(self):
        data_dir = self._seed_order_home()
        calls = []
        with mock.patch.object(backup, "_snapshot_db", self._recorder(
                calls, data_dir, "db", backup._snapshot_db)), \
                mock.patch.object(backup, "_snapshot_stream", self._recorder(
                    calls, data_dir, "stream", backup._snapshot_stream)), \
                mock.patch.object(backup, "_copy_plain", self._recorder(
                    calls, data_dir, "plain", backup._copy_plain)):
            backup.snapshot(self.home, self.dest)
        self.assertEqual(calls, [
            ("db", "inbox.db"), ("stream", "stream/%s.jsonl" % SESSION),
            ("db", "abc.db"), ("db", "chat.db"), ("db", "usage.db"),
            ("plain", "runner-session.json")])

    def test_a_turn_ending_between_any_two_copies_never_restores_a_closed_row_without_its_reply(self):
        """A turn writes its reply (chat.db), its result (the stream), then
        closes its row (inbox.db). Ended between any two copies, the
        restore either answers the row again or closes it with the reply
        in the copy: never closed with the reply missing."""
        data_dir = self._seed_order_home()
        real = {"db": backup._snapshot_db, "stream": backup._snapshot_stream,
                "plain": backup._copy_plain}
        steps = []
        with mock.patch.object(backup, "_snapshot_db", self._recorder(
                steps, data_dir, "db", real["db"])), \
                mock.patch.object(backup, "_snapshot_stream", self._recorder(
                    steps, data_dir, "stream", real["stream"])), \
                mock.patch.object(backup, "_copy_plain", self._recorder(
                    steps, data_dir, "plain", real["plain"])):
            backup.snapshot(self.home, self.dest)
        for k in range(len(steps) + 1):
            with self.subTest(turn_ends_after=steps[k - 1] if k else "nothing"):
                self.home = temp_home(self, slug="wren", runner="fake")
                data_dir = self._seed_order_home()
                inbox = Inbox(self.home)
                (row,) = inbox.claim(limit=1, claimant=SESSION)
                done = []

                def end_turn():
                    con = sqlite3.connect(data_dir / "chat.db")
                    con.execute("INSERT INTO t VALUES ('reply')")
                    con.commit()
                    con.close()
                    EventStream(self.home, SESSION).append(
                        "result", {"inbox_ids": [row["id"]], "is_error": False})
                    inbox.done(row["id"], "delivered", "turn")

                def step(kind):
                    def wrapper(src, dst):
                        out = real[kind](src, dst)
                        done.append(kind)
                        if len(done) == k:
                            end_turn()
                        return out
                    return wrapper
                if k == 0:
                    end_turn()
                dest = _tmpdir(self)
                with mock.patch.object(backup, "_snapshot_db", step("db")), \
                        mock.patch.object(backup, "_snapshot_stream", step("stream")), \
                        mock.patch.object(backup, "_copy_plain", step("plain")):
                    snap = backup.snapshot(self.home, dest)
                home = _restore(self, snap)
                restored = Inbox(home)
                restored.close_recorded()
                con = sqlite3.connect(home / "data" / "chat.db")
                replies = con.execute("SELECT COUNT(*) FROM t").fetchone()[0]
                con.close()
                if restored.get(row["id"])["state"] == "done":
                    self.assertEqual(replies, 1, "a closed row whose reply is not in the copy")

    def test_a_mid_turn_snapshot_restores_and_the_row_is_answered(self):
        inbox = Inbox(self.home)
        first = inbox.put(Item("operator:priya", "chat", "a", sender="Priya"))
        second = inbox.put(Item("operator:priya", "chat", "b", sender="Priya"))
        # mid-turn: a runner (long gone by restore time) holds the first row
        claimed = inbox.claim(limit=1, claimant="pid:99999")
        self.assertEqual([r["id"] for r in claimed], [first])
        stream = EventStream(self.home, SESSION)
        stream.append("state", {"from": "idle", "to": "running", "detail": ""})
        stream.append("turn_start", {"inbox_ids": [first]})
        before = list(stream.tail())

        snap = backup.snapshot(self.home, self.dest)
        home = _restore(self, snap)

        self.assertEqual(_states(home), {first: "claimed", second: "queued"})
        self.assertEqual(
            list(EventStream(home, SESSION).tail()), before)
        self.assertEqual(Inbox(home).requeue_stale(0.0), 1)
        self.assertEqual(_states(home), {first: "queued", second: "queued"})

        rc = runner_main.runner_main(["--home", str(home), "--once"])
        self.assertEqual(rc, 0)
        self.assertEqual(_states(home), {first: "done", second: "done"})
        # answered once each: every row is in exactly one turn's result
        answered = []
        for path in sorted((home / "data" / "stream").glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                event = json.loads(line)
                if event["kind"] == "result":
                    answered.extend(event["payload"]["inbox_ids"])
        self.assertEqual(sorted(answered), [first, second])
        # the restored session's file is intact; the runner wrote its own
        self.assertEqual(list(EventStream(home, SESSION).tail()), before)
