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
_SIZE = _REPO / "docker" / "image-size.sh"
_WORKFLOW = _REPO / ".github" / "workflows" / "image.yml"
_COMPOSE_OPENCODE = _REPO / "compose.opencode.yml"
# The opencode variant (phase 9 R17): the npm registry's platform package
# of the pinned release, checked twice (the tarball the build downloads,
# then the binary it copies). The binary's sha256 is the live install's
# pinned copy's; the tarball's was computed once from the registry's file,
# whose sha512 matched the registry's dist.integrity.
_OPENCODE_VERSION = "1.18.31"
_OPENCODE_TGZ = ("https://registry.npmjs.org/opencode-linux-x64/-/"
                 "opencode-linux-x64-%s.tgz" % _OPENCODE_VERSION)
_OPENCODE_TGZ_SHA256 = "6d89da252a8b030d923e728396dc34465cf6095101b78222b0ee337b68140dea"
_OPENCODE_BIN_SHA256 = "f9dab32248695e9ebd56b16a1921798fd85112cf5a69c7dfd0cabc1e17be4a11"
_OPENCODE_BIN = "/opt/opencode/bin/opencode"
# The default image's budget plus the pinned tarball (60.2 MB): measured
# 222.8 MB against the default's 162.2 MB.
_OPENCODE_BUDGET = 240000000
# Executables of a JS runtime or its package manager, as words: the
# registry's host name (registry.npmjs.org) is not one.
_JS_TOOLS = re.compile(r"\b(node|nodejs|npm|npx|bun|bunx|yarn|pnpm)\b")
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


def _stages(ins):
    """[(base, name, [(INSTRUCTION, argument), ...])] per FROM, in order."""
    out = []
    for word, arg in ins:
        if word == "FROM":
            base, _, name = arg.partition(" AS ")
            out.append((base.strip(), name.strip() or None, []))
        elif out:
            out[-1][2].append((word, arg))
    return out


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

    def _stage(self, name):
        found = [s for s in _stages(self.ins) if s[1] == name]
        self.assertEqual(len(found), 1, name)
        return found[0]

    def test_two_stages_on_the_slim_base(self):
        # The default image is builder then final; the stages after final
        # are the opencode variant's and the default alias (below).
        stages = _stages(self.ins)
        self.assertEqual([(b, n) for b, n, _ in stages[:2]],
                         [("python:3.13-slim", "builder"), ("python:3.13-slim", "final")])
        self.assertTrue(all(b.startswith("python:3.13-slim") or b == "final"
                            for b, _, _ in stages), stages)

    def test_the_source_is_installed_in_place_with_the_sdk_extra(self):
        run = " ".join(self._args("RUN"))
        self.assertIn('pip install --no-cache-dir -e "/opt/framework[sdk]"', run)
        copies = self._args("COPY")
        self.assertIn("--from=builder /opt/venv /opt/venv", copies)
        self.assertIn("--from=builder /opt/framework /opt/framework", copies)

    def test_pip_is_removed_from_the_image(self):
        # The venv's pip in the builder, the base image's own in the final
        # stage, as root: before the one USER line.
        builder = " ".join(a for w, a in self._stage("builder")[2] if w == "RUN")
        self.assertIn("/opt/venv/bin/pip uninstall -y pip", builder)
        final = self._stage("final")[2]
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

    def test_the_image_says_it_is_the_frameworks_container(self):
        """accounts.in_container() reads this marker: a login line in the
        image names the compose exec, not a host user on the container's id."""
        from cousin_lib import accounts
        env = " ".join(self._args("ENV")).split()
        self.assertIn("%s=1" % accounts.CONTAINER_VAR, env)

    def test_healthcheck_asks_the_version_route_without_curl(self):
        check = " ".join(self._args("HEALTHCHECK"))
        self.assertIn("http://127.0.0.1:8600/api/version", check)
        self.assertIn("urllib", check)
        self.assertNotIn("curl", check)

    def test_entrypoint_and_command(self):
        self.assertEqual([json.loads(a) for a in self._args("ENTRYPOINT")],
                         [["/opt/framework/docker/entrypoint.sh"]])
        self.assertEqual([json.loads(a) for a in self._args("CMD")], [_CMD])


class TestDockerfileOpencode(unittest.TestCase):
    """R17: `--target opencode` is the default image plus the pinned
    binary; `docker build .` (no target) is still the default image."""

    def setUp(self):
        self.ins = _instructions(_DOCKERFILE.read_text())
        self.stages = _stages(self.ins)
        self.by_name = {n: (b, body) for b, n, body in self.stages}

    def test_the_stages_in_order(self):
        self.assertEqual([(b, n) for b, n, _ in self.stages],
                         [("python:3.13-slim", "builder"), ("python:3.13-slim", "final"),
                          ("python:3.13-slim", "opencode-fetch"), ("final", "opencode"),
                          ("final", "default")])

    def test_a_build_without_a_target_is_the_default_image(self):
        # The last stage is what `docker build .` builds: final, unchanged.
        base, name, body = self.stages[-1]
        self.assertEqual((base, name, body), ("final", "default", []))

    def test_the_default_image_names_no_opencode_and_no_js_runtime(self):
        for stage in ("builder", "final"):
            text = " ".join("%s %s" % pair for pair in self.by_name[stage][1])
            with self.subTest(stage=stage):
                self.assertNotIn("opencode", text.lower())
                self.assertIsNone(_JS_TOOLS.search(text), text)

    def test_the_opencode_stage_adds_the_binary_and_nothing_else(self):
        base, body = self.by_name["opencode"]
        self.assertEqual(base, "final")
        self.assertEqual([w for w, _ in body], ["COPY", "ENV"])
        self.assertEqual(body[0][1], "--from=opencode-fetch /opt/opencode /opt/opencode")
        env = body[1][1].split()
        self.assertEqual(env, ["COUSIN_OPENCODE_BIN=%s" % _OPENCODE_BIN,
                               "PATH=/opt/opencode/bin:$PATH"])

    def test_the_download_is_the_pinned_platform_package_checked_twice(self):
        base, body = self.by_name["opencode-fetch"]
        self.assertEqual(body[0], ("ARG", "TARGETARCH"))
        self.assertEqual([w for w, _ in body], ["ARG", "RUN"])
        run = body[1][1]
        pkg, version = "opencode-linux-x64", _OPENCODE_VERSION
        self.assertEqual(_OPENCODE_TGZ, "https://registry.npmjs.org/%s/-/%s-%s.tgz"
                         % (pkg, pkg, version))
        self.assertIn("pkg=%s" % pkg, run)
        self.assertIn("version=%s" % version, run)
        self.assertIn('url="https://registry.npmjs.org/$pkg/-/$pkg-$version.tgz"', run)
        self.assertIn("tgz_sha256=%s" % _OPENCODE_TGZ_SHA256, run)
        self.assertIn("bin_sha256=%s" % _OPENCODE_BIN_SHA256, run)
        # Both checks are sha256sum -c, and each comes before its file is used.
        checks = [m.start() for m in re.finditer(r"sha256sum -c", run)]
        self.assertEqual(len(checks), 2, run)
        self.assertLess(run.index("urllib.request"), checks[0])
        self.assertLess(checks[0], run.index("tar -xzf"))
        self.assertLess(run.index("tar -xzf"), checks[1])
        self.assertLess(checks[1], run.index("install -D -m 0755"))
        self.assertIn("/opt/opencode/bin/opencode", run)
        # Only amd64 is pinned: any other architecture stops the build.
        self.assertIn("amd64)", run)
        self.assertIn("*)", run)
        self.assertIsNone(_JS_TOOLS.search(run), run)
        self.assertNotIn("curl", run)

    def test_every_sha_is_a_full_sha256(self):
        run = [a for w, a in self.by_name["opencode-fetch"][1] if w == "RUN"][0]
        shas = re.findall(r"_sha256=(\S+?);", run)
        self.assertEqual(shas, [_OPENCODE_TGZ_SHA256, _OPENCODE_BIN_SHA256])
        for sha in shas:
            self.assertRegex(sha, r"^[0-9a-f]{64}$")

    def test_no_stage_installs_a_js_runtime(self):
        for base, name, body in self.stages:
            for word, arg in body:
                if word == "RUN":
                    with self.subTest(stage=name):
                        self.assertIsNone(_JS_TOOLS.search(arg), arg)


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


# Probes both images: which names resolve on PATH, what COUSIN_OPENCODE_BIN
# says, whether /opt/opencode exists.
_PROBE = ("import os, shutil\n"
          "print([shutil.which(n) for n in %r])\n"
          "print(shutil.which('opencode'), os.environ.get('COUSIN_OPENCODE_BIN'),"
          " os.path.exists('/opt/opencode'))\n"
          % (("node", "nodejs", "npm", "npx", "bun", "bunx", "yarn", "pnpm"),))


@unittest.skipUnless(_docker_enabled(), "opt-in: COUSIN_DOCKER=1 and docker on PATH")
class TestOpencodeImage(unittest.TestCase):
    """R17 against built images: the default target and `--target opencode`.
    COUSIN_DOCKER_IMAGE and COUSIN_DOCKER_OPENCODE_IMAGE name images
    already built from this checkout; otherwise both are built here."""

    @classmethod
    def setUpClass(cls):
        cls.built = []
        cls.default = os.environ.get("COUSIN_DOCKER_IMAGE")
        cls.opencode = os.environ.get("COUSIN_DOCKER_OPENCODE_IMAGE")
        tag = uuid.uuid4().hex[:8]
        if not cls.default:
            cls.default = "cousins-framework:test-%s" % tag
            _docker("build", "-t", cls.default, str(_REPO), timeout=1800)
            cls.built.append(cls.default)
        if not cls.opencode:
            cls.opencode = "cousins-framework:test-%s-opencode" % tag
            _docker("build", "--target", "opencode", "-t", cls.opencode, str(_REPO),
                    timeout=1800)
            cls.built.append(cls.opencode)

    @classmethod
    def tearDownClass(cls):
        for image in cls.built:
            _docker("image", "rm", "-f", image, check=False)

    def probe(self, image):
        out = _docker("run", "--rm", "--network", "none", image, "python", "-c", _PROBE,
                      timeout=300).stdout.splitlines()
        return ast.literal_eval(out[0]), out[1].split()

    def test_the_default_image_has_no_opencode(self):
        js, (which, env, opt) = self.probe(self.default)
        self.assertEqual(js, [None] * 8)
        self.assertEqual((which, env, opt), ("None", "None", "False"))

    def test_the_opencode_image_runs_the_pinned_binary_offline(self):
        js, (which, env, opt) = self.probe(self.opencode)
        self.assertEqual(js, [None] * 8, "no node, npm or bun in the opencode image")
        self.assertEqual((which, env, opt), (_OPENCODE_BIN, _OPENCODE_BIN, "True"))
        for argv in (["opencode", "--version"], [_OPENCODE_BIN, "--version"]):
            with self.subTest(argv=argv):
                out = _docker(*(["run", "--rm", "--network", "none", self.opencode] + argv),
                              timeout=300).stdout
                self.assertEqual(out.strip(), _OPENCODE_VERSION)

    def test_the_binary_is_the_pinned_file_owned_by_root(self):
        script = ("import hashlib, os\n"
                  "p = %r\n"
                  "st = os.stat(p)\n"
                  "print(hashlib.sha256(open(p, 'rb').read()).hexdigest(), st.st_uid,"
                  " oct(st.st_mode & 0o7777), os.access(p, os.W_OK))\n" % _OPENCODE_BIN)
        out = _docker("run", "--rm", "--network", "none", self.opencode, "python", "-c",
                      script, timeout=300).stdout.split()
        self.assertEqual(out, [_OPENCODE_BIN_SHA256, "0", "0o755", "False"])

    def test_the_opencode_image_is_the_default_image_plus_one_layer(self):
        def inspect(image, field):
            return json.loads(_docker("image", "inspect", "-f", "{{json %s}}" % field,
                                      image).stdout)
        base = inspect(self.default, ".RootFS.Layers")
        variant = inspect(self.opencode, ".RootFS.Layers")
        self.assertEqual(variant[:len(base)], base)
        self.assertEqual(len(variant), len(base) + 1)
        a, b = inspect(self.default, ".Config"), inspect(self.opencode, ".Config")
        for key in ("Entrypoint", "Cmd", "User", "Healthcheck", "WorkingDir", "Volumes",
                    "ExposedPorts"):
            self.assertEqual(a[key], b[key], key)
        self.assertEqual(sorted(set(b["Env"]) - set(a["Env"])),
                         ["COUSIN_OPENCODE_BIN=%s" % _OPENCODE_BIN,
                          [e for e in b["Env"] if e.startswith("PATH=")][0]])
        path = [e for e in b["Env"] if e.startswith("PATH=")][0]
        old = [e for e in a["Env"] if e.startswith("PATH=")][0]
        self.assertEqual(path, "PATH=/opt/opencode/bin:" + old[len("PATH="):])

    def test_both_images_are_measured_within_their_budgets(self):
        for image, budget in ((self.default, 180000000),
                              (self.opencode, _OPENCODE_BUDGET)):
            with self.subTest(image=image):
                r = subprocess.run(["sh", str(_SIZE), image, str(budget)],
                                   capture_output=True, text=True, timeout=600)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertRegex(r.stdout, r"^compressed: \d+\.\d MB")


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


class TestComposeOpencode(unittest.TestCase):
    """R17: the opencode variant is an override of the one framework
    service (its image), never a second service on the same volume."""

    def test_the_override_changes_the_build_target_and_the_tag_only(self):
        services = _services(_COMPOSE_OPENCODE)
        self.assertEqual(sorted(services), ["framework"])
        self.assertEqual(services["framework"],
                         ["build:", "context: .", "target: opencode",
                          "image: cousins-framework:opencode"])
        live = _live_lines(_COMPOSE_OPENCODE)
        self.assertEqual([l for l in live if not l.startswith(" ")], ["services:"])

    def test_the_default_compose_file_names_no_opencode_and_points_at_the_override(self):
        self.assertNotIn("opencode", "\n".join(_live_lines(_COMPOSE)))
        text = _COMPOSE.read_text()
        # assertTrue, not assertIn: a miss would print the whole file.
        self.assertTrue("docker compose -f compose.yml -f compose.opencode.yml up -d --build"
                        in text)
        self.assertFalse("arrives with that runner" in text)

    def test_the_override_file_is_ascii_without_a_home_path_and_not_in_the_image(self):
        text = _COMPOSE_OPENCODE.read_text()
        text.encode("ascii")
        self.assertNotIn("/home/", text)
        patterns = [l.strip() for l in _DOCKERIGNORE.read_text().splitlines()
                    if l.strip() and not l.strip().startswith("#")]
        self.assertTrue(_dockerignored("compose.opencode.yml", patterns))

    def test_the_install_page_documents_the_variant(self):
        text = " ".join((_REPO / "docs" / "install.md").read_text().split())
        for needle in ("compose.opencode.yml",
                       "docker compose -f compose.yml -f compose.opencode.yml up -d --build",
                       "--target opencode", "no node", _OPENCODE_VERSION):
            self.assertTrue(needle in text, "%r not in install.md" % needle)
        conf = (_REPO / "docs" / "configuration.md").read_text()
        row = [l for l in conf.splitlines() if l.startswith("| `COUSIN_OPENCODE_BIN` |")]
        self.assertEqual(len(row), 1)
        self.assertIn(_OPENCODE_BIN, row[0])


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


@unittest.skipUnless(_docker_enabled(), "opt-in: COUSIN_DOCKER=1 and docker on PATH")
class TestComposeOpencodeConfig(unittest.TestCase):
    def config(self, *files):
        args = []
        for f in files:
            args += ["-f", str(f)]
        r = _compose(*(args + ["config", "--format", "json"]))
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_the_default_builds_no_target(self):
        fw = self.config(_COMPOSE)["services"]["framework"]
        self.assertNotIn("target", fw["build"])
        self.assertEqual(fw["image"], "cousins-framework:local")

    def test_the_override_runs_the_same_service_on_the_opencode_target(self):
        base = self.config(_COMPOSE)["services"]["framework"]
        cfg = self.config(_COMPOSE, _COMPOSE_OPENCODE)
        self.assertEqual(sorted(cfg["services"]), ["framework"])
        fw = cfg["services"]["framework"]
        self.assertEqual(fw["build"]["target"], "opencode")
        self.assertEqual(fw["build"]["context"], base["build"]["context"])
        self.assertEqual(fw["image"], "cousins-framework:opencode")
        for key in ("ports", "volumes", "environment", "stop_grace_period", "restart"):
            self.assertEqual(fw[key], base[key], key)

    def test_it_combines_with_the_key_override(self):
        cfg = self.config(_COMPOSE, _COMPOSE_KEY, _COMPOSE_OPENCODE)
        fw = cfg["services"]["framework"]
        self.assertEqual(fw["build"]["target"], "opencode")
        self.assertEqual([s["source"] for s in fw["secrets"]], ["anthropic_api_key"])



_FAKE_DOCKER = """#!/bin/sh
# A stand-in docker: `save` writes FAKE_SAVE_BYTES random bytes (random
# data does not compress, so the gzip size is about the same), or fails.
case "$1" in
  image) [ -z "${FAKE_MISSING:-}" ] || exit 1; exit 0 ;;
  save) [ -z "${FAKE_SAVE_FAIL:-}" ] || { echo "save failed" >&2; exit 1; }
        head -c "$FAKE_SAVE_BYTES" /dev/urandom ;;
  *) exit 64 ;;
esac
"""


class TestImageSizeScript(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bin_dir = pathlib.Path(tmp.name)
        fake = bin_dir / "docker"
        fake.write_text(_FAKE_DOCKER)
        fake.chmod(0o755)
        self.env = dict(os.environ, PATH="%s:%s" % (bin_dir, os.environ["PATH"]),
                        FAKE_SAVE_BYTES="300000")

    def size(self, *args, **env):
        return subprocess.run(["sh", str(_SIZE)] + list(args),
                              env=dict(self.env, **env), capture_output=True,
                              text=True, timeout=_TIMEOUT)

    def test_under_budget_passes_and_prints_both_sizes(self):
        r = self.size("cousins-framework:ci", "1000000")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"^compressed: 0\.3 MB \(budget 1\.0 MB\)\n$")

    def test_over_budget_fails(self):
        r = self.size("cousins-framework:ci", "200000")
        self.assertEqual(r.returncode, 1)
        self.assertRegex(r.stdout, r"^compressed: 0\.3 MB \(budget 0\.2 MB\)\n$")
        self.assertIn("over budget", r.stderr)

    def test_the_default_budget_is_180_mb(self):
        r = self.size("cousins-framework:ci")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("(budget 180.0 MB)", r.stdout)

    def test_usage_errors_exit_2(self):
        for args in ([], ["a", "1", "extra"], ["cousins-framework:ci", "180MB"],
                     ["cousins-framework:ci", ""]):
            with self.subTest(args=args):
                r = self.size(*args)
                self.assertEqual(r.returncode, 2)
                self.assertIn("usage", r.stderr)

    def test_an_image_that_cannot_be_saved_is_never_under_budget(self):
        # Without pipefail, gzip of an empty stream is 20 bytes: a failed
        # save must not read as a tiny image.
        for env in ({"FAKE_MISSING": "1"}, {"FAKE_SAVE_FAIL": "1"}):
            with self.subTest(env=env):
                r = self.size("cousins-framework:ci", **env)
                self.assertEqual(r.returncode, 2, r.stdout)
                self.assertNotIn("compressed:", r.stdout)

    def test_it_is_an_executable_posix_sh_script(self):
        text = _SIZE.read_text()
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertTrue(os.access(_SIZE, os.X_OK))
        self.assertIn("gzip -6", text)
        for bashism in ("[[ ", "function ", "pipefail", "$'", "<<<", "local "):
            self.assertFalse(bashism in text, "bashism %r" % bashism)


class TestImageWorkflow(unittest.TestCase):
    def setUp(self):
        self.text = _WORKFLOW.read_text()
        self.flat = " ".join(self.text.split())

    def test_it_runs_on_every_push_and_pull_request(self):
        self.assertIn("on:\n  push:\n  pull_request:\n", self.text)

    def test_it_builds_the_image_from_the_checkout(self):
        self.assertIn('docker build -t "$IMAGE" .', self.flat)

    def test_the_contract_suite_runs_inside_the_image_with_tests_mounted_read_only(self):
        self.assertIn('docker run --rm -v "$PWD/tests:/opt/framework/tests:ro"'
                      ' -w /opt/framework "$IMAGE"'
                      " python -m unittest discover -s tests/runner/contract -t .",
                      self.flat)

    def test_the_image_tests_run_on_the_host_against_that_image(self):
        self.assertIn('COUSIN_DOCKER: "1"', self.text)
        self.assertIn("COUSIN_DOCKER_IMAGE: ${{ env.IMAGE }}", self.text)
        self.assertIn("python -m unittest tests.test_docker_files -v", self.flat)

    def test_the_size_budget_is_checked_last(self):
        self.assertIn('sh docker/image-size.sh "$IMAGE" 180000000', self.flat)
        steps = [self.flat.index(s) for s in (
            "docker build", "tests/runner/contract", "tests.test_docker_files",
            "docker/image-size.sh")]
        self.assertEqual(steps, sorted(steps))

    def test_ascii_and_no_home_path(self):
        self.text.encode("ascii")
        self.assertNotIn("/home/", self.text)


if __name__ == "__main__":
    unittest.main()
