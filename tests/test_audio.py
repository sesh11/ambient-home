"""Audio helper tests."""

import base64

import numpy as np
import pytest

from ambient_device_sdk import AudioFormat
from ambient_home.audio import (
    PrerollBuffer,
    PcmStreamConverter,
    to_bytes,
    pcm16_to_base64,
    frame_to_mono_int16,
)


def test_frame_to_mono_int16_channels_last() -> None:
    frame = np.column_stack(
        (
            np.full(1024, 0.5, dtype=np.float32),
            np.full(1024, -0.5, dtype=np.float32),
        )
    )
    result = frame_to_mono_int16(frame)
    assert result.shape == (1024,)
    np.testing.assert_array_equal(result, np.zeros(1024, dtype=np.int16))


def test_frame_to_mono_int16_transposes_channels_first() -> None:
    frame = np.full((2, 1024), 0.5, dtype=np.float32)
    result = frame_to_mono_int16(frame)
    assert result.shape == (1024,)
    np.testing.assert_array_equal(result, np.full(1024, 16383, dtype=np.int16))


def test_frame_to_mono_int16_passthrough() -> None:
    frame = np.array([1, -2, 3], dtype=np.int16)
    np.testing.assert_array_equal(frame_to_mono_int16(frame), frame)


def test_frame_to_mono_int16_rejects_unsupported_dtype() -> None:
    with pytest.raises(TypeError):
        frame_to_mono_int16(np.array([1, 2], dtype=np.int32))


def test_pcm16_base64_round_trip() -> None:
    samples = np.array([-1, 0, 32767], dtype=np.int16)
    assert np.frombuffer(base64.b64decode(pcm16_to_base64(samples)), dtype="<i2").tolist() == samples.tolist()


def test_preroll_keeps_latest_samples_and_drains() -> None:
    buffer = PrerollBuffer(seconds=1, sample_rate=4)
    buffer.append(np.array([1, 2, 3], dtype=np.int16))
    buffer.append(np.array([4, 5, 6], dtype=np.int16))
    np.testing.assert_array_equal(buffer.drain(), np.array([3, 4, 5, 6], dtype=np.int16))
    assert buffer.drain().size == 0


def test_pcm_converter_passthrough_and_channel_conversion() -> None:
    stereo = np.array([[100, 300], [-100, 100]], dtype=np.int16)
    passthrough = PcmStreamConverter(AudioFormat(16000, 2), 16000, 2)
    np.testing.assert_array_equal(passthrough.convert(to_bytes(stereo)), stereo)
    mono = PcmStreamConverter(AudioFormat(16000, 2), 16000).convert(to_bytes(stereo))
    np.testing.assert_array_equal(mono, np.array([200, 0], dtype=np.int16))


def test_pcm_converter_streaming_resample_handles_odd_byte_chunks() -> None:
    samples = np.arange(480, dtype=np.int16).reshape(-1, 2)
    payload = to_bytes(samples)
    expected = PcmStreamConverter(AudioFormat(48000, 2), 16000).convert(payload)
    converter = PcmStreamConverter(AudioFormat(48000, 2), 16000)
    chunks = [converter.convert(payload[index : index + 3]) for index in range(0, len(payload), 3)]
    actual = np.concatenate(chunks)
    np.testing.assert_allclose(actual, expected, atol=1)


def test_pcm_converter_upsamples_and_duplicates_mono() -> None:
    converter = PcmStreamConverter(AudioFormat(16000), 24000, 2)
    result = converter.convert(to_bytes(np.array([0, 1000, 2000, 3000], dtype=np.int16)))
    assert result.ndim == 2
    assert result.shape[1] == 2
    np.testing.assert_array_equal(result[:, 0], result[:, 1])
