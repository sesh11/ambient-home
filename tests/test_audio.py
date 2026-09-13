"""Audio helper tests."""

import base64

import numpy as np

from ambient_home.audio import PrerollBuffer, pcm16_to_base64, frame_to_mono_int16


def test_frame_to_mono_int16_channels_last() -> None:
    frame = np.array([[0.0, 1.0], [1.0, 0.0]], dtype=np.float32)
    np.testing.assert_array_equal(frame_to_mono_int16(frame), np.array([16383, 16383], dtype=np.int16))


def test_frame_to_mono_int16_transposes_channels_first() -> None:
    frame = np.array([[0.0, 1.0], [1.0, 0.0]], dtype=np.float32)
    np.testing.assert_array_equal(frame_to_mono_int16(frame), np.array([16383, 16383], dtype=np.int16))


def test_frame_to_mono_int16_passthrough() -> None:
    frame = np.array([1, -2, 3], dtype=np.int16)
    np.testing.assert_array_equal(frame_to_mono_int16(frame), frame)


def test_pcm16_base64_round_trip() -> None:
    samples = np.array([-1, 0, 32767], dtype=np.int16)
    assert np.frombuffer(base64.b64decode(pcm16_to_base64(samples)), dtype="<i2").tolist() == samples.tolist()


def test_preroll_keeps_latest_samples_and_drains() -> None:
    buffer = PrerollBuffer(seconds=1, sample_rate=4)
    buffer.append(np.array([1, 2, 3], dtype=np.int16))
    buffer.append(np.array([4, 5, 6], dtype=np.int16))
    np.testing.assert_array_equal(buffer.drain(), np.array([3, 4, 5, 6], dtype=np.int16))
    assert buffer.drain().size == 0
