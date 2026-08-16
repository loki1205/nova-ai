"""Interrupting must actually stop the reply, and must not lose the new question.

Two failure modes, and the second is the subtle one. Cutting the speech off is
easy; the trap is that the question you interrupted it to ask arrives while the
old turn still holds the turn lock, gets refused, and is silently dropped -- so
barge-in appears to work and then nothing happens.
"""

import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from bridge import load_config  # noqa: E402
import nova as app  # noqa: E402


class FakeMouth:
    def __init__(self):
        self.speaking = True
        self.shushed = False
        self.fed = []

    def is_speaking(self):
        return self.speaking

    def feed(self, fragment):
        self.fed.append(fragment)

    def flush(self):
        pass

    def shush(self):
        self.shushed = True
        self.speaking = False


class FakeLink:
    """A turn that takes a while and notices when it is cancelled."""

    def __init__(self):
        self.busy = False
        self.cancelled = False
        self.asked = []

    def ask(self, text, on_text=None, on_tool=None):
        self.asked.append(text)
        self.busy = True
        for _ in range(30):
            if self.cancelled:
                break
            time.sleep(0.05)
            if on_text:
                on_text("word ")
        self.busy = False
        return "".join(["word "] * 30)

    def cancel(self):
        self.cancelled = True

    def start(self):
        pass

    def stop(self):
        pass

    def alive(self):
        return True


def main():
    cfg = load_config(os.path.join(os.path.dirname(HERE), "config.json"))
    bot = app.Assistant(cfg, voice=False)
    bot.mouth = FakeMouth()
    bot.link = FakeLink()

    # A long reply is under way.
    threading.Thread(target=bot.ask, args=("first question",), daemon=True).start()
    time.sleep(0.4)
    print("  reply in progress, spoken so far: %d fragments" % len(bot.mouth.fed))

    # The user starts talking over it.
    bot._interrupt()
    print("  speech stopped:  %s" % bot.mouth.shushed)
    print("  turn cancelled:  %s" % bot.link.cancelled)

    # ...and a moment later, the transcription of what they said arrives.
    time.sleep(0.5)
    started = time.time()
    bot.ask("second question")
    took = time.time() - started

    accepted = "second question" in bot.link.asked
    print("  new question accepted: %s (after %.2fs)" % (accepted, took))

    ok = bot.mouth.shushed and bot.link.cancelled and accepted
    print("\n%s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
