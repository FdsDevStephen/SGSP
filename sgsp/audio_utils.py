"""
Audio utilities — WAV saving, mixing, and per-call logging setup.
"""

import logging
import sys
import wave
from pathlib import Path

import numpy as np


def save_wav(path: Path, pcm_bytes: bytes, rate: int):
    if not pcm_bytes:
        return
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm_bytes)


def mix_wav(path: Path, pcm_a: bytes, pcm_b: bytes, rate: int):
    """Mix two mono PCM16 buffers (same rate) into one WAV by summing with clipping."""
    if not pcm_a and not pcm_b:
        return
    a = np.frombuffer(pcm_a, dtype=np.int16).astype(np.int32) if pcm_a else np.array([], dtype=np.int32)
    b = np.frombuffer(pcm_b, dtype=np.int16).astype(np.int32) if pcm_b else np.array([], dtype=np.int32)
    max_len = max(len(a), len(b))
    a = np.pad(a, (0, max_len - len(a)))
    b = np.pad(b, (0, max_len - len(b)))
    mixed = np.clip(a + b, -32768, 32767).astype(np.int16)
    save_wav(path, mixed.tobytes(), rate)


def setup_call_logging(call_dir: Path, run_ts: str) -> logging.Logger:
    """Creates a per-call logger that writes to both file and stdout."""
    log_path = call_dir / "call.log"
    fmt = "%(asctime)s.%(msecs)03d  %(message)s"
    datefmt = "%H:%M:%S"
    fh = logging.FileHandler(log_path, encoding="utf-8")
    ch = logging.StreamHandler(sys.stdout)
    for h in (fh, ch):
        h.setFormatter(logging.Formatter(fmt, datefmt))
        h.setLevel(logging.DEBUG)
    # Named after the call directory (timestamp + phone) plus an id: two calls starting in the same
    # second used to share one logger, so each call's log contained the other call's lines.
    logger = logging.getLogger(f"sgsp_{call_dir.name}_{id(call_dir)}")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


def cleanup_call_logging(logger: logging.Logger):
    """Close and remove all handlers, then drop the logger from the manager."""
    for h in logger.handlers[:]:
        h.close()
        logger.removeHandler(h)
    logging.Logger.manager.loggerDict.pop(logger.name, None)
