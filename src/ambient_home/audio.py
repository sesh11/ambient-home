"""Audio conversion and pre-roll buffering."""

import base64
from collections import deque

import numpy as np
from numpy.typing import NDArray
from reachy_mini_conversation_app.streaming import audio_to_int16


def frame_to_mono_int16(audio: NDArray[np.generic]) -> NDArray[np.int16]:
    """Convert a Reachy audio frame to mono signed 16-bit samples."""
    samples = np.asarray(audio)
    if samples.ndim not in (1, 2):
        raise ValueError("audio frames must be one- or two-dimensional")
    if samples.ndim == 2:
        if samples.shape[0] < samples.shape[1]:
            samples = samples.T
        samples = np.mean(samples, axis=1)
    return np.asarray(audio_to_int16(np.asarray(samples)), dtype=np.int16)


def pcm16_to_base64(samples: NDArray[np.int16]) -> str:
    """Encode little-endian PCM16 samples as base64."""
    little_endian = np.asarray(samples, dtype="<i2")
    return base64.b64encode(little_endian.tobytes()).decode("ascii")


class PrerollBuffer:
    """Keep the most recent mono audio for wake-word handoff."""

    def __init__(self, seconds: float, sample_rate: int) -> None:
        """Create a bounded sample ring buffer."""
        if seconds < 0 or sample_rate <= 0:
            raise ValueError("seconds must be non-negative and sample_rate must be positive")
        self._samples: deque[NDArray[np.int16]] = deque()
        self._capacity = int(seconds * sample_rate)
        self._size = 0

    def append(self, samples: NDArray[np.int16]) -> None:
        """Append samples, retaining only the configured duration."""
        chunk = np.asarray(samples, dtype=np.int16).reshape(-1).copy()
        if self._capacity == 0:
            return
        if chunk.size >= self._capacity:
            chunk = chunk[-self._capacity :]
            self._samples.clear()
            self._samples.append(chunk)
            self._size = int(chunk.size)
            return
        self._samples.append(chunk)
        self._size += int(chunk.size)
        while self._size > self._capacity and self._samples:
            oldest = self._samples.popleft()
            overflow = self._size - self._capacity
            if oldest.size > overflow:
                self._samples.appendleft(oldest[overflow:])
                self._size = self._capacity
            else:
                self._size -= int(oldest.size)

    def drain(self) -> NDArray[np.int16]:
        """Return buffered samples in order and clear the buffer."""
        if not self._samples:
            return np.empty(0, dtype=np.int16)
        drained = np.concatenate(tuple(self._samples)).astype(np.int16, copy=False)
        self._samples.clear()
        self._size = 0
        return drained
