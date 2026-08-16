"""Does the link to Claude actually hold open across turns?

The whole design rests on one assumption -- that a `claude` process fed
stream-json on stdin stays alive and answers again -- so it is checked first and
directly, before anything is built on top of it.
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from bridge import Claude, load_config  # noqa: E402


def main():
    cfg = load_config(os.path.join(os.path.dirname(HERE), "config.json"))["claude"]
    events = []
    link = Claude(cfg, on_event=lambda kind, payload: events.append((kind, payload)))

    print("starting the session...")
    link.start()

    spoken = []
    started = time.time()
    first_token_at = [None]

    def on_text(fragment):
        if first_token_at[0] is None:
            first_token_at[0] = time.time() - started
        spoken.append(fragment)

    reply = link.ask("Reply with exactly: link established.", on_text=on_text)
    print("turn 1 -> %r" % reply.strip())
    print("  first token after %.1fs" % (first_token_at[0] or -1))

    ready = [p for k, p in events if k == "ready"]
    if ready:
        print("  session %s" % ready[0]["session_id"])
        print("  model   %s" % ready[0]["model"])
        print("  mcp     %s" % (", ".join(ready[0]["mcp"]) or "none connected"))

    # The point of the whole design: a second turn on the same process, with
    # memory of the first.
    started = time.time()
    reply2 = link.ask("What did I just ask you to say? Answer in three words.")
    print("turn 2 -> %r  (%.1fs)" % (reply2.strip(), time.time() - started))

    alive = link.alive()
    print("process still alive after two turns:", alive)

    link.stop()

    ok = bool(reply.strip()) and bool(reply2.strip()) and alive
    print("\n%s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
