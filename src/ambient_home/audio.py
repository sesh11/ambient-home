"""Audio conversion and pre-roll buffering."""

import base64
from collections import deque

import numpy as np
from numpy.typing import NDArray

from ambient_device_sdk import AudioFormat


def frame_to_mono_int16(audio: NDArray[np.generic]) -> NDArray[np.int16]:
    """Convert a Reachy audio frame to mono signed 16-bit samples."""
    samples: NDArray[np.generic] = np.asarray(audio)
    if samples.ndim not in (1, 2):
        raise ValueError("audio frames must be one- or two-dimensional")
    if samples.ndim == 2:
        if samples.shape[0] < samples.shape[1]:
            samples = samples.T
        if samples.dtype == np.int16:
            samples = np.mean(samples.astype(np.int32), axis=1).astype(np.int16)
        elif samples.dtype == np.float32:
            float_samples = np.asarray(samples, dtype=np.float32)
            samples = np.asarray(np.mean(float_samples, axis=1, dtype=np.float32), dtype=np.float32)
    if samples.dtype == np.int16:
        return np.asarray(samples, dtype=np.int16)
    if samples.dtype == np.float32:
        return (samples * 32767).astype(np.int16)
    raise TypeError(f"unsupported audio dtype: {samples.dtype}")


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

    @property
    def size(self) -> int:
        """Return the number of retained samples."""
        return self._size

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


class PcmStreamConverter:
    """Convert streaming PCM16 chunks while preserving frame and resampling state."""

    def __init__(self, source: AudioFormat, target_rate: int, target_channels: int = 1) -> None:
        """Set the source and target audio formats."""
        if target_rate <= 0:
            raise ValueError("target_rate must be positive")
        if target_channels < 1:
            raise ValueError("target_channels must be at least 1")
        self.source = source
        self.target_rate = target_rate
        self.target_channels = target_channels
        self._partial = b""
        self._buffer = np.empty((0, target_channels), dtype=np.int16)
        self._base_index = 0
        self._next_position = 0.0

    def reset(self) -> None:
        """Discard incomplete frames and interpolation history."""
        self._partial = b""
        self._buffer = np.empty((0, self.target_channels), dtype=np.int16)
        self._base_index = 0
        self._next_position = 0.0

    def convert(self, pcm16: bytes) -> NDArray[np.int16]:
        """Convert one chunk, retaining any partial source frame for the next call."""
        payload = self._partial + pcm16
        frame_bytes = self.source.bytes_per_frame
        complete_bytes = len(payload) - len(payload) % frame_bytes
        self._partial = payload[complete_bytes:]
        if complete_bytes == 0:
            return (
                np.empty((0,), dtype=np.int16)
                if self.target_channels == 1
                else np.empty((0, self.target_channels), dtype=np.int16)
            )
        samples = np.frombuffer(payload[:complete_bytes], dtype="<i2").reshape(-1, self.source.channels)
        samples = self._convert_channels(samples)
        if self.source.sample_rate == self.target_rate:
            converted = samples
        else:
            converted = self._resample(samples)
        if self.target_channels == 1:
            return converted[:, 0]
        return converted

    def _convert_channels(self, samples: NDArray[np.int16]) -> NDArray[np.int16]:
        if self.source.channels == self.target_channels:
            return samples
        if self.target_channels == 1:
            mono = np.mean(samples.astype(np.int32), axis=1).astype(np.int16)
            return np.asarray(mono.reshape(-1, 1), dtype=np.int16)
        if self.source.channels == 1:
            return np.repeat(samples, self.target_channels, axis=1)
        mono = np.mean(samples.astype(np.int32), axis=1).astype(np.int16)
        return np.repeat(mono.reshape(-1, 1), self.target_channels, axis=1)

    def _resample(self, samples: NDArray[np.int16]) -> NDArray[np.int16]:
        if samples.size:
            self._buffer = np.concatenate((self._buffer, samples), axis=0)
        ratio = self.source.sample_rate / self.target_rate
        end_index = self._base_index + len(self._buffer)
        output: list[NDArray[np.float64]] = []
        while int(self._next_position) + 1 < end_index:
            left_index = int(self._next_position)
            offset = self._next_position - left_index
            left = self._buffer[left_index - self._base_index].astype(np.float64)
            right = self._buffer[left_index + 1 - self._base_index].astype(np.float64)
            output.append(left + (right - left) * offset)
            self._next_position += ratio
        keep_from = min(end_index, max(self._base_index, int(self._next_position)))
        discard = keep_from - self._base_index
        if discard:
            self._buffer = self._buffer[discard:]
            self._base_index = keep_from
        if not output:
            return np.empty((0, self.target_channels), dtype=np.int16)
        return np.rint(np.stack(output)).astype(np.int16)


def to_bytes(samples: NDArray[np.int16]) -> bytes:
    """Serialize samples as interleaved little-endian PCM16."""
    return np.asarray(samples, dtype="<i2").tobytes()
