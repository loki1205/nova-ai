"""Persistent speech daemon.

Loading the Piper ONNX model costs ~2s. Doing that per utterance made every
spoken reply start two seconds late. This keeps the model resident and takes
work over a loopback socket, so an utterance starts in roughly the time it
takes to synthesize one sentence.

Two threads do the audio work: a producer runs Piper's per-sentence generator
into a bounded queue, a consumer plays from it. That overlap matters -- without
it you hear a gap between every sentence while the next one synthesizes.

Carried over from an earlier build where this ran as its own process and took
work over a socket. Nova runs it in-process, so the server is gone and the
Speaker is used directly -- see mouth.py, which wraps it for streaming speech.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time

# numpy and sounddevice are NOT imported here on purpose. They cost ~1.5s to
# import, and paying that up front delays the greeting on every launch. They
# load on a background thread while the rest of the assistant starts.
np = None
sd = None

BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE, "state")
MUTE = os.path.join(STATE, "muted")

# Release the audio device once the conversation has clearly stopped. Holding a
# Bluetooth stream open keeps the headset awake and drains it, but reopening one
# costs 300-1000ms, so this wants to be longer than a normal gap between turns.
# Correctness does not depend on it: a background watcher notices output changes
# within a couple of seconds regardless (see _watch_default_device).
IDLE_CLOSE_SECONDS = 60
PLAY_SLICE = 1024            # ~46ms at 22kHz -- also the interrupt granularity
QUEUE_DEPTH = 8


LOG = os.path.join(STATE, "ttsd.log")


def log(message):
    """Append a timestamped line. The daemon runs detached with no console, so
    without this there is no way to tell a silent failure from a silent success."""
    try:
        os.makedirs(STATE, exist_ok=True)
        stamp = time.strftime("%H:%M:%S")
        if os.path.exists(LOG) and os.path.getsize(LOG) > 512_000:
            os.replace(LOG, LOG + ".1")
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write("%s.%03d  %s\n" % (stamp, int(time.time() * 1000) % 1000, message))
    except OSError:
        pass


def load_config():
    # utf-8-sig, not utf-8: PowerShell writes config.json with a BOM.
    with open(os.path.join(BASE, "config.json"), encoding="utf-8-sig") as fh:
        return json.load(fh)


class Speaker:
    """Owns the voice model, the output stream, and the one thing being said."""

    def __init__(self, allow_rescan=True):
        # False whenever something else in this process owns an audio stream --
        # see _rescan_devices for why that matters.
        self.allow_rescan = allow_rescan
        self.cfg = load_config()
        self.voice = None
        self.voice_name = None
        self.load_error = None
        self.ready = threading.Event()
        self.stream = None
        self.sample_rate = 22050
        self.device_name = None

        self.jobs = queue.Queue()
        self.cancel = threading.Event()
        self.speaking = False
        self.last_spoke = time.time()
        self.sapi_child = None
        self.spoken_count = 0
        self.device_dirty = False
        self.default_id = None

        threading.Thread(target=self._load_voice, daemon=True).start()
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._watch_default_device, daemon=True).start()

    def _watch_default_device(self):
        """Notice when you switch speakers, without disturbing playback.

        Core Audio answers this in ~4ms; PortAudio cannot answer it at all once
        initialised. Marking the stream dirty is enough -- it gets rebuilt before
        the next utterance rather than mid-sentence.
        """
        try:
            from audiodev import com_init, default_render_id
        except Exception as exc:
            print("device watcher unavailable: %s" % exc, file=sys.stderr, flush=True)
            return
        com_init()
        while True:
            try:
                current = default_render_id()
                if current and self.default_id and current != self.default_id:
                    self.device_dirty = True
                if current:
                    self.default_id = current
            except Exception:
                pass
            time.sleep(2.0)

    # -- model --------------------------------------------------------------
    def _load_voice(self):
        global np, sd
        try:
            import numpy
            import sounddevice
            np, sd = numpy, sounddevice

            if self.cfg["tts"].get("engine") != "piper":
                self.ready.set()
                return
            from piper import PiperVoice

            name = self.cfg["tts"].get("piper_voice")
            model = os.path.join(BASE, "piper", "voices", name + ".onnx")
            if not os.path.exists(model):
                raise FileNotFoundError(model)
            self.voice = PiperVoice.load(model)
            self.voice_name = name
            self.sample_rate = self.voice.config.sample_rate
        except Exception as exc:
            # Don't die -- fall back to SAPI so the user still hears something.
            self.load_error = "%s: %s" % (type(exc).__name__, exc)
            self.voice = None
        finally:
            self.ready.set()

    def _syn_config(self):
        from piper.config import SynthesisConfig

        speed = float(self.cfg["tts"].get("piper_speed", 1.0)) or 1.0
        return SynthesisConfig(length_scale=1.0 / speed)

    # -- audio device -------------------------------------------------------
    def _rescan_devices(self):
        """Force PortAudio to re-enumerate, so newly connected outputs appear.

        `sd._terminate()` tears down the whole PortAudio library, not just this
        speaker's stream -- *every* stream in the process dies with it,
        including any microphone somebody else opened.

        That was harmless when this ran as its own daemon process. In Nova it
        shares a process with the listener, and the consequence was precise and
        baffling: the first question was heard, answering it opened the output
        stream for the first time, the rescan destroyed PortAudio, and the
        microphone never delivered another frame. Answers once, then deaf --
        with no error, because from the listener's side the callback simply
        stops being called.

        So it is opt-in now, and off wherever an input stream shares the
        process. The cost is that a *newly plugged in* output device is not
        noticed until restart; switching between devices already present when
        Nova started still works, because that goes through the Core Audio
        watcher in audiodev.py rather than through PortAudio.
        """
        if not self.allow_rescan:
            log("device rescan skipped -- shared process, it would kill the mic")
            return
        try:
            sd._terminate()
            sd._initialize()
        except Exception as exc:
            print("device rescan failed: %s" % exc, file=sys.stderr, flush=True)

    def _resolve_output(self):
        """Config can pin an output by name substring; otherwise system default."""
        wanted = self.cfg["tts"].get("output_device")
        if not wanted:
            return None, sd.query_devices(kind="output")["name"]
        try:
            for idx, dev in enumerate(sd.query_devices()):
                if dev["max_output_channels"] > 0 and wanted.lower() in dev["name"].lower():
                    return idx, dev["name"]
            print("output_device %r not found; using default" % wanted,
                  file=sys.stderr, flush=True)
        except Exception:
            pass
        return None, sd.query_devices(kind="output")["name"]

    def _ensure_stream(self):
        # `active` is not redundant with `is not None`. PortAudio stops a stream
        # by itself when the device disappears -- unplugging a headset, or a
        # Bluetooth link dropping -- and leaves the object in place. Writing to
        # it then fails with "Stream is stopped [PaErrorCode -9983]" and the
        # assistant goes mute until restarted. Seen in practice the first time a
        # headset was disconnected mid-session.
        fresh = (self.stream is not None
                 and self.stream.samplerate == self.sample_rate
                 and not self.device_dirty)
        if fresh:
            try:
                if not self.stream.active:
                    log("STREAM was stopped underneath us -- rebinding")
                    fresh = False
            except Exception:
                fresh = False
        if fresh:
            return self.stream
        if self.device_dirty:
            print("default output changed -- rebinding", flush=True)
        self.device_dirty = False
        self._close_stream()
        self._rescan_devices()
        device, name = self._resolve_output()
        self.device_name = name
        self.stream = sd.OutputStream(
            samplerate=self.sample_rate, channels=1, dtype="float32",
            latency="low", device=device,
        )
        self.stream.start()
        log("STREAM open -> %s (index %s, %d Hz)" % (name, device, self.sample_rate))
        return self.stream

    def _close_stream(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    # -- the speaking loop --------------------------------------------------
    def _worker(self):
        while True:
            try:
                text = self.jobs.get(timeout=IDLE_CLOSE_SECONDS)
            except queue.Empty:
                self._close_stream()          # give the device back when idle
                continue
            if text is None:
                return
            self.speaking = True
            self.cancel.clear()
            started = time.time()
            log("PLAY start  %r" % text[:70])
            try:
                self._say(text)
            except Exception as exc:
                # One retry on a fresh stream. Almost every failure here is the
                # output device having changed underneath us, and rebuilding
                # costs less than losing the sentence.
                log("PLAY FAILED %s: %s -- rebuilding the stream and retrying"
                    % (type(exc).__name__, exc))
                self._close_stream()
                self.device_dirty = True
                try:
                    self._say(text)
                except Exception as second:
                    log("PLAY FAILED AGAIN %s: %s" % (type(second).__name__, second))
                    print("say failed: %s" % second, file=sys.stderr, flush=True)
            finally:
                log("PLAY end    %.2fs%s"
                    % (time.time() - started, " (CANCELLED)" if self.cancel.is_set() else ""))
                self.speaking = False
                self.last_spoke = time.time()
                self.spoken_count += 1

    def _say(self, text):
        self.ready.wait()
        if self.voice is None:
            self._say_sapi(text)
        else:
            self._say_piper(text)

    def _say_piper(self, text):
        chunks = queue.Queue(maxsize=QUEUE_DEPTH)
        syn = self._syn_config()

        def produce():
            # Runs ahead of playback so sentence N+1 is ready when N finishes.
            try:
                for chunk in self.voice.synthesize(text, syn_config=syn):
                    if self.cancel.is_set():
                        break
                    chunks.put(chunk.audio_float_array)
            except Exception as exc:
                print("synth failed: %s" % exc, file=sys.stderr, flush=True)
            finally:
                chunks.put(None)

        threading.Thread(target=produce, daemon=True).start()
        stream = self._ensure_stream()

        while True:
            audio = chunks.get()
            if audio is None or self.cancel.is_set():
                break
            for start in range(0, len(audio), PLAY_SLICE):
                if self.cancel.is_set():
                    break
                stream.write(np.ascontiguousarray(audio[start:start + PLAY_SLICE]))

        if self.cancel.is_set():
            try:
                stream.abort()       # drop whatever the device still has buffered
                stream.start()
            except Exception:
                self._close_stream()
            # Drain so a cancelled utterance can't leak into the next one.
            while True:
                try:
                    if chunks.get_nowait() is None:
                        break
                except queue.Empty:
                    break

    def _say_sapi(self, text):
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".txt", text=True)
        os.close(fd)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        voice = str(self.cfg["tts"].get("sapi_voice", "")).replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "try { $s.SelectVoice('%s') } catch {}; "
            "$s.Rate = %d; "
            "$s.Speak([System.IO.File]::ReadAllText('%s', [System.Text.Encoding]::UTF8))"
        ) % (voice, int(self.cfg["tts"].get("sapi_rate", 0)), path.replace("'", "''"))
        try:
            self.sapi_child = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self.sapi_child.wait()
        finally:
            self.sapi_child = None
            try:
                os.remove(path)
            except OSError:
                pass

    # -- commands -----------------------------------------------------------
    def speak(self, text, interrupt=True):
        log("QUEUE       %d chars, interrupt=%s, speaking=%s"
            % (len(text or ""), interrupt, self.speaking))
        if interrupt:
            self.stop()
        self.jobs.put(text)

    def stop(self):
        log("STOP        (was speaking=%s, queued=%d)" % (self.speaking, self.jobs.qsize()))
        self.cancel.set()
        with self.jobs.mutex:
            self.jobs.queue.clear()
        child = self.sapi_child
        if child is not None:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(child.pid), "/T", "/F"],
                    check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except Exception:
                pass

    def reload(self):
        self.stop()
        self.cfg = load_config()
        self.ready.clear()
        self.voice = None
        self.load_error = None
        threading.Thread(target=self._load_voice, daemon=True).start()

    def status(self):
        engine = self.cfg["tts"].get("engine", "sapi")
        if engine == "piper" and self.voice is None and self.ready.is_set():
            engine = "sapi (piper failed to load)"
        return {
            "pid": os.getpid(),
            "engine": engine,
            "voice": self.voice_name or self.cfg["tts"].get("sapi_voice"),
            "model_ready": self.ready.is_set(),
            "load_error": self.load_error,
            "speaking": self.speaking,
            "queued": self.jobs.qsize(),
            "muted": os.path.exists(MUTE) or not self.cfg["tts"].get("enabled", True),
            "utterances": self.spoken_count,
            "sample_rate": self.sample_rate,
            "output_device": self.device_name,
            "stream_open": self.stream is not None,
        }
