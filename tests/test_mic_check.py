"""Microphone diagnostic tests."""

import numpy as np

from ambient_home.mic_check import verdict, measure_frames


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
