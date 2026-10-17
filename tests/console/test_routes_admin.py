import pathlib
"""Host stats, the chat-server log tail, and the console's own restart."""
import os
import socket
import time
import unittest
from unittest import mock

from tests.console._harness import ConsoleCase


class TestHost(ConsoleCase):
    def test_host_block_shape(self):
        self.serve()
        _, body = self.get("/api/host")
        self.assertEqual(body["host"], socket.gethostname())
        for key in ("kernel", "uptime", "cpu", "mem", "disk", "net",
                    "console_uptime"):
            self.assertIn(key, body)
        self.assertEqual(set(body["cpu"]), {"pct", "load1", "load5", "load15"})
        self.assertEqual(set(body["mem"]), {"total", "used", "cached"})
        self.assertEqual(set(body["net"]),
                         {"rx", "tx", "rx_total_gb", "tx_total_gb"})
        self.assertGreaterEqual(body["console_uptime"], 0)


class TestCpuPct(unittest.TestCase):
    """#335: CPU % is time used over what the system may use, not the
    load average (an LXC's is the host's)."""

    def _run(self, files, clock, affinity=4, calls=2):
        from cousin_lib.console import routes_admin
        server = mock.Mock(state={})
        files = dict(files)
        seq = iter(clock)

        def read(path):
            value = files.get(path, "")
            return value.pop(0) if isinstance(value, list) else value
        out = []
        with mock.patch.object(routes_admin, "_read", side_effect=read), \
             mock.patch.object(routes_admin.time, "time", side_effect=lambda: next(seq)), \
             mock.patch.object(routes_admin.time, "sleep"), \
             mock.patch.object(routes_admin.os, "sched_getaffinity",
                               return_value=set(range(affinity)), create=True):
            for _ in range(calls):
                out.append(routes_admin._cpu_pct(server, next(seq)))
        return out

    def test_cgroup_time_over_the_cpus_it_may_use(self):
        # 3 s of CPU over 10 s on 4 CPUs is 7.5%, whatever the load says
        stat = ["usage_usec 1000000\n", "usage_usec 1000000\n",
                "usage_usec 4000000\n"]
        pct = self._run({"/sys/fs/cgroup/cpu.stat": stat,
                         "/sys/fs/cgroup/cpu.max": "max 100000\n"},
                        clock=[0.0, 0.1, 10.1])
        self.assertEqual(pct[1], 7.5)

    def test_a_quota_is_the_capacity(self):
        stat = ["usage_usec 0\n", "usage_usec 0\n", "usage_usec 1000000\n"]
        pct = self._run({"/sys/fs/cgroup/cpu.stat": stat,
                         "/sys/fs/cgroup/cpu.max": "50000 100000\n"},
                        clock=[0.0, 0.1, 2.1])
        self.assertEqual(pct[1], 100.0)     # 1 s over 2 s of half a CPU, capped

    def test_proc_stat_is_the_fallback(self):
        line = lambda busy, idle: "cpu  %d 0 0 %d 0 0 0 0 0 0\nintr 1\n" % (busy, idle)
        pct = self._run({"/proc/stat": [line(100, 900), line(100, 900), line(130, 970)]},
                        clock=[0.0, 0.1, 5.1])
        self.assertEqual(pct[1], 30.0)

    def test_the_first_call_samples_rather_than_answering_zero(self):
        stat = ["usage_usec 0\n", "usage_usec 50000\n"]
        pct = self._run({"/sys/fs/cgroup/cpu.stat": stat}, clock=[0.0, 0.1],
                        affinity=1, calls=1)
        self.assertEqual(pct, [50.0])

    def test_no_source_is_zero(self):
        self.assertEqual(self._run({}, clock=[0.0, 1.0], calls=1), [0.0])


class TestRestart(ConsoleCase):
    def test_answers_then_exits_through_the_seam(self):
        server = self.serve()
        exits = []
        server.exit_fn = lambda: exits.append(time.time())
        with mock.patch.dict(os.environ, {"INVOCATION_ID": "abc"}):
            before = time.time()
            status, body = self.post("/api/admin/restart/framework")
        self.assertEqual(body, {"ok": True, "target": "console",
                                "supervised": True, "eta_seconds": 4})
        self.assertEqual(exits, [])
        deadline = time.time() + 3
        while not exits and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(len(exits), 1)
        self.assertGreaterEqual(exits[0] - before, 0.5)
        with mock.patch.dict(os.environ, {}, clear=True):
            server.exit_fn = lambda: None
            _, body = self.post("/api/admin/restart/framework")
        self.assertFalse(body["supervised"])


class TestNoLogsRoute(ConsoleCase):
    """GET /api/logs served only the retired "tail logs" drawer and was
    removed with it; the canary fails if the route is registered
    again."""

    def test_the_route_is_gone(self):
        self.cousin("wren")
        self.serve()
        self.assertEqual(self.get("/api/logs")[0], 404)
        self.assertEqual(self.get("/api/logs?cousin=wren")[0], 404)


if __name__ == "__main__":
    unittest.main()


class TestRestartExitCode(unittest.TestCase):
    def test_the_restart_exit_is_a_failure_so_systemd_restarts_it(self):
        # Canary: a restart button that exits 0 under a unit that only
        # restarts on failure would stop the console instead.
        from cousin_lib.console import routes_admin
        self.assertNotEqual(routes_admin.RESTART_EXIT_CODE, 0)
        unit = (pathlib.Path(__file__).resolve().parents[2] / "systemd"
                / "cousin-console.service").read_text()
        self.assertIn("Restart=on-failure", unit)
