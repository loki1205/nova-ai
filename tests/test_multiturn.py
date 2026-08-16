"""Five turns down one session, back to back, with no pauses to hide a race."""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from bridge import Claude, load_config  # noqa: E402


def main():
    cfg = load_config(os.path.join(os.path.dirname(HERE), "config.json"))["claude"]
    sessions = []
    link = Claude(cfg, on_event=lambda k, p: sessions.append(p.get("session_id"))
                  if k == "ready" else None)
    link.start()

    questions = ["Say: one.", "Say: two.", "Say: three.", "Say: four.",
                 "What number did I ask for first? One word."]
    replies = []
    for index, question in enumerate(questions, 1):
        started = time.time()
        try:
            reply = link.ask(question)
        except Exception as exc:
            print("  turn %d FAILED: %s" % (index, exc))
            print("  process alive: %s, exit code: %s"
                  % (link.alive(), link.proc.poll() if link.proc else "?"))
            print("  stderr: %s" % " | ".join(link._stderr[-5:]))
            replies.append(None)
            continue
        replies.append(reply.strip())
        print("  turn %d (%.1fs) alive=%s -> %r"
              % (index, time.time() - started, link.alive(), reply.strip()[:40]))

    print("\n  sessions seen: %d (%s)"
          % (len(set(s for s in sessions if s)),
             ", ".join(sorted(set(s[:8] for s in sessions if s)))))
    link.stop()

    ok = all(replies) and len(set(s for s in sessions if s)) == 1
    print("\n%s" % ("PASS" if ok else "FAIL -- the session did not survive"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
