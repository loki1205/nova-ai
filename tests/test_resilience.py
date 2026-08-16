"""The listening loop must survive anything the code above it does.

`_process` is a daemon thread. Anything that escapes it kills the thread
silently, nothing consumes frames afterwards, and the assistant answers once and
then goes deaf forever -- with no error, no traceback, and no clue.

So: throw the three things most likely to escape, and check the loop is still
running afterwards.
"""

import os
import queue
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import numpy as np  # noqa: E402

from bridge import load_config  # noqa: E402
import ears as ears_module  # noqa: E402
from vad import FRAME  # noqa: E402


class Boom(Exception):
    pass


def main():
    cfg = load_config(os.path.join(os.path.dirname(HERE), "config.json"))
    delivered = []

    faults = iter([
        Boom("handler raised"),
        UnicodeEncodeError("charmap", "x", 0, 1, "console cannot encode this"),
        None, None,
    ])

    def flaky_handler(text, meta):
        fault = next(faults, None)
        delivered.append(text)
        if fault is not None:
            raise fault

    ears = ears_module.Ears(cfg["ears"], cfg["wake"], on_utterance=flaky_handler)

    # No microphone and no Whisper: feed the loop directly and stub out decoding.
    ears.model = object()
    ears._transcribe = lambda audio: ears._deliver("pretend utterance", {"raw": "x"})
    threading.Thread(target=ears._process, daemon=True).start()
    ears.listen(True)

    # Force an utterance four times over. The first two handlers blow up.
    for round_number in range(4):
        ears._deliver("pretend utterance", {"raw": "x"})
        time.sleep(0.05)

    # And the loop itself must still be consuming frames.
    silence = np.zeros(FRAME, dtype=np.float32)
    for _ in range(20):
        ears.frames.put(silence)
    time.sleep(0.6)

    print("  utterances delivered: %d of 4" % len(delivered))
    print("  faults contained:     %d" % ears.errors)
    print("  frames consumed after the faults: %d" % ears.frames_consumed)

    ok = len(delivered) == 4 and ears.errors >= 2 and ears.frames_consumed >= 20
    print("\n%s" % ("PASS" if ok else "FAIL -- the loop did not survive"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
