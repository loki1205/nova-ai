"""Wake-name matching, against real transcripts from a real session.

Every "should wake" line below was actually said and actually thrown away. They
are kept verbatim, misrecognitions and all, because a wake word invented at a
desk matches nothing that happens in a room with an accent in it.
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from bridge import load_config  # noqa: E402
import ears as ears_module  # noqa: E402

# Said to it, and rejected, in the session that prompted this test.
SHOULD_WAKE = [
    "Hi Nova!",
    "Hi Nova, you are not addressing my comments.",
    "Nova we are not building anything today. We are just planning",
    "Hi Nova, let's plan something.",
    "Nova, what is on my screen?",
    "okay Nova open notepad",
    # The name in the middle of the sentence -- the case that lost fifty
    # seconds of a genuine idea.
    "Okay, then I think this is working great Nova, let's build something.",
    "So I was thinking Nova that we could try something else.",
    # Whisper's near misses on a four-letter name.
    "Novo, what time is it?",
    "Nova's, can you open Slack?",
]

# Not addressed to it, and must stay private.
SHOULD_NOT = [
    "Can you pass me the salt?",
    "the deployment failed again this morning",
    "I told him the november release is slipping",
    "no I don't think that works",
    "yeah I kept saying that but nobody listened",
    # The two that woke it before the common-word guard existed. "Nova" is one
    # edit from "now" and two from "no", both of which appear constantly.
    "no I don't think that works",
    "now close the window please",
    "I will know more tomorrow",
]


def main():
    cfg = load_config(os.path.join(os.path.dirname(HERE), "config.json"))
    ears = ears_module.Ears(cfg["ears"], cfg["wake"], on_utterance=lambda t, m: None)
    name = cfg["wake"]["name"]
    print("wake name: %r\n" % name)

    failures = 0

    for line in SHOULD_WAKE:
        ears.wake_until = 0.0
        woke, stripped = ears.addressed(line)
        mark = "ok  " if woke else "FAIL"
        if not woke:
            failures += 1
        print("  %s wake   %-58s -> %r" % (mark, line[:58], stripped[:44]))

    print()
    for line in SHOULD_NOT:
        ears.wake_until = 0.0
        woke, _ = ears.addressed(line)
        mark = "ok  " if not woke else "FAIL"
        if woke:
            failures += 1
        print("  %s ignore %s" % (mark, line[:58]))

    # And the follow-up window: one wake, then no name needed for a while.
    ears.wake_until = 0.0
    ears.addressed("Nova, open notepad")
    follow, _ = ears.addressed("now close it")
    print("\n  %s follow-up without the name: %s"
          % ("ok  " if follow else "FAIL", follow))
    if not follow:
        failures += 1

    ears.wake_until = time.time() - 1
    expired, _ = ears.addressed("now close it")
    print("  %s ignored once the window expires: %s"
          % ("ok  " if not expired else "FAIL", not expired))
    if expired:
        failures += 1

    print("\n%s" % ("PASS" if failures == 0 else "FAIL -- %d case(s)" % failures))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
