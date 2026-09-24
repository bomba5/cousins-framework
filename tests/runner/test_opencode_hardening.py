"""Two OpencodeRunner fixes that Task 7's proofs surfaced (phase 9 Task 7),
pinned on the fake `opencode serve`; never the binary.

1. The live proof on the real binary, offline: opencode installs
   `@opencode-ai/plugin` from npm into its config dir whenever ANY plugin
   is configured, and loads no plugin (so answers no /event) until that
   install ends: 71 s and a failure with no network, npm egress at every
   fresh data dir with one. The runner seeds the config dir so opencode's
   own check (`Npm.install`: a `node_modules` dir and a lock naming every
   dependency) finds nothing to install; measured: /event 0.8 s after
   health, offline, the plugin loaded.
2. The contract suite on a loaded disk (`priority_order` failed once, the
   inbox's claim and requeue taking 0.1-0.4 s each): the turn loop read
   ONE event per poll cycle once the poll's own work (interrupt rows, the
   fold's claim and requeue of a queued peer or loop row) outlasted
   `poll_s`, so a 25-event turn took 25 cycles. It now reads every event
   already there before polling again.
3. The live proof's aborts, which the fake did not model (Review Focus 4),
   now reproduced by it (`PARTIAL`, `PREP`) with the runner's outcome for
   each: an abort mid-text (the open text part ends only after the first
   idle pair) and an abort before the model answered (one idle pair, no
   error, no assistant message)."""
import json
import time
import unittest

from cousin_lib import accounts
from cousin_lib.delivery import Item
from cousin_lib.runner import opencode
from cousin_lib.runner.opencode import OpencodeRunner
from tests._hermetic import HermeticCase
from tests.runner._home import temp_home
from tests.runner.test_opencode import Factory

ENDPOINT = "http://127.0.0.1:11434/v1"


def _wait(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


class Case(HermeticCase):
    def runner(self, scripts=(), factory=None):
        home = temp_home(self, runner="opencode")
        with open(home / "cousin.toml", "a") as f:
            f.write('model = "local/m1"\n')
        self.data_dir = home.parent.parent / ".secrets" / "accounts" / "lab.opencode"
        account = accounts.Account("lab", "opencode", None, None, data_dir=self.data_dir,
                                   endpoint=ENDPOINT, endpoint_model="m1")
        self.factory = factory or Factory(scripts)
        r = OpencodeRunner(home, account=account, server_factory=self.factory)
        self.addCleanup(lambda: r.stop(timeout=5))
        return r

    def started(self, r):
        r.start()
        self.assertTrue(_wait(lambda: r.opencode_session is not None or r.fatal, 10))
        self.assertIsNone(r.fatal)
        return r

    def lock(self):
        return json.loads((self.data_dir / "config" / "opencode" / "package-lock.json")
                          .read_text())


class TestPluginDependencySeed(Case):
    def test_the_config_dir_is_seeded_before_the_server_starts(self):
        seen = []
        inner = Factory()

        def factory(**kw):
            d = self.data_dir / "config" / "opencode"
            seen.append(((d / "node_modules").is_dir(), (d / "package-lock.json").exists()))
            return inner(**kw)

        self.started(self.runner(factory=factory))
        self.assertEqual(seen, [(True, True)], "seeded before `opencode serve` starts")
        self.assertEqual(self.lock()["packages"][""]["dependencies"],
                         {opencode.PLUGIN_DEPENDENCY: "*"})

    def test_an_existing_lock_is_merged_never_replaced(self):
        r = self.runner()
        d = self.data_dir / "config" / "opencode"
        (d / "node_modules").mkdir(parents=True)
        (d / "package-lock.json").write_text(json.dumps({
            "name": "opencode", "lockfileVersion": 3,
            "packages": {"": {"dependencies": {"left-pad": "1.3.0"}},
                         "node_modules/left-pad": {"version": "1.3.0"}}}))
        self.started(r)
        lock = self.lock()
        self.assertEqual(lock["lockfileVersion"], 3)
        self.assertEqual(lock["packages"]["node_modules/left-pad"], {"version": "1.3.0"})
        self.assertEqual(lock["packages"][""]["dependencies"],
                         {"left-pad": "1.3.0", opencode.PLUGIN_DEPENDENCY: "*"})

    def test_a_lock_that_names_the_dependency_is_left_alone(self):
        """A real install (an online start before this one) stays as it is."""
        r = self.runner()
        d = self.data_dir / "config" / "opencode"
        (d / "node_modules").mkdir(parents=True)
        real = json.dumps({"packages": {"": {"dependencies": {
            opencode.PLUGIN_DEPENDENCY: "1.18.31"}}}}, indent=1)
        (d / "package-lock.json").write_text(real)
        self.started(r)
        self.assertEqual((d / "package-lock.json").read_text(), real)


class TestEventsAreNotHeldBehindThePoll(Case):
    def test_a_slow_poll_does_not_hold_back_the_turns_events(self):
        r = self.runner([[("tool", "bash", {"command": "true"}, ""), ("text", "ok")]])
        slow = r._take_interrupts

        def take_interrupts():        # a loaded disk: the poll outlasts poll_s
            time.sleep(0.3)
            return slow()

        r._take_interrupts = take_interrupts
        self.started(r)
        t0 = time.monotonic()
        r.enqueue(Item("operator:priya", "chat", "go", sender="Priya"))
        self.assertTrue(_wait(lambda: any(e["kind"] == "result" for e in r.events()), 10))
        took = time.monotonic() - t0
        self.assertGreater(len(self.factory.fake.events), 20, "a turn of many events")
        self.assertLess(took, 2.0, "every event already read is handled before the next poll")


class TestTheLiveAbortShapes(Case):
    def _interrupted(self, r, *, after):
        self.started(r)
        a = r.enqueue(Item("operator:priya", "chat", "go", sender="Priya"))
        self.assertTrue(_wait(lambda: r.state() == "running"))
        after(self.factory.fake)
        self.assertTrue(r.interrupt())
        self.assertTrue(_wait(lambda: r.inbox.get(a.inbox_id)["state"] == "done"))
        self.assertTrue(_wait(lambda: r.state() == "idle"))
        self.assertTrue(self.factory.fake.settle())
        time.sleep(0.3)                     # the tail after the first idle pair is read
        return a

    def test_an_abort_mid_text_keeps_the_partial_text(self):
        r = self.runner([[("PARTIAL", "partial "), ("HANG",)]])
        a = self._interrupted(r, after=lambda fake: fake.wait_event(
            "message.part.delta", pred=lambda e: e["properties"]["delta"] == "partial "))
        types = [e["type"] for e in self.factory.fake.events]
        ends = [i for i, e in enumerate(self.factory.fake.events)
                if e["type"] == "message.part.updated"
                and e["properties"]["part"]["type"] == "text"
                and (e["properties"]["part"].get("time") or {}).get("end")]
        self.assertLess(types.index("session.idle"), ends[-1], "the part ends after the idle")
        self.assertEqual([e["payload"] for e in r.events() if e["kind"] == "text"],
                         [{"text": "partial ", "partial": True}])
        result = [e["payload"] for e in r.events() if e["kind"] == "result"]
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["interrupted"])
        self.assertEqual(r.inbox.get(a.inbox_id)["outcome"], "delivered")

    def test_an_abort_before_the_model_answers_is_one_idle_and_a_delivered_row(self):
        r = self.runner([[("PREP", 30.0), ("text", "never")]])
        a = self._interrupted(r, after=lambda fake: fake.wait_event("session.status"))
        types = [e["type"] for e in self.factory.fake.events]
        self.assertNotIn("session.error", types)
        self.assertEqual(types.count("session.idle"), 1)
        self.assertNotIn("assistant", [e["properties"]["info"]["role"]
                                       for e in self.factory.fake.events
                                       if e["type"] == "message.updated"])
        result = [e["payload"] for e in r.events() if e["kind"] == "result"]
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["interrupted"])
        self.assertFalse(result[0]["is_error"])
        self.assertEqual(r.inbox.get(a.inbox_id)["outcome"], "delivered")
        self.assertEqual([e for e in r.events() if e["kind"] == "error"], [])


if __name__ == "__main__":
    unittest.main()
