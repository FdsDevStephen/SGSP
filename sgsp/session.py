"""
SgspSession — real-time audio bridge between a government employee and Gemini Live.

Same architecture as kasp/session.py — one conversation core, two transports:
  • ExotelSession — phone call, Exotel media stream (8 kHz base64 JSON)
  • WebSession    — browser test page, raw PCM16 over WebSocket
                    (client → server 16 kHz binary, server → client 24 kHz binary,
                    plus JSON text frames for transcripts / state / outcome)

The call ends through one tool, record_outcome: the tool runs first, its result carries the
scripted closing line (say_now), the agent speaks it, and the session hangs up once it has
played — even if the model never calls end_call.
"""

import audioop

import asyncio
import base64
import json
import os
import time
from datetime import datetime, timezone, timedelta
from enum import Enum, auto
from pathlib import Path

import numpy as np
import requests
from fastapi import WebSocket, WebSocketDisconnect
from google import genai
from google.genai import types

from .audio_pause import PauseExtender
from .audio_stretch import Stretcher
from .audio_utils import mix_wav
from .config import (
    AUTO_END_TURN_TIMEOUT_S,
    COST_IN_AUDIO_PER_1M,
    COST_IN_TEXT_PER_1M,
    COST_OUT_AUDIO_PER_1M,
    COST_OUT_TEXT_PER_1M,
    EXOTEL_RATE,
    GEMINI_MODEL,
    GEMINI_NUDGE_S,
    GEMINI_SILENCE_TIMEOUT_S,
    LANGUAGE_CODE,
    MIC_BATCH_MAX_CHUNKS,
    MIC_QUEUE_MAX,
    MIC_RATE,
    NO_RESPONSE_PROMPT_S,
    NOISE_GATE_RMS_PHONE,
    NOISE_GATE_RMS_WEB,
    PAUSE_MS,
    REC_RATE,
    SAVE_ON_EXIT_TIMEOUT_S,
    SEND_TIMEOUT_S,
    SILENCE_TIMEOUT_S,
    SMS_ENABLED,
    SPEAKER_RATE,
    SPEECH_SPEED,
    VOICEMAIL_DETECTION_WINDOW_S,
    VOICEMAIL_PHRASES,
)
from .extract import classify_transcript
from .live import LiveConnection
from .progress import NON_ANSWER_LIMIT, is_usable_reply, non_answer_instruction
from .prompts import (
    REPEAT_LIMIT, has_identity_check, no_response_instruction, normalize_turn, repeated_question_instruction,
    spoken_line,
)
from .sms import render_sms, send_sms

IST = timezone(timedelta(hours=5, minutes=30))
_FLUSH = object()  # play-queue marker: end of an agent turn — flush the time-stretcher

# record_outcome outcome → script line spoken next (None: the escalation line was already spoken).
OUTCOME_LINES = {
    "already_enrolled": "already_enrolled",
    "will_visit": "will_visit",
    "not_now": "not_now",
    "declined": "declined",
    "do_not_call": "do_not_call",
    "wrong_person": "wrong_person",
    "not_available": "not_available",
    "busy_callback": "busy_callback",
    "unclear": "unclear_end",
    "no_response": "no_response_2",
    "escalated": None,
}
OUTCOMES = list(OUTCOME_LINES)
# Outcomes followed by the SMS of the process and documents (V2: only if interested = yes).
SMS_OUTCOMES = {"will_visit"}
SMS_TIMEOUT_S = 10.0
ENROLLMENT_STATUSES = ["ENROLLED", "NOT_ENROLLED", "NOT_SURE", "UNKNOWN"]

# escalate trigger → line the agent speaks next (None: just record and continue).
ESCALATION_LINES = {
    "human_requested": "human_callback",
    "out_of_scope": "limited_role",
    "grievance": "grievance",
    "privacy_objection": "data_source",
    "fraud_report": "fraud_report",
    "distress": "emergency",
    "other": None,
}
ESCALATION_TRIGGERS = list(ESCALATION_LINES)
# Triggers after which the call carries on — the agent must re-ask its open question.
CONTINUE_AFTER = {"grievance", "out_of_scope"}
# Triggers that end the call (V2: stop the SGSP flow / don't insist on continuing).
END_AFTER = {"human_requested", "fraud_report", "distress"}

# The live model has been seen asking a question and calling the ending tool in the same breath
# (KASP). An outcome must follow the employee's answer, never the agent's own question.
OUTCOME_BEFORE_ANSWER_MSG = (
    "Not recorded: you asked the employee a question in this turn and did not wait for the answer. "
    "Stop now and wait for their reply; call record_outcome only after they have answered."
)


def asked_question_this_turn(agent_text_this_turn: str) -> bool:
    """True if the agent's speech in the current turn (before a tool call) ends in a question."""
    return agent_text_this_turn.strip().endswith("?")


def outcome_response(args: dict, employee: dict | None, sms_sent: bool = SMS_ENABLED) -> tuple[str, dict]:
    """(outcome, tool response) for record_outcome — shared with the flow-test harness.
    sms_sent picks the closing line: one that promises the SMS, or its no-SMS twin."""
    outcome = args.get("outcome") if args.get("outcome") in OUTCOME_LINES else "declined"
    language = args.get("language") or "kn"
    response = {"status": "recorded", "then": f"end_call(reason='{outcome}')"}
    key = OUTCOME_LINES[outcome]
    lines = [spoken_line("ai_ack", language, employee=employee)] if args.get("employee_asked_if_ai") else []
    if key:
        lines.append(spoken_line(key, language, employee=employee, sms_enabled=sms_sent))
    if lines:
        response["say_now"] = " ".join(lines)
    return outcome, response


def escalation_response(args: dict, employee: dict | None) -> dict:
    """Tool response for escalate — shared with the flow-test harness."""
    response = {"status": "recorded"}
    key = ESCALATION_LINES.get(args.get("trigger"))
    if key:
        response["say_now"] = spoken_line(key, args.get("language") or "kn", employee=employee)
    if args.get("trigger") in CONTINUE_AFTER:
        response["then"] = ("In the SAME turn, right after say_now, ask again the question the employee has "
                            "not answered yet (e.g. the SGSP registration question), word for word.")
    elif args.get("trigger") in END_AFTER:
        response["then"] = "Right after say_now, call record_outcome with outcome 'escalated'."
    return response


def _str(description: str) -> types.Schema:
    return types.Schema(type=types.Type.STRING, description=description)


TOOLS = [
    types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name="record_outcome",
            description=(
                "Record how the call ended. Call exactly once, silently, as soon as the flow reaches an "
                "OUTCOME — after the employee has answered. Then speak the result's say_now line word for "
                "word and call end_call. Never say this function's name."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "outcome": types.Schema(type=types.Type.STRING, enum=OUTCOMES,
                                            description="Which OUTCOME the flow reached."),
                    "enrollment_status": types.Schema(
                        type=types.Type.STRING, enum=ENROLLMENT_STATUSES,
                        description="Answer to the core question; UNKNOWN if never answered."),
                    "bank_name": _str("Bank the employee mentioned anywhere in the call, in English, only if said."),
                    "employee_asked_if_ai": types.Schema(
                        type=types.Type.BOOLEAN,
                        description="true if the employee asked whether you are a human / robot / AI and it is not answered yet."),
                    "employee_questions": _str("Short English summary of questions the employee asked, if any."),
                    "objection": _str("Short English summary of why they said no / not now, if they said."),
                    "callback_time": _str("When they asked to be called back, as they said it, if they did."),
                    "language": types.Schema(type=types.Type.STRING, enum=["kn", "en"],
                                             description="Language the conversation is in now."),
                },
                required=["outcome", "enrollment_status", "language"],
            ),
        ),
        types.FunctionDeclaration(
            name="escalate",
            description=(
                "Flag the call for follow-up by an officer. Call silently, FIRST, as soon as a trigger occurs; "
                "if the result has say_now, speak it word for word. May be called more than once. Never say "
                "this function's name."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "trigger": types.Schema(type=types.Type.STRING, enum=ESCALATION_TRIGGERS,
                                            description="Which trigger occurred."),
                    "language": types.Schema(type=types.Type.STRING, enum=["kn", "en"],
                                             description="Language of the conversation right now."),
                    "employee_statement": _str("The employee's own words that triggered it, verbatim."),
                },
                required=["trigger"],
            ),
        ),
        types.FunctionDeclaration(
            name="end_call",
            description=(
                "End the conversation. Call silently right after your final line. Say nothing after "
                "calling it. Never say this function's name."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={"reason": types.Schema(type=types.Type.STRING, enum=OUTCOMES,
                                                   description="The outcome that was recorded.")},
                required=["reason"],
            ),
        ),
    ])
]


class State(Enum):
    IDLE = auto()
    LISTENING = auto()
    GENERATING = auto()
    SPEAKING = auto()


class SgspSession:
    """Transport-agnostic core. Subclasses implement the _read_client / _send_* / _hangup hooks."""

    channel = ""
    noise_gate_rms = 0
    # Safety margin on top of the estimated client playout before hanging up.
    playout_margin_s = 0.5
    # Max audio sent ahead of the client's playback. Exotel dropped a KASP call when a pre-warmed
    # greeting arrived in one burst, so the phone path is paced to real time.
    max_audio_lead_s = float("inf")

    def __init__(self, ws: WebSocket, log, run_ts: str, call_dir: Path,
                 employee: dict | None = None, phone: str | None = None, call_sid: str | None = None):
        self.ws = ws
        self.log = log
        self.run_ts = run_ts
        self.call_dir = call_dir
        self.employee = employee or {}
        self.phone = phone
        self.call_sid = call_sid

        self.state = State.IDLE
        self.turn_count = 0

        self._mic_queue: asyncio.Queue = asyncio.Queue(maxsize=MIC_QUEUE_MAX)
        self._play_queue: asyncio.Queue = asyncio.Queue()
        self._ws_lock = asyncio.Lock()

        self._t_state = time.time()
        self._call_start_time = time.time()
        self._last_speech_time = time.time()
        self._last_gemini_msg_time = time.time()
        self._last_audio_chunk_time = None
        self._turn_complete_event = asyncio.Event()
        # Wall-clock time the client should finish playing everything sent so far.
        self._playout_end = time.time()
        self._clear_count = 0
        self._stretcher = Stretcher(SPEECH_SPEED)
        self._pauser = PauseExtender(SPEAKER_RATE, PAUSE_MS)
        self._speech_raw_s = 0.0
        self._speech_sent_s = 0.0

        self._dropped_qfull = 0
        self._noise_gated = 0

        self._mic_pcm_all = bytearray()
        self._resp_pcm_all = bytearray()
        self._rec_state = None

        self._stt_cur: list[str] = []
        self._turn_interrupted = False
        self._tts_cur: list[str] = []
        self._transcript_lines: list[str] = []
        self._transcript = None
        self._session = None
        self._client: genai.Client | None = None
        self._employee_spoke = False

        self._outcome_task: asyncio.Task | None = None
        self._ending_pending = False   # an outcome line must be spoken before hanging up
        self.escalations: list[dict] = []
        self._no_response_count = 0
        self._last_no_response_prompt = 0.0
        self._last_agent_turn = ""
        self._same_turn_count = 0
        self._prev_agent_text = ""
        self._non_answers = 0
        self._stall_handoff_sent = False
        self._call_ending = False
        self._voicemail_detected = False
        self._caller_transcriptions: list[str] = []
        self._background_tasks: list[asyncio.Task] = []

        # Result of the call — read by app.py after run() to update the campaign.
        self.outcome = "incomplete"
        self.outcome_source: str | None = None   # "agent" | "transcript" | "system"
        self.details: dict = {}
        self.sms: dict | None = None
        self.end_reason: str | None = None
        self.call_duration = 0
        self.call_cost = 0.0

        self._turn_tokens = {"in_text": 0, "in_audio": 0, "out_text": 0, "out_audio": 0}
        self._total_tokens = {"in_text": 0, "in_audio": 0, "out_text": 0, "out_audio": 0}

    # ── Transport hooks ────────────────────────────────────────────────────────

    async def _read_client(self):
        raise NotImplementedError

    async def _send_audio(self, pcm_24k: bytes):
        raise NotImplementedError

    async def _send_clear(self):
        raise NotImplementedError

    async def _send_event(self, event: dict):
        """UI events (transcripts, state, outcome). Only the web transport uses them."""

    async def _hangup(self):
        raise NotImplementedError

    # ── Shared plumbing ────────────────────────────────────────────────────────

    def _push_mic(self, pcm_16k: bytes):
        self._mic_pcm_all += pcm_16k
        try:
            self._mic_queue.put_nowait(pcm_16k)
        except asyncio.QueueFull:
            self._dropped_qfull += 1

    def _end_mic(self):
        try:
            self._mic_queue.put_nowait(None)
        except asyncio.QueueFull:
            try:
                self._mic_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._mic_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def _close_ws(self):
        try:
            await self.ws.close()
        except Exception:
            pass

    def _set_state(self, new_state: State):
        if new_state == self.state:
            return
        elapsed = (time.time() - self._t_state) * 1000
        self.log.info(f"[STATE]  {self.state.name:<12} -> {new_state.name:<12}  (spent {elapsed:.0f}ms in {self.state.name})")
        self.state = new_state
        self._t_state = time.time()
        self._spawn(self._send_event({"type": "state", "state": new_state.name.lower()}))

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._background_tasks.append(task)
        return task

    # ── Speaker ────────────────────────────────────────────────────────────────

    async def _speaker_task(self):
        try:
            while True:
                item = await self._play_queue.get()
                if item is None:
                    break
                if item is not _FLUSH:
                    self._speech_raw_s += len(item) / 2 / SPEAKER_RATE
                if item is _FLUSH:
                    pcm_24k = self._pauser.feed(self._stretcher.flush()) + self._pauser.flush()
                else:
                    pcm_24k = self._pauser.feed(self._stretcher.feed(item))
                if not pcm_24k:
                    continue
                pcm_rec, self._rec_state = audioop.ratecv(pcm_24k, 2, 1, SPEAKER_RATE, REC_RATE, self._rec_state)
                lead = self._playout_end - time.time()
                if lead > self.max_audio_lead_s:
                    clears_before = self._clear_count
                    await asyncio.sleep(lead - self.max_audio_lead_s)
                    if self._clear_count != clears_before:
                        continue  # barge-in while waiting — this chunk is stale
                mic_pos = len(self._mic_pcm_all)
                if len(self._resp_pcm_all) < mic_pos:
                    self._resp_pcm_all += bytes(mic_pos - len(self._resp_pcm_all))
                self._resp_pcm_all += pcm_rec
                await self._send_audio(pcm_24k)
                duration = len(pcm_24k) / 2 / SPEAKER_RATE
                self._speech_sent_s += duration
                self._playout_end = max(time.time(), self._playout_end) + duration
        except Exception as e:
            self.log.warning(f"[SPK]   Error: {e}")

    # ── Mic sender ─────────────────────────────────────────────────────────────

    def _gate(self, pcm: bytes) -> bytes:
        """Silence one chunk if it's below the noise gate or the agent is generating."""
        if self.state == State.GENERATING:
            return bytes(len(pcm))
        arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        rms = int(np.sqrt(np.mean(arr ** 2))) if len(arr) else 0
        if rms < self.noise_gate_rms:
            self._noise_gated += 1
            return bytes(len(pcm))
        self._last_speech_time = time.time()
        return pcm

    async def _mic_sender_task(self, session):
        # Send whatever has queued up (up to MIC_BATCH_MAX_CHUNKS) as one blob — one send per
        # 20 ms frame can't keep up when a send is slow (KASP's first live call lost ~9 s).
        ended = False
        while not ended:
            first = await self._mic_queue.get()
            if first is None:
                break
            chunks = [self._gate(first)]
            while len(chunks) < MIC_BATCH_MAX_CHUNKS:
                try:
                    nxt = self._mic_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if nxt is None:
                    ended = True
                    break
                chunks.append(self._gate(nxt))
            try:
                await asyncio.wait_for(
                    session.send_realtime_input(audio=types.Blob(data=b"".join(chunks), mime_type=f"audio/pcm;rate={MIC_RATE}")),
                    timeout=SEND_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                continue
            await asyncio.sleep(0)

    # ── Gemini receiver ────────────────────────────────────────────────────────

    def _count_tokens(self, um):
        # One usage message per turn; the prompt count is the whole context re-billed that turn.
        for key_prefix, details, total in (
            ("in", um.prompt_tokens_details, um.prompt_token_count),
            ("out", um.response_tokens_details, um.response_token_count),
        ):
            audio = sum((d.token_count or 0) for d in (details or []) if getattr(d, "modality", None) == "AUDIO")
            itemised_text = sum((d.token_count or 0) for d in (details or []) if getattr(d, "modality", None) == "TEXT")
            text = max((total or 0) - audio, itemised_text)
            for key, count in ((f"{key_prefix}_audio", audio), (f"{key_prefix}_text", text)):
                self._turn_tokens[key] += count
                self._total_tokens[key] += count

    def _handle_tool_call(self, fc):
        args = dict(fc.args or {})
        if fc.name == "record_outcome":
            if self._outcome_task:
                self._spawn(self._send_tool_response(fc.id, fc.name, {"status": "already_recorded"}))
            elif asked_question_this_turn("".join(self._tts_cur)):
                self.log.warning("[GUARD] record_outcome right after the agent's own question — rejected")
                self._spawn(self._send_tool_response(fc.id, fc.name, {"status": "rejected", "message": OUTCOME_BEFORE_ANSWER_MSG}))
            else:
                self.log.info(f"[TOOL]  record_outcome args={args}")
                self._ending_pending = True
                self._outcome_task = self._spawn(self._handle_record_outcome(fc.id, args))
        elif fc.name == "escalate":
            self._record_escalation(fc.id, args)
        elif fc.name == "end_call" and not self._call_ending:
            self.end_reason = args.get("reason", "")
            self.log.info(f"[TOOL]  end_call reason={self.end_reason}")
            if self._ending_pending:
                # The closing line from the tool result hasn't been spoken yet —
                # _auto_end_after_ending hangs up once it has.
                self.log.info("[CALL]  end_call deferred until the closing line is spoken")
            else:
                self._call_ending = True
                if not self._outcome_task:
                    self.outcome, self.outcome_source = (self.end_reason or "incomplete"), "agent"
                self._spawn(self._end_call_task())

    async def _handle_gemini_response(self, response) -> bool:
        self._last_gemini_msg_time = time.time()

        tool_call = getattr(response, "tool_call", None)
        if tool_call:
            for fc in (tool_call.function_calls or []):
                self._handle_tool_call(fc)

        if response.data:
            if self.state != State.SPEAKING:
                self._set_state(State.SPEAKING)
            self._last_audio_chunk_time = time.time()
            await self._play_queue.put(response.data)

        if response.usage_metadata:
            self._count_tokens(response.usage_metadata)

        sc = getattr(response, "server_content", None)
        if not sc:
            return False

        if getattr(sc, "interrupted", False) and self.state in (State.SPEAKING, State.GENERATING):
            drained = 0
            while not self._play_queue.empty():
                try:
                    self._play_queue.get_nowait()
                    drained += 1
                except asyncio.QueueEmpty:
                    break
            await self._send_clear()
            self._stretcher.reset()
            self._pauser.reset()
            self._clear_count += 1
            self._playout_end = time.time()
            self._turn_interrupted = True
            self.log.info(f"[BARGE] Gemini interrupted — drained {drained} queued chunks + cleared client buffer")
            self._set_state(State.LISTENING)

        it = getattr(sc, "input_transcription", None)
        if it and getattr(it, "text", None):
            self.log.info(f"[STT]   Employee: \"{it.text}\"")
            self._stt_cur.append(it.text)
            self._employee_spoke = True
            self._last_speech_time = time.time()
            self._no_response_count = 0
            await self._send_event({"type": "transcript", "role": "user", "text": it.text})

            if self._check_voicemail(it.text):
                self.log.info("[VOICEMAIL] Hanging up — voicemail detected from caller transcription")
                await self._hangup()
                return False

            if self.state == State.IDLE:
                self._set_state(State.LISTENING)
            if self.state == State.LISTENING:
                self._set_state(State.GENERATING)

        ot = getattr(sc, "output_transcription", None)
        if ot and getattr(ot, "text", None):
            text = ot.text
            # Gemini occasionally re-sends a chunk duplicating the tail of what it already streamed.
            current_norm = " ".join("".join(self._tts_cur).split())
            text_norm = " ".join(text.split())
            if text_norm and current_norm.endswith(text_norm):
                self.log.debug(f"[TTS]   Duplicate transcription chunk suppressed: \"{text}\"")
            else:
                self.log.info(f"[TTS]   Agent:  \"{text}\"")
                self._tts_cur.append(text)
                await self._send_event({"type": "transcript", "role": "agent", "text": text})

        if getattr(sc, "turn_complete", False):
            t = self._turn_tokens
            if self.turn_count == 0 and not self._tts_cur and t["out_audio"]:
                # Gemini sometimes speaks the (pre-warmed) greeting without sending its transcription,
                # so the transcript started mid-call. The greeting is scripted — record that text.
                opening = self._scripted_opening()
                self.log.info(f"[TTS]   Agent:  \"{opening}\"  (scripted opening — no live transcription received)")
                self._tts_cur.append(opening)
            self.log.info("─" * 60)
            self.log.info(f"[TURN]  #{self.turn_count} complete | in_text={t['in_text']} in_audio={t['in_audio']} "
                          f"out_text={t['out_text']} out_audio={t['out_audio']}")
            self.log.info("─" * 60)
            self._turn_tokens = dict.fromkeys(self._turn_tokens, 0)
            self._last_audio_chunk_time = None
            self._check_repeated_question("".join(self._tts_cur))
            self._check_progress("".join(self._stt_cur), "".join(self._tts_cur))
            self._flush_transcript(interrupted=self._turn_interrupted)
            self._turn_interrupted = False
            self._set_state(State.IDLE)
            self._last_speech_time = time.time()
            await self._play_queue.put(_FLUSH)
            self._turn_complete_event.set()
            await self._send_event({"type": "turn_complete"})
            return True

        return False

    async def _receiver_task(self, conn: LiveConnection):
        try:
            while True:
                item = await conn.queue.get()
                if isinstance(item, Exception):
                    raise item
                await self._handle_gemini_response(item)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.log.error(f"[RECV]  Error: {e}", exc_info=True)
            await self._hangup()

    # ── Outcome ────────────────────────────────────────────────────────────────

    async def _send_tool_response(self, tool_call_id: str, name: str, response: dict):
        if not self._session:
            return
        try:
            await self._session.send_tool_response(
                function_responses=types.FunctionResponse(id=tool_call_id, name=name, response=response)
            )
            self.log.info(f"[TOOL]  {name} response sent: {response}")
        except Exception as e:
            self.log.error(f"[TOOL]  Failed to send {name} response: {e}", exc_info=True)

    async def _handle_record_outcome(self, tool_call_id: str, args: dict):
        # V2: send the SMS before the closing line, so the agent says so if it could not be sent.
        sms_sent = SMS_ENABLED
        if args.get("outcome") in SMS_OUTCOMES:
            self.sms = await self._send_followup_sms()
            sms_sent = bool(self.sms.get("sent"))
        outcome, response = outcome_response(args, self.employee, sms_sent)
        self.outcome, self.outcome_source = outcome, "agent"
        self.details = {k: args.get(k) for k in
                        ("enrollment_status", "bank_name", "employee_questions", "objection", "callback_time", "language")}
        self._write_transcript_line(f"SYSTEM : Outcome — {outcome} ({self.details.get('enrollment_status')})")

        self._arm_auto_end(outcome)
        await self._send_tool_response(tool_call_id, "record_outcome", response)
        await self._send_event({"type": "outcome", "outcome": outcome, "details": self.details})

        # The live model often leaves out the bank the employee named (flow test S18) — recover it
        # from the transcript for the report. The employee has already heard the closing line.
        if not self.details.get("bank_name") and self._client and self._employee_spoke:
            try:
                transcript = "".join(self._transcript_lines) + "".join(self._stt_cur)
                bank = (await classify_transcript(self._client, transcript, OUTCOMES)).get("bank_name")
                if bank:
                    self.details["bank_name"] = bank
                    self.log.info(f"[OUTCOME] bank_name filled from transcript: {bank}")
            except Exception as e:
                self.log.warning(f"[OUTCOME] bank_name fill failed: {e}")

    async def _send_followup_sms(self) -> dict:
        if not SMS_ENABLED:
            return {"sent": False, "reason": "disabled"}
        if not self.phone:
            return {"sent": False, "reason": "no phone number"}
        try:
            loop = asyncio.get_running_loop()
            sms = await asyncio.wait_for(
                loop.run_in_executor(None, send_sms, self.phone, render_sms(self.employee)), SMS_TIMEOUT_S)
        except Exception as e:
            sms = {"sent": False, "reason": f"error: {e!r}"}
        self.log.info(f"[SMS]   {sms}")
        return sms

    def _arm_auto_end(self, reason: str):
        """The model sometimes speaks the closing line but never calls end_call. The ending is
        already decided by the tool that returned it, so hang up once that line has played."""
        self._turn_complete_event.clear()
        self._spawn(self._auto_end_after_ending(reason))

    async def _auto_end_after_ending(self, reason: str):
        t_armed = time.time()
        try:
            # No audio at all after the response → the model has nothing more to say; no dead air.
            while time.time() - t_armed < 6.0:
                if self._last_audio_chunk_time and self._last_audio_chunk_time > t_armed:
                    break
                await asyncio.sleep(0.2)
            else:
                raise asyncio.TimeoutError
            await asyncio.wait_for(self._turn_complete_event.wait(), timeout=AUTO_END_TURN_TIMEOUT_S)
        except asyncio.TimeoutError:
            self.log.warning("[CALL]  Closing line not (fully) spoken after the outcome response")
        await asyncio.sleep(0.5)  # an end_call in the same turn arrives right after turn_complete
        if self._call_ending:
            return
        self._call_ending = True
        self.end_reason = self.end_reason or reason
        self.log.info(f"[CALL]  Closing line spoken — ending the call (outcome={reason})")
        await self._end_call_task()

    def _record_escalation(self, tool_call_id: str, args: dict):
        """Recorded on the call (session.json, campaign report) for an officer to follow up."""
        entry = {
            "trigger": args.get("trigger"),
            "employeeStatement": args.get("employee_statement"),
            "at": datetime.now(IST).isoformat(),
        }
        self.escalations.append(entry)
        self.log.warning(f"[ESCALATE] {entry}")
        self._write_transcript_line(f"SYSTEM : Escalation — {entry['trigger']}")
        self._spawn(self._send_tool_response(tool_call_id, "escalate", escalation_response(args, self.employee)))

    async def _backstop_outcome(self):
        """No outcome was recorded (hang-up, dropped connection, model never called the tool) —
        classify the call from the transcript so it still lands in the report correctly."""
        if self.outcome_source or not self._client or not self._employee_spoke:
            return
        transcript = "".join(self._transcript_lines) + "".join(self._stt_cur) + "".join(self._tts_cur)
        try:
            data = await classify_transcript(self._client, transcript, OUTCOMES)
        except Exception as e:
            self.log.warning(f"[OUTCOME] Transcript classification failed: {e}")
            return
        previous = self.outcome
        self.outcome, self.outcome_source = data["outcome"], "transcript"
        self.details = {"enrollment_status": data.get("enrollment_status"), "bank_name": data.get("bank_name"),
                        "notes": data.get("notes"), "ended_as": previous}
        self.log.info(f"[OUTCOME] From transcript: {self.outcome} (call ended as {previous})")

    # ── End of call ────────────────────────────────────────────────────────────

    async def _end_call_task(self):
        self.log.info("[CALL]  end_call — waiting for the closing audio to play out")
        if self._outcome_task:
            try:
                await asyncio.wait_for(asyncio.shield(self._outcome_task), timeout=15.0)
            except Exception:
                pass
        await asyncio.sleep(0.5)  # closing audio can trail the tool call slightly
        deadline = time.time() + 20
        while not self._play_queue.empty() and time.time() < deadline:
            await asyncio.sleep(0.1)
        remaining = max(0.0, self._playout_end - time.time())
        self.log.info(f"[CALL]  Queue drained — waiting {remaining:.1f}s (+{self.playout_margin_s}s) for client playout")
        await asyncio.sleep(min(remaining, 30.0) + self.playout_margin_s)
        self.log.info("[CALL]  Playout done — hanging up")
        await self._hangup()

    # ── Kickoff ────────────────────────────────────────────────────────────────

    async def _kickoff_message(self, conn: LiveConnection):
        try:
            await conn.start_greeting()
            await asyncio.wait_for(self._turn_complete_event.wait(), timeout=45.0)
        except asyncio.TimeoutError:
            self.log.warning("[KICKOFF] No turn_complete within 45s")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.log.error(f"[KICKOFF] Error: {e}", exc_info=True)

    # ── Watchdogs ──────────────────────────────────────────────────────────────

    async def _send_system(self, text: str):
        await self._session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text=text)]), turn_complete=True)

    async def _silence_watchdog_task(self):
        while True:
            await asyncio.sleep(1.0)
            silence_s = time.time() - self._last_speech_time
            if silence_s >= SILENCE_TIMEOUT_S and self.state == State.IDLE:
                self.log.info(f"[WATCH] No speech for {silence_s:.0f}s — disconnecting")
                if not self.outcome_source:
                    self.outcome = "silence_timeout"
                await self._hangup()
                break

    def _check_repeated_question(self, agent_text: str):
        """Force the 'unclear' ending when the agent asks the identical question REPEAT_LIMIT times."""
        norm = normalize_turn(agent_text)
        if not norm or self._call_ending or self._ending_pending:
            return
        self._same_turn_count = self._same_turn_count + 1 if norm == self._last_agent_turn else 1
        self._last_agent_turn = norm
        if self._same_turn_count == REPEAT_LIMIT and self._session:
            self.log.warning(f"[GUARD] Same question asked {REPEAT_LIMIT} times in a row — forcing the unclear ending")
            self._write_transcript_line(f"SYSTEM : Same question {REPEAT_LIMIT}x — unclear ending")
            self._spawn(self._send_system(repeated_question_instruction()))

    def _check_progress(self, employee_text: str, agent_text: str):
        """Background non-answer check (see progress.py). This turn's speech answers the agent's previous turn."""
        question, self._prev_agent_text = self._prev_agent_text, agent_text or self._prev_agent_text
        if self.turn_count <= 1:   # the reply to the opening is often "hello?" while the caller orients
            return
        if employee_text.strip() and question.strip() and self._client and not self._call_ending and not self._ending_pending:
            self._spawn(self._judge_reply(question, employee_text))

    async def _judge_reply(self, question: str, reply: str):
        usable = await is_usable_reply(self._client, question, reply)
        if usable is None:
            return
        if usable:
            self._non_answers = 0
            return
        self._non_answers += 1
        self.log.info(f"[GUARD] Non-answer #{self._non_answers}: \"{reply.strip()[:60]}\"")
        if (self._non_answers >= NON_ANSWER_LIMIT and not self._stall_handoff_sent
                and not self._call_ending and not self._ending_pending and self._session):
            self._stall_handoff_sent = True
            self.log.warning(f"[GUARD] {NON_ANSWER_LIMIT} non-answers in a row — forcing the unclear ending")
            self._write_transcript_line(f"SYSTEM : {NON_ANSWER_LIMIT} non-answers — unclear ending")
            await self._send_system(non_answer_instruction())

    async def _no_response_watchdog_task(self):
        """When the employee stays silent after the agent finishes speaking, prompt the 1st and then
        the 2nd "couldn't hear you" line; after the 2nd the agent records no_response."""
        while True:
            await asyncio.sleep(0.5)
            if (self._call_ending or self._ending_pending or self.turn_count == 0 or self.state != State.IDLE
                    or not self._session or self._no_response_count >= 2):
                continue
            quiet_since = max(self._last_speech_time, self._playout_end, self._last_no_response_prompt)
            if time.time() - quiet_since < NO_RESPONSE_PROMPT_S:
                continue
            self._no_response_count += 1
            self._last_no_response_prompt = time.time()
            self.log.info(f"[WATCH] No response for {NO_RESPONSE_PROMPT_S:.0f}s — prompt #{self._no_response_count}")
            self._write_transcript_line(f"SYSTEM : No response (prompt {self._no_response_count})")
            try:
                await self._send_system(no_response_instruction(self._no_response_count))
            except Exception as e:
                self.log.warning(f"[WATCH] no-response prompt failed: {e}")

    async def _gemini_health_watchdog_task(self):
        # Gemini is legitimately quiet while the employee talks; only fail when we're waiting on it.
        nudged_for = None  # the quiet period (keyed by the last Gemini message time) already nudged
        while True:
            await asyncio.sleep(1.0)
            quiet_s = time.time() - self._last_gemini_msg_time
            if (self.state == State.GENERATING and quiet_s >= GEMINI_NUDGE_S
                    and nudged_for != self._last_gemini_msg_time):
                nudged_for = self._last_gemini_msg_time
                self.log.warning(f"[WATCH] Gemini silent for {quiet_s:.0f}s while GENERATING — nudging it to reply")
                try:
                    await self._send_system(
                        "[SYSTEM] The employee spoke and is waiting for your reply. Respond now, out loud: "
                        "if what they said doesn't answer your last question, confirm it in one short sentence.")
                except Exception as e:
                    self.log.warning(f"[WATCH] nudge failed: {e}")
            if self.state == State.GENERATING and quiet_s >= GEMINI_SILENCE_TIMEOUT_S:
                self.log.warning(f"[WATCH] Gemini silent for {quiet_s:.0f}s while GENERATING — disconnecting")
                if not self.outcome_source:
                    self.outcome = "session_failed"
                await self._hangup()
                break

    # ── Voicemail (outbound phone only) ────────────────────────────────────────

    def _check_voicemail(self, text: str) -> bool:
        if self.channel != "phone" or self._voicemail_detected:
            return False
        if (time.time() - self._call_start_time) > VOICEMAIL_DETECTION_WINDOW_S:
            return False
        self._caller_transcriptions.append(text)
        combined = " ".join(self._caller_transcriptions).lower()
        for phrase in VOICEMAIL_PHRASES:
            if phrase.lower() in combined:
                self.log.warning(f"[VOICEMAIL] Detected — matched phrase: \"{phrase}\" in \"{combined}\"")
                self._voicemail_detected = True
                self.outcome, self.outcome_source = "voicemail", "system"
                self._call_ending = True
                return True
        return False

    # ── Transcript ─────────────────────────────────────────────────────────────

    def _write_transcript_line(self, line: str):
        ts = datetime.now(IST).strftime("%H:%M:%S")
        entry = f"[{ts}]  {line}\n"
        self._transcript_lines.append(entry)
        if self._transcript:
            self._transcript.write(entry)
            self._transcript.flush()

    def _flush_transcript(self, interrupted: bool = False):
        user_text = "".join(self._stt_cur).strip()
        agent_text = "".join(self._tts_cur).strip()
        if user_text:
            self._write_transcript_line(f"EMPLOYEE: {user_text}")
        if agent_text:
            suffix = "  [interrupted]" if interrupted else ""
            self._write_transcript_line(f"AGENT   : {agent_text}{suffix}")
        self._stt_cur.clear()
        self._tts_cur.clear()
        self.turn_count += 1

    def _scripted_opening(self) -> str:
        if has_identity_check(self.employee):
            return spoken_line("opening", employee=self.employee)
        return (f'{spoken_line("opening_inbound", employee=self.employee)} '
                f'{spoken_line("core_question_inbound", employee=self.employee)}')

    # ── Main run ───────────────────────────────────────────────────────────────

    async def run(self, conn: LiveConnection | None = None):
        """conn: a Gemini Live connection opened (and greeting started) while the phone was
        ringing. Without one, connect now (web path, inbound, or no pre-warm)."""
        if not os.environ.get("GOOGLE_API_KEY"):
            self.log.error("GOOGLE_API_KEY not set")
            await self._close_ws()
            return
        self.log.info("━" * 65)
        self.log.info("  SGSP Salary Package Voice Agent")
        self.log.info(f"  Channel  : {self.channel}")
        self.log.info(f"  Model    : {GEMINI_MODEL}")
        self.log.info(f"  Language : {LANGUAGE_CODE} (switches on request)")
        self.log.info(f"  Employee : {self.employee.get('name') or 'unknown'} {self.phone or ''}")
        self.log.info("━" * 65)

        try:
            self._transcript = open(self.call_dir / "transcript.txt", "w", encoding="utf-8")
            self._transcript.write(
                f"SGSP Voice Agent — {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Channel: {self.channel} | Employee: {self.employee.get('name') or 'unknown'} | Phone: {self.phone or '-'}\n"
                + "━" * 60 + "\n\n"
            )
            prewarmed = conn is not None
            if conn is None:
                conn = await LiveConnection(self.employee, self.channel, TOOLS, self.log).open()
            else:
                self.log.info(
                    f"[LIVE]  Using pre-warmed Gemini session (opened {time.time() - conn.opened_at:.1f}s ago, "
                    f"greeting audio {'ready' if conn.first_audio.is_set() else 'pending'}, {conn.queue.qsize()} messages buffered)"
                )
            self._client = conn.client
            session = conn.session
            try:
                self._session = session
                self._last_gemini_msg_time = time.time()
                self.log.info(f"[WS]    Gemini Live ready ({'pre-warmed' if prewarmed else 'connected now'})")
                await self._send_event({"type": "ready"})

                tasks = [
                    asyncio.create_task(self._speaker_task(), name="speaker"),
                    asyncio.create_task(self._receiver_task(conn), name="receiver"),
                    asyncio.create_task(self._mic_sender_task(session), name="sender"),
                    asyncio.create_task(self._silence_watchdog_task(), name="watchdog"),
                    asyncio.create_task(self._no_response_watchdog_task(), name="no_response"),
                    asyncio.create_task(self._gemini_health_watchdog_task(), name="gem_watchdog"),
                    asyncio.create_task(self._kickoff_message(conn), name="kickoff"),
                ]
                try:
                    await self._read_client()
                finally:
                    self._end_mic()
                    if self._outcome_task and not self._outcome_task.done():
                        try:  # let the SMS finish — the employee has already gone
                            await asyncio.wait_for(asyncio.shield(self._outcome_task), timeout=SAVE_ON_EXIT_TIMEOUT_S)
                        except Exception:
                            pass
                    for task in tasks + self._background_tasks:
                        task.cancel()
                    try:
                        await asyncio.wait_for(asyncio.gather(*tasks, *self._background_tasks, return_exceptions=True), timeout=5.0)
                    except asyncio.TimeoutError:
                        self.log.warning("[WS]    Task cleanup timed out")
            finally:
                await conn.close()

        except Exception as e:
            if not self.outcome_source:
                self.outcome = "session_failed"
            self.log.error(f"[WS]    Error: {e}", exc_info=True)
            await self._send_event({"type": "error", "message": "Voice session failed"})
            await self._close_ws()
        finally:
            if self.outcome == "incomplete" and not self._employee_spoke:
                self.outcome = "hung_up"
            try:
                await asyncio.wait_for(self._backstop_outcome(), timeout=20.0)
            except Exception as e:
                self.log.warning(f"[OUTCOME] Backstop failed: {e}")
            if self.outcome == "incomplete":
                self.outcome = "hung_up"
            self._finalize()

    def result(self) -> dict:
        """What the campaign store / webhook gets for this call."""
        return {
            "outcome": self.outcome,
            "outcomeSource": self.outcome_source,
            "details": self.details,
            "sms": self.sms,
            "escalations": self.escalations,
            "channel": self.channel,
            "callSid": self.call_sid,
            "phone": self.phone,
            "employee": self.employee,
            "turns": self.turn_count,
            "durationSeconds": self.call_duration,
            "costUsd": self.call_cost,
            "callDir": self.call_dir.name,
            "transcript": "".join(self._transcript_lines),
        }

    def _finalize(self):
        if self._stt_cur or self._tts_cur:
            self._flush_transcript(interrupted=True)
        if self._transcript:
            self._transcript.write("━" * 60 + "\n")
            self._transcript.write(f"Ended — {self.turn_count} turns | outcome={self.outcome}\n")
            self._transcript.close()
            self._transcript = None

        mix_wav(self.call_dir / "full_call.wav", bytes(self._mic_pcm_all), bytes(self._resp_pcm_all), REC_RATE)
        self.call_duration = int(time.time() - self._call_start_time)

        t = self._total_tokens
        cost = (t["in_text"] / 1e6 * COST_IN_TEXT_PER_1M + t["in_audio"] / 1e6 * COST_IN_AUDIO_PER_1M
                + t["out_text"] / 1e6 * COST_OUT_TEXT_PER_1M + t["out_audio"] / 1e6 * COST_OUT_AUDIO_PER_1M)
        self.call_cost = round(cost, 4)
        self.log.info("─" * 60)
        self.log.info(f"[END]   {self.turn_count} turns | duration={self.call_duration}s | outcome={self.outcome} "
                      f"({self.outcome_source}) | dropped={self._dropped_qfull} gated={self._noise_gated}")
        self.log.info(f"[TOKENS] {t} | [COST] ${cost:.4f}")
        self.log.info("─" * 60)

        result = self.result()
        result.pop("transcript")
        (self.call_dir / "session.json").write_text(
            json.dumps({**result, "model": GEMINI_MODEL, "endReason": self.end_reason, "tokens": t},
                       ensure_ascii=False, indent=2), encoding="utf-8")


# =====================================================================================
# Phone — Exotel media stream
# =====================================================================================

class ExotelSession(SgspSession):
    channel = "phone"
    noise_gate_rms = NOISE_GATE_RMS_PHONE
    playout_margin_s = 1.0  # Exotel network/jitter buffer
    max_audio_lead_s = 1.0

    def __init__(self, ws: WebSocket, stream_sid: str, call_sid: str, log, run_ts: str,
                 call_dir: Path, phone: str, employee: dict | None, inbound: bool = False):
        super().__init__(ws, log, run_ts, call_dir, employee=employee, phone=phone, call_sid=call_sid)
        if inbound:
            self.channel = "inbound"  # employee dialled the ExoPhone — no voicemail check
        self.stream_sid = stream_sid
        self._up_state = None
        self._down_state = None

    async def _read_client(self):
        media_count = 0
        try:
            async for raw in self.ws.iter_text():
                msg = json.loads(raw)
                event = msg.get("event")
                if event == "media":
                    pcm_8k = base64.b64decode(msg["media"]["payload"])
                    pcm_16k, self._up_state = audioop.ratecv(pcm_8k, 2, 1, EXOTEL_RATE, MIC_RATE, self._up_state)
                    self._push_mic(pcm_16k)
                    media_count += 1
                elif event == "stop":
                    self.log.info("[EXOTEL] stop event received")
                    break
        except WebSocketDisconnect:
            self.log.info("[EXOTEL] WebSocket disconnected")
        except RuntimeError:
            self.log.info("[EXOTEL] WebSocket closed (RuntimeError)")
        finally:
            self.log.info(f"[EXOTEL] reader done — {media_count} frames")

    async def _send_audio(self, pcm_24k: bytes):
        pcm_8k, self._down_state = audioop.ratecv(pcm_24k, 2, 1, SPEAKER_RATE, EXOTEL_RATE, self._down_state)
        async with self._ws_lock:
            await self.ws.send_text(json.dumps({
                "event": "media",
                "stream_sid": self.stream_sid,
                "media": {"payload": base64.b64encode(pcm_8k).decode()},
            }))

    async def _send_clear(self):
        try:
            async with self._ws_lock:
                await self.ws.send_text(json.dumps({"event": "clear", "stream_sid": self.stream_sid}))
        except Exception as e:
            self.log.warning(f"[EXOTEL] clear failed: {e}")

    async def _hangup(self):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._hangup_exotel_call)
        await self._close_ws()

    def _hangup_exotel_call(self):
        sid = os.environ.get("EXOTEL_SID")
        api_key = os.environ.get("EXOTEL_API_KEY")
        api_token = os.environ.get("EXOTEL_API_TOKEN")
        domain = os.environ.get("EXOTEL_API_DOMAIN", "api.in.exotel.com")
        if not all([sid, api_key, api_token]) or not self.call_sid or self.call_sid.startswith("test-"):
            self.log.info("[HANGUP] No real Exotel call to terminate — closing stream only")
            return
        try:
            resp = requests.post(
                f"https://{domain}/v1/Accounts/{sid}/Calls/{self.call_sid}.json",
                auth=(api_key, api_token), data={"Status": "completed"}, timeout=10,
            )
            if resp.status_code == 403:
                # This Exotel API key may not end calls; closing the stream ends the Stream applet,
                # and the flow hangs up after it — so the call still ends.
                self.log.info("[HANGUP] Exotel refused the terminate request (403) — ending via stream close")
                return
            resp.raise_for_status()
            self.log.info(f"[HANGUP] Exotel call {self.call_sid} terminated")
        except Exception as e:
            self.log.error(f"[HANGUP] Failed to terminate call: {e}")


# =====================================================================================
# Web — browser test page
# =====================================================================================

class WebSession(SgspSession):
    channel = "web"
    noise_gate_rms = NOISE_GATE_RMS_WEB

    async def _read_client(self):
        frames = 0
        try:
            while True:
                msg = await self.ws.receive()
                if msg["type"] == "websocket.disconnect":
                    self.log.info("[WEB]   Browser disconnected")
                    break
                data = msg.get("bytes")
                if data:
                    if len(data) % 2:
                        data = data[:-1]
                    self._push_mic(data)
                    frames += 1
                    continue
                text = msg.get("text")
                if text:
                    try:
                        event = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "stop":
                        self.log.info("[WEB]   Tester ended the session")
                        break
        except WebSocketDisconnect:
            self.log.info("[WEB]   Browser disconnected")
        except RuntimeError:
            self.log.info("[WEB]   WebSocket closed (RuntimeError)")
        finally:
            self.log.info(f"[WEB]   reader done — {frames} frames")

    async def _send_audio(self, pcm_24k: bytes):
        async with self._ws_lock:
            await self.ws.send_bytes(pcm_24k)

    async def _send_clear(self):
        await self._send_event({"type": "clear"})

    async def _send_event(self, event: dict):
        try:
            async with self._ws_lock:
                await self.ws.send_text(json.dumps(event, ensure_ascii=False))
        except Exception:
            pass  # browser already gone

    async def _hangup(self):
        await self._send_event({"type": "end", "outcome": self.outcome})
        await self._close_ws()
