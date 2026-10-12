"""
Slow the agent's speech down without changing its pitch (streaming WSOLA time-stretch).

gemini-3.8-live ignores pace instructions — told "about 120 words per minute" it still spoke at
155-188 wpm — so the pace is set here instead. Chunks are stretched as they stream in;
flush() at the end of each turn releases the few ms WSOLA holds back, so no word is clipped.
"""

import numpy as np
from audiotsm import wsola
from audiotsm.io.array import ArrayReader, ArrayWriter


class Stretcher:
    def __init__(self, speed: float):
        self.speed = speed
        self.enabled = abs(speed - 1.0) > 1e-3
        self._tsm = wsola(channels=1, speed=speed) if self.enabled else None

    def feed(self, pcm: bytes) -> bytes:
        if not self.enabled or not pcm:
            return pcm
        reader = ArrayReader((np.frombuffer(pcm, np.int16).astype(np.float32) / 32768).reshape(1, -1))
        writer = ArrayWriter(channels=1)
        while not reader.empty:
            self._tsm.read_from(reader)
            while self._tsm.write_to(writer)[0] > 0:
                pass
        return self._to_pcm(writer)

    def flush(self) -> bytes:
        """End of a turn: emit what is buffered and start fresh for the next turn."""
        if not self.enabled:
            return b""
        writer = ArrayWriter(channels=1)
        while self._tsm.flush_to(writer)[0] > 0:
            pass
        self._tsm.clear()
        return self._to_pcm(writer)

    def reset(self):
        """Barge-in: drop buffered audio."""
        if self.enabled:
            self._tsm.clear()

    @staticmethod
    def _to_pcm(writer: ArrayWriter) -> bytes:
        data = writer.data
        if data.size == 0:
            return b""
        return (np.clip(data[0], -1.0, 1.0) * 32767).astype(np.int16).tobytes()
