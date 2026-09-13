"""Reachy Mini microphone diagnostic and XMOS recovery tool."""

import time
import argparse
from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray


def measure_frames(frames: Sequence[NDArray[np.float32]]) -> tuple[float, float]:
    """Measure RMS and peak of a sequence of stereo audio frames."""
    mono_frames: list[NDArray[np.float32]] = []
    for frame in frames:
        samples = np.asarray(frame, dtype=np.float32)
        if samples.ndim not in (1, 2):
            raise ValueError("audio frames must be one- or two-dimensional")
        if samples.ndim == 2:
            if samples.shape[0] < samples.shape[1]:
                samples = samples.T
            samples = np.mean(samples, axis=1, dtype=np.float32)
        mono_frames.append(samples)
    if not mono_frames:
        return 0.0, 0.0
    mono = np.concatenate(mono_frames)
    if mono.size == 0:
        return 0.0, 0.0
    rms = float(np.sqrt(np.mean(np.square(mono, dtype=np.float32))))
    peak = float(np.max(np.abs(mono)))
    return rms, peak


def verdict(rms: float, peak: float) -> str:
    """Classify measured microphone signal levels."""
    del rms
    if peak == 0.0:
        return "silent"
    if peak < 1e-3:
        return "floor-only"
    return "ok"


def main() -> int:
    """Run the microphone diagnostic."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--reboot-xmos", action="store_true")
    args = parser.parse_args()

    from reachy_mini.media.audio_control_utils import ReSpeaker, init_respeaker_usb

    rs: ReSpeaker | None = init_respeaker_usb()
    if rs is None:
        print("Reachy Mini Audio USB device not found")
        return 2
    try:
        try:
            print(f"firmware: {rs.read('VERSION')}")
        except ValueError as exc:
            print(str(exc))
        if args.reboot_xmos:
            rs.write("REBOOT", [1])
    finally:
        rs.close()

    if args.reboot_xmos:
        print("rebooting XMOS, waiting 5 s")
        time.sleep(5)

    from reachy_mini.media.audio_gstreamer import GStreamerAudio

    audio = GStreamerAudio()
    frames: list[NDArray[np.float32]] = []
    started_at = time.monotonic()
    audio.start_recording()
    try:
        while time.monotonic() - started_at < args.seconds:
            frame = audio.get_audio_sample()
            if frame is not None:
                frames.append(frame)
            time.sleep(0.01)
    finally:
        audio.stop_recording()

    rms, peak = measure_frames(frames)
    result = verdict(rms, peak)
    samples = sum(frame.shape[0] for frame in frames)
    print(f"frames={len(frames)} samples={samples} rms={rms:.6f} peak={peak:.6f} verdict={result}")
    if result != "ok" and not args.reboot_xmos:
        print("Try: stop reachy-mini-daemon, then `uv run ambient-mic-check --reboot-xmos`")
    return 0 if result == "ok" else 1
