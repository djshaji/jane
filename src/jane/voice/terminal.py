"""Terminal voice interface; capture and network calls are explicitly opted into."""

import asyncio
import json
import sys
from pathlib import Path

from jane.core.clock import SystemClock
from jane.motivation.goals import Goals, Terminal
from jane.offline import OfflineRuntime
from jane.providers.openai import OpenAIAdapter, ProviderConfig
from jane.storage.journal import Journal
from jane.voice.devices import EspeakSpeaker, SoundDeviceMicrophone
from jane.voice.session import Utterance, VoiceSession
from jane.voice.transcription import OpenAITranscriber


class VoiceBridge:
    def __init__(self, runtime: OfflineRuntime, object_id: str) -> None:
        self.runtime, self.object_id = runtime, object_id
        self.task: str | None = None

    async def think(self, text: str, utterance: Utterance) -> str:
        runtime = self.runtime
        if any(g.status not in Terminal for g in Goals(runtime.j).list()):
            raise ValueError("resolve existing goals before sending another voice task")
        self.task = runtime.submit(
            text,
            self.object_id,
            source_id="microphone",
            source_event_id=utterance.capture_id,
            occurred_start=utterance.started,
            occurred_end=utterance.ended,
        )
        await runtime.run()
        responses = [
            e.payload.text
            for e in runtime.j.events()
            if e.task_id == self.task and e.kind == "communication.response"
        ]
        return " ".join(responses)

    def cancel(self) -> None:
        self.runtime.coordinator.cancel()
        goals = Goals(self.runtime.j)
        if self.task and goals.get(self.task).status not in Terminal:
            goals.change(self.task, "cancel", self.runtime.clock.now())


async def terminal(path: Path, device: int | None, object_id: str) -> None:
    # Validate optional dependencies before opening a journal or microphone.
    speaker = EspeakSpeaker()
    transcriber = OpenAITranscriber()
    adapter = None
    journal = None
    runtime = None
    session = None
    try:
        adapter = OpenAIAdapter(ProviderConfig())
        journal = Journal(path)
        runtime = OfflineRuntime(
            journal, Path(str(path) + ".sim.db"), SystemClock(), adapter=adapter
        )
        runtime.startup()
        bridge = VoiceBridge(runtime, object_id)
        session = VoiceSession(
            SoundDeviceMicrophone(device),
            transcriber,
            speaker,
            bridge.think,
            bridge.cancel,
            runtime.clock,
            enabled=True,
        )
        print(
            "AI-generated voice. Audio/transcripts go to OpenAI. "
            "Actions inspect a simulated object."
        )
        print("record → release → status (review transcript) → send. stop interrupts; quit exits.")
        print("Maximum recording: 15 seconds. Use a dedicated voice database and one process.")
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            command = line.strip().lower()
            if not line or command == "quit":
                break
            try:
                if command == "record":
                    session.record()
                elif command == "release":
                    session.release()
                elif command == "send":
                    session.send()
                elif command == "stop":
                    print(json.dumps(await session.stop()))
                elif command != "status":
                    print("Commands: record, release, status, send, stop, quit")
                print(
                    json.dumps(
                        {
                            "state": session.state,
                            "transcript": session.preview,
                            "last_record": list(session.audit)[-1:],
                        },
                        sort_keys=True,
                    )
                )
            except ValueError as error:
                print(str(error), file=sys.stderr)
    finally:
        if session:
            await session.close()
        if runtime:
            runtime.close()
        if journal:
            journal.close()
        if adapter:
            await adapter.close()
        await transcriber.close()
