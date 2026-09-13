"""Local openWakeWord detection."""

import time
import logging
from typing import Protocol
from pathlib import Path
from collections.abc import Callable

import numpy as np
from numpy.typing import NDArray


logger = logging.getLogger(__name__)


def resolve_model_path(model_name: str) -> str:
    """Resolve a configured model name to a bundled model when available."""
    model_path = Path(model_name).expanduser()
    if model_path.exists():
        return str(model_path)
    if "/" not in model_name and "\\" not in model_name:
        bundled_path = Path(__file__).resolve().parent / "models" / f"{model_name}.onnx"
        if bundled_path.exists():
            return str(bundled_path)
    return model_name


class WakeWordDetector(Protocol):
    """Protocol for streaming wake-word detectors."""

    def feed(self, mono_int16: NDArray[np.int16]) -> bool:
        """Feed mono 16 kHz samples and report one-shot detections."""

    def reset(self) -> None:
        """Reset detector state."""


class OpenWakeWordDetector:
    """Detect a configured openWakeWord model on 80 ms frames."""

    _SAMPLE_RATE = 16_000
    _CHUNK_SIZE = 1_280
    _SUPPRESSION_S = 2.0

    def __init__(
        self,
        model_name: str,
        threshold: float,
        *,
        model_factory: Callable[[str], object] | None = None,
    ) -> None:
        """Load an openWakeWord model lazily when first needed."""
        self.model_name = model_name
        self.threshold = threshold
        self._model_factory = model_factory
        self._model: object | None = None
        self._buffer = np.empty(0, dtype=np.int16)
        self._suppress_until = 0.0

    def _load_model(self) -> object:
        if self._model is not None:
            return self._model
        if self._model_factory is not None:
            self._model = self._model_factory(self.model_name)
            return self._model
        from openwakeword.model import Model
        from openwakeword.utils import download_models

        resolved_model = resolve_model_path(self.model_name)
        model_path = Path(resolved_model).expanduser()
        if (
            resolved_model == self.model_name
            and not model_path.exists()
            and "/" not in self.model_name
            and "\\" not in self.model_name
        ):
            logger.info("Downloading openWakeWord model %s", self.model_name)
            download_models([self.model_name])
        self._model = Model(wakeword_models=[resolved_model], inference_framework="onnx")
        return self._model

    def feed(self, mono_int16: NDArray[np.int16]) -> bool:
        """Feed samples and return true once for a threshold crossing."""
        self._buffer = np.concatenate((self._buffer, np.asarray(mono_int16, dtype=np.int16).reshape(-1)))
        model = self._load_model()
        detected = False
        while self._buffer.size >= self._CHUNK_SIZE:
            chunk = self._buffer[: self._CHUNK_SIZE]
            self._buffer = self._buffer[self._CHUNK_SIZE :]
            now = time.monotonic()
            if now < self._suppress_until:
                continue
            predictions = model.predict(chunk)  # type: ignore[attr-defined]
            score = max((float(value) for value in predictions.values()), default=0.0)
            if score >= self.threshold:
                detected = True
                self._suppress_until = now + self._SUPPRESSION_S
                self.reset()
                break
        return detected

    def reset(self) -> None:
        """Reset model prediction state and pending audio."""
        self._buffer = np.empty(0, dtype=np.int16)
        if self._model is not None:
            reset = getattr(self._model, "reset", None)
            if callable(reset):
                reset()
