"""Microphone diagnostic tests."""

import numpy as np
import pytest

from ambient_home import mic_check
from ambient_home.mic_check import MicProbe, verdict, measure_frames


def test_measure_frames_downmixes_stereo() -> None:
    frame = np.column_stack(
        (
            np.full(1600, 0.5, dtype=np.float32),
            np.full(1600, 0.25, dtype=np.float32),
        )
    )

    rms, peak = measure_frames([frame])

    assert rms == np.float32(0.375)
    assert peak == np.float32(0.375)


def test_measure_frames_empty() -> None:
    assert measure_frames([]) == (0.0, 0.0)


def test_verdict() -> None:
    assert verdict(0.0, 0.0) == "silent"
    assert verdict(1e-4, 9e-4) == "floor-only"
    assert verdict(0.1, 0.1) == "ok"


def _probe(frames: int, peak: float) -> MicProbe:
    return MicProbe(frames=frames, samples=frames * 160, rms=peak / 2, peak=peak, verdict=verdict(0.0, peak))


def test_ensure_microphone_audio_skips_reboot_when_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mic_check, "probe_microphone", lambda seconds: _probe(100, 0.2))
    monkeypatch.setattr(mic_check, "restart_audio_processor", lambda: pytest.fail("must not reboot"))

    assert mic_check.ensure_microphone_audio() == "ok"


def test_ensure_microphone_audio_reboots_once_when_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    probes = iter([_probe(100, 0.0), _probe(100, 0.2)])
    restarts: list[bool] = []
    monkeypatch.setattr(mic_check, "probe_microphone", lambda seconds: next(probes))
    monkeypatch.setattr(mic_check, "restart_audio_processor", lambda: restarts.append(True) or True)

    assert mic_check.ensure_microphone_audio() == "ok"
    assert restarts == [True]


def test_ensure_microphone_audio_reports_persistent_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mic_check, "probe_microphone", lambda seconds: _probe(100, 0.0))
    monkeypatch.setattr(mic_check, "restart_audio_processor", lambda: True)

    assert mic_check.ensure_microphone_audio() == "silent"


def test_ensure_microphone_audio_no_frames_does_not_reboot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mic_check, "probe_microphone", lambda seconds: _probe(0, 0.0))
    monkeypatch.setattr(mic_check, "restart_audio_processor", lambda: pytest.fail("must not reboot"))

    assert mic_check.ensure_microphone_audio() == "silent"
