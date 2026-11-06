import asyncio
import io
import json
import struct
import tempfile
import unittest
import wave
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from jane.core.clock import FakeClock
from jane.offline import OfflineRuntime
from jane.storage.journal import Journal
from jane.voice.devices import MAX_BYTES, PCMBuffer
from jane.voice.session import VoiceSession
from jane.voice.terminal import VoiceBridge
from jane.voice.transcription import OpenAITranscriber


def audio():
    buffer = PCMBuffer()
    buffer.feed(struct.pack("<h", 1000) * 8000)
    return buffer.finish()


class Mic:
    def __init__(self):
        self.active = False
        self.failure = False
        self.aborts = 0

    def start(self):
        if self.failure:
            raise OSError("unplugged")
        self.active = True

    def finish(self):
        self.active = False
        if self.failure:
            raise ValueError("disconnected")
        return audio()

    def abort(self):
        self.active = False
        self.aborts += 1


class Transcriber:
    async def transcribe(self, wav):
        return "Inspect the simulated cup"


class Speaker:
    def __init__(self, clock):
        self.clock = clock
        self.texts = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def speak(self, text):
        self.texts.append(text)
        self.started.set()
        await self.release.wait()

    async def stop(self):
        self.clock.advance(0.025)
        self.release.set()


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name)
        self.j = Journal(path / "voice.db")
        self.clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        self.runtime = OfflineRuntime(self.j, path / "sim.db", self.clock, modes=("succeed",))
        self.runtime.startup()
        self.bridge = VoiceBridge(self.runtime, "cup")
        self.mic = Mic()
        self.speaker = Speaker(self.clock)
        self.session = VoiceSession(
            self.mic,
            Transcriber(),
            self.speaker,
            self.bridge.think,
            self.bridge.cancel,
            self.clock,
            enabled=True,
        )

    async def asyncTearDown(self):
        await self.session.stop()
        await asyncio.sleep(0)
        self.runtime.close()
        self.j.close()
        self.temp.cleanup()

    async def preview(self):
        self.session.record()
        self.clock.advance(0.5)
        self.session.release()
        await self.session._task

    async def test_confirmed_voice_reaches_verified_runtime_and_output(self):
        await self.preview()
        self.assertEqual(self.session.state, "preview")
        self.assertFalse(self.j.events())
        self.assertEqual(self.session._utterance.wav, b"")
        self.session.send()
        await asyncio.wait_for(self.speaker.started.wait(), 2)
        event = next(e for e in self.j.events() if e.kind == "user.message")
        self.assertEqual(event.source_id, "microphone")
        self.assertIsNotNone(event.source_event_id)
        self.assertEqual((event.occurred_end - event.occurred_start).total_seconds(), 0.5)
        self.assertTrue(self.speaker.texts)
        with self.assertRaises(ValueError):
            self.session.record()  # Echo cannot reenter the cognitive loop.
        stopped = await self.session.stop()
        self.assertEqual(stopped["ack_seconds"], 0)
        self.assertAlmostEqual(stopped["playback_process_stop_seconds"], 0.025)
        self.assertEqual(self.session.state, "idle")

    async def test_explicit_enablement_and_disconnected_device_recovery(self):
        self.session.enabled = False
        with self.assertRaises(ValueError):
            self.session.record()
        self.assertFalse(self.mic.active)
        self.session.enabled = True
        self.mic.failure = True
        with self.assertRaisesRegex(ValueError, "reconnect"):
            self.session.record()
        self.mic.failure = False
        self.session.record()
        self.mic.failure = True
        with self.assertRaises(ValueError):
            self.session.release()
        self.assertEqual(self.session.state, "idle")
        self.mic.failure = False
        await self.preview()
        self.assertEqual(self.session.state, "preview")

    async def test_partial_and_late_transcript_cannot_create_task(self):
        entered, finish = asyncio.Event(), asyncio.Event()

        class Late:
            async def transcribe(self, wav):
                entered.set()
                try:
                    await finish.wait()
                except asyncio.CancelledError:
                    await finish.wait()
                return "Old instruction"

        self.session.transcriber = Late()
        self.session.record()
        self.session.release()
        await entered.wait()
        old = self.session._task
        self.session.update_partial("partial command", self.session._generation)
        self.assertFalse(self.j.events())
        await self.session.stop()
        finish.set()
        await old
        self.assertEqual(self.session.preview, "")
        self.assertFalse(self.j.events())
        with self.assertRaises(ValueError):
            self.session.send()

    async def test_cancelled_inference_cannot_speak_late_result(self):
        entered, finish = asyncio.Event(), asyncio.Event()

        async def late(text, utterance):
            entered.set()
            try:
                await finish.wait()
            except asyncio.CancelledError:
                await finish.wait()
            return "late speech"

        self.session.think = late
        await self.preview()
        self.session.send()
        await entered.wait()
        old = self.session._task
        await self.session.stop()
        finish.set()
        await old
        self.assertFalse(self.speaker.texts)

    async def test_empty_transcript_is_not_actionable_and_next_recording_works(self):
        class Empty:
            async def transcribe(self, wav):
                return "   "

        self.session.transcriber = Empty()
        await self.preview()
        self.assertEqual(self.session.state, "idle")
        self.assertFalse(self.j.events())
        self.session.transcriber = Transcriber()
        await self.preview()
        self.assertEqual(self.session.state, "preview")

    async def test_stop_during_recording_discards_audio(self):
        self.session.record()
        await self.session.stop()
        self.assertFalse(self.mic.active)
        self.assertFalse(self.j.events())
        with self.assertRaises(ValueError):
            self.session.release()


class BufferTests(unittest.TestCase):
    def test_valid_pcm_wav_and_metrics(self):
        data, quality = audio()
        with wave.open(io.BytesIO(data)) as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels()), (16000, 1))
            self.assertEqual(wav.getnframes(), 8000)
        self.assertEqual(quality["duration_seconds"], 0.5)

    def test_silence_clipping_overflow_device_error_and_retention(self):
        for raw, error in [
            (b"\0" * 16000, False),
            (struct.pack("<h", 32767) * 8000, False),
            (b"x" * (MAX_BYTES + 1), False),
            (b"x" * 16000, True),
            (b"x", False),
        ]:
            with self.subTest(size=len(raw), device_error=error):
                buffer = PCMBuffer()
                buffer.feed(raw, error)
                self.assertLessEqual(len(buffer.data), MAX_BYTES)
                with self.assertRaises(ValueError):
                    buffer.finish()
                self.assertFalse(buffer.data)


class TranscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def transcribe(self, body, status=200):
        import httpx

        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(status, content=body)

        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-secret"}):
            with patch("httpx.AsyncClient") as constructor:
                adapter = OpenAITranscriber()
                self.assertFalse(constructor.call_args.kwargs["follow_redirects"])
        adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        try:
            text = await adapter.transcribe(audio()[0])
            self.assertEqual(len(requests), 1)
            self.assertEqual(str(requests[0].url), "https://api.openai.com/v1/audio/transcriptions")
            self.assertIn(b"gpt-transcribe", requests[0].content)
            return text
        finally:
            await adapter.close()

    async def test_final_response(self):
        self.assertEqual(
            await self.transcribe(b'{"text":"hello","languages":[{"code":"en"}]}'), "hello"
        )

    async def test_fail_closed_on_unclear_audio_errors_and_oversized_responses(self):
        for body, status in [
            (b'{"text":"invented","languages":[]}', 200),
            (b'{"text":"","languages":[{"code":"en"}]}', 200),
            (b"not-json test-secret", 200),
            (b"x" * 40000, 200),
            (json.dumps({"error": "test-secret"}).encode(), 429),
            (b"redirect", 302),
        ]:
            with self.subTest(status=status, size=len(body)):
                with self.assertRaisesRegex(ValueError, "transcription failed") as exc:
                    await self.transcribe(body, status)
                self.assertNotIn("test-secret", str(exc.exception))


class SpeakerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_during_spawn_still_reaps_process(self):
        from jane.voice.devices import EspeakSpeaker

        entered, release = asyncio.Event(), asyncio.Event()

        class Process:
            returncode = None
            terminated = False

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            async def wait(self):
                return self.returncode

        process = Process()

        async def spawn(*args, **kwargs):
            entered.set()
            await release.wait()
            return process

        with patch("shutil.which", return_value="/usr/bin/espeak-ng"):
            speaker = EspeakSpeaker()
        with patch("asyncio.create_subprocess_exec", side_effect=spawn):
            speaking = asyncio.create_task(speaker.speak("test"))
            await entered.wait()
            speaking.cancel()
            stopping = asyncio.create_task(speaker.stop())
            release.set()
            await stopping
            with self.assertRaises(asyncio.CancelledError):
                await speaking
        self.assertTrue(process.terminated)
        self.assertIsNone(speaker.process)

    async def test_stop_kills_unresponsive_output_process(self):
        from jane.voice.devices import EspeakSpeaker

        class Process:
            returncode = None
            killed = False

            def terminate(self):
                pass

            def kill(self):
                self.killed = True
                self.returncode = -9

            async def wait(self):
                if not self.killed:
                    await asyncio.Event().wait()
                return self.returncode

        with patch("shutil.which", return_value="/usr/bin/espeak-ng"):
            speaker = EspeakSpeaker()
        process = Process()
        speaker.process = process
        await asyncio.wait_for(speaker.stop(), 2)
        self.assertTrue(process.killed)
        self.assertIsNone(speaker.process)
