"""Two sessions of one cousin write memory at the same moment and nothing
is lost (master plan phase 8): one memory writer at a time per home,
across threads and processes, reentrant within one thread.

Each race test holds session A inside the window where an unserialized
writer loses the other's write (after its read, before its write) until
session B has finished, or PAUSE_S has passed. Without the lock B runs
inside A's window and one write is lost; with it, B waits for A, A's
pause times out, and both land."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from cousin_lib import distill, memory, memory_trash, raw_fold, reinforce
from cousin_lib.runner import extract, tools
from cousin_lib.runner.policy import Policy
from cousin_lib.runner.turn import Turn
from tests._hermetic import HermeticCase

PAUSE_S = 0.5


def _home(case):
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    home = pathlib.Path(tmp.name) / "cousins" / "wren"
    (home / "data").mkdir(parents=True)
    memory.ensure_layout(home)
    return home


def _ctx(home, slug="wren"):
    return tools.ToolContext(home=home, slug=slug, name=slug.capitalize(),
                             root=home.parent.parent, turn=Turn(), policy=Policy())


class _Pause:
    """Wraps a function so the thread named "session-a" stops right after
    it returns (once, and only when `when(*args)` holds) until "session-b"
    is done or PAUSE_S has passed."""

    def __init__(self):
        self.reached = threading.Event()
        self.other_done = threading.Event()

    def wrap(self, fn, when=lambda *a, **k: True):
        def wrapped(*a, **k):
            out = fn(*a, **k)
            if threading.current_thread().name == "session-a" and not self.reached.is_set() \
                    and when(*a, **k):
                self.reached.set()
                self.other_done.wait(PAUSE_S)
            return out
        return wrapped


class RaceCase(HermeticCase):
    def race(self, pause, first, second):
        errors = []

        def run(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - the test reports it
                errors.append(exc)

        a = threading.Thread(target=run, args=(first,), name="session-a")
        a.start()
        self.assertTrue(pause.reached.wait(5), "session A never reached its window")

        def second_then_done():
            run(second)
            pause.other_done.set()

        b = threading.Thread(target=second_then_done, name="session-b")
        b.start()
        a.join(10)
        b.join(10)
        self.assertFalse(a.is_alive() or b.is_alive(), "a session never finished")
        self.assertEqual(errors, [])


def _decisions(home):
    out = []
    for path in sorted((home / "data").glob("decisions*.jsonl")):
        for line in path.read_text().splitlines():
            out.append(json.loads(line)["decision"])
    return out


class TestTwoSessionsWriteMemory(RaceCase):
    def test_two_sessions_write_memory_concurrently_without_loss(self):
        """The master plan's test, through the tool layer: two sessions'
        tool contexts decide at once while the log rotates."""
        home = _home(self)
        with open(home / "data" / "decisions.jsonl", "w") as fh:
            for i in range(3):
                fh.write(json.dumps({"timestamp": "2030-01-01T10:00:00+00:00",
                                     "topic": "old", "decision": "old %d" % i,
                                     "reasoning": "r"}) + "\n")
        pause = _Pause()
        real_read = pathlib.Path.read_text
        calls = []

        def decide(ctx, text):
            out, is_error = tools.call(ctx, "memory", {"command": "decide", "topic": "ledger",
                                                       "decision": text, "reasoning": "why"})
            calls.append((text, is_error, out))

        with mock.patch.object(memory, "DECISIONS_ROTATE_BYTES", 1), \
                mock.patch.object(memory, "DECISIONS_KEEP_TAIL", 1), \
                mock.patch.object(pathlib.Path, "read_text", pause.wrap(
                    real_read, when=lambda self, *a, **k: self.name == "decisions.jsonl")):
            self.race(pause, lambda: decide(_ctx(home), "Priya closes it"),
                      lambda: decide(_ctx(home, "wren"), "Sam keeps the keys"))
        self.assertEqual([c[1] for c in calls], [False, False], calls)
        logged = _decisions(home)
        self.assertIn("Priya closes it", logged)
        self.assertIn("Sam keeps the keys", logged)
        raw = [json.loads(line)["content"] for path in memory.raw_dir(home).glob("*.jsonl")
               for line in path.read_text().splitlines()]
        self.assertEqual(sorted(c.split(" - why:")[0] for c in raw),
                         ["Priya closes it", "Sam keeps the keys"])

    def test_two_sessions_recall_counts_are_both_kept(self):
        home = _home(self)
        pause = _Pause()
        with mock.patch.object(reinforce, "load_counts", pause.wrap(reinforce.load_counts)):
            self.race(pause, lambda: reinforce.record(home, ["memory/ledger.md"], query="ledger"),
                      lambda: reinforce.record(home, ["memory/keys.md"], query="keys"))
        counts = reinforce.load_counts(home)
        self.assertEqual({k: v["count"] for k, v in counts.items()},
                         {"memory/ledger.md": 1, "memory/keys.md": 1})

    def test_two_sessions_mining_keep_both_cursors(self):
        home = _home(self)

        class Store:
            def entries_after(self, session_id, cursor):
                return [], cursor + 5

        pause = _Pause()
        results = []
        with mock.patch.object(extract, "_load", pause.wrap(extract._load)):
            self.race(pause,
                      lambda: results.append(extract.mine_turn(home, "sess-primary", 1,
                                                               store=Store())),
                      lambda: results.append(extract.mine_turn(home, "sess-peer", 1,
                                                               store=Store())))
        self.assertEqual(results, [0, 0])
        state = json.loads((home / "data" / "extract-cursor.json").read_text())
        self.assertEqual(state, {"sess-peer": 5, "sess-primary": 5})

    def test_two_sessions_distilling_at_once_both_finish(self):
        home = _home(self)
        memory.remember(home, "ledger cadence", "Priya closes the ledger on the first Monday.")
        pause = _Pause()
        real_write = pathlib.Path.write_text
        with mock.patch.object(pathlib.Path, "write_text", pause.wrap(
                real_write, when=lambda self, *a, **k: self.name.endswith(".md.tmp"))):
            self.race(pause, lambda: distill.distill(home), lambda: distill.distill(home))
        self.assertIn("ledger cadence",
                      (memory.distilled_dir(home) / "decisions.md").read_text())


class TestWholeSections(RaceCase):
    def test_a_distill_that_read_raw_first_does_not_write_last(self):
        """Review M7: the read of raw and the write of the views are one
        section. A distill that read raw before another session remembered
        and distilled must not overwrite that session's newer views."""
        home = _home(self)
        memory.remember(home, "ledger cadence", "Priya closes the ledger on the first Monday.")
        pause = _Pause()

        def remember_then_distill():
            memory.remember(home, "spare keys", "Toki keeps the spare keys.")
            distill.distill(home)

        with mock.patch.object(memory, "list_raw", pause.wrap(memory.list_raw)):
            self.race(pause, lambda: distill.distill(home), remember_then_distill)
        text = (memory.distilled_dir(home) / "decisions.md").read_text()
        self.assertIn("spare keys", text)
        self.assertIn("ledger cadence", text)


def _old_day(days=40):
    from datetime import date, timedelta
    return (date.today() - timedelta(days=days)).isoformat()


def _every_raw_line(home):
    """Every raw line, the day files and the fold's gzip archives alike."""
    import gzip
    out = []
    for path in memory.raw_dir(home).rglob("*.jsonl*"):
        if path.name.endswith(".jsonl.gz"):
            with gzip.open(path, "rt") as fh:
                out.extend(fh.read().splitlines())
        elif not path.name.endswith("-digest.jsonl"):
            out.extend(path.read_text().splitlines())
    return out


class TestRawFoldAndTrash(RaceCase):
    """Review P8-5 and the trash: the raw fold and the trash's rewrite are
    read-then-replace (or read-then-unlink) on a raw file another session
    appends to; under the lock the append waits and is kept."""

    def test_a_backfill_append_racing_the_raw_fold_is_kept(self):
        """The backfill appends a decision to its own (old) day file; the
        fold reads that file, archives it and unlinks it. An append between
        the read and the unlink was lost for good."""
        home = _home(self)
        day = _old_day()
        (memory.raw_dir(home) / ("%s.jsonl" % day)).write_text(json.dumps(
            {"timestamp": day + "T09:00:00+00:00", "topic": "ledger",
             "content": "Priya closes the ledger.", "source": "remember"}) + "\n")
        (home / "data" / "decisions.jsonl").write_text(json.dumps(
            {"timestamp": day + "T10:00:00+00:00", "topic": "keys",
             "decision": "Sam keeps the keys", "reasoning": "he is home"}) + "\n")
        pause = _Pause()
        real_read = pathlib.Path.read_text
        with mock.patch.object(pathlib.Path, "read_text", pause.wrap(
                real_read, when=lambda self, *a, **k: self.name == "%s.jsonl" % day)):
            self.race(pause, lambda: raw_fold.fold_raw(home),
                      lambda: memory.backfill_decisions(home))
        contents = [json.loads(line).get("content") for line in _every_raw_line(home)]
        self.assertIn("Sam keeps the keys - why: he is home", contents)
        self.assertIn("Priya closes the ledger.", contents)

    def test_an_append_racing_the_trash_rewrite_is_kept(self):
        """The trash checks the file did not change, then replaces it: an
        append between the check and the replace was lost."""
        home = _home(self)
        memory.remember(home, "ledger", "Priya closes the ledger.")
        memory.remember(home, "audit", "Mallory audits in March.")
        raw = next(memory.raw_dir(home).glob("*.jsonl"))
        rel = "memory/raw/%s" % raw.name
        pause = threading.Event(), threading.Event()     # reached, other_done
        real_replace = os.replace

        def replace(src, dst, *a, **k):
            if threading.current_thread().name == "session-a" and not pause[0].is_set() \
                    and pathlib.Path(dst) == raw:
                pause[0].set()
                pause[1].wait(PAUSE_S)
            return real_replace(src, dst, *a, **k)

        class Before:
            reached, other_done = pause

        with mock.patch.object(memory_trash.os, "replace", replace):
            self.race(Before, lambda: memory_trash.trash_lines(home, [(rel, 1, None)]),
                      lambda: memory.remember(home, "keys", "Toki keeps the spare keys."))
        contents = [json.loads(line)["content"] for line in raw.read_text().splitlines()]
        self.assertEqual(contents, ["Mallory audits in March.", "Toki keeps the spare keys."])


class TestTheLock(HermeticCase):
    def test_a_lock_file_this_user_cannot_write_still_locks(self):
        """Review M6: flock needs no write access; a lock file another uid
        created (read-only to the cousin) must not stop a memory write."""
        home = _home(self)
        path = home / "data" / ".memory-write.lock"
        path.write_text("")
        path.chmod(0o444)
        self.addCleanup(path.chmod, 0o644)
        out = memory.remember(home, "keys", "Toki keeps the spare keys.")
        self.assertTrue(out.startswith("Remembered [keys]"), out)

    def test_the_lock_is_reentrant_in_one_thread(self):
        from cousin_lib import memory_lock
        home = _home(self)
        done = []

        def nested():
            with memory_lock.write_lock(home):
                memory.decide(home, "ledger", "Priya closes it", "why")   # takes it again
                done.append(True)

        t = threading.Thread(target=nested, daemon=True)
        t.start()
        t.join(5)
        self.assertEqual(done, [True], "a nested acquisition in one thread deadlocked")

    def test_a_second_thread_waits_for_the_holder(self):
        from cousin_lib import memory_lock
        home = _home(self)
        order = []
        held = threading.Event()

        def holder():
            with memory_lock.write_lock(home):
                held.set()
                time.sleep(0.3)
                order.append("holder done")

        t = threading.Thread(target=holder)
        t.start()
        self.assertTrue(held.wait(5))
        memory.remember(home, "keys", "Toki keeps the spare keys.")
        order.append("remember done")
        t.join(5)
        self.assertEqual(order, ["holder done", "remember done"])

    def test_another_process_holding_the_lock_makes_a_write_wait(self):
        home = _home(self)
        path = home / "data" / ".memory-write.lock"
        code = ("import fcntl, os, sys, time\n"
                "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
                "fcntl.flock(fd, fcntl.LOCK_EX)\n"
                "print('held', flush=True)\n"
                "time.sleep(0.8)\n")
        proc = subprocess.Popen([sys.executable, "-c", code, str(path)],
                                stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.wait)
        self.assertEqual(proc.stdout.readline().strip(), "held")
        t0 = time.monotonic()
        memory.remember(home, "keys", "Toki keeps the spare keys.")
        self.assertGreaterEqual(time.monotonic() - t0, 0.5)
        proc.stdout.close()

    def test_many_concurrent_writes_from_two_sessions_all_parse(self):
        """Guard: appends were already whole lines; the lock keeps them so."""
        home = _home(self)

        def burst(ctx, who):
            for i in range(50):
                out, is_error = tools.call(ctx, "memory", {"command": "remember",
                                                           "topic": "burst %s" % who,
                                                           "fact": "%s fact %d " % (who, i) * 40})
                assert not is_error, out

        threads = [threading.Thread(target=burst, args=(_ctx(home), who))
                   for who in ("primary", "peer")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        lines = [line for path in memory.raw_dir(home).glob("*.jsonl")
                 for line in path.read_text().splitlines()]
        self.assertEqual(len(lines), 100)
        self.assertEqual(len([json.loads(line) for line in lines]), 100)


if __name__ == "__main__":
    unittest.main()
