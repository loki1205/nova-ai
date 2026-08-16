"""Speaking, sentence by sentence, while the answer is still being written.

The old build waited for a turn to finish before saying anything, which meant
ten to thirty seconds of silence on anything agentic -- long enough that you
assume it has crashed. Here the reply arrives as a stream of fragments, and this
cuts it at sentence boundaries and speaks each one as soon as it is complete.

Sentence boundaries, not fragments, because Piper synthesises per sentence and
handing it half a clause produces audible seams and wrong intonation. A comma is
not a boundary; a full stop followed by a space is.

The Speaker underneath is carried over unchanged from the previous build: it
keeps the voice model resident (loading it costs ~2.5s an utterance otherwise),
runs synthesis and playback on separate threads so there is no gap between
sentences, and rebinds the output stream when you switch headphones.
"""

import re
import threading

from clean import clean
from ttsd import Speaker

# A full stop, question or exclamation mark, then whitespace. Kept deliberately
# dumb: the alternative is a sentence tokeniser that stalls on "e.g." and
# "Dr.", and a wrong split costs an odd pause, while a missed one costs nothing
# at all -- the tail is flushed at the end of the turn regardless.
BOUNDARY = re.compile(r"([.!?])(\s+|$)")

# Long enough to be worth a breath. Below this, a "sentence" is usually "Yes."
# or a stray initial, and speaking it alone sounds clipped.
MIN_SPEAKABLE = 12


class Mouth:
    """Streaming speech. Feed it fragments; it decides when to say something."""

    def __init__(self, cfg):
        self.cfg = cfg
        # No PortAudio rescans: the microphone lives in this same process and
        # a rescan terminates the library out from under it.
        self.speaker = Speaker(allow_rescan=False)
        self.buffer = ""
        self.said = 0
        self.lock = threading.Lock()

    def ready(self, timeout=20.0):
        self.speaker.ready.wait(timeout)
        return {"voice": self.speaker.voice_name,
                "error": self.speaker.load_error}

    def is_speaking(self):
        return bool(self.speaker.speaking) or not self.speaker.jobs.empty()

    def feed(self, fragment):
        """Take a piece of the reply. Speaks whole sentences as they complete."""
        with self.lock:
            self.buffer += fragment
            while True:
                match = BOUNDARY.search(self.buffer)
                if not match:
                    break
                cut = match.end()
                sentence = self.buffer[:cut].strip()
                remainder = self.buffer[cut:]
                if len(sentence) < MIN_SPEAKABLE and remainder:
                    # Too short to speak alone -- let it join the next sentence
                    # rather than firing off "Right." as its own utterance.
                    following = BOUNDARY.search(remainder)
                    if following:
                        cut += following.end()
                        sentence = self.buffer[:cut].strip()
                    else:
                        break
                self.buffer = self.buffer[cut:]
                self._say(sentence)

    def flush(self):
        """Say whatever is left when the turn ends."""
        with self.lock:
            tail = self.buffer.strip()
            self.buffer = ""
        if tail:
            self._say(tail)

    def _say(self, text):
        spoken = clean(text, max_chars=int(self.cfg.get("max_chars", 700)),
                       speak_code_blocks=False)
        if not spoken.strip():
            return
        # interrupt=False: sentences queue in order instead of cutting each
        # other off as the reply continues to arrive.
        self.speaker.speak(spoken, interrupt=False)
        self.said += 1

    def say_now(self, text):
        """Speak immediately, cutting off anything in progress."""
        with self.lock:
            self.buffer = ""
        self.speaker.speak(clean(text, max_chars=300), interrupt=True)

    def shush(self):
        """Stop mid-sentence and drop what was queued. This is barge-in."""
        with self.lock:
            self.buffer = ""
        self.speaker.stop()
