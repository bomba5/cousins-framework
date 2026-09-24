"""Phase 6's exit criteria, run for real against the image and compose.

On a machine with only git and docker: `docker compose up`, the console
answers, the supervisor runs it and the loops daemon; a cousin spawned
from the console answers a message sent through the console; `docker
compose down` then `up` loses no message and no session, and a cousin
the operator stopped stays stopped; the compressed image is within its
budget. These compose the work of the whole phase (the supervisor, its
holds, the runner lane's start, the image, compose): each is a guard of
that composition, not of one function.

The compose classes are opt-in, as every docker test is: COUSIN_DOCKER=1
and a docker binary on PATH. COUSIN_DOCKER_IMAGE names an image already
built; without it one is built from this checkout and removed at the end.
Each stack is a copy of compose.yml plus a compose.override.yml (the
file an operator writes the same way) that points it at that image and
publishes the console on a free loopback port. The project is named
COUSIN_DOCKER_PROJECT (default "cfp6exit") plus a random suffix, and is
removed with its volume at the end, whatever happened. Every wait has a
deadline. The doc checks at the end run everywhere.
"""
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import tempfile
import time
import tomllib
import unittest
import urllib.error
import urllib.request
import uuid

_REPO = pathlib.Path(__file__).resolve().parents[1]
_COMPOSE = _REPO / "compose.yml"
_SIZE = _REPO / "docker" / "image-size.sh"
_VERSION = tomllib.loads((_REPO / "pyproject.toml").read_text())["project"]["version"]
# Phase 6's CHANGELOG entry is found by what it says, never by its
# number or its place: a release landing before or after it renumbers it
# and moves the top of the file, never what phase 6 shipped.
_PHASE6_NEEDLE = "cousin-supervisor"


def _phase6_entry(text):
    """(version, body, the version of the entry right below it) of the
    CHANGELOG entry whose body names cousin-supervisor."""
    heads = list(re.finditer(r"^## (\d+\.\d+\.\d+) - .*$", text, re.M))
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[head.end():end]
        if _PHASE6_NEEDLE in body:
            below = heads[i + 1].group(1) if i + 1 < len(heads) else None
            return head.group(1), body, below
    raise AssertionError("no CHANGELOG entry names %s" % _PHASE6_NEEDLE)
_UP_S = 120          # compose up until the console answers /api/version
_ANSWER_S = 90       # a delivered row until the fake runner closes it
_DOWN_S = 120        # compose down: the ordered stop is at most 45 s
_EXEC_S = 60

# Timings and sizes this module measured, printed at the end of a run.
MEASURED = {}


def _docker_enabled():
    return os.environ.get("COUSIN_DOCKER") == "1" and bool(shutil.which("docker"))


def _docker(*args, timeout=600, check=True, cwd=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("COMPOSE_")}
    r = subprocess.run(["docker"] + list(args), capture_output=True, text=True,
                       timeout=timeout, cwd=cwd, env=env)
    if check and r.returncode != 0:
        raise AssertionError("docker %s: rc %d\n%s%s"
                             % (" ".join(args), r.returncode, r.stdout, r.stderr))
    return r


_IMAGE = {"name": None, "built": False}


def setUpModule():
    if not _docker_enabled():
        return
    name = os.environ.get("COUSIN_DOCKER_IMAGE")
    if not name:
        name = "cousins-framework:test-%s" % uuid.uuid4().hex[:8]
        started = time.monotonic()
        _docker("build", "-t", name, str(_REPO), timeout=1800)
        MEASURED["image build (s)"] = round(time.monotonic() - started, 1)
        _IMAGE["built"] = True
    _IMAGE["name"] = name


def tearDownModule():
    if _IMAGE["built"]:
        _docker("image", "rm", "-f", _IMAGE["name"], check=False)
    if MEASURED:
        print("\nphase 6 exit criteria, measured:")
        for key, value in MEASURED.items():
            print("  %s: %s" % (key, value))


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for(predicate, timeout, step=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return None


# Run with `python -c` in a one-off container on the volume while the
# stack is down, argv: <slug> <sender> <body>. It files the message as a
# chat message from that sender is filed, and with no runner live the
# put is the delivery: the row waits, queued, for the next runner.
_DELIVER = """
import sys
from cousin_lib import delivery
from cousin_lib.config import CousinConfig
home = "/data/cousins/" + sys.argv[1]
thread = delivery.thread_for_chat(CousinConfig.load(home), sys.argv[2])
item = delivery.Item(thread_id=thread, source="chat", sender=sys.argv[2], body=sys.argv[3])
print(thread, delivery.deliver(home, item, wait=False))
"""

# Run inside the container, argv: <slug>. What the volume carries for one
# cousin: its inbox rows, its event stream files ([size, sha256] each), the
# inbox ids the stream records as answered, its hold marker and the
# runner's session files.
_STATE = """
import hashlib, json, sqlite3, sys
from pathlib import Path
home = Path("/data/cousins") / sys.argv[1]
data = home / "data"
def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
cols = ("id", "thread_id", "source", "sender", "body", "state", "outcome", "done_at")
rows = []
if (data / "inbox.db").is_file():
    conn = sqlite3.connect(data / "inbox.db", timeout=10)
    try:
        rows = [dict(zip(cols, r)) for r in conn.execute(
            "SELECT %s FROM inbox ORDER BY id" % ", ".join(cols))]
    except sqlite3.OperationalError:
        rows = []                      # the file exists before its schema
    finally:
        conn.close()
stream, answered = {}, []
for p in sorted((data / "stream").glob("*.jsonl")):
    stream[p.name] = [p.stat().st_size, sha(p)]
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue                   # a line still being written
        if event.get("kind") == "result":
            answered += event["payload"].get("inbox_ids") or []
print(json.dumps({"inbox_db": (data / "inbox.db").is_file(), "rows": rows,
                  "stream": stream, "answered": answered,
                  "held": sha(home / "run" / "held"),
                  "sessions": {n: sha(data / n) for n in ("sessions.db", "runner-session.json")}}))
"""

# Run inside the container, argv: <path> <text>: write one file as the
# container's user, its directories included (`~` is the container's HOME).
_WRITE = """
import sys
from pathlib import Path
path = Path(sys.argv[1]).expanduser()
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(sys.argv[2])
"""

# Run inside the container, argv: <path>...: {path: sha256 | null}
# (`~` is the container's HOME).
_SHA = """
import hashlib, json, sys
from pathlib import Path
print(json.dumps({p: hashlib.sha256(Path(p).expanduser().read_bytes()).hexdigest()
                  if Path(p).expanduser().is_file() else None for p in sys.argv[1:]}))
"""

# Run inside the container, argv: <path> <size>: the sha256 of the file's
# first <size> bytes (an append-only file keeps what it had).
_PREFIX = """
import hashlib, sys
with open(sys.argv[1], "rb") as fh:
    print(hashlib.sha256(fh.read(int(sys.argv[2]))).hexdigest())
"""


class _Stack:
    """One compose project over a copy of compose.yml, with an override
    that uses the test image and a free loopback port."""

    def __init__(self, image):
        prefix = os.environ.get("COUSIN_DOCKER_PROJECT", "cfp6exit")
        self.project = "%s%s" % (prefix, uuid.uuid4().hex[:8])
        self.image = image
        self.port = _free_port()
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        shutil.copy(_COMPOSE, self.dir / "compose.yml")
        (self.dir / "compose.override.yml").write_text(
            "services:\n"
            "  framework:\n"
            "    build: !reset null\n"
            "    image: %s\n"
            "    pull_policy: never\n"
            "    ports: !override\n"
            "      - \"127.0.0.1:%d:8600\"\n" % (image, self.port))
        self.volume = "%s_framework-data" % self.project

    def compose(self, *args, timeout=_EXEC_S, check=True):
        return _docker(*(["compose", "-p", self.project, "--project-directory",
                          str(self.dir)] + list(args)),
                       timeout=timeout, check=check, cwd=str(self.dir))

    def up(self):
        started = time.monotonic()
        self.compose("up", "-d", timeout=_UP_S)
        if _wait_for(lambda: self.get("/api/version")[0] == 200, _UP_S) is None:
            logs = self.compose("logs", "--no-color", check=False).stdout
            raise AssertionError("the console never answered on port %d\n%s"
                                 % (self.port, logs[-4000:]))
        return time.monotonic() - started

    def down(self, *, volumes=False):
        started = time.monotonic()
        self.compose(*(["down"] + (["-v"] if volumes else [])), timeout=_DOWN_S)
        return time.monotonic() - started

    def cleanup(self):
        self.compose("down", "-v", "--remove-orphans", "-t", "5",
                     timeout=_DOWN_S, check=False)
        _docker("volume", "rm", "-f", self.volume, check=False)
        self._tmp.cleanup()

    def http(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                     data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=70) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")
        except (OSError, ValueError):
            return None, None          # not up yet, or reset mid-start

    def get(self, path):
        return self.http("GET", path)

    def post(self, path, body):
        return self.http("POST", path, body)

    def exec(self, *command, check=True):
        return self.compose(*(["exec", "-T", "framework"] + list(command)), check=check)

    def status(self):
        r = self.exec("cousin-supervisor", "status", "--json", check=False)
        if r.returncode != 0:
            return {}
        return {name: row["state"] for name, row in json.loads(r.stdout)["children"].items()}

    def state(self, slug):
        return json.loads(self.exec("python", "-c", _STATE, slug).stdout)

    def write(self, path, text):
        self.exec("python", "-c", _WRITE, path, text)

    def sha(self, *paths):
        return json.loads(self.exec("python", "-c", _SHA, *paths).stdout)

    def prefix_sha(self, path, size):
        return self.exec("python", "-c", _PREFIX, path, str(size)).stdout.strip()

    def send(self, slug, user, message):
        """The operator's message through the console, as the page sends
        it (phase 5's chat route); the sent message is in the thread."""
        status, sent = self.post("/api/chat/send",
                                 {"cousin": slug, "user": user, "message": message})
        assert status == 200 and sent.get("ok"), (status, sent)
        status, listed = self.get("/api/messages?cousin=%s&user=%s" % (slug, user))
        assert status == 200, (status, listed)
        assert sent["id"] in [m["id"] for m in listed["messages"]], listed
        return sent

    def deliver_while_down(self, slug, sender, body):
        """A message for `slug` put on the volume while the stack is down,
        by a one-off container of the same image: the state `down` leaves
        when a row arrives after the runner's last turn."""
        r = _docker("run", "--rm", "-v", "%s:/data" % self.volume, "--entrypoint", "python",
                    self.image, "-c", _DELIVER, slug, sender, body, timeout=_EXEC_S)
        thread, outcome = r.stdout.split()
        return thread, outcome

    def spawn(self, slug):
        """Spawn a `fake` runner cousin from the console and start it,
        as the console page does (create, then /start)."""
        status, body = self.post("/api/cousins", {
            "slug": slug, "role": "keeps the ledger", "voice": "Plain and brief.",
            "operator": "Priya", "runner": "fake"})
        assert status == 201, (status, body)
        status, body = self.post("/api/cousins/%s/start" % slug, {})
        assert status == 200 and body.get("status") == "started", (status, body)

    def wait_running(self, name, timeout=_UP_S):
        return _wait_for(lambda: self.status().get(name) == "running", timeout)

    def wait_done(self, slug, body, timeout=_ANSWER_S):
        return _wait_for(lambda: (lambda r: r if r and r["state"] == "done" else None)(
            _row(self.state(slug), body)), timeout)


def _row(state, body):
    mine = [r for r in state["rows"] if r["body"] == body]
    return mine[0] if mine else None


@unittest.skipUnless(_docker_enabled(), "opt-in: COUSIN_DOCKER=1 and docker on PATH")
class TestComposeDelivery(unittest.TestCase):
    """One stack for the class: each test spawns its own cousins."""

    @classmethod
    def setUpClass(cls):
        cls.stack = _Stack(_IMAGE["name"])
        try:
            MEASURED["compose up until /api/version answers (s)"] = round(cls.stack.up(), 1)
        except BaseException:
            cls.stack.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.stack.cleanup()

    def test_compose_up_serves_the_console_and_the_supervisor_runs_it(self):
        status, body = self.stack.get("/api/version")
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], _VERSION)
        self.assertTrue(self.stack.wait_running("loops", timeout=30),
                        "supervisor status: %s" % self.stack.status())
        children = self.stack.status()
        self.assertEqual(children.get("console"), "running", children)
        self.assertEqual(children.get("loops"), "running", children)

    def test_spawn_from_the_console_then_a_message_is_answered(self):
        started = time.monotonic()
        self.stack.spawn("wren")
        self.assertTrue(self.stack.wait_running("runner:wren"),
                        "supervisor status: %s" % self.stack.status())
        body = "Wren, is the ledger balanced? %s" % uuid.uuid4().hex
        self.stack.send("wren", "Priya", body)
        row = self.stack.wait_done("wren", body)
        MEASURED["spawn + start + one message sent and answered (s)"] = round(
            time.monotonic() - started, 1)
        self.assertIsNotNone(row, "the fake runner never closed the row: %s"
                             % self.stack.state("wren")["rows"])
        self.assertEqual(row["outcome"], "delivered")
        self.assertEqual(row["thread_id"], "operator:Priya")
        self.assertEqual(row["source"], "chat")
        self.assertIn(row["id"], self.stack.state("wren")["answered"])

    def test_down_up_loses_no_message_and_no_session(self):
        """Two cousins. `sam` is stopped through the supervisor (a hold)
        with a message queued: after down and up it stays down, its hold
        and its row untouched, until `start` answers the row. `toki` runs
        at `down`; a row put on the volume while the stack is down is
        answered after `up` with no start of ours. Toki also carries an
        SDK-shaped session file and a transcript under HOME, which must
        come back byte for byte (the fake runner writes neither)."""
        stack = self.stack
        for slug in ("sam", "toki"):
            stack.spawn(slug)
        for slug in ("sam", "toki"):
            self.assertTrue(stack.wait_running("runner:%s" % slug), stack.status())
            # One answered turn first, so the volume holds a session's stream.
            first = "%s, count the jars. %s" % (slug.title(), uuid.uuid4().hex)
            stack.send(slug, "Priya", first)
            self.assertIsNotNone(stack.wait_done(slug, first), stack.state(slug)["rows"])

        # sam: stopped through the supervisor, which holds it; then a message.
        r = stack.exec("cousin-supervisor", "stop", "sam", check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "runner:sam stopped")
        held = "Sam, label the jars. %s" % uuid.uuid4().hex
        stack.send("sam", "Priya", held)

        # toki: what an SDK runner leaves on the volume, beside its data.
        session_id = str(uuid.uuid4())
        sdk_files = {
            "/data/cousins/toki/data/runner-session.json": json.dumps(
                {"session_id": session_id, "lane": "api-key", "generation": 1,
                 "updated": time.time()}),
            # the agent CLI's transcript, under HOME (the image sets it on /data)
            "~/.claude/projects/-data-cousins-toki/%s.jsonl" % session_id:
                "".join(json.dumps({"type": t, "sessionId": session_id, "n": i}) + "\n"
                        for i, t in enumerate(("user", "assistant", "user"))),
        }
        for path, text in sdk_files.items():
            stack.write(path, text)
        files_before = stack.sha(*sdk_files)
        self.assertTrue(all(files_before.values()), files_before)

        before = {slug: stack.state(slug) for slug in ("sam", "toki")}
        self.assertEqual(_row(before["sam"], held)["state"], "queued")
        self.assertIsNotNone(before["sam"]["held"], "the stop wrote no run/held")
        for slug in ("sam", "toki"):
            self.assertTrue(before[slug]["inbox_db"])
            self.assertTrue(before[slug]["stream"])
        self.assertEqual(stack.status().get("runner:toki"), "running", stack.status())

        MEASURED["compose down, ordered stop with one live runner (s)"] = round(stack.down(), 1)
        self.assertEqual(_docker("volume", "inspect", stack.volume, check=False).returncode, 0,
                         "down without -v must keep the volume")
        waiting = "Toki, sweep the shelf. %s" % uuid.uuid4().hex
        thread, outcome = stack.deliver_while_down("toki", "Priya", waiting)
        self.assertEqual((thread, outcome), ("operator:Priya", "queued"))

        started = time.monotonic()
        MEASURED["compose up again until /api/version answers (s)"] = round(stack.up(), 1)
        # R4: toki has no auto_start = false and no hold, so the new
        # supervisor starts it, and it answers the row that waited.
        self.assertTrue(stack.wait_running("runner:toki"), stack.status())
        mine = stack.wait_done("toki", waiting)
        MEASURED["compose up again until the waiting row is answered (s)"] = round(
            time.monotonic() - started, 1)
        self.assertIsNotNone(mine, "the waiting row was not answered after up")
        self.assertEqual(mine["outcome"], "delivered")

        # R4': sam's hold outlived the supervisor and the container. It was
        # not started; its marker and its rows are exactly as they were.
        self.assertNotEqual(stack.status().get("runner:sam"), "running", stack.status())
        sam_up = stack.state("sam")
        self.assertEqual(sam_up["held"], before["sam"]["held"])
        self.assertEqual(sam_up["rows"], before["sam"]["rows"])
        self.assertEqual(_row(sam_up, held)["state"], "queued")

        # No session lost: the SDK-shaped files byte for byte.
        self.assertEqual(stack.sha(*sdk_files), files_before)

        # The operator's start lifts the hold and the row is answered.
        r = stack.exec("cousin-supervisor", "start", "sam", check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "runner:sam running")
        started = time.monotonic()
        answered = stack.wait_done("sam", held)
        MEASURED["start of the held cousin until its queued row is answered (s)"] = round(
            time.monotonic() - started, 1)
        self.assertIsNotNone(answered, "the held row was not answered after start")
        self.assertEqual(answered["outcome"], "delivered")

        for slug, ours in (("sam", {held}), ("toki", {waiting})):
            after = stack.state(slug)
            if slug == "sam":
                self.assertIsNone(after["held"], "start left run/held behind")
            # No message lost: every row kept its identity; a row that was
            # done is untouched; a row still open was answered after up.
            after_rows = {r["id"]: r for r in after["rows"]}
            for old in before[slug]["rows"]:
                new = after_rows.get(old["id"])
                self.assertIsNotNone(new, "%s: row %s is gone" % (slug, old["id"]))
                for key in ("thread_id", "source", "sender", "body"):
                    self.assertEqual(new[key], old[key])
                if old["state"] == "done":
                    self.assertEqual(new, old)
            old_ids = {o["id"] for o in before[slug]["rows"]}
            self.assertTrue(_wait_for(lambda: all(
                r["state"] == "done" for r in stack.state(slug)["rows"]
                if r["id"] in old_ids), _ANSWER_S))
            self.assertIn(_row(after, next(iter(ours)))["id"], after["answered"])
            # Rows added after down can only be ours or the loops daemon's:
            # a heartbeat (source loop), or the day's flip_at rollover (source
            # flip), which a cousin not yet flipped today gets at its first
            # live tick, and the state digest that rollover queues for the
            # new session (source boot). Never another message.
            added = [r for r in after["rows"]
                     if r["id"] not in old_ids and r["body"] not in ours]
            self.assertEqual([(r["source"], r["thread_id"]) for r in added
                              if r["source"] not in ("loop", "flip", "boot")], [])
            MEASURED["%s: rows before down / at the end (framework rows added)" % slug] = (
                "%d / %d (%s)" % (len(before[slug]["rows"]), len(after["rows"]),
                                  ", ".join(r["source"] for r in added) or "none"))
            # Every stream file of the sessions before down is on the volume.
            # A stopped session's is byte for byte (sam's: stopped before the
            # snapshot); a session live at down appends its stop to its own
            # file, so what it had is a prefix of what it has (toki's). A new
            # runner writes a file of its own.
            grew = 0
            for name, (size, digest) in before[slug]["stream"].items():
                self.assertIn(name, after["stream"], "%s: %s is gone" % (slug, name))
                now_size, now_digest = after["stream"][name]
                if slug == "sam":
                    self.assertEqual((now_size, now_digest), (size, digest), name)
                self.assertEqual(self.stack.prefix_sha(
                    "/data/cousins/%s/data/stream/%s" % (slug, name), size), digest,
                    "%s: %s lost bytes it had before down" % (slug, name))
                grew += now_size - size
            MEASURED["%s: stream files before / after (bytes appended to old ones)" % slug] = (
                "%d / %d (%d)" % (len(before[slug]["stream"]), len(after["stream"]), grew))


@unittest.skipUnless(_docker_enabled(), "opt-in: COUSIN_DOCKER=1 and docker on PATH")
class TestImageBudget(unittest.TestCase):
    def test_the_image_is_within_the_budget(self):
        started = time.monotonic()
        r = subprocess.run(["sh", str(_SIZE), _IMAGE["name"]], capture_output=True,
                           text=True, timeout=900)
        MEASURED["image-size.sh (s)"] = round(time.monotonic() - started, 1)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"^compressed: [0-9.]+ MB \(budget 180\.0 MB\)$")
        MEASURED["compressed (gzip -6)"] = r.stdout.strip()
        size = _docker("image", "inspect", "-f", "{{.Size}}", _IMAGE["name"]).stdout.strip()
        MEASURED["on disk (MB)"] = round(int(size) / 1e6, 1)


# ------------------------------------------------------------ the docs

def _section(text, heading):
    """The body under `heading` (a whole line) up to the next heading of
    the same or a higher level (a `#` line inside a code fence is code)."""
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.splitlines()
    start = lines.index(heading) + 1
    fenced = False
    for i in range(start, len(lines)):
        if lines[i].startswith("```"):
            fenced = not fenced
        m = re.match(r"^(#+) ", lines[i])
        if m and not fenced and len(m.group(1)) <= level:
            return "\n".join(lines[start:i])
    return "\n".join(lines[start:])


def _flat(text):
    return " ".join(text.split())


def _semver(text):
    return tuple(int(part) for part in text.split("."))


class TestDeliveryDocs(unittest.TestCase):
    """The close-out's docs say what the exit criteria and the code do."""

    def read(self, rel):
        return (_REPO / rel).read_text()

    def has(self, needle, text, where):
        # Not assertIn: its message would print the whole document. Runs
        # of whitespace compare equal, so a rewrapped line still matches.
        self.assertTrue(_flat(needle) in _flat(text), "%r not in %s" % (needle, where))

    def hasnt(self, needle, text, where):
        self.assertFalse(_flat(needle) in _flat(text), "%r in %s" % (needle, where))

    def test_install_leads_with_docker_and_the_bare_host_follows(self):
        text = self.read("docs/install.md")
        headings = re.findall(r"^## .*$", text, re.M)
        self.assertEqual(headings[:2], ["## Install with Docker", "## Install on a bare host"])
        docker = _section(text, "## Install with Docker")
        for needle in ("git clone", "cp compose.api-key.yml compose.override.yml",
                       "docker compose up -d",
                       "docker compose exec framework cousin-console adduser",
                       "http://127.0.0.1:8600", "MB", "terms risk",
                       "COUSIN_DEFAULT_RUNNER=sdk", "COUSIN_DEFAULT_ACCOUNT",
                       "stays stopped"):
            self.has(needle, docker, "install.md, Install with Docker")

    def test_operations_covers_the_container(self):
        section = _section(self.read("docs/operations.md"), "## The container")
        for needle in ("cousin-supervisor status", "docker compose logs -f", "/data",
                       "HOME=/data/home", "cousin-backup", "docker compose build",
                       "FROM ", "terms risk", "cousin-supervisor.service",
                       # the hold (R4'), the stop that does not wait (R6'), names (M2)
                       "run/held", "until `start`", "stop --no-wait", "202 `stopping`",
                       "start --name loops", "exits 5",
                       # why never beside the old units: the clock and the address
                       "run/loops.lock", "`--host` and `--port`",
                       "pip is removed from the image", "at least once",
                       # round 2 N1: a second clock is busy, waited out
                       "exits 5 (busy)", "waits in `backoff`"):
            self.has(needle, section, "operations.md, The container")
        self.hasnt("two consoles cannot share the port", section, "operations.md")
        self.hasnt("`failing`. And", section, "operations.md")

    def test_operations_backup_is_at_least_once(self):
        section = _section(self.read("docs/operations.md"), "## Backups")
        self.has("at-least-once", section, "operations.md, Backups")
        self.has("`inbox.db` first", section, "operations.md, Backups")

    def test_the_console_spawn_dialog_says_the_environment_decides_the_lane(self):
        section = _section(self.read("docs/console.md"), "### Spawning a cousin")
        for needle in ("COUSIN_DEFAULT_RUNNER", "COUSIN_DEFAULT_ACCOUNT", "no runner or account"):
            self.has(needle, section, "console.md, Spawning a cousin")

    def test_the_console_restart_route_exits_75(self):
        section = _section(self.read("docs/reference/console-api.md"),
                           "### `POST /api/admin/restart/framework`")
        self.has("exits 75", section, "the restart route")
        self.assertNotIn("exits 0", section)

    def test_the_changelog_top_entry_is_one_minor_above_the_last(self):
        """Phase 6's entry (found by content: the top one when it closed) is
        one MINOR above the entry right below it and says what phase 6
        shipped."""
        text = self.read("CHANGELOG.md")
        version, entry, below = _phase6_entry(text)
        self.assertGreaterEqual(_semver(_VERSION), _semver(version))
        top, last = _semver(version), _semver(below)
        # SemVer: the next MINOR resets PATCH; MAJOR stays.
        self.assertEqual(top, (last[0], last[1] + 1, 0),
                         "%s is not one MINOR above %s" % (version, below))
        for needle in ("cousin-supervisor", "run/held", "exits 5", "run/loops.lock",
                       "202 `stopping`", "--no-wait", "--name", "at least once",
                       "pip removed from the image", "200 with `\"runner\": \"not running\"",
                       # round 2: N1 busy uncounted, O9 the hold, #79 the retry, N7 the 502
                       "never counted toward `failing`", "for a runner and for the loops daemon",
                       "`\"held\": true`", "#79", "is_running", "502"):
            self.has(needle, entry, "the top CHANGELOG entry")
        self.hasnt("answers the row once", entry, "the top CHANGELOG entry")
        self.hasnt("exits 2 with \"another loops", entry, "the top CHANGELOG entry")

    def test_the_master_plan_locks_the_supervisor_interfaces(self):
        section = _section(self.read("docs/design/plans/agent-loop-runner-plan.md"),
                           "## Interfaces locked across phases")
        for needle in ("# cousin_lib/supervisor.py (P6)",
                       "def request(root, op, *, timeout=10.0, **args)",
                       'HELD = "run/held"', "LOCK_HELD_EXIT = 5", "def hold_loops_lock(root)",
                       "`run` exits 5 (busy) if held", "LOCK_TAKE_S = 1.0"):
            self.has(needle, section, "the locked interfaces")

    def test_the_master_plan_marks_phase_6_done_at_this_version(self):
        text = self.read("docs/design/plans/agent-loop-runner-plan.md")
        row = next(line for line in text.splitlines()
                   if line.startswith("| 6 | Supervisor and docker delivery |"))
        self.has("**DONE**", row, "the phase 6 row")
        self.has("v%s," % _phase6_entry(self.read("CHANGELOG.md"))[0], row, "the phase 6 row")
        self.assertRegex(row, r"v[0-9.]+, [0-9]+ tests")
        phase6 = text.index("\n## Phase 6")
        criteria = text[text.index("**Exit criteria**", phase6):
                        text.index("\n## Phase 7", phase6)]
        self.assertEqual(criteria.count("- [x]"), 3, criteria)
        self.has("more than git and docker", criteria, "the phase 6 exit criteria")
        self.has("by construction", criteria, "the phase 6 exit criteria")


if __name__ == "__main__":
    unittest.main()
