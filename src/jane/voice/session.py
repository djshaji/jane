"""Hardware-independent, cancellable push-to-talk state machine."""

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from jane.core.clock import Clock


@dataclass(frozen=True)
class Utterance:
    capture_id: str
    started: datetime
    ended: datetime
    wav: bytes
    quality: dict[str, float]


class Microphone(Protocol):
    def start(self) -> None: ...
    def finish(self) -> tuple[bytes, dict[str, float]]: ...
    def abort(self) -> None: ...


class Transcriber(Protocol):
    async def transcribe(self, wav: bytes) -> str: ...


class Speaker(Protocol):
    async def speak(self, text: str) -> None: ...
    async def stop(self) -> None: ...


Think = Callable[[str, Utterance], Awaitable[str]]


class VoiceSession:
    def __init__(
        self,
        mic: Microphone,
        transcriber: Transcriber,
        speaker: Speaker,
        think: Think,
        cancel_thinking: Callable[[], None],
        clock: Clock,
        *,
        enabled: bool = False,
    ) -> None:
        self.mic, self.transcriber, self.speaker = mic, transcriber, speaker
        self.think, self.cancel_thinking, self.clock = think, cancel_thinking, clock
        self.enabled = enabled
        self.state = "idle"
        self.preview = ""
        self.partial = ""
        self.audit: deque[dict[str, object]] = deque(maxlen=64)
        self._generation = 0
        self._task: asyncio.Task[None] | None = None
        self._utterance: Utterance | None = None
        self._started = clock.now()

    def record(self) -> None:
        if not self.enabled:
            raise ValueError("microphone requires explicit enablement")
        if self.state != "idle":
            raise ValueError("recording requires idle state; stop first")
        self._generation += 1
        self._started = self.clock.now()
        try:
            self.mic.start()
        except Exception:
            self.mic.abort()
            self.audit.append({"status": "device_error"})
            raise ValueError("microphone unavailable; reconnect and retry") from None
        self.state = "recording"

    def update_partial(self, text: str, generation: int) -> None:
        # Presentation only. Never sent to think(), storage, or tools.
        if generation == self._generation and self.state == "transcribing":
            self.partial = text[:2000]

    def release(self) -> None:
        if self.state != "recording":
            raise ValueError("release requires an active recording")
        try:
            wav, quality = self.mic.finish()
        except Exception as exc:
            self.mic.abort()
            self.state = "idle"
            self.audit.append({"status": "capture_rejected"})
            raise ValueError("capture rejected: " + str(exc)) from None
        utterance = Utterance(uuid4().hex, self._started, self.clock.now(), wav, quality)
        self.audit.append({"capture_id": utterance.capture_id, "status": "captured", **quality})
        self.state = "transcribing"
        self._task = asyncio.create_task(self._transcribe(utterance, self._generation))

    async def _transcribe(self, utterance: Utterance, generation: int) -> None:
        try:
            text = (await self.transcriber.transcribe(utterance.wav)).strip()
            if generation != self._generation:
                return
            if not text or len(text) > 4000:
                raise ValueError("unintelligible transcript")
            # Drop the raw audio immediately after transcription, before user confirmation.
            self._utterance = Utterance(
                utterance.capture_id, utterance.started, utterance.ended, b"", utterance.quality
            )
            self.preview, self.partial, self.state = text, "", "preview"
        except asyncio.CancelledError:
            pass
        except Exception:
            if generation == self._generation:
                self.audit.append({"status": "transcription_failed"})
                self.state = "idle"
        finally:
            # In-flight coroutine locals can retain bytes until the adapter returns/cancels.
            del utterance

    def send(self) -> None:
        if self.state != "preview" or self._utterance is None:
            raise ValueError("send requires a final transcript preview")
        text, utterance = self.preview, self._utterance
        self.preview, self._utterance = "", None
        self.state = "thinking"
        self._task = asyncio.create_task(self._respond(text, utterance, self._generation))

    async def _respond(self, text: str, utterance: Utterance, generation: int) -> None:
        try:
            response = await self.think(text, utterance)
            if generation != self._generation:
                return
            if response:
                self.state = "speaking"
                await self.speaker.speak(response)
        except asyncio.CancelledError:
            pass
        except Exception:
            if generation == self._generation:
                self.audit.append({"status": "response_failed"})
        finally:
            if generation == self._generation:
                self.state = "idle"

    async def stop(self) -> dict[str, object]:
        requested = self.clock.monotonic()
        self._generation += 1
        self.state = "stopping"
        self.preview, self.partial, self._utterance = "", "", None
        if self._task:
            self._task.cancel()
        self.cancel_thinking()
        acknowledged = self.clock.monotonic()
        # No inference/transcription await on the playback stop path.
        try:
            await self.speaker.stop()
            stopped = self.clock.monotonic()
        finally:
            self.mic.abort()
        self.state = "idle"
        result: dict[str, object] = {
            "status": "stopped",
            "ack_seconds": acknowledged - requested,
            "playback_process_stop_seconds": stopped - requested,
        }
        self.audit.append(result)
        return result

    async def close(self) -> None:
        """Fence work, stop devices, and let cooperative cancellation settle before DB close."""
        try:
            await self.stop()
        finally:
            if self._task is not None:
                await asyncio.wait({self._task}, timeout=1.0)
