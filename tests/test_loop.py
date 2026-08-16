"""The audio loop must never be the thread that waits for an answer.

This is the bug that made the assistant answer once and then go deaf: `_heard` is
called from the audio processing loop, and answering inline blocked that loop
for the length of the turn. No frames were consumed, the queue filled with stale
audio, and when the loop resumed it worked through the backlog instead of
listening to you.

Checked without a microphone by timing `_heard` against a deliberately slow
answer. If it returns while the answer is still running, the audio thread is
free; if it waits, the bug is back.
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from bridge import load_config  # noqa: E402
import nova as app  # noqa: E402

SLOW = 1.5


def main():
    cfg = load_config(os.path.join(os.path.dirname(HERE), "config.json"))
    bot = app.Assistant(cfg, voice=False)

    answered = []

    def slow_answer(text):
        time.sleep(SLOW)
        answered.append(text)

    bot.ask = slow_answer

    started = time.time()
    bot._heard("first question", {"seconds": 1.0, "decode_s": 0.2})
    handed_back = time.time() - started

    print("  _heard returned after %.3fs (the answer takes %.1fs)"
          % (handed_back, SLOW))
    non_blocking = handed_back < SLOW / 2
    print("  audio thread free during the turn: %s" % non_blocking)

    # And a second utterance arriving mid-answer must still reach the loop.
    started = time.time()
    bot._heard("second question", {"seconds": 1.0, "decode_s": 0.2})
    second_handed_back = time.time() - started
    print("  second utterance accepted in %.3fs" % second_handed_back)

    time.sleep(SLOW + 0.6)
    print("  answers completed: %d" % len(answered))

    ok = non_blocking and second_handed_back < SLOW / 2 and len(answered) == 2
    print("\n%s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
