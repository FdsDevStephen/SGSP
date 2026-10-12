"""
Lengthen the pauses between the agent's sentences, without slowing the words.

Measured on gemini-3.8-live output: gaps between words are <= 150 ms, gaps between sentences /
clauses 300-670 ms. Silent gaps of at least MIN_GAP_MS inside a turn are extended to
TARGET_MS, so every sentence is followed by a clear ~1 s-style pause. (The model ignores
"pause after each sentence" instructions, so it is done on the audio.)
"""

import numpy as np

FRAME_MS = 10
SILENCE_RMS = 300      # int16 RMS below this is silence (the TTS output is clean)
MIN_GAP_MS = 250       # shorter gaps are between words — left alone


class PauseExtender:
    def __init__(self, sample_rate: int, target_ms: int):
        self.enabled = target_ms > MIN_GAP_MS
        self.frame_bytes = sample_rate * FRAME_MS // 1000 * 2
        self.target_ms = target_ms
        self._rest = b""
        self._silent_ms = 0
        self._seen_speech = False  # don't stretch the silence before the first word of a turn

    def feed(self, pcm: bytes) -> bytes:
        if not self.enabled or not pcm:
            return pcm
        data = self._rest + pcm
        cut = len(data) - len(data) % self.frame_bytes
        self._rest = data[cut:]
        out = bytearray()
        for i in range(0, cut, self.frame_bytes):
            frame = data[i:i + self.frame_bytes]
            arr = np.frombuffer(frame, np.int16).astype(np.float32)
            if np.sqrt(np.mean(arr * arr)) < SILENCE_RMS:
                self._silent_ms += FRAME_MS
            else:
                if self._seen_speech and MIN_GAP_MS <= self._silent_ms < self.target_ms:
                    extra = (self.target_ms - self._silent_ms) // FRAME_MS
                    out += bytes(self.frame_bytes * extra)
                self._silent_ms = 0
                self._seen_speech = True
            out += frame
        return bytes(out)

    def flush(self) -> bytes:
        """End of a turn: release the partial frame and start fresh."""
        rest, self._rest = self._rest, b""
        self.reset()
        return rest

    def reset(self):
        self._silent_ms = 0
        self._seen_speech = False
