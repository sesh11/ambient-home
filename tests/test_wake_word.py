"""Wake-word detector tests."""

import numpy as np

from ambient_home.wake_word import OpenWakeWordDetector


class FakeModel:
    def __init__(self) -> None:
        self.chunks: list[np.ndarray] = []
        self.reset_count = 0

    def predict(self, chunk: np.ndarray) -> dict[str, float]:
        self.chunks.append(chunk.copy())
        return {"hey_jarvis": 0.9}

    def reset(self) -> None:
        self.reset_count += 1


def test_detector_chunks_and_suppresses_retrigger(monkeypatch) -> None:
    model = FakeModel()
    detector = OpenWakeWordDetector("hey_jarvis", 0.5, model_factory=lambda _: model)
    assert detector.feed(np.zeros(1000, dtype=np.int16)) is False
    assert detector.feed(np.zeros(1280, dtype=np.int16)) is True
    assert len(model.chunks) == 1
    assert detector.feed(np.zeros(1280, dtype=np.int16)) is False
    assert len(model.chunks) == 1
