"""Streaming voice-activity detection, for deciding when you've stopped talking.

faster-whisper bundles Silero VAD v6, but its wrapper zeroes the LSTM state on
every call -- correct for scoring a finished recording, useless for live
endpointing. This drives the same ONNX model frame by frame and carries the
state forward, which is what makes "has he stopped speaking yet" answerable.

No download: the model ships inside faster-whisper.
"""

import os

import numpy as np

SAMPLE_RATE = 16000
FRAME = 512          # 32ms at 16kHz -- the size Silero v6 expects
CONTEXT = 64


class StreamingVad:
    """Per-frame speech probability with carried state."""

    def __init__(self, threshold=0.5):
        import onnxruntime
        from faster_whisper.vad import get_assets_path

        path = os.path.join(get_assets_path(), "silero_vad_v6.onnx")
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self.session = onnxruntime.InferenceSession(
            path, providers=["CPUExecutionProvider"], sess_options=opts
        )
        self.threshold = threshold
        self.reset()

    def reset(self):
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT), dtype=np.float32)

    def probability(self, frame):
        """frame: exactly FRAME float32 samples. Returns P(speech)."""
        frame = np.asarray(frame, dtype=np.float32).reshape(1, FRAME)
        batch = np.concatenate([self._context, frame], axis=1)
        out, self._h, self._c = self.session.run(
            None, {"input": batch, "h": self._h, "c": self._c}
        )
        self._context = frame[:, -CONTEXT:]
        return float(np.asarray(out).ravel()[0])


class Endpointer:
    """Turns a stream of frames into complete utterances.

    Holds a rolling pre-roll so the first syllable isn't clipped -- speech is
    always detected slightly after it starts, and without the pre-roll you lose
    the leading consonant of the first word.
    """

    IDLE, SPEAKING = "idle", "speaking"

    def __init__(self, threshold=0.5, silence_ms=900, min_speech_ms=400,
                 preroll_ms=300, max_utterance_s=60, start_ms=150):
        self.vad = StreamingVad(threshold)
        self.threshold = threshold
        self.silence_frames = max(1, int(silence_ms / 1000 * SAMPLE_RATE / FRAME))
        self.min_speech_frames = max(1, int(min_speech_ms / 1000 * SAMPLE_RATE / FRAME))
        self.start_frames = max(1, int(start_ms / 1000 * SAMPLE_RATE / FRAME))
        self.preroll_frames = max(1, int(preroll_ms / 1000 * SAMPLE_RATE / FRAME))
        self.max_frames = int(max_utterance_s * SAMPLE_RATE / FRAME)

        self.state = self.IDLE
        self._preroll = []
        self._collected = []
        self._voiced = 0
        self._quiet = 0
        self._run = 0

    def reset(self):
        self.vad.reset()
        self.state = self.IDLE
        self._preroll = []
        self._collected = []
        self._voiced = self._quiet = self._run = 0

    def feed(self, frame):
        """Feed one FRAME-sized block. Returns finished audio, or None."""
        speech = self.vad.probability(frame) >= self.threshold

        if self.state == self.IDLE:
            self._preroll.append(frame)
            if len(self._preroll) > self.preroll_frames:
                self._preroll.pop(0)
            self._run = self._run + 1 if speech else 0
            if self._run >= self.start_frames:
                self.state = self.SPEAKING
                self._collected = list(self._preroll)
                self._preroll = []
                self._voiced = self._run
                self._quiet = 0
            return None

        self._collected.append(frame)
        if speech:
            self._voiced += 1
            self._quiet = 0
        else:
            self._quiet += 1

        done = self._quiet >= self.silence_frames
        too_long = len(self._collected) >= self.max_frames
        if not (done or too_long):
            return None

        audio = np.concatenate(self._collected) if self._collected else None
        enough = self._voiced >= self.min_speech_frames
        self.state = self.IDLE
        self._collected = []
        self._preroll = []
        self._voiced = self._quiet = self._run = 0
        return audio if enough else None
