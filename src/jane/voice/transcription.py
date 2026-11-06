"""Explicit hosted transcription; bounded response and no automatic retries."""

import asyncio
import importlib
import json
import os
from typing import Any


class OpenAITranscriber:
    def __init__(self) -> None:
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise ValueError("OPENAI_API_KEY is required for transcription")
        try:
            httpx = importlib.import_module("httpx")
        except ImportError:
            raise ValueError("install jane-neural-engine[voice]") from None
        self.client: Any = httpx.AsyncClient(
            timeout=30, follow_redirects=False, headers={"Authorization": "Bearer " + key}
        )

    async def transcribe(self, wav: bytes) -> str:
        if not 44 < len(wav) <= 480044 or not wav.startswith(b"RIFF"):
            raise ValueError("invalid or oversized audio")
        try:
            async with (
                asyncio.timeout(30),
                self.client.stream(
                    "POST",
                    "https://api.openai.com/v1/audio/transcriptions",
                    data={"model": "gpt-transcribe"},
                    files={"file": ("utterance.wav", wav, "audio/wav")},
                ) as response,
            ):
                if response.status_code != 200:
                    raise ValueError("transcription provider rejected request")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > 32768:
                        raise ValueError("transcript exceeds limit")
                    data.extend(chunk)
            result = json.loads(data)
            if not isinstance(result, dict) or not result.get("languages"):
                raise ValueError("language could not be identified")
            text = result.get("text")
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 4000:
                raise ValueError("empty or invalid transcript")
            return text.strip()
        except Exception:
            # Never expose provider response bodies, headers, or credentials.
            raise ValueError("transcription failed; repeat or use text input") from None

    async def close(self) -> None:
        await self.client.aclose()
