"""Crash points (#286): a real process killed at a named point, then the
recovery checked in the stores. Every registered point is exercised here
or in another test that names it."""
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest import mock

from cousin_lib import crashpoint
from tests._hermetic import HermeticCase

ROOT = pathlib.Path(__file__).resolve().parents[1]


class TestRegistry(HermeticCase):
    def test_every_point_called_is_registered(self):
        called = set()
        for path in (ROOT / "cousin_lib").rglob("*.py"):
            if path.name == "crashpoint.py":
                continue        # its docstring shows the call
            called.update(re.findall(r"""crashpoint\(["']([\w.]+)["']\)""", path.read_text()))
        self.assertEqual(sorted(called - set(crashpoint.POINTS)), [])
        self.assertEqual(sorted(set(crashpoint.POINTS) - called), [], "a registered point nobody calls")

    def test_every_registered_point_is_exercised_by_a_test(self):
        text = "".join(p.read_text() for p in (ROOT / "tests").rglob("test_*.py"))
        self.assertEqual([n for n in crashpoint.POINTS if "CRASH_AT=%s" % n not in text
                          and "%r" % n not in text and '"%s"' % n not in text.replace(
                              "crashpoint(\"%s\")" % n, "")], [])

    def test_an_unregistered_name_raises_only_when_a_point_is_asked_for(self):
        from unittest import mock
        crashpoint.crashpoint("no.such.point")          # unset: a pure no-op
        with mock.patch.object(crashpoint, "_TARGET", ("job.exited", 1)):
            with self.assertRaises(ValueError):
                crashpoint.crashpoint("no.such.point")

    def test_the_target_parses_a_count(self):
        self.assertEqual(crashpoint._target("job.exited:3"), ("job.exited", 3))
        self.assertEqual(crashpoint._target("job.exited"), ("job.exited", 1))
        self.assertIsNone(crashpoint._target(""))


class JobCrashCase(HermeticCase):
    """cousin-job run in a real interpreter with COUSIN_CRASH_AT set."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "wren"
        (self.home / "data").mkdir(parents=True)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n\n[agent]\nrunner = "sdk"\n')
        self.env = dict(os.environ, FRAMEWORK_ROOT=str(self.root), COUSIN_HOME=str(self.home),
                        PYTHONPATH=str(ROOT))

    def start(self, point, *cmd, extra=()):
        env = dict(self.env, COUSIN_CRASH_AT=point)
        code = ("import sys; from cousin_lib.jobs import jobs_main;"
                " sys.exit(jobs_main(sys.argv[1:]))")
        return subprocess.run([sys.executable, "-c", code, "start", "shell", "--json", "--notify",
                               *extra, "--", "crash test", *cmd],
                              env=env, capture_output=True, text=True, timeout=60)

    def rows(self):
        conn = sqlite3.connect(self.root / "data" / "jobs.db")
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM jobs")]
        finally:
            conn.close()

    def notices(self):
        path = self.home / "data" / "inbox.db"
        if not path.exists():
            return []
        conn = sqlite3.connect(path)
        try:
            return [r[0] for r in conn.execute("SELECT body FROM inbox WHERE source='job'")]
        finally:
            conn.close()

    def reap(self):
        code = "from cousin_lib import jobs; print(jobs.reap_lost())"
        out = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True,
                             text=True, timeout=60)
        return out.stdout.strip()

    def wait_running_gone(self, timeout=20):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.reap()
            if all(r["status"] != "running" for r in self.rows()):
                return True
            time.sleep(0.2)
        return False


class TestJobCrashes(JobCrashCase):
    def test_a_runner_killed_after_the_command_exited_leaves_a_lost_row_told_once(self):
        # COUSIN_CRASH_AT=job.exited: the command ran, its runner died
        # before closing the row
        out = self.start("job.exited", "true")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(self.wait_running_gone(), self.rows())
        (row,) = self.rows()
        self.assertEqual(row["status"], "lost")
        self.reap()
        self.assertEqual(len(self.notices()), 1, self.notices())
        self.assertIn("lost", self.notices()[0])

    def test_a_kill_after_the_artifacts_keeps_them_naming_the_lost_job(self):
        # COUSIN_CRASH_AT=job.artifacts_recorded
        target = self.home / "out.bin"
        out = self.start("job.artifacts_recorded", "sh", "-c", "echo x > %s" % target,
                         extra=("--artifact", str(target)))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(self.wait_running_gone(), self.rows())
        (row,) = self.rows()
        self.assertEqual(row["status"], "lost")
        conn = sqlite3.connect(self.root / "data" / "artifacts.db")
        try:
            self.assertEqual([r[0] for r in conn.execute("SELECT job_id FROM artifacts")], [row["id"]])
        finally:
            conn.close()

    def test_a_start_killed_before_the_fork_leaves_no_running_row(self):
        # COUSIN_CRASH_AT=job.registered: the row exists, no process ever ran
        out = self.start("job.registered", "true")
        self.assertEqual(out.returncode, -9, out.stderr)
        self.reap()
        self.assertEqual(self.rows()[0]["status"], "running")     # within the spawn grace
        conn = sqlite3.connect(self.root / "data" / "jobs.db")
        conn.execute("UPDATE jobs SET started_at='2026-01-01T00:00:00+00:00'")
        conn.commit(); conn.close()
        self.assertTrue(self.wait_running_gone(timeout=5), self.rows())
        (row,) = self.rows()
        self.assertEqual(row["status"], "lost")
        self.assertEqual(len(self.notices()), 1)

    def test_a_hook_tracked_background_shell_is_never_taken_for_unforked(self):
        # recording registers a run_in_background Bash with its command and
        # no pid: it runs, and only its exit closes it
        code = ("from cousin_lib import jobs; i = jobs.register_job(kind='shell', title='bg',"
                " description='background shell', spawned_by='wren', command='sleep 300');"
                " c = jobs._db(); c.execute(\"UPDATE jobs SET started_at='2026-01-01T00:00:00+00:00'\");"
                " c.commit(); print(jobs.reap_lost())")
        out = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True,
                             text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "[]", out.stderr)
        self.assertEqual(self.rows()[0]["status"], "running")



class ProducerCrashCase(HermeticCase):
    """A producer killed between its delivery and its own record of it
    (#286, points 3 and 6): the retry after the restart must deliver
    once, never zero times and never twice."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        self.home = self.root / "cousins" / "wren"
        (self.home / "data").mkdir(parents=True)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n\n[agent]\nrunner = "sdk"\n')
        self.env = dict(os.environ, FRAMEWORK_ROOT=str(self.root), PYTHONPATH=str(ROOT))
        self.env.pop("COUSIN_CRASH_AT", None)

    def py(self, code, crash=None):
        env = dict(self.env, **({"COUSIN_CRASH_AT": crash} if crash else {}))
        return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                              text=True, timeout=60)

    def inbox(self, source):
        if not (self.home / "data" / "inbox.db").exists():
            return []
        conn = sqlite3.connect(self.home / "data" / "inbox.db")
        try:
            return [r[0] for r in conn.execute("SELECT body FROM inbox WHERE source=?", (source,))]
        finally:
            conn.close()


ACCEPT = r"""
import json, os, pathlib, sys, time
from cousin_lib import peer_inbound
root = pathlib.Path(os.environ["FRAMEWORK_ROOT"])
try:
    out = peer_inbound.accept(root, identity="kestrel", display="Kestrel", to="wren",
                              message="the tins moved", msg_id="m-crash-0001",
                              sent_at=time.time(), allowed=lambda slug: True)
    print(json.dumps({"ok": bool(out.get("ok"))}))
except peer_inbound.Refused as err:
    print(json.dumps({"status": err.status}))
"""


class TestPeerCrashes(ProducerCrashCase):
    def test_a_gate_killed_after_the_id_was_seen_delivers_on_the_retry(self):
        # COUSIN_CRASH_AT=peer.seen: the id is recorded, nothing delivered.
        # The retry used to hear 409 "already delivered": a silent loss.
        first = self.py(ACCEPT, crash="peer.seen")
        self.assertEqual(first.returncode, -9, first.stderr)
        self.assertEqual(self.inbox("chat"), [])
        retry = self.py(ACCEPT)
        self.assertEqual(json.loads(retry.stdout), {"ok": True}, retry.stderr)
        self.assertEqual(self.inbox("chat"), ["the tins moved"])
        again = self.py(ACCEPT)
        self.assertEqual(json.loads(again.stdout), {"status": 409}, again.stderr)
        self.assertEqual(self.inbox("chat"), ["the tins moved"])


DRAIN = r"""
import json, os, pathlib, sys, time, io, email.message, urllib.error
from cousin_lib import outbox, peer_inbound
root = pathlib.Path(os.environ["FRAMEWORK_ROOT"])
def send(peer, message, msg_id):
    # the peer's gate, in-process: this root's wren is the receiver
    try:
        peer_inbound.accept(root, identity="testbed", display="Testbed", to="wren",
                            message=message, msg_id=msg_id, sent_at=time.time(),
                            allowed=lambda slug: True)
    except peer_inbound.Refused as err:
        raise urllib.error.HTTPError("http://192.0.2.20:8600/peer/send", err.status, str(err),
                                     email.message.Message(), io.BytesIO(b"{}"))
print(json.dumps(outbox.drain(root, send=send)))
"""


class TestOutboxCrashes(ProducerCrashCase):
    def setUp(self):
        super().setUp()
        secrets = self.root / ".secrets" / "peers"
        secrets.mkdir(parents=True)
        (secrets / "kestrel").write_text("peer-secret-4f9a1c0b27de81d2e4f09a3c5c7e9b1d\n")
        os.chmod(secrets / "kestrel", 0o600)
        (self.root / "config" / "external-peers.toml").write_text(
            '[peers.kestrel]\nurl = "http://192.0.2.20:8600"\nreach = []\n'
            'token_file = ".secrets/peers/kestrel"\nsender = "testbed"\n')
        from cousin_lib import outbox
        outbox.enqueue(self.root, msg_id="m-outbox-0001", sender="wren", dest="kestrel",
                       message="kept for a retry", error="timed out")
        conn = sqlite3.connect(outbox.store_path(self.root))
        conn.execute("UPDATE outbox SET next_at = 0")
        conn.commit(); conn.close()

    def test_a_drain_killed_after_the_send_finishes_it_delivered_once(self):
        # COUSIN_CRASH_AT=outbox.sent: the peer took it, the row still pending
        first = self.py(DRAIN, crash="outbox.sent")
        self.assertEqual(first.returncode, -9, first.stderr)
        again = self.py(DRAIN)
        self.assertEqual(json.loads(again.stdout)["delivered"], ["m-outbox-0001"], again.stderr)
        self.assertEqual(self.inbox("chat"), ["kept for a retry"])
        from cousin_lib import outbox
        (row,) = outbox.list_rows(self.root)
        self.assertEqual(row["state"], "delivered")


TICK = r"""
from cousin_lib import schedule
print(schedule.tick(now_ts=2000000000, deliver=schedule._default_deliver))
"""


class TestScheduleCrashes(ProducerCrashCase):
    def test_a_tick_killed_after_the_delivery_does_not_deliver_twice(self):
        # COUSIN_CRASH_AT=schedule.delivered: in the inbox, not marked fired
        out = self.py("from cousin_lib import schedule; schedule._db().close()")
        self.assertEqual(out.returncode, 0, out.stderr)
        conn = sqlite3.connect(self.root / "data" / "scheduled.db")
        conn.execute("INSERT INTO scheduled_jobs (cousin, target_ts, prompt, created_at)"
                     " VALUES ('wren', 1999999000, 'water the tins', 1999990000)")
        conn.commit(); conn.close()
        first = self.py(TICK, crash="schedule.delivered")
        self.assertEqual(first.returncode, -9, first.stderr)
        self.assertEqual(len(self.inbox("schedule")), 1)
        again = self.py(TICK)
        self.assertEqual(again.stdout.strip(), "1", again.stderr)
        self.assertEqual(len(self.inbox("schedule")), 1, self.inbox("schedule"))
        conn = sqlite3.connect(self.root / "data" / "scheduled.db")
        try:
            self.assertEqual(conn.execute("SELECT status FROM scheduled_jobs").fetchone()[0], "fired")
        finally:
            conn.close()


CHAT = r"""
import json, os, pathlib, sys
from cousin_lib import chat
from cousin_lib.config import CousinConfig
from cousin_lib.server import chat_api
wren = CousinConfig.load(pathlib.Path(os.environ["FRAMEWORK_ROOT"]) / "cousins" / "wren")
if sys.argv[1] == "send":
    print(json.dumps(chat.deliver_local(wren, {"user": "Priya", "message": "the tins moved"})))
else:
    print(json.dumps(chat_api.redeliver_pending(wren, older_than_s=0)))
"""


class TestChatCrashes(ProducerCrashCase):
    """(#294, point 7) an inbound chat row and its inbox row are two
    stores: a sender killed between them used to leave a message shown in
    the chat that the cousin never got."""

    def chat(self, what, crash=None):
        env = dict(self.env, **({"COUSIN_CRASH_AT": crash} if crash else {}))
        return subprocess.run([sys.executable, "-c", CHAT, what], env=env,
                              capture_output=True, text=True, timeout=60)

    def chat_rows(self):
        conn = sqlite3.connect(self.home / "data" / "chat.db")
        try:
            return conn.execute("SELECT id, message, delivery FROM messages").fetchall()
        finally:
            conn.close()

    def chat_key(self):
        conn = sqlite3.connect(self.home / "data" / "chat.db")
        try:
            return conn.execute("SELECT delivery_key FROM messages").fetchone()[0]
        finally:
            conn.close()

    def inbox_rows(self):
        if not (self.home / "data" / "inbox.db").exists():
            return []
        conn = sqlite3.connect(self.home / "data" / "inbox.db")
        try:
            return conn.execute("SELECT message_id, dedup_key FROM inbox"
                                " WHERE source='chat'").fetchall()
        finally:
            conn.close()

    def test_a_sender_killed_before_the_put_is_delivered_by_the_sweep(self):
        # COUSIN_CRASH_AT=chat.stored
        first = self.chat("send", crash="chat.stored")
        self.assertEqual(first.returncode, -9, first.stderr)
        ((cid, _m, state),) = self.chat_rows()
        self.assertEqual((state, self.inbox_rows()), ("pending", []))
        swept = self.chat("sweep")
        self.assertEqual(json.loads(swept.stdout), [cid], swept.stderr)
        self.assertTrue(self.chat_key().startswith("chat:"))
        self.assertEqual(self.inbox_rows(), [(cid, self.chat_key())])
        self.assertEqual(self.chat_rows(), [(cid, "the tins moved", None)])
        self.assertEqual(json.loads(self.chat("sweep").stdout), [])

    def test_a_sender_killed_after_the_put_leaves_one_inbox_row(self):
        # COUSIN_CRASH_AT=chat.put
        first = self.chat("send", crash="chat.put")
        self.assertEqual(first.returncode, -9, first.stderr)
        ((cid, _m, state),) = self.chat_rows()
        self.assertEqual(state, "pending")
        self.assertEqual(len(self.inbox_rows()), 1)
        swept = self.chat("sweep")
        self.assertEqual(json.loads(swept.stdout), [cid], swept.stderr)
        self.assertEqual(self.inbox_rows(), [(cid, self.chat_key())])
        self.assertEqual(self.chat_rows()[0][2], None)

    def test_a_peer_retry_after_the_row_was_stored_shows_one_line(self):
        # the gate died after the chat row, before the put: the retry
        # delivers that row, never a second line
        first = self.py(ACCEPT, crash="chat.stored")
        self.assertEqual(first.returncode, -9, first.stderr)
        retry = self.py(ACCEPT)
        self.assertEqual(json.loads(retry.stdout), {"ok": True}, retry.stderr)
        ((cid, message, state),) = self.chat_rows()
        self.assertEqual((message, state), ("the tins moved", None))
        self.assertEqual(self.inbox_rows(), [(cid, "peer:kestrel:m-crash-0001")])
        self.assertEqual(json.loads(self.chat("sweep").stdout), [])


HANDOFF = r"""
import os, pathlib
from cousin_lib.runner import tools
from cousin_lib.runner.policy import Policy
from cousin_lib.runner.turn import Turn
root = pathlib.Path(os.environ["FRAMEWORK_ROOT"])
home = root / "cousins" / "wren"
ctx = tools.ToolContext(home=home, slug="wren", name="Wren", root=root, turn=Turn(),
                        policy=Policy.load(home), stream=None)
print(tools.call(ctx, "handoff", {"position": "new position", "next_action": "new next",
                                  "status": "- [ ] new loop", "active_threads": ["new thread"]}))
"""

TRASH = r"""
import os, pathlib, sys
from cousin_lib import memory_trash
home = pathlib.Path(os.environ["COUSIN_HOME"])
if sys.argv[1] == "trash":
    memory_trash.trash_lines(home, [("memory/raw/2026-10-09.jsonl", 2, None)], by="test")
else:
    import json
    print(json.dumps(memory_trash.list_trash(home)))
"""


class TestWholeFileCrashes(ProducerCrashCase):
    """(#294, point 8) a file rewritten whole is replaced in one step: a
    kill leaves the old file or the new one, never a torn one."""

    def setUp(self):
        super().setUp()
        self.env["COUSIN_HOME"] = str(self.home)
        (self.home / "STATUS.md").write_text("# Status\n\n## Open loops\n\n- [ ] old loop\n")
        (self.home / "data" / "handoff.md").write_text("# Handoff - old\n")

    def test_a_handoff_killed_mid_write_leaves_whole_files(self):
        old_status = (self.home / "STATUS.md").read_bytes()
        # COUSIN_CRASH_AT=atomic.written:1: STATUS.md's new text is in its
        # temp file, not yet over STATUS.md
        out = self.py(HANDOFF, crash="atomic.written:1")
        self.assertEqual(out.returncode, -9, out.stderr)
        self.assertEqual((self.home / "STATUS.md").read_bytes(), old_status)
        self.assertEqual((self.home / "data" / "handoff.md").read_text(), "# Handoff - old\n")
        self.assertEqual(len(list(self.home.glob(".STATUS.md.*.tmp"))), 1)
        # the second: STATUS.md is new, handoff.md still whole
        out = self.py(HANDOFF, crash="atomic.written:2")
        self.assertEqual(out.returncode, -9, out.stderr)
        self.assertIn("new loop", (self.home / "STATUS.md").read_text())
        self.assertEqual((self.home / "data" / "handoff.md").read_text(), "# Handoff - old\n")
        out = self.py(HANDOFF)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("new position", (self.home / "data" / "handoff.md").read_text())

    def raw(self):
        path = self.home / "memory" / "raw" / "2026-10-09.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"n": 1}\n{"n": 2}\n{"n": 3}\n')
        return path

    def age_batches(self):
        for manifest in (self.home / "memory").rglob("manifest.json"):
            os.utime(manifest, (time.time() - 3600, time.time() - 3600))

    def test_a_trash_killed_before_the_move_leaves_the_line_and_no_batch(self):
        # COUSIN_CRASH_AT=trash.moving: the moving manifest holds the line,
        # nothing moved yet
        path = self.raw()
        out = self.py(TRASH.replace("sys.argv[1]", "'trash'"), crash="trash.moving")
        self.assertEqual(out.returncode, -9, out.stderr)
        self.age_batches()
        listed = self.py(TRASH.replace("sys.argv[1]", "'list'"))
        self.assertEqual(json.loads(listed.stdout), [], listed.stderr)
        self.assertIn('{"n": 2}', path.read_text())

    def test_a_trash_killed_after_the_move_keeps_the_line_in_the_batch(self):
        # the line is out of its file, the final manifest not yet written
        # (the second atomic write): before #294 the line was kept nowhere
        path = self.raw()
        out = self.py(TRASH.replace("sys.argv[1]", "'trash'"), crash="atomic.written:2")
        self.assertEqual(out.returncode, -9, out.stderr)
        self.assertNotIn('{"n": 2}', path.read_text())
        self.age_batches()
        listed = self.py(TRASH.replace("sys.argv[1]", "'list'"))
        (batch,) = json.loads(listed.stdout)
        self.assertNotIn("moving", batch)
        self.assertEqual([i["line"] for i in batch["items"]], ['{"n": 2}'])
        from cousin_lib import memory_trash
        memory_trash.restore(self.home, batch["id"])
        self.assertEqual(path.read_text(), '{"n": 1}\n{"n": 2}\n{"n": 3}\n')


class TestChatRedeliveryHealth(ProducerCrashCase):
    def test_a_row_the_inbox_will_not_take_stays_pending_and_fails_the_health_line(self):
        from unittest import mock
        from cousin_lib import delivery, loops
        from cousin_lib.server.storage import ChatStore
        store = ChatStore(self.home / "data" / "chat.db")
        try:
            row = store.add_message(chat_user="priya", user="Priya", message="stuck",
                                    msg_type="user", pending=True)
        finally:
            store.close()
        conn = sqlite3.connect(self.home / "data" / "chat.db")
        conn.execute("UPDATE messages SET timestamp='2026-01-01T00:00:00+00:00'")
        conn.commit(); conn.close()
        report = {"errors": []}
        with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(self.root)}), \
                mock.patch.object(delivery.InboxBackend, "send", return_value=delivery.FAILED):
            self.assertEqual(loops._redeliver_chat(report), [])
        self.assertEqual(len(report["errors"]), 1, report)
        self.assertIn("chat rows [%d] still pending" % row["id"], report["errors"][0])
        conn = sqlite3.connect(self.home / "data" / "chat.db")
        try:
            self.assertEqual(conn.execute("SELECT delivery FROM messages").fetchone()[0], "pending")
        finally:
            conn.close()


class TestTrashSettle(ProducerCrashCase):
    def setUp(self):
        super().setUp()
        self.env["COUSIN_HOME"] = str(self.home)

    def test_a_trashed_line_with_a_twin_left_is_kept_by_the_settle(self):
        path = self.home / "memory" / "raw" / "2026-10-09.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"n": 1}\n{"n": 2}\n{"n": 2}\n')
        out = self.py(TRASH.replace("sys.argv[1]", "'trash'"), crash="atomic.written:2")
        self.assertEqual(out.returncode, -9, out.stderr)
        self.assertEqual(path.read_text(), '{"n": 1}\n{"n": 2}\n')
        (batch,) = json.loads(self.py(TRASH.replace("sys.argv[1]", "'list'")).stdout)
        self.assertEqual([i["line"] for i in batch["items"]], ['{"n": 2}'])

    def test_a_batch_whose_mover_holds_the_lock_is_left_alone(self):
        import threading
        from cousin_lib import memory_lock, memory_trash
        batch = self.home / "memory" / ".trash" / "20261009T000000-000001"
        batch.mkdir(parents=True)
        (batch / "manifest.json").write_text(json.dumps(
            {"id": batch.name, "moving": True, "items": [{"kind": "file", "path": "notes/x.md"}]}))
        held, release = threading.Event(), threading.Event()

        def mover():
            with memory_lock.write_lock(self.home):
                held.set()
                release.wait(10)
        t = threading.Thread(target=mover)
        t.start()
        try:
            self.assertTrue(held.wait(10))
            with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": str(self.root),
                                              "COUSIN_HOME": str(self.home)}):
                listed = memory_trash.list_trash(self.home)
            self.assertEqual([b.get("moving") for b in listed], [True])
            self.assertTrue(batch.is_dir())
        finally:
            release.set()
            t.join(10)
