"""The one way anything reaches a cousin (docs/design/agent-loop-runner.md)."""
import unittest

from cousin_lib import delivery
from cousin_lib.delivery import DeliveryError, Item
import os
import pathlib
import tempfile
from datetime import datetime, timezone
from cousin_lib.server.injection import compose_delivery
import stat
from unittest import mock
from tests.server.test_injection import _FAKE_TMUX
from types import SimpleNamespace


class TestThreadIds(unittest.TestCase):
    def test_a_keyed_kind_renders_kind_colon_key(self):
        self.assertEqual(delivery.thread_id("peer", "testa"), "peer:testa")

    def test_a_bare_kind_renders_alone(self):
        self.assertEqual(delivery.thread_id("schedule"), "schedule")
        self.assertEqual(delivery.thread_id("system"), "system")

    def test_a_keyed_kind_without_a_key_is_refused(self):
        with self.assertRaises(DeliveryError):
            delivery.thread_id("operator")

    def test_a_bare_kind_with_a_key_is_refused(self):
        with self.assertRaises(DeliveryError):
            delivery.thread_id("schedule", "x")

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(DeliveryError):
            delivery.thread_id("pane", "wren")

    def test_parse_round_trips_and_keeps_colons_in_the_key(self):
        self.assertEqual(delivery.parse_thread("meeting:12"), ("meeting", "12"))
        self.assertEqual(delivery.parse_thread("loop:a:b"), ("loop", "a:b"))
        self.assertEqual(delivery.parse_thread("system"), ("system", ""))


class TestItem(unittest.TestCase):
    def test_an_item_validates_its_thread_and_source(self):
        with self.assertRaises(DeliveryError):
            Item(thread_id="nope:x", source="chat", body="hi")
        with self.assertRaises(DeliveryError):
            Item(thread_id="system", source="keyboard", body="hi")

    def test_attachments_are_frozen_into_a_tuple(self):
        item = Item(thread_id="operator:Sam", source="chat", body="hi",
                    attachments=["a.png"])
        self.assertEqual(item.attachments, ("a.png",))

    def test_the_three_outcomes_are_the_only_ones(self):
        self.assertEqual((delivery.DELIVERED, delivery.QUEUED,
                          delivery.FAILED),
                         ("delivered", "queued", "failed"))


class TestTmuxRender(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name)
        (self.home / "data").mkdir()
        self.marker = self.home / "data" / ".last-user-msg"
        self.now = datetime(2026, 8, 6, 5, 30, tzinfo=timezone.utc)
        self.backend = delivery.TmuxBackend()

    def test_chat_is_byte_identical_to_compose_delivery(self):
        self.marker.touch()
        ago = self.now.timestamp() - 12 * 60
        os.utime(self.marker, (ago, ago))
        item = Item(thread_id="operator:Sam", source="chat", sender="Sam",
                    body="two\nlines  here", attachments=("/tmp/a.png",))
        expected = compose_delivery("Sam", "two\nlines  here",
                                    marker_path=self.marker,
                                    attachments=("/tmp/a.png",), now=self.now)
        self.assertEqual(self.backend.render(self.home, item, now=self.now),
                         expected)

    def test_chat_context_rides_as_the_old_recall_suffix_did(self):
        item = Item(thread_id="operator:Sam", source="chat", sender="Sam",
                    body="where is the plan", context="[fw-recall] plan.md")
        expected = compose_delivery(
            "Sam", "where is the plan" + " " + "[fw-recall] plan.md",
            marker_path=self.marker, now=self.now)
        self.assertEqual(self.backend.render(self.home, item, now=self.now),
                         expected)

    def test_a_chat_hook_line_is_composed_like_chat(self):
        item = Item(thread_id="system", source="hook", sender="fw-hook",
                    body="rotated")
        expected = compose_delivery("fw-hook", "rotated",
                                    marker_path=self.marker, now=self.now)
        self.assertEqual(self.backend.render(self.home, item, now=self.now),
                         expected)

    def test_a_schedule_gets_its_prefix(self):
        item = Item(thread_id="schedule", source="schedule", body="check CI")
        self.assertEqual(self.backend.render(self.home, item),
                         "[cousin-schedule] check CI")

    def test_every_other_source_is_typed_exactly_as_given(self):
        for source, thread in (("reaction", "operator:Sam"),
                               ("loop", "loop:digest"),
                               ("meeting", "meeting:7"),
                               ("flip", "system"), ("boot", "system")):
            body = "[x] line one\nline two"
            item = Item(thread_id=thread, source=source, body=body)
            self.assertEqual(self.backend.render(self.home, item), body)


class TestDeliver(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        self.home = root / "cousins" / "wren"
        (self.home / "data").mkdir(parents=True)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n[chat]\nport = 8099\n')
        self.tmux = root / "tmux"
        self.tmux.write_text(_FAKE_TMUX)
        self.tmux.chmod(self.tmux.stat().st_mode | stat.S_IEXEC)
        self.log = root / "calls.log"
        patcher = mock.patch.dict(os.environ, {
            "FAKE_TMUX_LOG": str(self.log),
            "FAKE_TMUX_PANE": str(root / "pane.txt")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.opts = dict(tmux_bin=str(self.tmux), settle=lambda n: 0,
                         verify_delay=0)

    def _calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_a_typed_line_is_delivered(self):
        item = Item(thread_id="schedule", source="schedule", body="check CI")
        self.assertEqual(delivery.deliver(self.home, item, **self.opts),
                         delivery.DELIVERED)
        self.assertTrue(any("[cousin-schedule] check CI" in c
                            for c in self._calls()))

    def test_a_failed_paste_is_failed_not_delivered(self):
        item = Item(thread_id="loop:digest", source="loop", body="beat")
        with mock.patch.dict(os.environ, {"FAKE_TMUX_RC": "1"}):
            self.assertEqual(delivery.deliver(self.home, item, **self.opts),
                             delivery.FAILED)

    def test_not_waiting_is_queued_and_still_types(self):
        item = Item(thread_id="meeting:7", source="meeting", body="your turn")
        outcome = delivery.deliver(self.home, item, wait=False, **self.opts)
        self.assertEqual(outcome, delivery.QUEUED)
        for thread in list(__import__("threading").enumerate()):
            if thread.daemon and thread is not __import__(
                    "threading").current_thread():
                thread.join(2)
        self.assertTrue(any("your turn" in c for c in self._calls()))

    def test_an_explicit_backend_is_used_instead(self):
        seen = []

        class Recorder:
            def send(self, home, item, *, wait=True, **opts):
                seen.append((item.thread_id, wait))
                return delivery.QUEUED

        item = Item(thread_id="peer:testa", source="chat", sender="Testa",
                    body="hi")
        self.assertEqual(
            delivery.deliver(self.home, item, backend=Recorder()),
            delivery.QUEUED)
        self.assertEqual(seen, [("peer:testa", True)])

    def test_a_missing_home_is_failed_not_an_exception(self):
        item = Item(thread_id="system", source="boot", body="x")
        self.assertEqual(
            delivery.deliver(self.home / "nope", item, **self.opts),
            delivery.FAILED)


class TestThreadForChat(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(operator_name="Sam", slug="wren",
                                      home=pathlib.Path("/nonexistent"))
        self.cousins = [SimpleNamespace(slug="wren", name="Wren"),
                        SimpleNamespace(slug="testa", name="Testa")]

    def test_the_operator_in_any_case_like_is_operator(self):
        # The same normalisation server/app.py _is_operator uses: case
        # and inner spaces, no stripping. The two MUST agree, or a sender
        # could be the operator for recall and a person for threading.
        self.assertEqual(
            delivery.thread_for_chat(self.config, "SAM",
                                     cousins=self.cousins),
            "operator:Sam")

    def test_a_peer_by_name_or_slug_maps_to_its_slug(self):
        for user in ("Testa", "testa"):
            self.assertEqual(
                delivery.thread_for_chat(self.config, user,
                                         cousins=self.cousins),
                "peer:testa")

    def test_anyone_else_is_a_person(self):
        self.assertEqual(
            delivery.thread_for_chat(self.config, "Priya",
                                     cousins=self.cousins),
            "person:Priya")

    def test_no_operator_configured_means_nobody_is(self):
        config = SimpleNamespace(operator_name=None, slug="wren",
                                 home=pathlib.Path("/nonexistent"))
        self.assertEqual(
            delivery.thread_for_chat(config, "Sam", cousins=self.cousins),
            "person:Sam")

    def test_an_unreadable_registry_never_costs_the_thread(self):
        self.assertEqual(delivery.thread_for_chat(self.config, "Testa"),
                         "person:Testa")


class TestProducersUseTheFacade(unittest.TestCase):
    """Each default-deliver hands a typed item to delivery.deliver and
    turns the outcome back into the bool its tick keys on."""

    def _run(self, call, outcome):
        seen = []

        def fake(home, item, **kw):
            seen.append((pathlib.Path(home).name, item))
            return outcome

        root = SimpleNamespace(
            root=pathlib.Path("/r"),
            list_cousins=lambda: [SimpleNamespace(
                slug="wren", home=pathlib.Path("/r/cousins/wren"))])
        with mock.patch("cousin_lib.delivery.deliver", fake), \
                mock.patch("cousin_lib.config.FrameworkConfig.from_env",
                           return_value=root):
            return call(), seen

    def test_schedule(self):
        from cousin_lib import schedule
        ok, seen = self._run(
            lambda: schedule._default_deliver("wren", "check CI"),
            delivery.DELIVERED)
        self.assertIs(ok, True)
        home, item = seen[0]
        self.assertEqual((home, item.thread_id, item.source, item.body),
                         ("wren", "schedule", "schedule", "check CI"))

    def test_schedule_failure_keeps_the_job_pending(self):
        from cousin_lib import schedule
        ok, _ = self._run(
            lambda: schedule._default_deliver("wren", "check CI"),
            delivery.FAILED)
        self.assertIs(ok, False)

    def test_meetings(self):
        from cousin_lib import meetings
        ok, seen = self._run(
            lambda: meetings.default_deliver("wren", "(Meeting 7) your turn"),
            delivery.DELIVERED)
        self.assertIs(ok, True)
        self.assertEqual((seen[0][1].source, seen[0][1].body),
                         ("meeting", "(Meeting 7) your turn"))

    def test_loops(self):
        from cousin_lib import loops
        ok, seen = self._run(
            lambda: loops._default_deliver("wren", "Context heartbeat."),
            delivery.FAILED)
        self.assertIs(ok, False)
        self.assertEqual((seen[0][1].source, seen[0][1].thread_id),
                         ("loop", "loop:daemon"))


class TestChatServerUsesTheFacade(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "wren"
        (self.home / "data").mkdir(parents=True)
        (self.root / "config").mkdir()
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n[chat]\nport = 0\n'
            '[operator]\nname = "Sam"\n')
        self.tmux = self.root / "tmux"
        self.tmux.write_text(_FAKE_TMUX)
        self.tmux.chmod(self.tmux.stat().st_mode | stat.S_IEXEC)

    def _server(self):
        from cousin_lib.server import app
        return app.build_server(self.home, framework_root=str(self.root),
                                tmux_bin=str(self.tmux))

    def test_a_send_becomes_a_chat_item_on_the_senders_thread(self):
        seen = []
        with mock.patch("cousin_lib.delivery.deliver",
                        lambda home, item, **kw: seen.append((item, kw))
                        or delivery.QUEUED):
            server = self._server()
            # Never started, so never stop(): shutdown() blocks forever
            # without a serve_forever loop. Closing the socket is enough.
            self.addCleanup(server.httpd.server_close)
            server.deliver(user="Sam", message="hello", message_id=4,
                           attachments=["/tmp/a.png"], context="[fw-recall] x")
        item, kw = seen[0]
        self.assertEqual(
            (item.thread_id, item.source, item.sender, item.body,
             item.attachments, item.context, item.message_id),
            ("operator:Sam", "chat", "Sam", "hello", ("/tmp/a.png",),
             "[fw-recall] x", 4))
        self.assertIs(kw["wait"], False)

    def test_a_reaction_notice_is_a_reaction_item(self):
        seen = []
        with mock.patch("cousin_lib.delivery.deliver",
                        lambda home, item, **kw: seen.append(item)
                        or delivery.QUEUED):
            server = self._server()
            # Never started, so never stop(): shutdown() blocks forever
            # without a serve_forever loop. Closing the socket is enough.
            self.addCleanup(server.httpd.server_close)
            server.notify("[fw-reaction] msg-id=4 emoji=x user=Sam"
                          " tap_count=1 op=added")
        self.assertEqual((seen[0].source, seen[0].thread_id),
                         ("reaction", "system"))


class TestFlipAndBootUseTheFacade(unittest.TestCase):
    def test_no_module_but_delivery_builds_an_injector(self):
        """The exit criterion of the phase, as a test: the pane is
        typed into from exactly one place."""
        root = pathlib.Path(delivery.__file__).parent
        allowed = {"delivery.py", "server/injection.py", "console/pane.py"}
        offenders = []
        for path in sorted(root.rglob("*.py")):
            rel = str(path.relative_to(root))
            if rel in allowed:
                continue
            if "TmuxInjector(" in path.read_text():
                offenders.append(rel)
        self.assertEqual(offenders, [])

    def test_a_pending_boot_is_a_boot_item(self):
        from cousin_lib import spawn
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = pathlib.Path(tmp.name)
        (home / "data").mkdir()
        packet = home / "data" / "boot-packet-gen-0002.md"
        packet.write_text("BOOT PACKET FOR COUSIN: wren")
        spawn.pending_boot_path(home).write_text(
            '{"generation": 2, "packet": "%s", "written_at": "x"}' % packet)
        seen = []
        with mock.patch("cousin_lib.delivery.deliver",
                        lambda h, item, **kw: seen.append((item, kw))
                        or delivery.DELIVERED):
            ok = spawn._inject_pending_boot(
                home, SimpleNamespace(tmux_session="wren"), tmux_bin="t",
                tmux_socket=None, settle=0)
        self.assertTrue(ok)
        item, kw = seen[0]
        self.assertEqual((item.source, item.thread_id), ("boot", "system"))
        self.assertEqual(
            item.body,
            "[cousin-start] the last session closed cleanly; boot packet"
            " follows. Do not announce the restart.\n"
            "BOOT PACKET FOR COUSIN: wren")
        self.assertEqual((kw["tmux_bin"], kw["socket"]), ("t", None))


if __name__ == "__main__":
    unittest.main()
