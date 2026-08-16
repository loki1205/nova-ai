"""Nova: talk to your machine, and have it act on what you said.

    python nova.py            start listening
    python nova.py --text     type instead of talk (no microphone)
    python nova.py --say "…"  one-shot, then exit

The loop is four steps and one rule.

    hear it → decide it was for us → ask Claude → speak the answer as it arrives

The rule is that speech starts before the answer is finished. Everything else
here is arrangement around that: sentences are cut and spoken as they stream in,
tool use is narrated rather than waited on silently, and the microphone can cut
the reply off mid-word when you start talking.
"""

import argparse
import os
import sys
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from bridge import Claude, BridgeError, load_config  # noqa: E402

DIM, BOLD, GREEN, YELLOW, RESET = (
    "\033[2m", "\033[1m", "\033[32m", "\033[33m", "\033[0m")


class Assistant:
    def __init__(self, cfg, voice=True):
        self.cfg = cfg
        self.voice = voice
        self.mouth = None
        self.ears = None
        self.turns = 0
        self._announced = None
        self.link = Claude(cfg["claude"], on_event=self._on_bridge_event)
        self._turn_lock = threading.Lock()

    # -- wiring -------------------------------------------------------------

    def _on_bridge_event(self, kind, payload):
        if kind == "ready":
            # The CLI emits its init event on every turn, not once per process.
            # Printing it each time buries the conversation in banner lines and,
            # worse, makes a healthy session look like it is restarting -- so
            # only say something when the session actually changes.
            if payload.get("session_id") == self._announced:
                return
            self._announced = payload.get("session_id")
            mcp = ", ".join(payload.get("mcp") or []) or "none"
            print("%s  link  %s · %s · mcp: %s%s"
                  % (DIM, payload.get("model"), payload["session_id"][:8], mcp, RESET))
        elif kind == "cancelled":
            print("%s  (interrupted)%s" % (DIM, RESET))

    def start(self):
        # The name comes from config. Hardcoding it means renaming the
        # assistant leaves the old name greeting you at every launch.
        print("%s%s%s" % (BOLD, self.cfg["wake"].get("name") or "Assistant", RESET))

        if self.voice:
            from mouth import Mouth
            self.mouth = Mouth(self.cfg["tts"])
            state = self.mouth.ready()
            if state.get("error"):
                print("  voice   %sfailed (%s) -- falling back%s"
                      % (YELLOW, state["error"], RESET))
            else:
                print("  voice   %s" % state["voice"])

        # Warm the link before the first question rather than after it. Session
        # startup and the MCP handshake cost several seconds, and paying that
        # while someone is waiting for an answer is the difference between
        # "thinking" and "broken".
        threading.Thread(target=self._warm, daemon=True).start()

        if self.voice:
            from ears import Ears
            self.ears = Ears(
                self.cfg["ears"], self.cfg["wake"],
                on_utterance=self._heard,
                is_speaking=(lambda: False) if self.cfg["ears"].get("barge_in")
                else self.mouth.is_speaking)
            self.ears.on_speech_start = self._interrupt
            name = self.ears.load()
            print("  ears    %s · %s" % (name, self.ears.device_name))
            self.ears.run()
            self.ears.listen(True)

            wake = self.cfg["wake"]
            if wake.get("required", True):
                print("  wake    say %r first%s" % (wake.get("name"),
                      "; barge-in on" if self.cfg["ears"].get("barge_in") else ""))
            else:
                print("  wake    %snot required -- everything heard is sent%s"
                      % (YELLOW, RESET))
        print()

    def _warm(self):
        try:
            self.link.start()
        except BridgeError as exc:
            print("%s  link failed: %s%s" % (YELLOW, exc, RESET))

    # -- the loop -----------------------------------------------------------

    def _interrupt(self):
        """The user started talking. Stop whatever we were saying.

        Both halves matter. Stopping the speech is the obvious one; cancelling
        the turn is what stops the rest of an answer nobody wants from arriving
        two seconds later and being spoken anyway.
        """
        if self.mouth and self.mouth.is_speaking():
            self.mouth.shush()
            self.link.cancel()
        elif self.link.busy:
            # Still thinking rather than speaking -- interrupting is just as
            # valid here, and more likely: the pause before the first word is
            # exactly when someone realises they asked the wrong thing.
            self.link.cancel()

    def _heard(self, text, meta):
        if text is None:
            reason = meta.get("reason") or meta.get("error") or "?"
            # Show it even when we are ignoring it. Silently discarding
            # unaddressed speech is right for privacy and terrible for
            # diagnosis: a mute microphone and a mismatched wake name look
            # identical from the outside, and the first thing anyone needs to
            # know is whether it heard anything at all.
            print("%s  · heard %r -- %s%s"
                  % (DIM, (meta.get("raw") or "")[:60], reason, RESET))
            return
        print("%s>%s %s %s(%.1fs speech, %.1fs decode)%s"
              % (BOLD, RESET, text, DIM, meta.get("seconds", 0),
                 meta.get("decode_s", 0), RESET))
        # On its own thread, not this one. `_heard` is called from the audio
        # processing loop, so answering here blocks that loop for the whole
        # turn -- five to ten seconds during which no frames are consumed. The
        # queue fills with stale audio, and when the loop resumes it grinds
        # through the backlog instead of listening. The symptom is an assistant
        # that answers exactly once and then goes deaf.
        threading.Thread(target=self.ask, args=(text,), daemon=True).start()

    def ask(self, text):
        # One turn at a time -- two answers interleaved into one sentence
        # stream is unintelligible. But a *short wait* rather than an immediate
        # refusal, because with barge-in the common case is that the previous
        # turn was just cancelled and is a fraction of a second from releasing.
        # Refusing outright there throws away the question you interrupted it
        # to ask, which is the one you actually cared about.
        if not self._turn_lock.acquire(timeout=4.0):
            print("%s  (dropped %r -- still working on the last one)%s"
                  % (DIM, text[:40], RESET))
            return
        try:
            self.turns += 1
            started = time.time()
            printed = []

            def on_text(fragment):
                printed.append(fragment)
                sys.stdout.write(fragment)
                sys.stdout.flush()
                if self.mouth:
                    self.mouth.feed(fragment)

            def on_tool(name):
                pretty = name.replace("mcp__desktop__desktop_", "").replace("_", " ")
                print("%s  [%s]%s" % (DIM, pretty, RESET))

            sys.stdout.write("%s<%s " % (GREEN, RESET))
            try:
                self.link.ask(text, on_text=on_text, on_tool=on_tool)
            except BridgeError as exc:
                print("\n%s  link error: %s%s" % (YELLOW, exc, RESET))
                if self.mouth:
                    self.mouth.say_now("The link to Claude dropped.")
                return
            finally:
                if self.mouth:
                    self.mouth.flush()

            print("\n%s  %.1fs%s" % (DIM, time.time() - started, RESET))
            if self.ears:
                # Keep the follow-up window open while the answer is being
                # spoken, so a reply to it does not need the wake name again.
                self.ears.defer_wake()
        finally:
            self._turn_lock.release()

    def wait(self):
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n%sstopped after %d turn(s)%s" % (DIM, self.turns, RESET))
        finally:
            self.link.stop()


def mic_test(cfg, seconds=15.0):
    """Is the microphone actually delivering audio, and does the VAD see speech?

    Exists because "nothing happens when I talk" has four unrelated causes and
    no way to tell them apart from the main loop.
    """
    from ears import Ears

    ears = Ears(cfg["ears"], cfg["wake"], on_utterance=lambda t, m: None)
    ears.run()
    ears.listen(True)

    print("Microphone: %s" % ears.device_name)
    print("Talk for %.0f seconds. Bars are input level; SPEECH means the "
          "endpointer agrees.\n" % seconds)

    from vad import FRAME
    deadline = time.time() + seconds
    last = 0
    loudest = 0.0
    while time.time() < deadline:
        time.sleep(0.25)
        level = ears.peak
        ears.peak = 0.0
        bar = "#" * min(40, int(level * 120))
        state = "SPEECH" if ears.endpointer.state == ears.endpointer.SPEAKING else "      "
        sys.stdout.write("\r  %-6s %-40s %8.6f  frames %d   "
                         % (state, bar, level, ears.frames_seen))
        sys.stdout.flush()
        loudest = max(loudest, level)
        last = ears.frames_seen
    print()

    if last == 0:
        print("\n  No audio arrived at all -- the stream is delivering nothing.")
        return 1

    print("\n  %d frames arrived; loudest sample %.6f" % (last, loudest))
    if loudest == 0.0:
        # Exactly zero is the tell. A live microphone in a silent room still
        # returns a noise floor around 0.0005; hard zeros mean the device is
        # muted in Windows, or app access to the microphone is blocked in
        # Settings > Privacy > Microphone.
        print("  %sExactly zero -- that is not a quiet room, that is a muted "
              "or blocked device.%s" % (YELLOW, RESET))
        print("  Check Windows Settings > Privacy > Microphone, and that the "
              "device is not muted in Sound settings.")
        return 1
    if loudest < 0.02:
        print("  %sVery quiet. If you were talking, this is the wrong input "
              "device.%s" % (YELLOW, RESET))
        print("  `nova --devices` lists them; set ears.input_device to a "
              "name fragment.")
        return 1
    print("  %sLooks healthy.%s" % (GREEN, RESET))
    return 0


def _finish_speaking(assistant, timeout=60.0):
    """Block until the last sentence has actually been played."""
    if not assistant.mouth:
        return
    deadline = time.time() + timeout
    while assistant.mouth.is_speaking() and time.time() < deadline:
        time.sleep(0.15)
    # The queue empties a moment before the audio finishes draining.
    time.sleep(0.35)


def main():
    parser = argparse.ArgumentParser(prog="nova")
    parser.add_argument("--text", action="store_true",
                        help="type instead of talking; no microphone")
    parser.add_argument("--say", metavar="TEXT", help="one question, then exit")
    parser.add_argument("--mute", action="store_true", help="no spoken replies")
    parser.add_argument("--mic-test", action="store_true",
                        help="check the microphone is delivering audio")
    parser.add_argument("--devices", action="store_true",
                        help="list input devices")
    parser.add_argument("--config", default=os.path.join(BASE, "config.json"))
    args = parser.parse_args()

    cfg = load_config(args.config)

    if args.devices:
        import sounddevice as sd
        for index, device in enumerate(sd.query_devices()):
            if device["max_input_channels"] > 0:
                print("  %2d  %s" % (index, device["name"]))
        return 0

    if args.mic_test:
        return mic_test(cfg)

    if not cfg["claude"].get("working_directory"):
        cfg["claude"]["working_directory"] = os.path.expanduser("~")

    assistant = Assistant(cfg, voice=not (args.text or args.mute) or bool(args.say and not args.mute))

    if args.say:
        assistant.voice = not args.mute
        assistant.start()
        assistant.ask(args.say)
        _finish_speaking(assistant)
        assistant.link.stop()
        return 0

    if args.text:
        assistant.voice = not args.mute
        assistant.start()
        print("%stype a question, or ctrl-c to stop%s\n" % (DIM, RESET))
        try:
            while True:
                line = input("> ").strip()
                if line:
                    assistant.ask(line)
        except (KeyboardInterrupt, EOFError):
            print()
        finally:
            # Wait for the last sentence before tearing down. Exiting while
            # Piper is still writing to the output stream kills it mid-word and
            # surfaces as "Stream is stopped [PaErrorCode -9983]" -- which looks
            # like an audio-device fault and is really just us leaving early.
            _finish_speaking(assistant)
            assistant.link.stop()
        return 0

    assistant.start()
    assistant.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
