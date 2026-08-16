"""Listening: microphone in, finished sentences out.

Three jobs, in order. Decide when a sentence has ended (Silero VAD, frame by
frame, state carried forward). Turn the audio into text (Whisper, local).
Decide whether it was addressed to us at all (the wake name).

The last one is what makes an always-on microphone tolerable. Without it,
arming the mic means every word in the room is transcribed and sent -- including
the half of a phone call the assistant should never have heard.
"""

import os
import queue
import threading
import time
import traceback

import numpy as np
import sounddevice as sd

from vad import Endpointer, FRAME, SAMPLE_RATE

BASE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(BASE, "models")
STATE = os.path.join(BASE, "state")
LOG = os.path.join(STATE, "ears.log")


def log(message):
    """A record of the listening loop, because it runs where nobody is looking.

    Without this, every way the loop can stop -- a dead thread, a stalled
    device, a wake name that never matches -- presents identically as "it went
    deaf", and the only tool left is guesswork.
    """
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(LOG, "a", encoding="utf-8", errors="replace") as fh:
            fh.write("%s  %s\n" % (time.strftime("%H:%M:%S"), message))
    except OSError:
        pass


def resolve_input(preferred):
    """Pick a microphone by name fragment, falling back to the system default.

    A name rather than an index because indices are reassigned whenever a device
    is plugged in, so a saved index quietly starts meaning a different mic.
    """
    if not preferred:
        return None, "system default"
    wanted = str(preferred).lower()
    for index, device in enumerate(sd.query_devices()):
        if device["max_input_channels"] > 0 and wanted in device["name"].lower():
            return index, device["name"]
    return None, "system default (no match for %r)" % preferred


class Ears:
    def __init__(self, cfg, wake_cfg, on_utterance, is_speaking=lambda: False):
        self.cfg = cfg
        self.wake = wake_cfg
        self.on_utterance = on_utterance
        self.is_speaking = is_speaking

        self.endpointer = Endpointer(
            threshold=cfg.get("threshold", 0.5),
            silence_ms=cfg.get("silence_ms", 900),
            min_speech_ms=cfg.get("min_speech_ms", 400),
            preroll_ms=cfg.get("preroll_ms", 300),
            max_utterance_s=cfg.get("max_utterance_s", 60),
        )
        self.frames = queue.Queue()
        self.listening = threading.Event()
        self.model = None
        self.device, self.device_name = resolve_input(cfg.get("input_device"))
        self.wake_until = 0.0
        self.deaf_until = 0.0
        self.on_speech_start = lambda: None
        self.stream = None
        self.frames_seen = 0
        self.frames_consumed = 0
        self.errors = 0
        self.peak = 0.0

    def load(self):
        from faster_whisper import WhisperModel
        name = self.cfg.get("model", "small.en")
        self.model = WhisperModel(
            name, device="cpu", compute_type="int8", download_root=MODELS)
        return name

    # -- audio --------------------------------------------------------------

    def _callback(self, indata, frames_count, time_info, status):
        block = indata[:, 0]
        # Cheap liveness counters. "It cannot hear me" has several very
        # different causes -- dead stream, muted device, VAD threshold too high,
        # wake name not matching -- and they are indistinguishable without
        # knowing whether any audio is arriving at all.
        self.frames_seen += 1
        level = float(np.abs(block).max())
        if level > self.peak:
            self.peak = level
        self.frames.put(block.copy())

    def _process(self):
        """The listening loop. It must never be allowed to die.

        This is a daemon thread, so an exception raised anywhere inside it --
        transcription, the wake-name check, or a print of a character the
        console cannot encode -- terminates the thread silently. Nothing else
        consumes frames after that, so the microphone keeps recording into a
        queue nobody reads and the assistant is deaf until restarted. It answers
        once, perfectly, and then never again.

        Which is exactly the symptom that was reported twice, and neither of my
        earlier fixes addressed, because both assumed the loop was still running.
        """
        while True:
            try:
                self._process_one()
            except Exception:
                log("LOOP ERROR (recovered)\n%s" % traceback.format_exc())
                self.errors += 1
                try:
                    self.endpointer.reset()
                    self._drain()
                except Exception:
                    pass

    def _process_one(self):
        while True:
            frame = self.frames.get()
            self.frames_consumed += 1
            if not self.listening.is_set() or len(frame) != FRAME:
                continue

            # Half duplex. Piper's output is loud enough to reach the mic, and
            # without this the assistant hears itself, answers itself, and the
            # conversation never ends. The cooldown covers the tail of the
            # audio still in the speaker buffer after the daemon says it is done.
            if self.is_speaking():
                self.deaf_until = time.time() + self.cfg.get("tts_cooldown_ms", 500) / 1000.0
                self.endpointer.reset()
                continue
            if time.time() < self.deaf_until:
                continue

            was_idle = self.endpointer.state == self.endpointer.IDLE
            audio = self.endpointer.feed(frame)
            if was_idle and self.endpointer.state == self.endpointer.SPEAKING:
                self.on_speech_start()
            if audio is not None:
                self._transcribe(audio)
                # Everything captured while we were busy is stale: the tail of
                # your own sentence, the reply being spoken, the room. Feeding
                # it to the endpointer afterwards produces a backlog of
                # phantom utterances, which is what makes an assistant answer
                # once and then appear to die.
                self._drain()
                self.endpointer.reset()

    def _drain(self):
        while True:
            try:
                self.frames.get_nowait()
            except queue.Empty:
                return

    def _transcribe(self, audio):
        seconds = len(audio) / SAMPLE_RATE
        started = time.time()
        try:
            segments, _ = self.model.transcribe(
                audio, language=self.cfg.get("language") or None, beam_size=1,
                vad_filter=True, condition_on_previous_text=False)
            text = " ".join(s.text.strip() for s in segments).strip()
        except Exception as exc:
            log("TRANSCRIBE FAILED: %s" % exc)
            self._deliver(None, {"error": str(exc)})
            return

        meta = {"seconds": seconds, "decode_s": time.time() - started, "raw": text}
        log("heard %.1fs -> %r" % (seconds, text))

        if not text or text.strip(".,!?- ") == "":
            self._deliver(None, dict(meta, reason="nothing intelligible"))
            return

        addressed, stripped = self.addressed(text)
        if not addressed:
            log("  ignored -- not addressed to %r" % self.wake.get("name"))
            self._deliver(None, dict(meta, reason="not addressed"))
            return
        log("  accepted -> %r" % stripped)
        self._deliver(stripped, meta)

    def _deliver(self, text, meta):
        """Hand it upward, and never let the caller's failure reach this loop.

        Whatever consumes an utterance is somebody else's code -- it prints,
        it spawns threads, it talks to a subprocess. A fault there is not a
        reason for the microphone to stop working.
        """
        try:
            self.on_utterance(text, meta)
        except Exception:
            log("HANDLER ERROR (contained)\n%s" % traceback.format_exc())
            self.errors += 1

    # -- wake name ----------------------------------------------------------

    FILLERS = {"hey", "ok", "okay", "hi", "hello", "yo", "um", "uh", "so", "hey,"}

    # Words too common to ever risk matching fuzzily.
    #
    # Fuzzy matching is what makes a wake name survive an accent, but it cuts
    # both ways on short names: "Nova" is a single edit from "now" and two from
    # "no", so "no I don't think that works" woke it, and so did "now close
    # it". A near miss on a rare word is a gift; a near miss on "now" means the
    # microphone is effectively always on.
    #
    # These still wake it on an *exact* match -- if you name it "Now", that is
    # your business -- but never on a near one.
    COMMON = {
        "no", "now", "know", "not", "note", "nope", "novel", "november",
        "new", "news", "never", "north", "nova" if False else "",
        "how", "who", "why", "what", "when", "where", "was", "were", "want",
        "one", "once", "only", "over", "our", "out", "own",
        "the", "this", "that", "these", "those", "then", "than", "them",
        "yes", "yeah", "you", "your", "here", "hear", "have", "has",
        "can", "could", "would", "should", "will", "with", "well",
        "just", "like", "look", "make", "made", "more", "most", "much",
        "some", "such", "same", "see", "say", "said", "does", "done",
    } - {""}

    def addressed(self, text):
        """(is it for us, text with the name removed)."""
        if not self.wake.get("required", True):
            return True, text
        name = (self.wake.get("name") or "").strip().lower()
        if not name:
            return True, text

        # Anywhere in the sentence, not just at the front.
        #
        # Requiring the name first is how a command manual says to talk to a
        # computer, not how anyone actually talks. Real examples from the log:
        # "Okay, then I think this is working great Jarvis, let's build
        # something" was thrown away, and so was fifty seconds of a genuine
        # product idea that happened to mention the name in the middle. Both
        # were plainly addressed to it; both were discarded.
        #
        # Scanning the whole utterance costs a little precision -- someone
        # saying the name while talking to another person will wake it. That is
        # much the cheaper mistake.
        words = text.strip().split()
        slack = 2 if len(name) >= 4 else 1
        for index, word in enumerate(words):
            candidate = word.lower().strip(",.!?'\";:")
            if not candidate:
                continue
            # Generous on purpose. Whisper rendered "Jarvis" as Jollis, JAWS and
            # JAVIS in a single session -- accents and sentence position both
            # move it -- and being ignored is the expensive failure, not being
            # woken by a near miss.
            exact = candidate == name
            near = (candidate not in self.COMMON or exact) and (
                _edit_distance(candidate, name, slack) <= slack
                or (len(name) >= 4 and candidate.startswith(name[:4])))
            if exact or near:
                self.wake_until = time.time() + float(self.wake.get("follow_up_s", 60))
                remainder = " ".join(words[:index] + words[index + 1:]).strip(" ,.!?")
                return True, remainder or text

        # Mid-conversation: no need to say the name again for a while.
        if time.time() < self.wake_until:
            return True, text
        return False, text

    def defer_wake(self, seconds=None):
        """Hold the follow-up window open, e.g. while a reply is being spoken."""
        self.wake_until = max(
            self.wake_until,
            time.time() + (seconds or float(self.wake.get("follow_up_s", 30))))

    # -- control ------------------------------------------------------------

    def _heartbeat(self):
        """Prove the loop is alive, in the log, once a minute.

        A stalled listener and an idle one are indistinguishable from outside.
        The counters separate them: frames_seen still climbing while
        frames_consumed is frozen means the audio is arriving and the loop that
        should be reading it has stopped.
        """
        while True:
            time.sleep(60)
            log("alive: %d frames in, %d consumed, %d error(s), listening=%s"
                % (self.frames_seen, self.frames_consumed, self.errors,
                   self.listening.is_set()))

    def run(self):
        threading.Thread(target=self._process, daemon=True).start()
        threading.Thread(target=self._heartbeat, daemon=True).start()
        # Held on the instance, not returned and forgotten. An InputStream that
        # nothing references is garbage collected, and PortAudio closes the
        # device on the way out -- so the callback simply stops being called and
        # the microphone goes quiet with no error anywhere. It looks exactly
        # like "it cannot hear me".
        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32",
            blocksize=FRAME, device=self.device, callback=self._callback)
        self.stream.start()
        return self.stream

    def listen(self, on=True):
        if on:
            self.endpointer.reset()
            self.listening.set()
        else:
            self.listening.clear()


def _edit_distance(a, b, cap=2):
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1,
                               previous[j - 1] + (ca != cb)))
        if min(current) > cap:
            return cap + 1
        previous = current
    return previous[-1]
