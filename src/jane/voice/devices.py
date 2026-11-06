"""Bounded PCM capture and independently interruptible local synthesized speech."""

import asyncio
import importlib
import io
import math
import shutil
import struct
import threading
import wave
from typing import Any

SAMPLE_RATE = 16000
MAX_BYTES = SAMPLE_RATE * 2 * 15


def audio_module() -> Any:
    try:
        return importlib.import_module("sounddevice")
    except (ImportError, OSError):
        raise ValueError(
            "install jane-neural-engine[voice] and the PortAudio system library"
        ) from None


def devices() -> list[dict[str, object]]:
    try:
        return [
            {"id": i, "name": d["name"], "input_channels": d["max_input_channels"]}
            for i, d in enumerate(audio_module().query_devices())
            if d["max_input_channels"] > 0
        ]
    except Exception:
        raise ValueError(
            "audio device discovery failed; check PortAudio and device access"
        ) from None


class PCMBuffer:
    """Callback-safe memory cap. A dropped sample invalidates the whole utterance."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.data = bytearray()
        self.error: str | None = None

    def feed(self, data: bytes, device_error: bool = False) -> None:
        with self.lock:
            if self.error:
                return
            if device_error or len(self.data) + len(data) > MAX_BYTES:
                self.error = "device error or overflow; shorten recording and retry"
                self.data.clear()
                return
            self.data.extend(data)

    def clear(self) -> None:
        with self.lock:
            self.data.clear()

    def finish(self) -> tuple[bytes, dict[str, float]]:
        with self.lock:
            raw = bytes(self.data)
            self.data.clear()
            error = self.error
        if error:
            raise ValueError(error)
        if len(raw) < SAMPLE_RATE // 2 or len(raw) % 2:
            raise ValueError("recording too short or malformed")
        samples = [x[0] for x in struct.iter_unpack("<h", raw)]
        rms = math.sqrt(sum(x * x for x in samples) / len(samples)) / 32768
        clipped = sum(abs(x) >= 32760 for x in samples) / len(samples)
        if rms < 0.005 or clipped > 0.1:
            raise ValueError("silent or heavily clipped audio; please repeat")
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(raw)
        return output.getvalue(), {
            "duration_seconds": len(samples) / SAMPLE_RATE,
            "rms": rms,
            "clipped_fraction": clipped,
        }


class SoundDeviceMicrophone:
    def __init__(self, device: int | None = None) -> None:
        self.device = device
        self.stream: Any = None
        self.buffer = PCMBuffer()

    def start(self) -> None:
        self.buffer = PCMBuffer()

        def callback(data: Any, frames: int, timing: Any, status: Any) -> None:
            self.buffer.feed(bytes(data), bool(status))

        self.stream = audio_module().RawInputStream(
            device=self.device,
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=800,
            callback=callback,
        )
        self.stream.start()

    def finish(self) -> tuple[bytes, dict[str, float]]:
        if self.stream is None:
            raise ValueError("microphone is not recording")
        try:
            if not self.stream.active:
                raise ValueError("microphone disconnected")
            self.stream.stop()
            return self.buffer.finish()
        finally:
            self.abort()

    def abort(self) -> None:
        stream, self.stream = self.stream, None
        try:
            if stream is not None:
                try:
                    stream.abort()
                finally:
                    stream.close()
        finally:
            self.buffer.clear()


class EspeakSpeaker:
    """Direct child process, no shell. Exit latency is not acoustic-stop latency."""

    def __init__(self) -> None:
        binary = shutil.which("espeak-ng")
        if binary is None:
            raise ValueError("install the espeak-ng system package for speech output")
        self.binary = binary
        self.process: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()

    async def speak(self, text: str) -> None:
        if not text or len(text) > 8000:
            raise ValueError("speech text must contain 1 to 8000 characters")
        process = None
        try:
            # Shield spawn and recover its handle even if cancelled during process creation.
            async with self.lock:
                if self.process is not None:
                    raise ValueError("speech output is already active")
                spawn = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        self.binary,
                        "--stdin",
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                )
                try:
                    process = await asyncio.shield(spawn)
                except asyncio.CancelledError:
                    process = await spawn
                    self.process = process
                    raise
                self.process = process
            await asyncio.wait_for(process.communicate(text.encode()), 120)
            if process.returncode:
                raise ValueError("speech output failed")
        finally:
            async with self.lock:
                # A cancelled older task must never terminate a newer utterance.
                if process is not None:
                    await self._terminate(process)
                    if self.process is process:
                        self.process = None

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 0.5)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()

    async def stop(self) -> None:
        async with self.lock:
            if self.process is not None:
                await self._terminate(self.process)
            self.process = None
