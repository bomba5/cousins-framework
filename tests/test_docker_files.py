"""The files that deliver the framework as an image.

Hermetic checks run everywhere: the entrypoint is run with `sh` against
a temporary root and a stand-in image tree, and the Dockerfile and
.dockerignore are read as text. The image itself is built and probed
only when COUSIN_DOCKER=1 and a docker binary is on PATH (the CI image
job sets both); set COUSIN_DOCKER_IMAGE to probe an image already
built instead of building one.
"""
import ast
import fnmatch
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import tempfile
import time
import tomllib
import unittest
import uuid

from tests._hermetic import HermeticCase

_REPO = pathlib.Path(__file__).resolve().parents[1]
_ENTRY = _REPO / "docker" / "entrypoint.sh"
_DOCKERFILE = _REPO / "Dockerfile"
_DOCKERIGNORE = _REPO / ".dockerignore"
_CMD = ["cousin-supervisor", "run", "--console-host", "0.0.0.0"]
_TIMEOUT = 60
_COMPOSE = _REPO / "compose.yml"
_COMPOSE_KEY = _REPO / "compose.api-key.yml"
_UNIT = _REPO / "systemd" / "cousin-supervisor.service"
# R5': a runner gets runner.main.STOP_TIMEOUT_S + 5 = 35 s after SIGTERM;
# then the loops daemon and the console get 10 s each, one after the
# other; each step adds the supervisor's KILL_GRACE_S after a SIGKILL.
_RUNNER_STOP_S = 35
_CHILD_STOP_S = 10
_KILL_GRACE_S = 5


def _stand_in_image(case):
    """A tree shaped like /opt/framework: the templates and two examples."""
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    src = pathlib.Path(tmp.name) / "framework"
    (src / "templates").mkdir(parents=True)
    (src / "templates" / "cousin-CLAUDE.template.md").write_text("# {{NAME}}\n")
    (src / "config").mkdir()
    (src / "config" / "harness.toml.example").write_text("# harness example\n")
    (src / "config" / "hive.toml.example").write_text("# hive example\n")
    return src


class _EntrypointCase(HermeticCase):
    def setUp(self):
        super().setUp()
        self.src = _stand_in_image(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = pathlib.Path(tmp.name)
        self.root = self.base / "data"
        self.secret = self.base / "anthropic_api_key"

    def run_entry(self, *command):
        env = dict(os.environ, FRAMEWORK_ROOT=str(self.root),
                   COUSIN_IMAGE_SRC=str(self.src),
                   COUSIN_API_KEY_SECRET=str(self.secret))
        return subprocess.run(["sh", str(_ENTRY)] + list(command or ["true"]),
                              env=env, capture_output=True, text=True,
                              timeout=_TIMEOUT)

    def ok(self, *command):
        r = self.run_entry(*command)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout + r.stderr


class TestEntrypointLayout(_EntrypointCase):
    def test_an_empty_root_gets_its_directories_and_the_examples(self):
        self.ok()
        for sub in ("config", "cousins", "data", "shared", "home"):
            self.assertTrue((self.root / sub).is_dir(), sub)
        self.assertEqual((self.root / "config" / "hive.toml.example").read_text(),
                         "# hive example\n")

    def test_only_harness_toml_goes_live(self):
        self.ok()
        live = sorted(p.name for p in (self.root / "config").iterdir()
                      if not p.name.endswith(".example"))
        self.assertEqual(live, ["harness.toml"])

    def test_harness_toml_is_created_once_and_never_overwritten(self):
        self.ok()
        self.assertEqual((self.root / "config" / "harness.toml").read_text(),
                         "# harness example\n")
        (self.root / "config" / "harness.toml").write_text("default_flip_at = \"never\"\n")
        self.ok()
        self.assertEqual((self.root / "config" / "harness.toml").read_text(),
                         "default_flip_at = \"never\"\n")

    def test_the_examples_are_refreshed_on_every_start(self):
        self.ok()
        (self.root / "config" / "hive.toml.example").write_text("stale\n")
        (self.src / "config" / "hive.toml.example").write_text("# hive, newer image\n")
        self.ok()
        self.assertEqual((self.root / "config" / "hive.toml.example").read_text(),
                         "# hive, newer image\n")

    def test_the_templates_link_points_into_the_image_and_a_stale_one_is_replaced(self):
        self.ok()
        link = self.root / "templates"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), str(self.src / "templates"))
        link.unlink()
        link.symlink_to(self.base / "an-older-image" / "templates")
        self.ok()
        self.assertEqual(os.readlink(link), str(self.src / "templates"))
        self.assertTrue((link / "cousin-CLAUDE.template.md").is_file())

    def test_a_real_templates_directory_is_refused(self):
        (self.root / "templates").mkdir(parents=True)
        (self.root / "templates" / "mine.md").write_text("Wren's own\n")
        r = self.run_entry("true")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("templates", r.stderr)
        self.assertIn("not a link", r.stderr)
        self.assertEqual((self.root / "templates" / "mine.md").read_text(), "Wren's own\n")


class TestEntrypointSecret(_EntrypointCase):
    def _key(self):
        return self.root / ".secrets" / "accounts" / "api-key"

    def test_the_api_key_secret_is_installed_private_and_declared_once(self):
        self.secret.write_text("sk-test-not-a-key\n")
        self.secret.chmod(0o644)
        self.ok()
        self.ok()
        key = self._key()
        self.assertEqual(key.read_text(), "sk-test-not-a-key\n")
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(key.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(key.parent.parent.stat().st_mode), 0o700)
        text = (self.root / "config" / "accounts.toml").read_text()
        self.assertEqual(text.count("[accounts.api-key]"), 1, text)
        self.assertEqual(tomllib.loads(text)["accounts"]["api-key"],
                         {"kind": "anthropic-key"})

    def test_a_rotated_key_replaces_the_installed_one(self):
        self.secret.write_text("sk-test-old\n")
        self.ok()
        self.secret.write_text("sk-test-new\n")
        self.ok()
        self.assertEqual(self._key().read_text(), "sk-test-new\n")

    def test_an_existing_accounts_table_is_left_alone(self):
        (self.root / "config").mkdir(parents=True)
        (self.root / "config" / "accounts.toml").write_text(
            '[accounts.api-key]\nkind = "anthropic-key"\nsecret_file = ".secrets/accounts/api-key"\n')
        self.secret.write_text("sk-test-not-a-key\n")
        self.ok()
        text = (self.root / "config" / "accounts.toml").read_text()
        self.assertEqual(text.count("api-key]"), 1, text)

    def test_the_installed_key_passes_the_strict_read(self):
        from cousin_lib import accounts
        from cousin_lib.agent_auth import read_private_file
        self.secret.write_text("sk-test-not-a-key\n")
        self.ok()
        account = accounts.load(self.root)["api-key"]
        self.assertEqual(account.kind, "anthropic-key")
        self.assertEqual(read_private_file(account.secret_file), b"sk-test-not-a-key\n")

    def test_without_a_secret_there_is_no_key_and_no_account(self):
        self.ok()
        self.assertFalse(self._key().exists())
        self.assertFalse((self.root / "config" / "accounts.toml").exists())

    def test_an_empty_secret_is_reported_and_not_installed(self):
        self.secret.write_text("")
        out = self.ok()
        self.assertFalse(self._key().exists())
        self.assertIn(str(self.secret), out)
        self.assertIn("empty or unreadable", out)


class TestEntrypointMessages(_EntrypointCase):
    def test_the_console_is_reported_open_without_a_users_file(self):
        out = self.ok()
        self.assertIn("OPEN", out)
        self.assertIn("docker compose exec framework cousin-console adduser <name>", out)

    def test_no_open_warning_once_a_user_exists(self):
        self.ok()
        (self.root / "config" / "console-users.json").write_text("{}\n")
        self.assertNotIn("OPEN", self.ok())

    def test_the_first_run_checklist_prints_only_on_an_empty_volume(self):
        first = self.ok()
        self.assertIn("first start", first)
        for name in ("config/harness.toml", "config/accounts.toml",
                     "cousin-account login"):
            self.assertIn(name, first)
        self.assertNotIn("first start", self.ok())

    def test_the_command_is_execed_not_forked(self):
        env = dict(os.environ, FRAMEWORK_ROOT=str(self.root),
                   COUSIN_IMAGE_SRC=str(self.src),
                   COUSIN_API_KEY_SECRET=str(self.secret))
        proc = subprocess.Popen(["sh", str(_ENTRY), "sh", "-c", 'echo "pid=$$"'],
                                env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        out, _ = proc.communicate(timeout=_TIMEOUT)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("pid=%d" % proc.pid, out)

    def test_the_command_exit_status_is_the_containers(self):
        self.assertEqual(self.run_entry("sh", "-c", "exit 7").returncode, 7)

    def test_no_command_is_a_usage_error(self):
        env = dict(os.environ, FRAMEWORK_ROOT=str(self.root),
                   COUSIN_IMAGE_SRC=str(self.src))
        r = subprocess.run(["sh", str(_ENTRY)], env=env, capture_output=True,
                           text=True, timeout=_TIMEOUT)
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage", r.stderr)


class TestEntrypointFile(unittest.TestCase):
    def test_it_is_an_executable_posix_sh_script(self):
        text = _ENTRY.read_text()
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn("set -eu", text)
        self.assertTrue(os.access(_ENTRY, os.X_OK))
        r = subprocess.run(["sh", "-n", str(_ENTRY)], capture_output=True, text=True,
                           timeout=_TIMEOUT)
        self.assertEqual(r.returncode, 0, r.stderr)
        _ENTRY.read_bytes().decode("ascii")
        for bashism in ("[[ ", "function ", "pipefail", "$'", "<<<", "local "):
            self.assertFalse(bashism in text, "bashism %r" % bashism)


def _instructions(text):
    """(INSTRUCTION, argument) per Dockerfile line, continuations joined."""
    joined = re.sub(r"\\\n", " ", text)
    out = []
    for line in joined.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            word, _, rest = line.partition(" ")
            out.append((word.upper(), rest.strip()))
    return out


class TestDockerfile(unittest.TestCase):
    def setUp(self):
        self.text = _DOCKERFILE.read_text()
        self.ins = _instructions(self.text)

    def _args(self, word):
        return [a for w, a in self.ins if w == word]

    def test_no_home_path_anywhere(self):
        self.assertNotIn("/home/", self.text)
        self.text.encode("ascii")

    def test_two_stages_on_the_slim_base(self):
        froms = self._args("FROM")
        self.assertEqual(len(froms), 2, froms)
        self.assertTrue(all(f.startswith("python:3.13-slim") for f in froms), froms)
        self.assertTrue(froms[0].endswith(" AS builder"), froms[0])

    def test_the_source_is_installed_in_place_with_the_sdk_extra(self):
        run = " ".join(self._args("RUN"))
        self.assertIn('pip install --no-cache-dir -e "/opt/framework[sdk]"', run)
        copies = self._args("COPY")
        self.assertIn("--from=builder /opt/venv /opt/venv", copies)
        self.assertIn("--from=builder /opt/framework /opt/framework", copies)

    def test_pip_is_removed_from_the_image(self):
        # The venv's pip in the builder, the base image's own in the final
        # stage, as root: before the one USER line.
        final_from = max(i for i, (w, _) in enumerate(self.ins) if w == "FROM")
        builder = " ".join(a for w, a in self.ins[:final_from] if w == "RUN")
        self.assertIn("/opt/venv/bin/pip uninstall -y pip", builder)
        final = self.ins[final_from:]
        user_at = [i for i, (w, _) in enumerate(final) if w == "USER"][0]
        before_user = " ".join(a for w, a in final[:user_at] if w == "RUN")
        self.assertIn("python3 -m pip uninstall -y pip", before_user)

    def test_state_code_and_user(self):
        env = " ".join(self._args("ENV"))
        for pair in ("FRAMEWORK_ROOT=/data", "HOME=/data/home",
                     "PATH=/opt/venv/bin:$PATH", "PYTHONUNBUFFERED=1"):
            self.assertIn(pair, env)
        self.assertEqual(self._args("VOLUME"), ["/data"])
        self.assertEqual(self._args("EXPOSE"), ["8600"])
        users = self._args("USER")
        self.assertEqual(len(users), 1)
        self.assertNotIn(users[0].split(":")[0], ("root", "0"))
        self.assertEqual(users[0], "10001:10001")

    def test_healthcheck_asks_the_version_route_without_curl(self):
        check = " ".join(self._args("HEALTHCHECK"))
        self.assertIn("http://127.0.0.1:8600/api/version", check)
        self.assertIn("urllib", check)
        self.assertNotIn("curl", check)

    def test_entrypoint_and_command(self):
        self.assertEqual([json.loads(a) for a in self._args("ENTRYPOINT")],
                         [["/opt/framework/docker/entrypoint.sh"]])
        self.assertEqual([json.loads(a) for a in self._args("CMD")], [_CMD])


def _dockerignored(path, patterns):
    """Docker's rule, enough for this file: the last pattern that matches
    the path or one of its parent directories wins; `!` re-includes."""
    parts = path.split("/")
    prefixes = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    ignored = False
    for pattern in patterns:
        negate = pattern.startswith("!")
        pat = pattern[1:] if negate else pattern
        if pat.startswith("**/"):
            hit = any(fnmatch.fnmatchcase(part, pat[3:]) for part in parts)
        else:
            hit = any(fnmatch.fnmatchcase(p, pat) for p in prefixes)
        if hit:
            ignored = not negate
    return ignored


class TestDockerignore(unittest.TestCase):
    def setUp(self):
        self.patterns = [l.strip() for l in _DOCKERIGNORE.read_text().splitlines()
                         if l.strip() and not l.strip().startswith("#")]

    def test_the_build_context_leaves_out_tests_docs_state_and_secrets(self):
        for path in (".git/config", "tests/test_spawn.py", "docs/install.md",
                     "cousins/wren/cousin.toml", "data/loops.db", "shared/hive.db",
                     "run/supervisor.sock", "config/accounts.toml", "config/hive.toml",
                     "config/console-users.json", ".secrets/accounts/api-key",
                     "secrets/anthropic_api_key", "examples/wren/CLAUDE.md",
                     "wren-node.tar.gz", ".env", ".venv/bin/python3",
                     "cousin_lib/__pycache__/spawn.cpython-313.pyc",
                     "compose.override.yml", "Dockerfile", ".github/workflows/ci.yml"):
            self.assertTrue(_dockerignored(path, self.patterns), path)

    def test_the_build_context_keeps_the_code_and_what_it_finds_beside_it(self):
        for path in ("pyproject.toml", "README.md", "LICENSE",
                     "cousin_lib/spawn.py", "cousin_lib/console_static/index.html",
                     "templates/cousin-CLAUDE.template.md", "templates/hive-node/README.md",
                     "hooks/pre_compact.sh", "config/harness.toml.example",
                     "config/mcp-registry.toml.example", "docker/entrypoint.sh"):
            self.assertFalse(_dockerignored(path, self.patterns), path)


def _docker_enabled():
    return os.environ.get("COUSIN_DOCKER") == "1" and shutil.which("docker")


def _docker(*args, timeout=600, check=True):
    r = subprocess.run(["docker"] + list(args), capture_output=True, text=True,
                       timeout=timeout)
    if check and r.returncode != 0:
        raise AssertionError("docker %s: rc %d\n%s%s"
                             % (" ".join(args), r.returncode, r.stdout, r.stderr))
    return r


@unittest.skipUnless(_docker_enabled(), "opt-in: COUSIN_DOCKER=1 and docker on PATH")
class TestImage(unittest.TestCase):
    """The built image: every probe runs through the entrypoint."""

    @classmethod
    def setUpClass(cls):
        cls.image = os.environ.get("COUSIN_DOCKER_IMAGE")
        cls.built = False
        if not cls.image:
            cls.image = "cousins-framework:test-%s" % uuid.uuid4().hex[:8]
            _docker("build", "-t", cls.image, str(_REPO), timeout=1800)
            cls.built = True

    @classmethod
    def tearDownClass(cls):
        if cls.built:
            _docker("image", "rm", "-f", cls.image, check=False)

    def run_in(self, *command, env=None):
        args = ["run", "--rm"]
        for k, v in (env or {}).items():
            args += ["-e", "%s=%s" % (k, v)]
        return _docker(*(args + [self.image] + list(command)), timeout=300)

    def test_cousin_version_prints_the_pyproject_version(self):
        want = tomllib.loads((_REPO / "pyproject.toml").read_text())["project"]["version"]
        out = self.run_in("cousin-version").stdout
        self.assertIn(want, out)
        self.assertNotIn("0+unknown", out)

    def test_the_package_runs_from_the_source_tree(self):
        out = self.run_in("python", "-c", "import cousin_lib; print(cousin_lib.__file__)").stdout
        self.assertEqual(out.strip(), "/opt/framework/cousin_lib/__init__.py")

    def test_the_sdk_extra_is_importable(self):
        out = self.run_in("python", "-c",
                          "import claude_agent_sdk; print('sdk', claude_agent_sdk.__version__)").stdout
        self.assertTrue(out.startswith("sdk 0."), out)

    def test_pip_is_absent(self):
        # Neither the venv's interpreter nor the base image's system one
        # has pip, and no pip script is left on PATH.
        for python in ("python", "/usr/local/bin/python3"):
            with self.subTest(python=python):
                r = _docker("run", "--rm", self.image, python, "-c", "import pip",
                            check=False)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("No module named 'pip'", r.stderr)
        out = self.run_in("python", "-c",
                          "import glob, shutil\n"
                          "print(sorted(glob.glob('/usr/local/bin/pip*')"
                          " + glob.glob('/opt/venv/bin/pip*')))\n"
                          "print(shutil.which('pip'), shutil.which('pip3'))\n").stdout
        self.assertEqual(out.split("\n")[:2], ["[]", "None None"])

    def test_it_runs_as_a_non_root_user_with_home_on_the_volume(self):
        out = self.run_in("python", "-c",
                          "import os; print(os.getuid(), os.environ['HOME'],"
                          " os.path.isdir(os.environ['HOME']))").stdout
        self.assertEqual(out.split(), ["10001", "/data/home", "True"])

    def test_the_image_creates_a_cousin_from_its_templates(self):
        script = ("from cousin_lib import spawn, version\n"
                  "r = spawn.create_cousin('/data', slug='wren', role='keeps the ledger',\n"
                  "                        voice='Plain and brief.')\n"
                  "home = r['home']\n"
                  "print(version.version())\n"
                  "print((home / 'cousin.toml').is_file())\n"
                  "print('Wren' in (home / 'CLAUDE.md').read_text())\n"
                  "print((home / '.mcp.json').is_file())\n")
        out = self.run_in("python", "-c", script).stdout.split()
        want = tomllib.loads((_REPO / "pyproject.toml").read_text())["project"]["version"]
        self.assertEqual(out, [want, "True", "True", "True"])

    def test_the_image_carries_the_code_and_nothing_else(self):
        script = ("import os\n"
                  "print(sorted(os.listdir('/opt/framework')))\n"
                  "print(sorted(os.listdir('/opt/framework/config')))\n"
                  "print(sorted(os.listdir('/opt/framework/docker')))\n")
        lines = self.run_in("python", "-c", script).stdout.splitlines()
        top = ast.literal_eval(lines[0])
        for absent in ("tests", "docs", ".git", "examples", "cousins", "compose.yml"):
            self.assertNotIn(absent, top)
        for present in ("cousin_lib", "templates", "hooks", "pyproject.toml", "LICENSE"):
            self.assertIn(present, top)
        self.assertTrue(ast.literal_eval(lines[1]))
        self.assertTrue(all(n.endswith(".example") for n in ast.literal_eval(lines[1])), lines[1])
        self.assertEqual(ast.literal_eval(lines[2]), ["entrypoint.sh"])

    def test_the_entrypoint_runs_under_the_image_shell(self):
        # The image's /bin/sh (dash on Debian) runs every branch twice:
        # first start, secret, second start.
        script = ("printf 'sk-test-not-a-key' > /tmp/key && "
                  "E=/opt/framework/docker/entrypoint.sh && "
                  "$E true >/tmp/first 2>&1 && $E true >/tmp/second 2>&1 && "
                  "grep -c 'first start' /tmp/first && "
                  "grep -c 'first start' /tmp/second || true; "
                  "stat -c '%a' /tmp/r/.secrets/accounts /tmp/r/.secrets/accounts/api-key; "
                  "grep -c 'accounts.api-key' /tmp/r/config/accounts.toml; "
                  "readlink /tmp/r/templates")
        out = _docker("run", "--rm", "-e", "FRAMEWORK_ROOT=/tmp/r",
                      "-e", "COUSIN_API_KEY_SECRET=/tmp/key", "--entrypoint", "sh",
                      self.image, "-c", script).stdout.split()
        self.assertEqual(out, ["1", "0", "700", "600", "1", "/opt/framework/templates"])

    def test_the_default_command_is_the_supervisor(self):
        cfg = json.loads(_docker("image", "inspect", "-f", "{{json .Config}}",
                                 self.image).stdout)
        self.assertEqual(cfg["Entrypoint"], ["/opt/framework/docker/entrypoint.sh"])
        self.assertEqual(cfg["Cmd"], _CMD)
        self.assertEqual(cfg["User"], "10001:10001")
        self.assertIn("/data", cfg["Volumes"])

    def test_the_default_container_turns_healthy(self):
        # The default CMD (the supervisor, which starts the console child)
        # under the image's own HEALTHCHECK: needs cousin-supervisor in the
        # image, so it runs green once the supervisor's tasks are below it.
        name = "cousins-framework-default-%s" % uuid.uuid4().hex[:8]
        _docker("run", "-d", "--name", name, self.image)
        self.addCleanup(_docker, "rm", "-f", "-v", name, check=False)
        # Docker 25+ probes every --start-interval (default 5 s) during the
        # --start-period; an older engine waits one --interval (30 s).
        deadline = time.monotonic() + 120
        state = None
        while time.monotonic() < deadline:
            state = json.loads(_docker("inspect", "-f", "{{json .State}}", name).stdout)
            if not state["Running"] or state["Health"]["Status"] in ("healthy", "unhealthy"):
                break
            time.sleep(2)
        logs = _docker("logs", name, check=False)
        self.assertTrue(state["Running"], logs.stdout + logs.stderr)
        self.assertEqual(state["Health"]["Status"], "healthy", logs.stdout + logs.stderr)
        top = _docker("top", name, "-o", "pid,args").stdout
        self.assertIn("cousin-supervisor run --console-host 0.0.0.0", top)
        self.assertIn("cousin_lib.console.app serve", top)
        self.assertIn("cousin_lib.loops run", top)

    def test_the_healthcheck_passes_against_a_running_console(self):
        name = "cousins-framework-health-%s" % uuid.uuid4().hex[:8]
        _docker("run", "-d", "--rm", "--name", name, self.image,
                "cousin-console", "--host", "0.0.0.0", "--port", "8600")
        self.addCleanup(_docker, "rm", "-f", "-v", name, check=False)
        cfg = json.loads(_docker("image", "inspect", "-f", "{{json .Config.Healthcheck}}",
                                 self.image).stdout)
        self.assertEqual(cfg["Test"][0], "CMD")
        deadline = time.monotonic() + 60
        rc = None
        while time.monotonic() < deadline:
            rc = _docker(*(["exec", name] + cfg["Test"][1:]), check=False).returncode
            if rc == 0:
                break
            time.sleep(1)
        self.assertEqual(rc, 0)



def _live_lines(path):
    """The file's lines without comment lines and blank lines."""
    return [l for l in path.read_text().splitlines()
            if l.strip() and not l.lstrip().startswith("#")]


def _services(path):
    """{service name: its block's live lines}, from a compose file written
    with two-space indentation (this repository's own files)."""
    out, current, inside = {}, None, False
    for line in _live_lines(path):
        if not line.startswith(" "):
            inside = line.rstrip() == "services:"
            current = None
            continue
        if inside and re.match(r"^  [A-Za-z0-9_-]+:\s*$", line):
            current = line.strip()[:-1]
            out[current] = []
        elif inside and current:
            out[current].append(line.strip())
    return out


class TestCompose(unittest.TestCase):
    def setUp(self):
        self.services = _services(_COMPOSE)
        self.framework = self.services["framework"]

    def test_compose_grace_period_covers_the_runner_stop(self):
        found = [l for l in self.framework if l.startswith("stop_grace_period:")]
        self.assertEqual(len(found), 1, self.framework)
        seconds = int(re.fullmatch(r"stop_grace_period:\s*(\d+)s", found[0]).group(1))
        self.assertGreaterEqual(seconds, _RUNNER_STOP_S + _CHILD_STOP_S)

    def test_the_console_port_binds_loopback(self):
        ports = [l for l in self.framework if l.startswith("- ") and "8600" in l]
        self.assertEqual(ports, ['- "127.0.0.1:8600:8600"'])

    def test_state_lives_on_one_volume_at_data(self):
        self.assertIn("- framework-data:/data", self.framework)
        mounts = [l for l in self.framework if re.match(r"- [^\s:]+:/", l)]
        self.assertEqual(mounts, ["- framework-data:/data"])
        self.assertIn("  framework-data:", _live_lines(_COMPOSE))

    def test_the_framework_builds_this_checkout_and_defaults_to_the_runner(self):
        self.assertIn("build: .", self.framework)
        self.assertIn("COUSIN_DEFAULT_RUNNER: sdk", self.framework)
        self.assertIn("restart: unless-stopped", self.framework)

    def test_the_login_lane_needs_no_secret_file(self):
        # A secret whose file is missing fails `docker compose up`, so the
        # key lane is an override file; the base file declares no secret
        # and no default account.
        live = "\n".join(_live_lines(_COMPOSE))
        self.assertNotIn("secrets:", live)
        self.assertNotIn("COUSIN_DEFAULT_ACCOUNT", live)

    def test_the_api_key_lane_is_one_override_file(self):
        key = _services(_COMPOSE_KEY)["framework"]
        self.assertIn("- anthropic_api_key", key)
        self.assertIn("COUSIN_DEFAULT_ACCOUNT: api-key", key)
        live = _live_lines(_COMPOSE_KEY)
        self.assertIn("  anthropic_api_key:", live)
        self.assertIn("    file: ./secrets/anthropic_api_key", live)

    def test_heavier_lanes_are_opt_in_profiles(self):
        for name, block in self.services.items():
            if name != "framework":
                self.assertTrue(any(l.startswith("profiles:") for l in block), name)
        embeddings = self.services["embeddings"]
        self.assertIn('profiles: ["embeddings"]', embeddings)
        self.assertTrue(any(l.startswith("image: ollama/ollama:") for l in embeddings))
        self.assertFalse(any(l.startswith("ports:") for l in embeddings),
                         "the embedding service is reached on the compose network only")

    def test_the_key_file_never_reaches_git_or_the_image(self):
        ignored = (_REPO / ".gitignore").read_text().splitlines()
        self.assertIn("/secrets/", ignored)
        self.assertIn("/compose.override.yml", ignored)
        patterns = [l.strip() for l in _DOCKERIGNORE.read_text().splitlines()
                    if l.strip() and not l.strip().startswith("#")]
        self.assertTrue(_dockerignored("secrets/anthropic_api_key", patterns))

    def test_ascii_and_no_home_path(self):
        for path in (_COMPOSE, _COMPOSE_KEY, _UNIT):
            text = path.read_text()
            text.encode("ascii")
            self.assertNotIn("/home/", text, path.name)


def _unit():
    import configparser
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string(_UNIT.read_text())
    return parser


class TestSupervisorUnit(unittest.TestCase):
    def test_it_runs_the_supervisor_and_reloads_through_it(self):
        service = _unit()["Service"]
        self.assertEqual(service["ExecStart"],
                         "{{USER_BIN}}/cousin-supervisor run --console-port 8600")
        self.assertEqual(service["ExecReload"], "{{USER_BIN}}/cousin-supervisor reload")
        self.assertEqual(service["Restart"], "on-failure")
        self.assertEqual(service["Type"], "simple")

    def test_the_stop_is_the_supervisors_ordered_stop(self):
        service = _unit()["Service"]
        # mixed: SIGTERM to the supervisor alone, which stops its children
        # in order; SIGKILL to whatever is left only at the timeout.
        self.assertEqual(service["KillMode"], "mixed")
        # The true worst case: every step's timeout plus its kill grace.
        worst = (_RUNNER_STOP_S + _KILL_GRACE_S) + 2 * (_CHILD_STOP_S + _KILL_GRACE_S)
        self.assertEqual(worst, 70)
        self.assertGreaterEqual(int(service["TimeoutStopSec"]), worst)

    def test_the_readme_says_it_replaces_the_console_and_loops_units(self):
        readme = (_REPO / "systemd" / "README.md").read_text()
        row = [l for l in readme.splitlines() if l.startswith("| `cousin-supervisor.service`")]
        self.assertEqual(len(row), 1)
        self.assertIn("instead of", row[0])
        for name in ("cousin-console.service", "cousin-loops.service",
                     "--no-console --no-loops"):
            self.assertIn(name, readme)

    def test_the_readme_migration_keeps_the_console_address_and_can_go_back(self):
        readme = (_REPO / "systemd" / "README.md").read_text()
        section = readme.split("## One unit instead of two", 1)[1].split("\n## ", 1)[0]
        # the old console's address, read and carried into a drop-in
        self.assertIn("systemctl --user cat cousin-console.service", section)
        self.assertIn("cousin-supervisor.service.d/", section)
        dropin = [l for l in section.splitlines()
                  if l.startswith("ExecStart=") and "--console-host" in l]
        self.assertEqual(len(dropin), 1, section)
        self.assertIn("--console-port", dropin[0])
        self.assertIn("ExecStart=\n" + dropin[0], section)
        # the switch and its exact reverse
        self.assertIn("systemctl --user disable --now cousin-console.service"
                      " cousin-loops.service\nsystemctl --user enable --now"
                      " cousin-supervisor.service", section)
        self.assertIn("systemctl --user disable --now cousin-supervisor.service &&"
                      " systemctl --user enable --now cousin-console.service"
                      " cousin-loops.service", section)


def _compose(*args):
    tmp = tempfile.TemporaryDirectory()
    try:
        empty = pathlib.Path(tmp.name) / "empty.env"
        empty.write_text("")
        env = {k: v for k, v in os.environ.items() if not k.startswith("COMPOSE_")}
        return subprocess.run(["docker", "compose", "--project-directory", str(_REPO),
                               "--env-file", str(empty)] + list(args),
                              capture_output=True, text=True, timeout=_TIMEOUT, env=env)
    finally:
        tmp.cleanup()


@unittest.skipUnless(_docker_enabled(), "opt-in: COUSIN_DOCKER=1 and docker on PATH")
class TestComposeConfig(unittest.TestCase):
    def config(self, *args):
        r = _compose(*(list(args) + ["config", "--format", "json"]))
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_the_file_is_valid_with_and_without_profiles(self):
        for extra in ([], ["--profile", "embeddings"], ["-f", str(_COMPOSE),
                                                          "-f", str(_COMPOSE_KEY)]):
            with self.subTest(extra=extra):
                args = extra if extra[:1] == ["-f"] else ["-f", str(_COMPOSE)] + extra
                r = _compose(*(args + ["config", "-q"]))
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_default_set_is_the_framework_alone(self):
        cfg = self.config("-f", str(_COMPOSE))
        self.assertEqual(sorted(cfg["services"]), ["framework"])
        fw = cfg["services"]["framework"]
        self.assertEqual(fw["stop_grace_period"], "45s")
        self.assertEqual([(p["host_ip"], p["published"], p["target"]) for p in fw["ports"]],
                         [("127.0.0.1", "8600", 8600)])
        self.assertEqual([(v["type"], v["source"], v["target"]) for v in fw["volumes"]],
                         [("volume", "framework-data", "/data")])
        self.assertEqual(fw["environment"]["COUSIN_DEFAULT_RUNNER"], "sdk")
        self.assertNotIn("secrets", fw)

    def test_the_embeddings_profile_adds_the_embedding_service(self):
        cfg = self.config("-f", str(_COMPOSE), "--profile", "embeddings")
        self.assertEqual(sorted(cfg["services"]), ["embeddings", "framework"])

    def test_the_key_override_adds_the_secret_and_the_default_account(self):
        cfg = self.config("-f", str(_COMPOSE), "-f", str(_COMPOSE_KEY))
        fw = cfg["services"]["framework"]
        self.assertEqual([s["source"] for s in fw["secrets"]], ["anthropic_api_key"])
        self.assertEqual(fw["environment"]["COUSIN_DEFAULT_ACCOUNT"], "api-key")
        self.assertEqual(fw["environment"]["COUSIN_DEFAULT_RUNNER"], "sdk")
        self.assertTrue(cfg["secrets"]["anthropic_api_key"]["file"].endswith(
            "/secrets/anthropic_api_key"))


if __name__ == "__main__":
    unittest.main()
