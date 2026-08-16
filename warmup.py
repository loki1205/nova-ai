"""Pull the speech models down now, rather than during the first conversation.

Whisper downloads on first use. Left to happen naturally, that means the first
thing you ever say to Nova is met with two minutes of silence, which is
indistinguishable from it being broken.
"""

import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from bridge import load_config  # noqa: E402


def main():
    cfg = load_config(os.path.join(BASE, "config.json"))
    name = cfg["ears"].get("model", "small.en")

    print("  whisper: %s" % name)
    started = time.time()
    from faster_whisper import WhisperModel
    WhisperModel(name, device="cpu", compute_type="int8",
                 download_root=os.path.join(BASE, "models"))
    print("  ready in %.0fs" % (time.time() - started))

    print("  voice:   %s" % cfg["tts"].get("piper_voice"))
    try:
        from mouth import Mouth
        mouth = Mouth(cfg["tts"])
        state = mouth.ready(timeout=40)
        if state.get("error"):
            print("  voice failed to load: %s" % state["error"])
            return 1
        print("  loaded")
    except Exception as exc:
        print("  voice failed to load: %s" % exc)
        return 1

    # Silero ships inside faster-whisper, so this only proves it loads.
    from vad import StreamingVad
    StreamingVad()
    print("  vad:     ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
