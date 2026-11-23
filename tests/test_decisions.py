"""The decision register (docs/decisions.md): a cousin asks, the operator
answers, and the answer is recorded before it reaches the cousin as an
operator-level memory and a turn. A failed delivery is kept and retried;
a group comes back as one turn once all of it is settled."""
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

from cousin_lib import decisions, delivery, mcp_server
from cousin_lib.runner import tools
from cousin_lib.runner.inbox import Inbox
from cousin_lib.runner.policy import Policy
from cousin_lib.runner.turn import Turn
from tests._hermetic import HermeticCase

ROOT = pathlib.Path(__file__).resolve().parents[1]
COUSIN = ('[cousin]\nslug = "%s"\nname = "%s"\n%s\n[agent]\nrunner = "fake"\n')


def _raw(home):
    out = []
    for path in sorted((pathlib.Path(home) / "memory" / "raw").glob("*.jsonl")):
        out.extend(json.loads(line) for line in path.read_text().splitlines() if line.strip())
    return out


class Case(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        os.environ["FRAMEWORK_ROOT"] = str(self.root)
        self.home = self.cousin("wren", operator="Priya")
        # the group grace has a test of its own; elsewhere a group is due at once
        grace = mock.patch.object(decisions, "GROUP_GRACE_S", 0)
        grace.start()
        self.addCleanup(grace.stop)

    def cousin(self, slug, operator=None, runner=True):
        home = self.root / "cousins" / slug
        (home / "memory").mkdir(parents=True, exist_ok=True)
        text = COUSIN % (slug, slug.capitalize(),
                         '\n[operator]\nname = "%s"\n' % operator if operator else "")
        if not runner:
            text = text.replace('\n[agent]\nrunner = "fake"\n', "\n")
        (home / "cousin.toml").write_text(text)
        return home

    def ask(self, question="Which cache?", **kw):
        kw.setdefault("options", ["a: keep it", "b: drop it"])
        return decisions.ask(kw.pop("cousin", "wren"), question, root=self.root, **kw)

    def turns(self, home=None):
        return Inbox(home or self.home).open_rows("decision")


class TestAsk(Case):
    def test_ask_list_show(self):
        row = self.ask(recommendation="a", why="speed", group="cache")
        self.assertEqual((row["id"], row["status"], row["cousin"], row["grp"]),
                         (1, "open", "wren", "cache"))
        self.assertEqual(row["options"], [{"key": "a", "text": "keep it"},
                                          {"key": "b", "text": "drop it"}])
        self.ask("Second?", cousin="testa")
        self.assertEqual([r["id"] for r in decisions.list_decisions(root=self.root)], [1, 2])
        self.assertEqual([r["id"] for r in decisions.list_decisions(cousin="testa",
                                                                    root=self.root)], [2])
        self.assertEqual(decisions.show(1, root=self.root)["why"], "speed")
        self.assertIsNone(decisions.show(9, root=self.root))
        self.assertEqual([h["field"] for h in decisions.history(1, root=self.root)],
                         ["created"])

    def test_options_parse_from_strings_and_objects(self):
        self.assertEqual(decisions.parse_options(
            ["x: one", "Use it: now", {"text": "three"}, {"key": "z", "text": "four"},
             "https://g.example/x", "Note: y"]),
            [{"key": "x", "text": "one"}, {"key": "a", "text": "Use it: now"},
             {"key": "b", "text": "three"}, {"key": "z", "text": "four"},
             {"key": "c", "text": "https://g.example/x"}, {"key": "d", "text": "Note: y"}])
        self.assertEqual(len(decisions.parse_options(["o%d" % i for i in range(26)])), 26)
        with self.assertRaises(decisions.DecisionError):     # the letters run out
            decisions.parse_options(["o%d" % i for i in range(27)])
        for bad in (["a: one", "a: two"], ["a:"], [{"key": "a"}]):
            with self.assertRaises(decisions.DecisionError, msg=bad):
                decisions.parse_options(bad)

    def test_a_blank_question_or_no_cousin_is_refused(self):
        with self.assertRaises(decisions.DecisionError):
            self.ask("  ")
        with self.assertRaises(decisions.DecisionError):
            self.ask("q", cousin="")


class TestAnswer(Case):
    def test_an_answer_is_recorded_remembered_and_delivered_on_the_operator_thread(self):
        self.ask(recommendation="a")
        row = decisions.answer(1, option="b", answer="today", user="admin",
                               via="the console", root=self.root)
        self.assertEqual((row["status"], row["answer_option"], row["answered_by"],
                          row["delivery"]), ("answered", "b", "admin", "delivered"))
        self.assertTrue(row["remembered_at"] and row["delivered_at"])
        [entry] = _raw(self.home)
        self.assertEqual(entry["truth_level"], "L0_OPERATOR")
        self.assertEqual(entry["topic"], "decision #1: Which cache?")
        self.assertIn("Answer: b: drop it (today)", entry["content"])
        self.assertIn("recommendation: a: keep it", entry["content"])
        self.assertTrue(entry["cite"].startswith("decision register #1, answered by admin at "))
        self.assertTrue(entry["cite"].endswith(" in the console"))
        self.assertEqual(decisions.memory_topic({"id": 4, "question": "Two\nlines  here?"}),
                         "decision #4: Two lines here?")
        [turn] = self.turns()
        self.assertEqual((turn["thread_id"], turn["source"], turn["sender"]),
                         ("operator:Priya", "decision", "admin"))
        self.assertEqual(turn["body"], "Decision #1 answered: Which cache? -> b: drop it (today)")

    def test_a_cousin_with_no_operator_gets_the_turn_on_system(self):
        home = self.cousin("testa")
        self.ask(cousin="testa")
        decisions.answer(1, answer="neither", user="admin", root=self.root)
        self.assertEqual([t["thread_id"] for t in self.turns(home)], ["system"])

    def test_refusals(self):
        self.ask()
        with self.assertRaises(decisions.DecisionError):
            decisions.answer(1, user="admin", root=self.root)
        with self.assertRaises(decisions.DecisionError):
            decisions.answer(1, option="q", user="admin", root=self.root)
        with self.assertRaises(decisions.DecisionNotFound):
            decisions.answer(7, option="a", user="admin", root=self.root)
        decisions.answer(1, option="a", user="admin", root=self.root)
        with self.assertRaises(decisions.DecisionError):
            decisions.answer(1, option="b", user="admin", root=self.root)
        self.assertEqual(len(self.turns()), 1)

    def test_a_failed_delivery_keeps_the_answer_and_redeliver_sends_it_once(self):
        self.ask()
        with mock.patch.object(delivery, "deliver", return_value=delivery.FAILED):
            row = decisions.answer(1, option="a", user="admin", root=self.root)
            self.assertEqual((row["status"], row["delivered_at"], row["delivery"]),
                             ("answered", None, "pending"))
            self.assertTrue(decisions.owed(self.root))
            self.assertEqual(decisions.redeliver(root=self.root), [{"ids": [1], "ok": False}])
        self.assertEqual(self.turns(), [])
        self.assertEqual(len(_raw(self.home)), 1)        # the memory does not wait
        self.assertEqual(decisions.redeliver(root=self.root), [{"ids": [1], "ok": True}])
        self.assertEqual(decisions.redeliver(root=self.root), [])
        self.assertEqual(len(self.turns()), 1)
        self.assertTrue(decisions.show(1, root=self.root)["delivered"])
        self.assertFalse(decisions.owed(self.root))

    def test_a_refused_delivery_is_undeliverable_until_a_manual_retry(self):
        self.cousin("wren", operator="Priya", runner=False)     # refused: no runner kind
        self.ask()
        row = decisions.answer(1, option="a", user="admin", root=self.root)
        self.assertEqual(row["delivery"], "undeliverable")
        self.assertTrue(row["delivered_at"].startswith("gone:"))
        self.assertIn("its delivery is refused", row["undeliverable"])
        self.assertFalse(row["delivered"])
        self.assertFalse(decisions.owed(self.root))      # the poller leaves it alone
        self.assertEqual(decisions.redeliver(root=self.root), [])
        self.cousin("wren", operator="Priya")             # the runner kind is back
        self.assertEqual(decisions.redeliver(root=self.root), [])
        self.assertEqual(decisions.redeliver(retry_gone=True, root=self.root),
                         [{"ids": [1], "ok": True}])
        self.assertEqual(len(self.turns()), 1)
        self.assertEqual(decisions.show(1, root=self.root)["undeliverable"], "")

    def test_a_cousin_whose_home_is_gone_is_undeliverable_until_a_manual_retry(self):
        import shutil
        self.ask()
        shutil.rmtree(self.home)
        row = decisions.answer(1, option="a", user="admin", root=self.root)
        self.assertEqual(row["delivery"], "undeliverable")
        self.assertTrue(row["remembered_at"].startswith("gone:"))
        self.assertEqual(row["undeliverable"], "the cousin's home is gone")
        self.assertFalse(decisions.owed(self.root))
        self.assertEqual(decisions.redeliver(root=self.root), [])
        self.home = self.cousin("wren", operator="Priya")
        self.assertEqual(decisions.redeliver(retry_gone=True, root=self.root),
                         [{"ids": [1], "ok": True}])
        row = decisions.show(1, root=self.root)
        self.assertEqual((row["remembered"], row["delivered"]), (True, True))
        self.assertEqual((len(_raw(self.home)), len(self.turns())), (1, 1))

    def test_a_delivery_that_raises_never_loses_the_answer(self):
        self.ask()
        with mock.patch.object(delivery, "deliver", side_effect=OSError("disk")):
            row = decisions.answer(1, option="a", user="admin", root=self.root)
        self.assertEqual((row["status"], row["delivery"]), ("answered", "pending"))
        decisions.redeliver(root=self.root)
        self.assertEqual(len(self.turns()), 1)

    def test_a_memory_that_failed_is_retried_by_redeliver(self):
        self.ask()
        from cousin_lib import memory
        with mock.patch.object(memory, "remember", side_effect=OSError("full")):
            row = decisions.answer(1, option="a", user="admin", root=self.root)
        self.assertIsNone(row["remembered_at"])
        self.assertEqual(_raw(self.home), [])
        decisions.redeliver(root=self.root)
        self.assertEqual(len(_raw(self.home)), 1)
        self.assertTrue(decisions.show(1, root=self.root)["remembered_at"])

    def test_a_lost_mark_is_reclaimed_after_its_lease_and_the_put_is_not_repeated(self):
        self.ask()
        real = decisions._release

        def crash_on_the_turn(root, column, ids, lease, ok):
            if column == "delivered_at":
                raise OSError("crash before the mark")
            return real(root, column, ids, lease, ok)
        with mock.patch.object(decisions, "_release", side_effect=crash_on_the_turn):
            row = decisions.answer(1, option="a", user="admin", root=self.root)
        self.assertTrue(row["delivered_at"].startswith("pending:"))
        self.assertEqual(len(self.turns()), 1)
        self.assertEqual(decisions.redeliver(root=self.root), [])      # the lease holds
        with mock.patch.object(decisions, "LEASE_S", 0):
            self.assertEqual(decisions.redeliver(root=self.root), [{"ids": [1], "ok": True}])
        self.assertEqual(len(self.turns()), 1)          # the keyed put is the first row
        self.assertTrue(decisions.show(1, root=self.root)["delivered"])

    def test_a_redeliver_during_an_answer_writes_the_memory_once(self):
        self.ask()
        from cousin_lib import memory
        real = memory.remember
        overlapped = []

        def remember_while_a_retry_runs(*args, **kw):
            if not overlapped:
                overlapped.append(decisions.redeliver(root=self.root))
            return real(*args, **kw)
        with mock.patch.object(memory, "remember", side_effect=remember_while_a_retry_runs):
            decisions.answer(1, option="a", user="admin", root=self.root)
        self.assertEqual(len(overlapped), 1)           # the retry ran inside the answer
        self.assertEqual(len(_raw(self.home)), 1)
        self.assertEqual(len(self.turns()), 1)

    def test_two_answers_to_one_id_one_wins(self):
        import threading
        self.ask()
        results = []

        def go(option):
            try:
                decisions.answer(1, option=option, user="admin", root=self.root)
                results.append(option)
            except decisions.DecisionError:
                results.append("refused")
        threads = [threading.Thread(target=go, args=(o,)) for o in ("a", "b")]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(sorted(results)[1], "refused")
        self.assertEqual(decisions.show(1, root=self.root)["answer_option"], sorted(results)[0])
        self.assertEqual((len(_raw(self.home)), len(self.turns())), (1, 1))

    def test_a_new_store_never_reuses_an_old_delivery_key(self):
        self.ask()
        decisions.answer(1, option="a", user="admin", root=self.root)
        decisions.db_path(self.root).unlink()          # restored or made anew: ids start again
        self.ask("Another?")
        self.assertEqual(decisions.answer(1, option="b", user="admin",
                                          root=self.root)["delivery"], "delivered")
        self.assertEqual([t["body"].split(" -> ")[1] for t in self.turns()],
                         ["a: keep it", "b: drop it"])

    def test_a_busy_store_after_the_commit_is_no_error(self):
        import sqlite3
        self.ask()
        with mock.patch.object(decisions, "_units",
                               side_effect=sqlite3.OperationalError("database is locked")):
            row = decisions.answer(1, option="a", user="admin", root=self.root)
        self.assertEqual((row["status"], row["delivery"]), ("answered", "pending"))
        decisions.redeliver(root=self.root)
        self.assertEqual(len(self.turns()), 1)

    def test_a_decision_turn_ranks_with_operator_chat_and_never_folds(self):
        from cousin_lib.runner import base
        self.assertEqual(base.priority("decision", "operator:Priya"), 1)
        self.assertEqual(base.priority("decision", "system"), 1)
        self.assertFalse(base.folds_into_turn("decision", "operator:Priya"))


class TestGroups(Case):
    def test_a_group_waits_for_the_grace_after_its_newest_question(self):
        from datetime import timedelta
        self.ask("First?", group="g")
        with mock.patch.object(decisions, "GROUP_GRACE_S", 60):
            self.assertEqual(decisions.answer(1, option="a", user="admin",
                                              root=self.root)["delivery"], "waiting")
            self.assertEqual(decisions.redeliver(root=self.root), [])
            self.assertEqual(self.turns(), [])
            later = decisions._clock() + timedelta(seconds=61)
            with mock.patch.object(decisions, "_clock", return_value=later):
                self.assertEqual(decisions.redeliver(root=self.root),
                                 [{"ids": [1], "ok": True}])
        self.assertEqual(len(self.turns()), 1)


    def test_a_group_comes_back_as_one_turn_once_settled(self):
        for q in ("First?", "Second?", "Third?"):
            self.ask(q, group="setup")
        testa = self.cousin("testa", operator="Priya")
        self.ask("Elsewhere?", group="setup", cousin="testa")
        row = decisions.answer(2, option="b", user="admin", root=self.root)
        self.assertEqual(row["delivery"], "waiting")
        self.assertEqual(self.turns(), [])
        self.assertEqual(len(_raw(self.home)), 1)        # memory per decision, at once
        decisions.answer(1, answer="my own", user="admin", root=self.root)
        self.assertEqual(self.turns(), [])
        decisions.withdraw(3, cousin="wren", root=self.root)
        [turn] = self.turns()
        self.assertEqual(turn["body"].split("\n"), [
            "Decisions answered (group setup):",
            "Decision #1 answered: First? -> my own",
            "Decision #2 answered: Second? -> b: drop it"])
        self.assertEqual(len(_raw(self.home)), 2)
        self.assertEqual(self.turns(testa), [])           # another cousin's group waits
        self.assertEqual(decisions.redeliver(root=self.root), [])

    def test_withdraw_is_the_asking_cousins_and_only_while_open(self):
        self.ask()
        with self.assertRaises(decisions.DecisionError):
            decisions.withdraw(1, cousin="testa", root=self.root)
        self.assertEqual(decisions.withdraw(1, cousin="wren", root=self.root)["status"],
                         "withdrawn")
        with self.assertRaises(decisions.DecisionError):
            decisions.withdraw(1, cousin="wren", root=self.root)
        with self.assertRaises(decisions.DecisionError):
            decisions.answer(1, option="a", user="admin", root=self.root)

    def test_open_summary_counts_per_cousin_and_never_creates_the_store(self):
        self.assertEqual(decisions.open_summary(self.root), {})
        self.assertFalse(decisions.db_path(self.root).exists())
        self.ask()
        self.ask("Two?")
        self.assertEqual(decisions.open_summary(self.root), {"wren": {"open": 2, "stops": []}})


class TestCli(Case):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = decisions.decision_main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_answer_is_refused_inside_a_cousins_environment(self):
        os.environ["COUSIN_HOME"] = str(self.home)
        rc, out, _ = self.run_cli("ask", "Which cache?", "--option", "a: keep it")
        self.assertEqual(rc, 0)
        self.assertIn("asked decision #1", out)
        rc, _, err = self.run_cli("answer", "1", "--option", "a")
        self.assertEqual(rc, 2)
        self.assertIn("the operator's act", err)
        self.assertEqual(decisions.show(1, root=self.root)["status"], "open")
        del os.environ["COUSIN_HOME"]
        rc, out, _ = self.run_cli("answer", "1", "--option", "a", "--user", "admin")
        self.assertEqual(rc, 0)
        self.assertIn("answered #1 -> a: keep it; delivered to wren", out)
        self.assertTrue(_raw(self.home)[0]["cite"].endswith(" in the CLI"))

    def test_the_cli_redeliver_retries_an_undeliverable_answer(self):
        self.cousin("wren", operator="Priya", runner=False)     # refused: no runner kind
        self.ask()
        self.assertEqual(decisions.answer(1, option="a", user="admin",
                                          root=self.root)["delivery"], "undeliverable")
        self.cousin("wren", operator="Priya")
        rc, out, _ = self.run_cli("redeliver")
        self.assertEqual((rc, out.strip()), (0, "delivered #1"))
        self.assertEqual(len(self.turns()), 1)

    def test_ask_and_withdraw_need_a_cousin(self):
        self.assertEqual(self.run_cli("ask", "q")[0], 2)
        self.ask()
        self.assertEqual(self.run_cli("withdraw", "1")[0], 2)
        rc, out, _ = self.run_cli("list", "--json")
        self.assertEqual([r["id"] for r in json.loads(out)["decisions"]], [1])
        self.assertEqual(self.run_cli("show", "5")[0], 1)
        rc, out, _ = self.run_cli("redeliver")
        self.assertEqual((rc, out.strip()), (0, "nothing to deliver"))


def _script_argv(cli_name):
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    module, func = scripts[cli_name].split(":")
    return [sys.executable, "-c", "import sys; from %s import %s; sys.exit(%s(sys.argv[1:]))"
            % (module, func, func)]


class TestBothLanes(Case):
    """The registry tool on the stdio lane (the CLI as a subprocess) and
    in process (HANDLERS) asks, lists and withdraws the same way."""

    def test_the_stdio_lane_and_the_in_process_lane_agree(self):
        reg = mcp_server.parse_registry(
            mcp_server.shipped_default_registry(ROOT).replace(
                'command = "cousin-decision"',
                "command = %s" % json.dumps(_script_argv("cousin-decision"))), "t")
        env = dict(os.environ, FRAMEWORK_ROOT=str(self.root), COUSIN_HOME=str(self.home),
                   PYTHONPATH=str(ROOT))
        args = {"command": "ask", "question": "-x? $(no) `shell`",
                "options": ["a: keep it", "drop it"], "recommendation": "a",
                "why": "w", "group": "g"}
        stdio, is_error = mcp_server.call_tool(reg, "decision", args, env)
        self.assertFalse(is_error, stdio)
        ctx = tools.ToolContext(home=self.home, slug="wren", name="Wren", root=self.root,
                                turn=Turn(), policy=Policy.load(self.home), registry=reg)
        inproc, is_error = tools.call(ctx, "decision", args)
        self.assertFalse(is_error, inproc)
        self.assertEqual(stdio.strip(), inproc.replace("#2", "#1").strip())
        one, two = decisions.list_decisions(root=self.root)
        for key in ("cousin", "question", "options", "recommendation", "why", "grp"):
            self.assertEqual(one[key], two[key], key)
        self.assertEqual(one["question"], "-x? $(no) `shell`")
        listed, _ = mcp_server.call_tool(reg, "decision", {"command": "list"}, env)
        self.assertEqual(listed, tools.call(ctx, "decision", {"command": "list"})[0] + "\n")
        text, is_error = mcp_server.call_tool(reg, "decision",
                                              {"command": "withdraw", "id": 1}, env)
        self.assertFalse(is_error, text)
        text, is_error = tools.call(ctx, "decision", {"command": "withdraw", "id": 2})
        self.assertFalse(is_error, text)
        self.assertEqual([r["status"] for r in decisions.list_decisions(root=self.root)],
                         ["withdrawn", "withdrawn"])

    def test_the_tool_has_no_answer_command(self):
        reg = mcp_server.parse_registry(mcp_server.shipped_default_registry(ROOT), "t")
        self.assertEqual(sorted(reg["tools"]["decision"]["commands"]),
                         ["ask", "list", "withdraw"])
        self.assertEqual(sorted(tools.HANDLERS["decision"]), ["ask", "list", "withdraw"])


class TestSkillStops(Case):
    def test_a_skill_stop_is_a_run_of_its_own_and_comes_back_as_one_turn(self):
        from datetime import timedelta
        a = self.ask("Which board?", skill="board-check", stop="review")
        b = self.ask("Which revision?", skill="board-check", stop="review",
                     group="board-check:review")
        other = self.ask("Publish?", skill="board-check", stop="publish")
        run = a["grp"]
        self.assertTrue(run.startswith("board-check:review@"), run)
        self.assertEqual(b["grp"], run)                 # the run under way at that stop
        self.assertTrue(other["grp"].startswith("board-check:publish@"))
        self.assertEqual(decisions.open_summary(self.root)["wren"]["stops"],
                         ["board-check:review", "board-check:publish"])
        decisions.answer(1, option="a", user="admin", root=self.root)
        self.assertEqual(self.turns(), [])
        decisions.answer(2, option="b", user="admin", root=self.root)
        [turn] = self.turns()
        self.assertEqual(turn["body"].split("\n"), [
            "Decisions answered at skill board-check, stop review (group %s):" % run,
            "Decision #1 answered: Which board? -> a: keep it",
            "Decision #2 answered: Which revision? -> b: drop it"])
        # the skill reaches the stop again later: a new run, never the old group
        later = decisions._clock() + timedelta(minutes=5)
        with mock.patch.object(decisions, "_clock", return_value=later):
            again = self.ask("Which board now?", skill="board-check", stop="review")
        self.assertNotEqual(again["grp"], run)
        self.assertTrue(again["grp"].startswith("board-check:review@"))
        self.assertEqual(self.ask("Named?", skill="s", stop="t", group="mine")["grp"], "mine")


if __name__ == "__main__":
    unittest.main()
