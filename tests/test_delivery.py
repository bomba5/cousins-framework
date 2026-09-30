"""The one way anything reaches a cousin (docs/design/agent-loop-runner.md)."""
import os
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from cousin_lib import delivery
from cousin_lib.delivery import DeliveryError, Item
from tests._hermetic import HermeticCase


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


class TestChatApiSeamsUseTheFacade(unittest.TestCase):
    """chat_api.make_deliver and make_notify (the console's, cousin-chat's)
    hand items to delivery.deliver without waiting."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "wren"
        (self.home / "data").mkdir(parents=True)
        (self.root / "config").mkdir()
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n'
            '[operator]\nname = "Sam"\n[agent]\nrunner = "fake"\n')

    def _config(self):
        from cousin_lib.config import CousinConfig
        return CousinConfig.load(self.home)

    def test_a_send_becomes_a_chat_item_on_the_senders_thread(self):
        seen = []
        with mock.patch("cousin_lib.delivery.deliver",
                        lambda home, item, **kw: seen.append((item, kw))
                        or delivery.QUEUED):
            from cousin_lib.server import chat_api
            chat_api.make_deliver(self._config())(
                user="Sam", message="hello", message_id=4, attachments=["/tmp/a.png"])
        item, kw = seen[0]
        self.assertEqual(
            (item.thread_id, item.source, item.sender, item.body,
             item.attachments, item.context, item.message_id),
            ("operator:Sam", "chat", "Sam", "hello", ("/tmp/a.png",), "", 4))
        self.assertIs(kw["wait"], False)

    def test_a_reaction_notice_is_a_reaction_item(self):
        seen = []
        with mock.patch("cousin_lib.delivery.deliver",
                        lambda home, item, **kw: seen.append(item)
                        or delivery.QUEUED):
            from cousin_lib.server import chat_api
            chat_api.make_notify(self._config())("[fw-reaction] msg-id=4 emoji=x user=Sam"
                          " tap_count=1 op=added")
        self.assertEqual((seen[0].source, seen[0].thread_id),
                         ("reaction", "system"))


class TestInboxBackend(HermeticCase):
    def setUp(self):
        super().setUp()
        from tests.runner._home import temp_home
        self.home = temp_home(self, runner="fake")

    def test_backend_for_picks_the_inbox_for_a_runner_cousin(self):
        self.assertEqual(type(delivery.backend_for(self.home)).__name__, "InboxBackend")

    def test_send_without_wait_puts_pokes_and_says_queued(self):
        from cousin_lib.runner.inbox import Inbox
        out = delivery.deliver(self.home, Item("operator:priya", "chat", "hi", sender="Priya"), wait=False)
        self.assertEqual(out, delivery.QUEUED)
        self.assertEqual(Inbox(self.home).pending(), 1)

    def test_send_with_wait_reports_delivered_when_a_runner_finishes_the_row(self):
        from cousin_lib.runner.fake import FakeRunner
        r = FakeRunner(self.home); self.addCleanup(lambda: r.stop(timeout=5)); r.start()
        out = delivery.deliver(self.home, Item("operator:priya", "chat", "hi", sender="Priya"),
                               wait=True, timeout=5.0)
        self.assertEqual(out, delivery.DELIVERED)

    def test_send_with_wait_and_no_runner_times_out_to_queued_not_failed(self):
        out = delivery.deliver(self.home, Item("operator:priya", "chat", "hi", sender="Priya"),
                               wait=True, timeout=0.3)
        self.assertEqual(out, delivery.QUEUED)


class TestInboxBackendNeverRaises(HermeticCase):
    """deliver() promises never to raise for a delivery problem."""

    def setUp(self):
        super().setUp()
        from tests.runner._home import temp_home
        self.home = temp_home(self, runner="fake")

    def _item(self):
        return Item("operator:priya", "chat", "hi", sender="Priya")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root writes anywhere")
    def test_an_unwritable_data_dir_is_failed_not_an_exception(self):
        data = self.home / "data"
        data.chmod(0o500)
        self.addCleanup(data.chmod, 0o700)
        for wait in (False, True):
            self.assertEqual(delivery.deliver(self.home, self._item(), wait=wait, timeout=0.2),
                             delivery.FAILED)

    def test_a_poke_that_raises_after_a_durable_put_is_queued(self):
        from cousin_lib.runner.inbox import Inbox
        with mock.patch("cousin_lib.runner.wake.poke", side_effect=OSError("no fds")):
            out = delivery.deliver(self.home, self._item(), wait=False)
        self.assertEqual(out, delivery.QUEUED)
        self.assertEqual(Inbox(self.home).pending(), 1)

    def test_a_store_error_while_waiting_is_queued_the_row_is_durable(self):
        import sqlite3
        from cousin_lib.runner.inbox import Inbox
        with mock.patch.object(Inbox, "get", side_effect=sqlite3.OperationalError("locked")):
            out = delivery.deliver(self.home, self._item(), wait=True, timeout=0.2)
        self.assertEqual(out, delivery.QUEUED)


class TestMalformedCousinToml(HermeticCase):
    def test_a_malformed_cousin_toml_is_refused_and_cousin_runner_refuses(self):
        import contextlib
        import io
        from tests.runner._home import temp_home
        from cousin_lib.runner import main as runner_main
        home = temp_home(self, runner="sdk")
        (home / "cousin.toml").write_text('[agent\nrunner = "sdk"\n')
        self.assertIsInstance(delivery.backend_for(home),
                              delivery.RefusedBackend)
        self.assertIn("cousin.toml", delivery.lane_refusal(home))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = runner_main.runner_main(["--home", str(home), "--once"])
        self.assertEqual(rc, 2)
        self.assertIn("cousin.toml", stderr.getvalue())


class TestProducerContracts(HermeticCase):
    def test_a_queued_outcome_is_accepted_once_for_a_runner_cousin(self):
        from tests.runner._home import temp_home
        home = temp_home(self, runner="fake")
        self.assertTrue(delivery.accepted(delivery.QUEUED, home))
        self.assertTrue(delivery.accepted(delivery.DELIVERED, home))
        self.assertFalse(delivery.accepted(delivery.FAILED, home))
        (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n')
        self.assertFalse(delivery.accepted(delivery.QUEUED, home))   # refused: nothing was kept

    def test_is_alive_reads_the_runner_lock_for_a_runner_cousin(self):
        from tests.runner._home import temp_home
        from cousin_lib.runner import main as runner_main
        home = temp_home(self, runner="fake")
        self.assertFalse(delivery.is_alive(home, fallback=lambda: True))
        with runner_main.hold_lock(home):           # a context manager main.py exposes for tests
            self.assertTrue(delivery.is_alive(home, fallback=lambda: False))
        (home / "cousin.toml").write_text('[cousin]\nslug = "wren"\nname = "Wren"\n')
        self.assertFalse(delivery.is_alive(home, fallback=lambda: True))


class TestRefusedLane(HermeticCase):
    """R2, R3, R14: a cousin with no runner kind is refused by name, and a
    delivery to it is `failed`, never typed anywhere."""

    def _home(self, text, slug="wren"):
        from tests.runner._home import temp_home
        home = temp_home(self, slug=slug)
        (home / "cousin.toml").write_text(text)
        return home

    def _no_runner(self, slug="wren"):
        return self._home('[cousin]\nslug = "%s"\nname = "%s"\n'
                          % (slug, slug.capitalize()), slug=slug)

    def test_a_cousin_with_no_runner_gets_the_refused_backend(self):
        self.assertIsInstance(delivery.backend_for(self._no_runner()),
                              delivery.RefusedBackend)

    def test_an_unknown_runner_value_is_refused(self):
        home = self._home('[cousin]\nslug = "wren"\nname = "Wren"\n'
                          '[agent]\nrunner = "docker"\n')
        self.assertIsInstance(delivery.backend_for(home),
                              delivery.RefusedBackend)

    def test_an_unparsable_cousin_toml_is_refused(self):
        home = self._home('[agent\nrunner = "sdk"\n')
        self.assertIsInstance(delivery.backend_for(home),
                              delivery.RefusedBackend)

    def test_a_refused_send_is_failed_and_never_raises(self):
        home = self._no_runner()
        threads = {"interrupt": "operator:Sam", "schedule": "schedule",
                   "flip": "system", "boot": "system", "hook": "system"}
        with mock.patch("subprocess.run", side_effect=AssertionError), \
                mock.patch("subprocess.Popen", side_effect=AssertionError), \
                mock.patch("socket.create_connection",
                           side_effect=AssertionError):
            for source in delivery.SOURCES:
                item = Item(thread_id=threads.get(source, "operator:Sam"),
                            source=source, sender="Sam", body="hello")
                for wait in (True, False):
                    self.assertEqual(delivery.deliver(home, item, wait=wait),
                                     delivery.FAILED, (source, wait))

    def test_the_refusal_names_the_slug_and_cousin_migrate(self):
        line = delivery.lane_refusal(self._no_runner(slug="testa"))
        for part in ("testa", "[agent] runner", "cousin-migrate apply testa",
                     "docs/migrating.md"):
            self.assertIn(part, line)
        self.assertNotIn("\n", line)

    def test_a_worker_is_refused_with_the_worker_line(self):
        home = self._home('[cousin]\nslug = "toki"\nname = "Toki"\n'
                          'type = "worker"\n', slug="toki")
        self.assertIsInstance(delivery.backend_for(home),
                              delivery.RefusedBackend)
        line = delivery.lane_refusal(home)
        self.assertIn("toki", line)
        self.assertIn("worker", line)
        self.assertIn("no session", line)
        self.assertNotIn("cousin-migrate", line)

    def test_is_alive_is_false_for_a_refused_cousin(self):
        self.assertFalse(delivery.is_alive(self._no_runner(),
                                           fallback=lambda: True))

    def test_tmux_backend_is_gone(self):
        self.assertFalse(hasattr(delivery, "TmuxBackend"))


if __name__ == "__main__":
    unittest.main()
