"""cousin-spawn-node: the copy-over hive node archive.

The builder mints a token through the queen's own store, renders the
node's identity from the shipped template, and writes one self-contained
tarball. Tested against real temporary framework roots; the tarball is
always built under a temp dir, never in the tree (it is a binary the
gate would refuse).
"""
import contextlib
import io
import os
import pathlib
import shutil
import tarfile
import tempfile
import unittest
from unittest import mock

from cousin_lib.hive import HiveStore
from cousin_lib.spawn_node import (
    SpawnNodeError,
    build_node_archive,
    render_node_env,
    spawn_node_main,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TEMPLATE_DIR = _REPO_ROOT / "templates" / "hive-node"

QUEEN = "http://queen.example.invalid:8101"
HOME_CHAT = "http://home.example.invalid:8090"


class SpawnNodeCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        shutil.copytree(_TEMPLATE_DIR, self.root / "templates" / "hive-node")
        self.out = self.root / "out"
        patcher = mock.patch.dict(os.environ,
                                  {"FRAMEWORK_ROOT": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _build(self, slug="testa", **kw):
        kw.setdefault("queen_url", QUEEN)
        kw.setdefault("name", "Testa")
        kw.setdefault("role", "test node")
        kw.setdefault("out", self.out)
        return build_node_archive(self.root, slug=slug, **kw)

    def _members(self, tarball):
        with tarfile.open(tarball) as tar:
            return {m.name: m for m in tar.getmembers()}

    def _read(self, tarball, name):
        with tarfile.open(tarball) as tar:
            return tar.extractfile(name).read().decode()

    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = spawn_node_main(argv)
        return rc, out.getvalue(), err.getvalue()


class TestArchiveContents(SpawnNodeCase):
    def test_tarball_lands_at_out_slug_node_tar_gz(self):
        result = self._build()
        self.assertEqual(result["tarball"],
                         self.out / "testa-node.tar.gz")
        self.assertTrue(result["tarball"].is_file())

    def test_the_five_files_under_one_top_directory(self):
        tarball = self._build()["tarball"]
        names = set(self._members(tarball))
        self.assertEqual(names, {
            "testa-node", "testa-node/cousin_node.py",
            "testa-node/install.sh", "testa-node/CLAUDE.md",
            "testa-node/node.env", "testa-node/README",
        })

    def test_install_is_executable_and_env_is_private(self):
        members = self._members(self._build()["tarball"])
        self.assertTrue(members["testa-node/install.sh"].mode & 0o111)
        # The env carries the bearer token: owner-only, like the queen's
        # own token store.
        self.assertEqual(members["testa-node/node.env"].mode & 0o777, 0o600)

    def test_runtime_is_the_template_byte_for_byte(self):
        tarball = self._build()["tarball"]
        self.assertEqual(
            self._read(tarball, "testa-node/cousin_node.py"),
            (_TEMPLATE_DIR / "cousin_node.py").read_text())
        self.assertEqual(
            self._read(tarball, "testa-node/install.sh"),
            (_TEMPLATE_DIR / "install.sh").read_text())

    def test_identity_is_rendered_with_no_leftover_placeholder(self):
        tarball = self._build(role="answers the doorbell")["tarball"]
        text = self._read(tarball, "testa-node/CLAUDE.md")
        self.assertNotIn("{{", text)
        self.assertIn("Testa", text)
        self.assertIn("answers the doorbell", text)

    def test_readme_names_the_two_steps(self):
        text = self._read(self._build()["tarball"], "testa-node/README")
        self.assertIn("install.sh", text)
        self.assertIn("node.env", text)


class TestArchiveInstalls(SpawnNodeCase):
    def test_the_unpacked_archive_passes_its_own_installer_check(self):
        # The tarball and the installer are proven together: unpack
        # what the builder wrote and let install.sh source node.env and
        # render the unit from inside, as the operator would.
        import subprocess
        tarball = self._build(name="Testa Two", home_chat=HOME_CHAT)["tarball"]
        with tarfile.open(tarball) as tar:
            tar.extractall(self.out, filter="data")
        unpacked = self.out / "testa-node"
        result = subprocess.run(
            ["bash", "./install.sh", "--print-unit"], cwd=unpacked,
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ExecStart=", result.stdout)
        self.assertIn(str(unpacked / "cousin_node.py"), result.stdout)
        self.assertIn("EnvironmentFile=%s" % (unpacked / "node.env"),
                      result.stdout)


class TestNodeEnv(SpawnNodeCase):
    def _env(self, tarball):
        text = self._read(tarball, "testa-node/node.env")
        return dict(line.split("=", 1) for line in text.splitlines()
                    if line and not line.startswith("#"))

    def test_env_carries_queen_token_slug_and_port(self):
        result = self._build(port=8210, home_chat=HOME_CHAT)
        env = self._env(result["tarball"])
        self.assertEqual(env["COUSIN_SLUG"], "testa")
        self.assertEqual(env["NODE_NAME"], "Testa")
        self.assertEqual(env["NODE_PORT"], "8210")
        self.assertEqual(env["QUEEN_URL"], QUEEN)
        self.assertEqual(env["HIVE_TOKEN"], result["token"])
        self.assertEqual(env["HOME_CHAT_URL"], HOME_CHAT)
        # Present and empty: the placeholder brain until the operator
        # fills it in on the node.
        self.assertEqual(env["AGENT_CMD"], "")

    def test_no_home_chat_means_an_empty_gateway(self):
        env = self._env(self._build()["tarball"])
        self.assertEqual(env["HOME_CHAT_URL"], "")

    def test_render_quotes_a_value_the_shell_would_split(self):
        text = render_node_env(slug="testa", name="Testa Two", port=8210,
                               queen_url=QUEEN, token="hive_x",
                               home_chat="", agent_cmd="")
        self.assertIn("NODE_NAME='Testa Two'", text)
        # ... and a plain value stays a plain KEY=VALUE line.
        self.assertIn("COUSIN_SLUG=testa\n", text)


class TestMint(SpawnNodeCase):
    def test_token_is_minted_in_the_queen_store_with_both_scopes(self):
        token = self._build()["token"]
        store = HiveStore(self.root / "shared" / "hive")
        self.assertEqual(store.resolve(token),
                         {"slug": "testa", "scope": {"own", "shared"}})

    def test_rebuilding_keeps_the_same_token(self):
        # Re-minting must not orphan a node already deployed with the
        # first token.
        first = self._build()["token"]
        second = self._build()["token"]
        self.assertEqual(first, second)

    def test_explicit_token_skips_the_mint(self):
        # A remote queen minted it; the builder must not invent a local
        # one that the real queen would 401.
        result = self._build(token="hive_from_the_remote_queen")
        self.assertEqual(result["token"], "hive_from_the_remote_queen")
        store = HiveStore(self.root / "shared" / "hive")
        self.assertIsNone(store.resolve("hive_from_the_remote_queen"))
        self.assertEqual(
            store.conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0],
            0)


class TestRefusals(SpawnNodeCase):
    def test_invalid_slug(self):
        with self.assertRaises(SpawnNodeError) as ctx:
            self._build(slug="Not A Slug")
        self.assertIn("slug", str(ctx.exception))

    def test_missing_template_directory_names_it(self):
        shutil.rmtree(self.root / "templates" / "hive-node")
        with self.assertRaises(SpawnNodeError) as ctx:
            self._build()
        self.assertIn("hive-node", str(ctx.exception))

    def test_a_failed_render_leaves_no_tarball(self):
        template = self.root / "templates" / "hive-node" / "CLAUDE.md"
        template.write_text(template.read_text() + "\n{{UNKNOWN_KEY}}\n")
        with self.assertRaises(SpawnNodeError) as ctx:
            self._build()
        self.assertIn("UNKNOWN_KEY", str(ctx.exception))
        self.assertFalse((self.out / "testa-node.tar.gz").exists())


class TestCli(SpawnNodeCase):
    def test_build_prints_the_tarball_path(self):
        rc, out, _ = self._main([
            "testa", "--queen-url", QUEEN, "--name", "Testa",
            "--role", "test node", "--out", str(self.out)])
        self.assertEqual(rc, 0)
        self.assertIn(str(self.out / "testa-node.tar.gz"), out)
        # The token is a secret: it goes in the archive, never on stdout.
        self.assertNotIn("hive_", out)

    def test_no_root_is_a_usage_error(self):
        # Outside any checkout: inside one, the root defaults to it.
        with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": ""}), \
                tempfile.TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            rc, _, err = self._main([
                "testa", "--queen-url", QUEEN, "--name", "Testa",
                "--role", "r", "--out", str(self.out)])
        self.assertEqual(rc, 2)
        self.assertIn("--root", err)

    def test_explicit_root_flag_wins(self):
        with mock.patch.dict(os.environ, {"FRAMEWORK_ROOT": ""}):
            rc, _, _ = self._main([
                "testa", "--root", str(self.root), "--queen-url", QUEEN,
                "--name", "Testa", "--role", "r", "--out", str(self.out)])
        self.assertEqual(rc, 0)

    def test_bad_slug_is_exit_two(self):
        rc, _, err = self._main([
            "BAD SLUG", "--queen-url", QUEEN, "--name", "x",
            "--role", "r", "--out", str(self.out)])
        self.assertEqual(rc, 2)
        self.assertIn("slug", err)


if __name__ == "__main__":
    unittest.main()
