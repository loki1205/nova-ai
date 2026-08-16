"""Two spoken turns in a row, with no microphone and no human.

Reported twice: "it works once and then goes deaf." Both of my earlier fixes
were reasoned from the code rather than reproduced, and both were wrong -- so
this reproduces it instead.

Piper synthesises the utterances, they are resampled to 16 kHz and pushed
through the real listening loop frame by frame, exactly as the microphone
callback would. Everything downstream is genuine: the same Silero endpointer,
the same Whisper decode, the same wake-name check. Only the sound card is
missing.

If the second utterance does not come through, the bug is here, not in your
headset.
"""

import os
import sys
import time
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from bridge import load_config  # noqa: E402
import ears as ears_module  # noqa: E402
from vad import FRAME, SAMPLE_RATE  # noqa: E402

SCRATCH = os.path.join(os.path.dirname(HERE), "state")


def synthesise(text, name):
    from piper import PiperVoice

    path = os.path.join(SCRATCH, name)
    voice = PiperVoice.load(os.path.join(
        os.path.dirname(HERE), "piper", "voices", "en_GB-alba-medium.onnx"))
    with wave.open(path, "wb") as handle:
        voice.synthesize_wav(text, handle)
    return path, voice.config.sample_rate


def load_16k(path):
    with wave.open(path, "rb") as handle:
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if rate != SAMPLE_RATE:
        # Linear resample is plenty: Whisper is not sensitive to the difference
        # and a proper filter would only add a dependency.
        length = int(len(samples) * SAMPLE_RATE / rate)
        samples = np.interp(np.linspace(0, len(samples) - 1, length),
                            np.arange(len(samples)), samples).astype(np.float32)
    return samples


def feed(ears, samples, label):
    """Push audio through the loop the way the sound card would."""
    for start in range(0, len(samples) - FRAME, FRAME):
        ears.frames.put(samples[start:start + FRAME].copy())
        time.sleep(0.002)
    # Trailing silence, so the endpointer decides the sentence has finished.
    for _ in range(int(1.6 * SAMPLE_RATE / FRAME)):
        ears.frames.put(np.zeros(FRAME, dtype=np.float32))
        time.sleep(0.002)


def main():
    os.makedirs(SCRATCH, exist_ok=True)
    cfg = load_config(os.path.join(os.path.dirname(HERE), "config.json"))

    # The wake name comes from config, never hardcoded -- this test failed
    # once purely because the assistant had been renamed and the fixtures still
    # said the old name, which reads as "it stopped listening" and is not.
    name = cfg["wake"]["name"]
    lines = [
        ("%s, what is on my screen?" % name, "utterance-1.wav"),
        ("%s, how many windows are open?" % name, "utterance-2.wav"),
        ("%s, close the window." % name, "utterance-3.wav"),
    ]
    print("synthesising %d utterances..." % len(lines))
    audio = []
    for text, name in lines:
        path, _ = synthesise(text, name)
        audio.append(load_16k(path))

    heard = []
    ignored = []

    def handler(text, meta):
        if text is None:
            ignored.append(meta.get("raw"))
        else:
            heard.append(text)

    ears = ears_module.Ears(cfg["ears"], cfg["wake"], on_utterance=handler)
    print("loading whisper (%s)..." % ears.load())

    import threading
    threading.Thread(target=ears._process, daemon=True).start()
    ears.listen(True)

    for index, samples in enumerate(audio, 1):
        print("\n  turn %d: feeding %.1fs of speech" % (index, len(samples) / SAMPLE_RATE))
        feed(ears, samples, index)
        deadline = time.time() + 25
        while len(heard) + len(ignored) < index and time.time() < deadline:
            time.sleep(0.2)
        got = (heard[-1] if len(heard) == index
               else "IGNORED (%r)" % (ignored[-1] if ignored else None))
        print("  turn %d -> %s" % (index, got))
        print("     frames in %d / consumed %d / errors %d"
              % (ears.frames_seen, ears.frames_consumed, ears.errors))

    print("\n  accepted %d of %d" % (len(heard), len(lines)))
    for text in heard:
        print("    %r" % text)
    if ignored:
        print("  ignored:")
        for text in ignored:
            print("    %r" % text)

    ok = len(heard) == len(lines) and ears.errors == 0
    print("\n%s" % ("PASS" if ok else "FAIL -- it stopped listening"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
