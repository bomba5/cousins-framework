"""The README quick start as CI: .github/scripts/quickstart.py and
.github/workflows/quickstart.yml.

The extractor's red cases are proven here (heading renamed, block split
or empty, a spawn line whose flags changed), the pty runner is driven
against a stand-in `docker` on PATH (adduser's two prompts typed, a
failing step named, a failing command inside a pipe fatal), and the
workflow is read as text for the pipefail rule. The job itself, with
Docker, runs only in GitHub Actions.
"""
import importlib.util
import io
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / ".github" / "scripts" / "quickstart.py"
_WORKFLOW = _REPO / ".github" / "workflows" / "quickstart.yml"
_README = _REPO / "README.md"


def _load():
    spec = importlib.util.spec_from_file_location("quickstart_ci", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


qs = _load()

_SPAWN_TAIL = ("    --operator ana --runner opencode --account zen \\\n"
               "    --model opencode/big-pickle --start\n")


def _readme(block, heading="## Quick start: Docker, no key, no Claude account",
            extra=""):
    return ("# The framework\n\nIntro.\n\n%s\n\n```\n%s```\n%s\nThen log in.\n\n"
            "## Quick start: bare host, Claude Code\n\n```\nnot this one\n```\n"
            % (heading, block, extra))


_BLOCK = ("git clone https://github.example.invalid/cousins-framework.git\n"
          "cd cousins-framework\n"
          "docker compose up -d --build\n"
          "docker compose exec framework cousin-console adduser ana\n"
          "docker compose exec -T framework cousin-spawn wren --name Wren \\\n"
          + _SPAWN_TAIL)


class TestExtract(unittest.TestCase):
    def test_the_readme_extracts_for_both_tiers(self):
        block = qs.quick_start_block(_README.read_text())
        for tier in (1, 2):
            with self.subTest(tier=tier):
                script = qs.script_for(_README.read_text(), tier)
                self.assertNotRegex(script, r"(?m)^git clone ")
                self.assertNotRegex(script, r"(?m)^cd ")
                self.assertIn("docker compose up -d --build", script)
                self.assertIn("cousin-console adduser ana", script)
                self.assertIn("cat >> config/accounts.toml", script)
                self.assertIn("cousin-spawn wren", script)
                proc = subprocess.run(["bash", "-n"], input=script, text=True,
                                      capture_output=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)
        # every README line but clone and cd runs as written; tier 1 changes
        # only the lane
        kept = [l for l in block.splitlines()
                if not l.startswith(("git clone ", "cd "))]
        tier2 = qs.script_for(_README.read_text(), 2)
        self.assertTrue(tier2.endswith("\n".join(kept) + "\n"))
        tier1 = qs.transform(block, 1)
        self.assertIn("--runner fake --start", tier1)
        self.assertNotIn("opencode/big-pickle", tier1)
        self.assertNotIn("--account zen\n", tier1)
        changed = set(tier1.splitlines()) ^ set(kept)
        self.assertEqual(len([l for l in changed if l in kept]), 2, changed)

    def test_the_lane_matches_on_one_line_too(self):
        one_line = _BLOCK.replace("--account zen \\\n    --model", "--account zen --model")
        out = qs.transform(qs.quick_start_block(_readme(one_line)), 1)
        self.assertIn("--operator ana --runner fake --start", out)

    def test_a_renamed_heading_is_red(self):
        with self.assertRaisesRegex(qs.StepError, "0 headings"):
            qs.quick_start_block(_readme(_BLOCK, heading="## Quick start with Docker"))

    def test_a_doubled_heading_is_red(self):
        text = _readme(_BLOCK) + "\n## Quick start: Docker, again\n\n```\nx\n```\n"
        with self.assertRaisesRegex(qs.StepError, "2 headings"):
            qs.quick_start_block(text)

    def test_a_split_block_is_red(self):
        with self.assertRaisesRegex(qs.StepError, "2 fenced blocks"):
            qs.quick_start_block(_readme(_BLOCK, extra="\n```\ndocker compose ps\n```\n"))

    def test_no_block_is_red(self):
        text = "## Quick start: Docker\n\nJust prose.\n\n## Next\n\n```\nx\n```\n"
        with self.assertRaisesRegex(qs.StepError, "0 fenced blocks"):
            qs.quick_start_block(text)

    def test_an_empty_block_is_red(self):
        with self.assertRaisesRegex(qs.StepError, "empty"):
            qs.quick_start_block(_readme("\n   \n"))

    def test_an_unclosed_fence_is_red(self):
        text = "## Quick start: Docker\n\n```\ndocker compose up -d --build\n"
        with self.assertRaisesRegex(qs.StepError, "unclosed"):
            qs.quick_start_block(text)

    def test_a_spawn_line_with_other_flags_is_red_on_tier_1(self):
        for old, new in (("opencode/big-pickle", "opencode/other-model"),
                         ("--account zen", "--account free"),
                         ("--runner opencode", "--runner sdk")):
            with self.subTest(new=new):
                block = qs.quick_start_block(_readme(_BLOCK.replace(old, new)))
                with self.assertRaisesRegex(qs.StepError,
                                            "tier 1's substitution .* matched 0 times"):
                    qs.transform(block, 1)
                qs.transform(block, 2)   # tier 2 runs the line as written

    def test_the_lane_twice_is_red(self):
        block = qs.quick_start_block(_readme(_BLOCK + "cousin-spawn kestrel \\\n" + _SPAWN_TAIL))
        with self.assertRaisesRegex(qs.StepError, "matched 2 times"):
            qs.transform(block, 1)

    def test_clone_and_cd_must_each_match_once(self):
        cases = {"the `git clone` line matched 0": _BLOCK.replace("git clone", "git fetch"),
                 "the `cd` line matched 0": _BLOCK.replace("cd cousins-framework\n", ""),
                 "the `cd` line matched 2": _BLOCK + "cd docker\n"}
        for message, block in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(qs.StepError, re.escape(message)):
                    qs.transform(qs.quick_start_block(_readme(block)), 2)

    def test_a_block_bash_cannot_parse_is_red_before_it_runs(self):
        broken = _BLOCK.replace("cousin-spawn wren --name Wren",
                                "cousin-spawn wren --name \"Wren")
        script = qs.script_for(_readme(broken), 1)
        with self.assertRaisesRegex(qs.StepError, "does not parse as bash"):
            qs.parses(script)
        qs.parses(qs.script_for(_README.read_text(), 1))

    def test_the_cli_says_why_and_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            readme = pathlib.Path(tmp) / "README.md"
            readme.write_text(_readme(_BLOCK, heading="## Docker"))
            proc = subprocess.run([sys.executable, str(_SCRIPT), "extract", "--tier", "1",
                                   "--readme", str(readme), "--out",
                                   str(pathlib.Path(tmp) / "qs.sh")],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("::error title=quick start::README.md has 0 headings", proc.stderr)
            self.assertFalse((pathlib.Path(tmp) / "qs.sh").exists())


# A stand-in `docker` for the extracted block: it logs every call, asks
# adduser's two password prompts on the pty the way getpass does, appends
# the accounts heredoc to a file, and fails the step named in STUB_FAIL.
_STUB = textwrap.dedent("""\
    #!/bin/sh
    printf '%%s\\n' "$*" >> "$STUB_DIR/calls"
    case "$*" in
        "compose up -d --build")
            [ "$STUB_FAIL" = up ] && { echo "build failed" >&2; exit 17; } ;;
        "compose exec framework cousin-console adduser ana")
            [ "$STUB_FAIL" = adduser ] && exit 2
            exec %(python)s -c 'import getpass, sys
    a = getpass.getpass("password for ana: ")
    b = getpass.getpass("again: ")
    open(sys.argv[1], "w").write(a + "|" + b)' "$STUB_DIR/password" ;;
        "compose exec -T framework sh -c cat >> config/accounts.toml")
            cat >> "$STUB_DIR/accounts.toml" ;;
        *cousin-spawn*)
            [ "$STUB_FAIL" = spawn ] && exit 3 ;;
    esac
    exit 0
    """)


class TestRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        stub = self.dir / "bin" / "docker"
        stub.parent.mkdir()
        stub.write_text(_STUB % {"python": sys.executable})
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        self.env = dict(os.environ, PATH="%s:%s" % (stub.parent, os.environ["PATH"]),
                        STUB_DIR=str(self.dir), STUB_FAIL="")

    def run_script(self, text, *, fail="", timeout=60):
        script = self.dir / "quickstart.sh"
        script.write_text(text)
        out = io.BytesIO()
        code = qs.run_under_pty(script, "pw-for-ana-123", timeout=timeout, out=out,
                                env=dict(self.env, STUB_FAIL=fail))
        return code, out.getvalue().decode("utf-8", "replace")

    def test_the_readme_block_runs_with_the_prompts_answered(self):
        code, out = self.run_script(qs.script_for(_README.read_text(), 1))
        self.assertEqual(code, 0, out)
        self.assertEqual((self.dir / "password").read_text(), "pw-for-ana-123|pw-for-ana-123")
        self.assertNotIn("pw-for-ana-123", out)
        self.assertIn("[accounts.zen]", (self.dir / "accounts.toml").read_text())
        calls = (self.dir / "calls").read_text().splitlines()
        self.assertEqual(calls[0], "compose up -d --build")
        self.assertIn("--runner fake --start", calls[-1])

    def test_a_failing_step_is_red_and_named(self):
        for fail, step in (("up", "compose build"), ("adduser", "adduser"),
                           ("spawn", "spawn")):
            with self.subTest(step=step):
                code, out = self.run_script(qs.script_for(_README.read_text(), 1), fail=fail)
                self.assertNotEqual(code, 0, out)
                self.assertIn("::error title=quick start::failed at %s" % step, out)

    def test_a_failing_command_inside_a_pipe_is_red(self):
        code, out = self.run_script("false | cat\necho reached\n")
        self.assertNotEqual(code, 0, out)
        self.assertNotIn("reached", out)

    def test_an_unset_variable_is_red(self):
        code, out = self.run_script("echo \"$QUICKSTART_NO_SUCH_VARIABLE\"\necho reached\n")
        self.assertNotEqual(code, 0, out)
        self.assertNotIn("reached", out)

    def test_a_script_past_its_timeout_is_124(self):
        code, _ = self.run_script("sleep 30\n", timeout=1)
        self.assertEqual(code, 124)


def _run_steps(text):
    """The workflow's `run:` steps, each as its list of lines, read as
    text (no YAML library in the suite)."""
    lines = text.splitlines()
    runs = []
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)(?:- )?run:(.*)$", line)
        if not m:
            continue
        indent, rest = len(m.group(1)), m.group(2).strip()
        if not rest:
            continue    # `defaults: run:`, a mapping, not a step
        body = []
        if rest in ("|", ">-", "|-", ">"):
            for nxt in lines[i + 1:]:
                if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                    break
                body.append(nxt.strip())
        else:
            body.append(rest)
        runs.append([l for l in body if l])
    return runs


class TestWorkflow(unittest.TestCase):
    def setUp(self):
        self.text = _WORKFLOW.read_text()
        self.runs = _run_steps(self.text)

    def test_ascii_and_no_home_path(self):
        self.text.encode("ascii")
        _SCRIPT.read_text().encode("ascii")
        self.assertNotIn("/home/", self.text)

    def test_tier_1_on_every_push_and_pull_request_tier_2_on_tags(self):
        self.assertIn('on:\n  push:\n    branches: ["**"]\n    tags: ["v*"]\n'
                      '  pull_request:\n', self.text)
        self.assertIn("startsWith(github.ref, 'refs/tags/v') && '[1, 2]' || '[1]'", self.text)

    def test_the_job_runs_bash_with_pipefail(self):
        self.assertEqual(self.text.count("\n  quickstart:\n"), 1)
        self.assertIn("    defaults:\n      run:\n        shell: bash\n", self.text)

    def test_every_run_step_starts_with_set_euo_pipefail(self):
        self.assertGreaterEqual(len(self.runs), 8)
        for body in self.runs:
            with self.subTest(step=body[:2]):
                self.assertEqual(body[0], "set -euo pipefail")

    def test_no_tolerated_failure_and_no_pipe_into_a_filter(self):
        for body in self.runs:
            for line in body:
                with self.subTest(line=line):
                    self.assertNotIn("|| true", line)
                    self.assertNotRegex(line, r"\|\s*(tail|tee|head|grep)\b")
        self.assertNotIn("continue-on-error", self.text)

    def test_each_step_s_set_line_alone_makes_a_pipe_fatal(self):
        # GitHub's default shell, `bash -e {0}`, has no pipefail: with only
        # the step's own first line, a failing command inside a pipe must
        # still stop the step.
        bare = subprocess.run(["bash", "-e", "-c", "false | cat; echo reached"],
                              capture_output=True, text=True)
        self.assertEqual((bare.returncode, bare.stdout.strip()), (0, "reached"))
        for body in self.runs:
            proc = subprocess.run(["bash", "-e", "-c", body[0] + "\nfalse | cat\necho reached"],
                                  capture_output=True, text=True)
            self.assertNotEqual(proc.returncode, 0)
            self.assertNotIn("reached", proc.stdout)

    def test_the_steps_run_the_helper_in_order(self):
        calls = [l for body in self.runs for l in body if '"$QS"' in l]
        names = [re.search(r'"\$QS" (\w+)', l).group(1) for l in calls]
        self.assertEqual(names, ["extract", "run", "healthy", "login", "converse",
                                 "remember", "healthy", "recall", "healthy", "recall"])
        flat = "\n".join(l for body in self.runs for l in body)
        self.assertIn("docker compose restart framework", flat)
        self.assertIn("docker compose down\ndocker compose up -d\n", flat)
        self.assertNotIn("down -v", flat)
        # the embeddings service is never waited on
        self.assertNotIn("embeddings", flat)


if __name__ == "__main__":
    unittest.main()
