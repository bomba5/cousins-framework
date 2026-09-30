"""cousin-supervisor runs a plugin's [service] (docs/plugins.md): one child
`plugin:<name>` while at least one cousin enables the plugin, started
before the runners, restarted with the backoff, added and removed by a
rescan, stopped with the supervisor. The supervisor runs in process; the
service is the fake plugin's stdlib HTTP server. Invented cast only."""
import io
import json
import os
import pathlib
import signal
import sys
import tempfile
import time
import urllib.request
from unittest import mock

from cousin_lib import supervisor
from cousin_lib.supervisor import ChildSpec, Supervisor
from tests import _plugins as fake
from tests._hermetic import HermeticCase

FAST = dict(backoff=(0.05, 0.1), window=60.0, max_exits=5, healthy_after=60.0, tick=0.02)
_SLEEP = "import time; time.sleep(600)"


def _wait_for(predicate, timeout=10.0, step=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if step:
            step()
        if predicate():
            return True
        time.sleep(0.02)
    return False


class _Case(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name) / "root"
        (self.root / "config").mkdir(parents=True)
        self.port = fake.free_port()
        self.dir = fake.write_plugin(self.root, "clock", port=self.port)
        fake.declare(self.root, "clock")

    def cousin(self, slug, *enabled):
        home = self.root / "cousins" / slug
        for sub in ("data", "run", "memory"):
            (home / sub).mkdir(parents=True, exist_ok=True)
        (home / "cousin.toml").write_text('[cousin]\nslug = "%s"\nname = "%s"\n\n[agent]\n'
                                          'runner = "fake"\n' % (slug, slug.capitalize()))
        if enabled:
            fake.enable(home, *enabled)
        return home

    def runner(self, home):
        slug = pathlib.Path(home).name
        return ChildSpec("runner:%s" % slug, "runner", [sys.executable, "-c", _SLEEP],
                         stop_timeout=5.0, slug=slug, home=home)

    def supervise(self, specs=()):
        # a rescan's runner cousins get the sleeping stub, never a real runner
        patch = mock.patch.object(supervisor, "runner_spec", self.runner)
        patch.start()
        self.addCleanup(patch.stop)
        out = io.StringIO()
        sup = Supervisor(self.root, list(specs), out=out, **FAST)
        self.addCleanup(self._kill_all, sup)
        sup.start_all()
        return sup, out

    def _kill_all(self, sup):
        for child in list(sup.children.values()):
            if child.proc is not None and child.proc.returncode is None:
                try:
                    os.killpg(child.proc.pid, signal.SIGKILL)
                except OSError:
                    pass
        sup.stop_all()

    def row(self, sup, name="plugin:clock"):
        return sup.status()["children"].get(name)

    def echo(self):
        with urllib.request.urlopen("http://127.0.0.1:%d/echo" % self.port, timeout=2) as r:
            return json.loads(r.read())

    def healthy(self):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % self.port,
                                        timeout=0.5) as r:
                return r.status == 200
        except OSError:
            return False


class TestTheChild(_Case):
    def test_nobody_enables_it_so_it_does_not_run(self):
        wren = self.cousin("wren")
        sup, out = self.supervise([self.runner(wren)])
        self.assertEqual(list(sup.status()["children"]), ["runner:wren"])
        self.assertNotIn("plugin:clock", out.getvalue())

    def test_one_cousin_enabling_it_starts_one_service_before_the_runners(self):
        wren = self.cousin("wren", "clock")
        sam = self.cousin("sam", "clock")
        sup, out = self.supervise([self.runner(wren), self.runner(sam)])
        self.assertEqual(sorted(sup.status()["children"]),
                         ["plugin:clock", "runner:sam", "runner:wren"])
        self.assertTrue(_wait_for(self.healthy, step=sup.step))
        lines = out.getvalue()
        self.assertLess(lines.index("started plugin:clock"), lines.index("started runner:wren"))
        env = self.echo()["env"]
        self.assertEqual(env, {"PLUGIN_PORT": str(self.port), "FRAMEWORK_ROOT": str(self.root),
                               "CLOCK_MODE": "fake"})
        log = self.root / "data" / "plugins" / "clock" / "service.log"
        self.assertTrue(_wait_for(lambda: log.is_file() and "clock service on" in
                                  log.read_text(), step=sup.step))
        self.assertIn("plugin:clock | clock service on", out.getvalue())

    def test_a_crash_restarts_it_with_the_backoff(self):
        self.cousin("wren", "clock")
        sup, _ = self.supervise()
        self.assertTrue(_wait_for(self.healthy, step=sup.step))
        first = self.row(sup)["pid"]
        os.kill(first, signal.SIGKILL)
        self.assertTrue(_wait_for(lambda: (self.row(sup)["pid"] not in (None, first)
                                           and self.healthy()), step=sup.step))
        self.assertGreaterEqual(self.row(sup)["restarts"], 1)

    def test_the_supervisor_stops_it(self):
        self.cousin("wren", "clock")
        sup, _ = self.supervise()
        self.assertTrue(_wait_for(self.healthy, step=sup.step))
        sup.stop_all()
        self.assertEqual(self.row(sup)["state"], "stopped")
        self.assertFalse(self.healthy())


class TestRescan(_Case):
    def test_reload_removes_it_when_no_cousin_enables_it_and_adds_it_back(self):
        wren = self.cousin("wren", "clock")
        sup, _ = self.supervise([self.runner(wren)])
        self.assertTrue(_wait_for(self.healthy, step=sup.step))
        (wren / "cousin.toml").write_text((wren / "cousin.toml").read_text()
                                          .replace('["clock"]', "[]"))
        added, removed = sup.reload()
        self.assertEqual((added, removed), ([], ["plugin:clock"]))
        self.assertTrue(_wait_for(lambda: self.row(sup) is None, step=sup.step))
        self.assertFalse(self.healthy())
        fake.enable(self.cousin("sam"), "clock")
        added, _ = sup.reload()
        self.assertEqual(added, ["runner:sam", "plugin:clock"])
        self.assertTrue(_wait_for(self.healthy, step=sup.step))

    def test_a_changed_manifest_restarts_it(self):
        self.cousin("wren", "clock")
        sup, out = self.supervise()
        self.assertTrue(_wait_for(self.healthy, step=sup.step))
        first = self.row(sup)["pid"]
        manifest = self.dir / "plugin.toml"
        manifest.write_text(manifest.read_text().replace('CLOCK_MODE = "fake"',
                                                         'CLOCK_MODE = "slow"'))
        sup.reload()
        self.assertTrue(_wait_for(lambda: (self.row(sup)["pid"] not in (None, first)
                                           and self.healthy()), step=sup.step))
        self.assertEqual(self.echo()["env"]["CLOCK_MODE"], "slow")
        self.assertIn("plugin:clock: its command or env changed", out.getvalue())

    def test_a_bad_plugin_is_one_line_and_named_in_status(self):
        fake.write_plugin(self.root, "dial", text='name = "dial"\nshade = 1\n')
        fake.declare(self.root, "clock", "dial")
        self.cousin("wren", "clock", "dial")
        sup, out = self.supervise()
        self.assertIn("plugin dial skipped: plugin.toml has unknown key shade", out.getvalue())
        self.assertEqual(sorted(sup.status()["children"]), ["plugin:clock"])
        found = supervisor.registry_findings(self.root)["plugins"]
        self.assertEqual([p["name"] for p in found], ["dial"])
        sup.reload()
        self.assertEqual(out.getvalue().count("plugin dial skipped"), 1)   # said on change only
