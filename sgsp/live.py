"""
Gemini Live connection, openable ahead of time.

Connecting to Gemini Live takes ~2-3 s and the first greeting audio another ~0.8 s. The
dispatcher opens the session and starts the greeting while the phone is still ringing:
everything Gemini sends is buffered in `queue`, and SgspSession replays it the moment
Exotel's stream arrives (same design as kasp/live.py).
"""

import asyncio
import os
import time

from google import genai
from google.genai import types

from .config import (
    GEMINI_MODEL,
    KICKOFF_AUDIO_TIMEOUT_S,
    LANGUAGE_CODE,
    VAD_SILENCE_DURATION_MS,
    VOICE_NAME,
)
from .prompts import build_system_prompt, kickoff_instruction


def build_live_config(employee: dict | None, channel: str, tools) -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=types.Content(parts=[types.Part(text=build_system_prompt(employee, channel))]),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE_NAME)),
            language_code=LANGUAGE_CODE,
        ),
        # Without hints, Kannada speech was transcribed in Devanagari (KASP finding).
        input_audio_transcription=types.AudioTranscriptionConfig(language_codes=["kn-IN", "en-IN"]),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                disabled=False,
                silence_duration_ms=VAD_SILENCE_DURATION_MS,  # None → Gemini default
            )
        ),
        tools=tools,
    )


class LiveConnection:
    """An open Gemini Live session whose responses are pumped into `queue`.

    Queue items are LiveServerMessage objects, or an Exception if the stream failed.
    """

    def __init__(self, employee: dict | None, channel: str, tools, log):
        self.employee = employee
        self.channel = channel
        self.tools = tools
        self.log = log
        self.client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
        self.session = None
        self.queue: asyncio.Queue = asyncio.Queue()
        self.first_audio = asyncio.Event()
        self.opened_at: float | None = None
        self._cm = None
        self._pump_task: asyncio.Task | None = None
        self._greeting_task: asyncio.Task | None = None
        self._closed = False

    async def open(self):
        t0 = time.time()
        self._cm = self.client.aio.live.connect(
            model=GEMINI_MODEL, config=build_live_config(self.employee, self.channel, self.tools)
        )
        self.session = await self._cm.__aenter__()
        self.opened_at = time.time()
        self._pump_task = asyncio.create_task(self._pump(), name="gemini_pump")
        self.log.info(f"[LIVE]  Gemini Live connected in {self.opened_at - t0:.2f}s")
        return self

    async def _pump(self):
        try:
            while True:
                async for response in self.session.receive():
                    if response.data:
                        self.first_audio.set()
                    await self.queue.put(response)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            await self.queue.put(e)

    def start_greeting(self) -> asyncio.Task:
        """Send the opening instruction (idempotent). Gemini occasionally ignores the first client
        turn, so it is resent once if no audio arrives — during ringing that retry is inaudible."""
        if not self._greeting_task:
            self._greeting_task = asyncio.create_task(self._greet(), name="greeting")
        return self._greeting_task

    async def _greet(self):
        for attempt in (1, 2):
            self.log.info(f"[KICKOFF] Sending the opening instruction (attempt {attempt})")
            await self.session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=kickoff_instruction(self.employee, self.channel))]),
                turn_complete=True,
            )
            try:
                await asyncio.wait_for(self.first_audio.wait(), timeout=KICKOFF_AUDIO_TIMEOUT_S)
                self.log.info(f"[KICKOFF] Greeting audio started {time.time() - self.opened_at:.2f}s after connect")
                return
            except asyncio.TimeoutError:
                self.log.warning(f"[KICKOFF] No greeting audio within {KICKOFF_AUDIO_TIMEOUT_S}s")

    async def close(self):
        if self._closed:
            return
        self._closed = True
        for task in (self._greeting_task, self._pump_task):
            if task:
                task.cancel()
        if self._cm:
            try:
                await self._cm.__aexit__(None, None, None)
            except Exception:
                pass
