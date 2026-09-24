"""A fake interactive Claude Code in a fake pane, for the tmux kind's tests
(phase 11 Task 4). It writes the transcript entries the real CLI writes,
in the shapes measured on 2.1.281 (phase 11 findings S1-S4): a typed
prompt's `user` entry (promptSource "typed", its own promptId) when it
takes the prompt, an assistant entry with a tool call, then
`system`/`turn_duration`. Escape during a turn writes the interrupt entry
and no `turn_duration`. `slow` holds the FIRST turn about 3 s;
`fail_first` ends the FIRST turn with an API-error entry. `/exit` ends
the CLI with no entry. `on_prompt(pane, first_line, body)` runs as a turn
starts and may return "limit" to end it with a usage-limit error (a test
plays the model's side there, e.g. the handoff tool). Nothing here
decides anything for the runner: it only plays the CLI."""
import json
import threading
import time
import uuid
from pathlib import Path

from cousin_lib.runner.tmux_pane import Outcome


class FakePane:
    def __init__(self, transcript, *, slow=False, fail_first=False, turn_s=0.05, slow_s=3.0,
                 attention=None, on_prompt=None):
        self.transcript = Path(transcript)
        self.slow, self.fail_first = slow, fail_first
        self.turn_s, self.slow_s = turn_s, slow_s
        self._attention = attention
        self.on_prompt = on_prompt
        self.exits = 0
        self._alive = False
        self._lock = threading.Lock()
        self._escape = threading.Event()
        self._busy = False
        self._turns = 0
        self.typed = []            # (first_line, body) per type_row
        self.keys = []
        self.started = []          # (argv, cwd, env_base)
        self.kills = 0

    # -- the Pane protocol ------------------------------------------------
    def alive(self):
        return self._alive

    def pid(self):
        return 4242 if self._alive else None

    def start(self, argv, *, cwd, env_base):
        self.started.append((list(argv), cwd, sorted(env_base)))   # names only, as the real pane
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        self.transcript.touch()
        self._alive = True

    def kill(self):
        self.kills += 1
        self._alive = False
        self._escape.set()

    def die(self):
        """The pane dies with its unit (KillMode=mixed): the CLI writes nothing more."""
        self._dead = True
        self._alive = False
        self._escape.set()

    def capture(self):
        return ""

    def box_text(self):
        return ""

    def queued(self):
        return False

    def attention(self):
        return self._attention

    def type_row(self, first_line, body):
        if not self._alive:
            return Outcome.FAILED
        if self._attention or self._busy:
            return Outcome.BLOCKED
        if first_line == "/exit" and not body:     # a slash command: no prompt entry
            self.exits += 1
            self._alive = False
            return Outcome.TYPED
        self.typed.append((first_line, body))
        with self._lock:
            self._busy = True
            self._turns += 1
            n = self._turns
        threading.Thread(target=self._play, args=(first_line, body, n), daemon=True).start()
        return Outcome.TYPED

    def key(self, name):
        self.keys.append(name)
        if name == "Escape" and self._busy:
            self._escape.set()

    def clear(self):
        self.key("C-u")

    # -- the CLI ------------------------------------------------------------
    def _write(self, obj):
        if getattr(self, "_dead", False):
            return
        try:
            with self._lock, self.transcript.open("a") as fh:
                fh.write(json.dumps(obj) + "\n")
        except OSError:
            pass            # the test's home is gone: the fake CLI outlived its test

    def _play(self, first_line, body, n):
        prompt_id = uuid.uuid4().hex
        self._escape.clear()
        text = first_line + ("\n\n<pasted_content id=\"f00d\">\n%s\n</pasted_content>" % body if body else "")
        self._write({"type": "user", "promptSource": "typed", "promptId": prompt_id,
                     "entrypoint": "cli", "message": {"role": "user", "content": text}})
        if self.on_prompt is not None and self.on_prompt(self, first_line, body) == "limit":
            self._write({"type": "assistant", "isApiErrorMessage": True, "message": {
                "role": "assistant", "content": [{"type": "text", "text":
                                                  "You've hit your usage limit - resets at 5pm"}]}})
            with self._lock:
                self._busy = False
            return
        self._write({"type": "assistant", "message": {"role": "assistant", "stop_reason": "tool_use",
                     "content": [{"type": "tool_use", "id": "toolu_%d" % n, "name": "Bash",
                                  "input": {"command": "true"}}],
                     "usage": {"input_tokens": 10, "output_tokens": 5}}})
        self._write({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_%d" % n, "content": "ok"}]}})
        hold = self.slow_s if (self.slow and n == 1) else self.turn_s
        if self._escape.wait(hold):
            self._write({"type": "user", "promptId": prompt_id,
                         "message": {"role": "user", "content": [
                             {"type": "text", "text": "[Request interrupted by user]"}]}})
        elif self.fail_first and n == 1:
            self._write({"type": "assistant", "isApiErrorMessage": True,
                         "message": {"role": "assistant", "content": [
                             {"type": "text", "text": "API Error: 500 scripted failure"}]}})
        else:
            self._write({"type": "assistant", "message": {"role": "assistant", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": "done"}],
                         "usage": {"input_tokens": 10, "output_tokens": 5}}})
            self._write({"type": "system", "subtype": "turn_duration", "durationMs": 50})
        with self._lock:
            self._busy = False
