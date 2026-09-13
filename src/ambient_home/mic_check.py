"""Reachy Mini microphone diagnostic and XMOS recovery tool."""

import time
import logging
import argparse
from dataclasses import dataclass
from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray


logger = logging.getLogger(__name__)

XMOS_RESTART_SETTLE_S = 5.0


@dataclass(frozen=True)
class MicProbe:
    """Result of a short microphone recording."""

    frames: int
    samples: int
    rms: float
    peak: float
    verdict: str


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


def read_firmware_version() -> str:
    """Return the XMOS audio firmware version, or why it could not be read."""
    from reachy_mini.media.audio_control_utils import ReSpeaker, init_respeaker_usb

    device: ReSpeaker | None = init_respeaker_usb()
    if device is None:
        return "Reachy Mini Audio USB device not found"
    try:
        return str(device.read("VERSION"))
    except ValueError as exc:
        return str(exc)
    finally:
        device.close()


def restart_audio_processor(settle_s: float = XMOS_RESTART_SETTLE_S) -> bool:
    """Reboot the XMOS audio processor and wait for it to come back."""
    from reachy_mini.media.audio_control_utils import ReSpeaker, init_respeaker_usb

    device: ReSpeaker | None = init_respeaker_usb()
    if device is None:
        logger.error("Reachy Mini Audio USB device not found; cannot restart the audio processor.")
        return False
    try:
        device.write("REBOOT", [1])
    finally:
        device.close()
    time.sleep(settle_s)
    return True


def probe_microphone(seconds: float) -> MicProbe:
    """Record briefly from the Reachy microphone and classify the signal."""
    from reachy_mini.media.audio_gstreamer import GStreamerAudio

    audio = GStreamerAudio()
    frames: list[NDArray[np.float32]] = []
    started_at = time.monotonic()
    audio.start_recording()
    try:
        while time.monotonic() - started_at < seconds:
            frame = audio.get_audio_sample()
            if frame is not None:
                frames.append(frame)
            time.sleep(0.01)
    finally:
        audio.stop_recording()

    rms, peak = measure_frames(frames)
    return MicProbe(
        frames=len(frames),
        samples=sum(frame.shape[0] for frame in frames),
        rms=rms,
        peak=peak,
        verdict=verdict(rms, peak),
    )


def ensure_microphone_audio(seconds: float = 1.5) -> str:
    """Probe the microphone and restart the audio processor when it is silent.

    Reachy Mini Lite streams all-zero microphone audio after some USB
    connections until the XMOS processor is rebooted, so recover once before
    the wake-word loop starts. Returns the final verdict.
    """
    probe = probe_microphone(seconds)
    if probe.verdict != "silent":
        logger.info("Microphone check: %s (peak=%.6f)", probe.verdict, probe.peak)
        return probe.verdict
    if probe.frames == 0:
        logger.error("Microphone delivered no frames; is the Reachy Mini Audio device connected?")
        return probe.verdict
    logger.warning("Microphone returned only zeros (%d frames); restarting the audio processor.", probe.frames)
    if not restart_audio_processor():
        return probe.verdict
    recheck = probe_microphone(seconds)
    if recheck.verdict == "silent":
        logger.error(
            "Microphone still silent after restarting the audio processor. "
            "Check the microphone FPC cable (Pollen troubleshooting guide)."
        )
    else:
        logger.info("Microphone recovered after audio processor restart: peak=%.6f", recheck.peak)
    return recheck.verdict


def main() -> int:
    """Run the microphone diagnostic."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--reboot-xmos", action="store_true")
    args = parser.parse_args()

    print(f"firmware: {read_firmware_version()}")
    if args.reboot_xmos:
        print("rebooting XMOS, waiting 5 s")
        if not restart_audio_processor():
            return 2

    probe = probe_microphone(args.seconds)
    print(
        f"frames={probe.frames} samples={probe.samples} "
        f"rms={probe.rms:.6f} peak={probe.peak:.6f} verdict={probe.verdict}"
    )
    if probe.verdict != "ok" and not args.reboot_xmos:
        print("Try: `uv run ambient-mic-check --reboot-xmos`")
    return 0 if probe.verdict == "ok" else 1
