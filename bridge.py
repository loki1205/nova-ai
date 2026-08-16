"""The link to Claude: one long-lived `claude` process, spoken to over stdin.

The obvious design is to shell out to `claude -p "..."` per utterance. It works
and it is wrong for a voice assistant: every turn pays process startup and MCP
handshake, and nothing remembers the last thing you said.

Instead this holds a single session open with `--input-format stream-json`,
writes each turn as one JSON line, and reads the reply back as it is generated.
Three properties follow, and all three are what separate an assistant from a
command-line wrapper:

* **It can speak before the answer is finished.** Replies arrive as
  `text_delta` events, so the first sentence can be spoken while the rest is
  still being written. Waiting for the turn to end costs 10-30 seconds of
  silence on anything agentic.
* **It thinks silently.** Reasoning arrives as `thinking_delta`, a different
  event, so it is trivially excluded from what gets said aloud.
* **It remembers.** One session, so "do that again in the other window" means
  something.

The reply also comes back *here*, rather than being scraped out of a transcript
file on disk -- which is how the previous version worked, and which failed
outright in sessions where Claude Code never wrote that file.
"""

import json
import os
import queue
import shutil
import subprocess
import threading
import time


class BridgeError(RuntimeError):
    pass


class Claude:
    """A conversation with Claude that stays open."""

    def __init__(self, cfg, on_event=None):
        self.cfg = cfg
        self.on_event = on_event or (lambda kind, payload: None)
        self.proc = None
        self.session_id = None
        self.lines = queue.Queue()
        self._reader = None
        self._stderr = []
        self.busy = False
        self._cancel = threading.Event()
        self._start_lock = threading.Lock()

    # -- process ------------------------------------------------------------

    def _command(self):
        exe = self.cfg.get("claude_path") or shutil.which("claude")
        if not exe:
            raise BridgeError(
                "the `claude` CLI is not on PATH. Install Claude Code, or set "
                "claude_path in config.json.")

        argv = [
            exe, "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--include-partial-messages",
            # Not optional: the CLI refuses stream-json output without it.
            "--verbose",
        ]
        if self.cfg.get("model"):
            argv += ["--model", self.cfg["model"]]
        if self.cfg.get("permission_mode"):
            argv += ["--permission-mode", self.cfg["permission_mode"]]
        # Grant specific tools rather than turning permissions off wholesale.
        # A spoken assistant cannot answer a permission prompt -- there is
        # nobody watching a terminal -- so without a grant it stops mid-task and
        # asks a question nobody hears. Naming the tools keeps the blast radius
        # to what was actually intended: the desktop tools have their own
        # confirmation gate for anything irreversible, and everything else
        # (Bash, Write, Edit) still needs a human.
        allowed = self.cfg.get("allowed_tools") or []
        if allowed:
            argv += ["--allowedTools"] + list(allowed)
        for path in self.cfg.get("mcp_config") or []:
            if os.path.exists(path):
                argv += ["--mcp-config", path]
        prompt = self.cfg.get("system_prompt")
        if prompt:
            argv += ["--append-system-prompt", prompt]
        argv += self.cfg.get("extra_args") or []
        return argv

    def start(self):
        # Locked, because two threads race for it. The session is warmed in the
        # background at startup while the main thread is already free to take a
        # question, and `ask` starts the process itself if it is not up yet.
        # Both firing at once spawns two `claude` processes; the second one
        # replaces `self.proc`, and the first then reports EOF on its stdout,
        # which surfaces as "the claude process exited mid-turn" on a session
        # that is in fact perfectly healthy.
        with self._start_lock:
            if self.proc and self.proc.poll() is None:
                return
            self._start_locked()

    def _start_locked(self):
        argv = self._command()
        cwd = self.cfg.get("working_directory") or os.path.expanduser("~")
        self.proc = subprocess.Popen(
            argv, cwd=cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        self.on_event("started", {"argv": argv, "cwd": cwd})

    def _pump(self):
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                self.lines.put(line)
        self.lines.put(None)  # the process ended

    def _pump_stderr(self):
        for line in self.proc.stderr:
            line = line.rstrip()
            if line:
                self._stderr.append(line)
                del self._stderr[:-40]

    def alive(self):
        return bool(self.proc) and self.proc.poll() is None

    def stop(self):
        if not self.proc:
            return
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None

    # -- talking ------------------------------------------------------------

    def cancel(self):
        """Abandon the reply in flight. Used when the user starts talking."""
        self._cancel.set()

    def ask(self, text, on_text=None, on_tool=None, timeout=300.0):
        """Send one turn and stream the reply.

        `on_text` receives prose fragments as they arrive, so speech can begin
        immediately. Returns the complete reply.
        """
        if not self.alive():
            self.start()
        self._cancel.clear()
        self.busy = True

        # Drain anything left from a previous turn -- a cancelled turn keeps
        # producing events for a moment after we stop caring about them, and
        # they would otherwise be attributed to this one.
        while True:
            try:
                self.lines.get_nowait()
            except queue.Empty:
                break

        message = {"type": "user",
                   "message": {"role": "user",
                               "content": [{"type": "text", "text": text}]}}
        try:
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self.busy = False
            raise BridgeError("the claude process is gone (%s). %s"
                              % (exc, "\n".join(self._stderr[-3:])))

        collected = []
        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                if self._cancel.is_set():
                    self.on_event("cancelled", {})
                    break
                try:
                    line = self.lines.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:
                    raise BridgeError("the claude process exited mid-turn. %s"
                                      % "\n".join(self._stderr[-5:]))
                done = self._handle(line, collected, on_text, on_tool)
                if done:
                    break
            else:
                self.on_event("timeout", {"seconds": timeout})
        finally:
            self.busy = False
        return "".join(collected)

    def _handle(self, line, collected, on_text, on_tool):
        """Interpret one event. Returns True when the turn is over."""
        try:
            event = json.loads(line)
        except ValueError:
            return False

        kind = event.get("type")

        if kind == "system" and event.get("subtype") == "init":
            self.session_id = event.get("session_id")
            self.on_event("ready", {
                "session_id": self.session_id,
                "model": event.get("model"),
                "mcp": [s["name"] for s in event.get("mcp_servers", [])
                        if s.get("status") == "connected"],
            })
            return False

        if kind == "stream_event":
            inner = event.get("event", {})
            if inner.get("type") == "content_block_delta":
                delta = inner.get("delta", {})
                # thinking_delta is deliberately ignored: it is reasoning, not
                # an answer, and reading it aloud is both long and confusing.
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        collected.append(text)
                        if on_text:
                            on_text(text)
            elif inner.get("type") == "content_block_start":
                block = inner.get("content_block", {})
                if block.get("type") == "tool_use" and on_tool:
                    on_tool(block.get("name", "a tool"))
            return False

        if kind == "result":
            self.on_event("result", {
                "duration_ms": event.get("duration_ms"),
                "cost_usd": event.get("total_cost_usd"),
                "error": event.get("is_error"),
            })
            return True

        return False


def load_config(path):
    """Read config.json, seeding it from config.example.json on first run.

    The two files are split because config.json is where personal settings
    live -- which microphone, which voice, which working directory -- and a
    tracked file cannot hold those without every machine's values leaking into
    the repo. So the example is tracked and config.json is not.

    Seeding here rather than only in setup.ps1 because the config is read on
    every start and the example is right there; making a fresh clone die with
    FileNotFoundError over a file it could have copied itself is a poor
    welcome.
    """
    if not os.path.exists(path):
        example = os.path.join(os.path.dirname(path) or ".",
                               "config.example.json")
        if not os.path.exists(example):
            raise BridgeError(
                "no config at %s, and no config.example.json beside it to "
                "copy from" % path)
        shutil.copyfile(example, path)
        print("created %s from config.example.json -- edit it to taste" % path)

    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)
