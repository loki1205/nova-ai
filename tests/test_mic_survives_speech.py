"""Does the microphone still work after Nova has spoken?

This is the regression for the bug that took three attempts to find, and it is
worth stating exactly why it was so hard to see.

The speaker used to force PortAudio to re-enumerate its devices by calling
`sd._terminate()` followed by `sd._initialize()`. That is safe in a process that
owns nothing but the speaker -- which is what it was written for, a standalone
daemon. Nova puts the listener and the speaker in one process, so terminating
PortAudio destroyed the microphone stream too.

The failure was silent and perfectly timed: the first question is heard, because
nothing has spoken yet. Answering it opens the output stream for the first time,
the rescan runs, and the microphone stops delivering frames. Every question after
that goes nowhere. No exception, no error, no log line -- the callback simply
stops being called.

The test is deliberately blunt: count microphone frames, speak, count again.
"""

import os
import sys
import time

import numpy as np
import sounddevice as sd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from bridge import load_config  # noqa: E402
from vad import FRAME, SAMPLE_RATE  # noqa: E402


def main():
    cfg = load_config(os.path.join(os.path.dirname(HERE), "config.json"))

    frames = {"n": 0}

    def callback(indata, count, timing, status):
        frames["n"] += 1

    stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                            blocksize=FRAME, callback=callback)
    stream.start()

    time.sleep(1.0)
    before = frames["n"]
    print("  microphone before speaking: %d frames" % before)
    if before == 0:
        print("  no audio at all -- cannot run this test")
        return 1

    from mouth import Mouth
    mouth = Mouth(cfg["tts"])
    mouth.ready()
    mouth.feed("Testing whether the microphone survives this sentence. ")
    mouth.flush()

    while mouth.is_speaking():
        time.sleep(0.1)
    time.sleep(0.5)

    during = frames["n"]
    print("  after speaking:             %d frames" % during)

    time.sleep(1.5)
    after = frames["n"]
    print("  a second later:             %d frames" % after)

    still_live = after > during
    print("\n  microphone still delivering: %s" % still_live)
    print("  stream reports active:       %s" % stream.active)

    stream.stop()
    stream.close()

    print("\n%s" % ("PASS" if still_live else
                    "FAIL -- speaking killed the microphone"))
    return 0 if still_live else 1


if __name__ == "__main__":
    sys.exit(main())
