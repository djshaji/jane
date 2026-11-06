"""One-shot Linux camera worker isolated from the authoritative journal process."""

import importlib
import subprocess
import sys
from typing import Protocol

from jane.storage.media import MAX_IMAGE_BYTES


class Camera(Protocol):
    def capture(self) -> bytes: ...


class OpenCVCamera:
    def __init__(self, device: int = 0) -> None:
        if type(device) is not int or not 0 <= device <= 64:
            raise ValueError("device must be an integer from 0 to 64")
        self.device = device

    def capture(self) -> bytes:
        try:
            result = subprocess.run(
                [sys.executable, "-m", "jane.vision.camera", str(self.device)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=8,
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            raise ValueError(
                "camera unavailable or timed out; check device and camera extra"
            ) from None
        if not 0 < len(result.stdout) <= MAX_IMAGE_BYTES:
            raise ValueError("empty or oversized camera frame")
        return result.stdout


def worker(device: int) -> None:
    cv2 = importlib.import_module("cv2")
    capture = cv2.VideoCapture(device)
    try:
        if not capture.isOpened():
            raise ValueError("camera unavailable")
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        ok, frame = capture.read()
        if not ok or frame is None:
            raise ValueError("no camera frame")
        height, width = frame.shape[:2]
        if width > 1280 or height > 720:
            scale = min(1280 / width, 720 / height)
            frame = cv2.resize(frame, (max(1, int(width * scale)), max(1, int(height * scale))))
        ok, image = cv2.imencode(".png", frame)
        if not ok or image.nbytes > MAX_IMAGE_BYTES:
            raise ValueError("camera encoding failed")
        sys.stdout.buffer.write(image.tobytes())
    finally:
        capture.release()


if __name__ == "__main__":
    worker(int(sys.argv[1]))
