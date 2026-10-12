"""
Configuration — audio constants, Gemini settings, campaign / dialing rules, paths, environment.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# ── Paths & env ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


# ── Audio config ───────────────────────────────────────────────────────────────
MIC_RATE = 16000          # what Gemini expects as input (browser also sends 16k)
SPEAKER_RATE = 24000      # what Gemini outputs (browser plays 24k directly)
EXOTEL_RATE = 8000        # what Exotel sends/receives
REC_RATE = 16000          # full_call.wav rate — common to both phone and web
CHANNELS = 1

# ── Voice activity / timeouts ──────────────────────────────────────────────────
# Gemini's automatic VAD decides turn boundaries; these only gate what audio we forward.
NOISE_GATE_RMS_PHONE = int(os.environ.get("SGSP_NOISE_GATE_RMS_PHONE", "500"))
NOISE_GATE_RMS_WEB = int(os.environ.get("SGSP_NOISE_GATE_RMS_WEB", "200"))
# How long the employee must pause before Gemini treats their turn as finished. Unset →
# Gemini's default.
VAD_SILENCE_DURATION_MS = int(os.environ["SGSP_VAD_SILENCE_MS"]) if os.environ.get("SGSP_VAD_SILENCE_MS") else None
SEND_TIMEOUT_S = 2.0
# When the call ends, wait this long for an in-flight outcome save / SMS to finish.
SAVE_ON_EXIT_TIMEOUT_S = 30.0
# Caller audio buffering: queue up to ~10 s of 20 ms frames, send up to 200 ms per Gemini call.
MIC_QUEUE_MAX = 500
MIC_BATCH_MAX_CHUNKS = 10
SILENCE_TIMEOUT_S = 60    # hard backstop — the no-response prompts come first
# If the employee says nothing this long after the agent finishes speaking, prompt the
# "couldn't hear you" line (twice, then end the call).
NO_RESPONSE_PROMPT_S = float(os.environ.get("SGSP_NO_RESPONSE_PROMPT_S", "8"))
GEMINI_SILENCE_TIMEOUT_S = 20
# Before giving up on a silent model, nudge it once to answer the employee (call 49: it went
# silent after a bare "Hmm." and the watchdog hung up mid-call).
GEMINI_NUDGE_S = 7
# Resend the greeting instruction if no audio arrives this long after sending it.
KICKOFF_AUDIO_TIMEOUT_S = 8.0
# After an outcome tool hands back the ending line, wait up to this long for the agent to
# finish speaking it before ending the call anyway.
AUTO_END_TURN_TIMEOUT_S = 25.0

# ── Voicemail detection (phone only) ───────────────────────────────────────────
VOICEMAIL_DETECTION_WINDOW_S = 30.0
VOICEMAIL_PHRASES = [
    "not available", "not reachable", "cannot be completed",
    "leave a message", "leave your message", "after the tone",
    "after the beep", "voicemail", "voice mail", "mailbox",
    "switched off", "switch off", "currently unavailable",
    "busy on another call", "not answering",
    "please try again later", "try your call later",
    "out of coverage", "network busy",
    "the number you have dialed", "the person you are calling",
    "the subscriber you have called",
    "उपलब्ध नहीं", "संपर्क में नहीं", "कृपया बाद में कॉल करें",
    "व्यस्त है", "बंद है", "नेटवर्क व्यस्त",
    "ಲಭ್ಯವಿಲ್ಲ", "ಸಂಪರ್ಕಕ್ಕೆ ಸಿಗುತ್ತಿಲ್ಲ", "ದಯವಿಟ್ಟು ನಂತರ ಕರೆ ಮಾಡಿ",
]

# ── Gemini config ──────────────────────────────────────────────────────────────
GEMINI_MODEL = os.environ.get("SGSP_GEMINI_MODEL", "gemini-3.8-live")
VOICE_NAME = os.environ.get("SGSP_VOICE", "Charon")
# Speech-synthesis locale. The call still starts in Kannada (the prompt drives the language);
# en-IN keeps English turns in an Indian accent (same finding as KASP).
LANGUAGE_CODE = os.environ.get("SGSP_LANGUAGE_CODE", "en-IN")
# Playback speed of the agent's voice (pitch unchanged). 1.0 disables it.
SPEECH_SPEED = float(os.environ.get("SGSP_SPEECH_SPEED", "1.0"))
# Pause between the agent's sentences, in ms. 0 disables it.
PAUSE_MS = int(os.environ.get("SGSP_PAUSE_MS", "800"))

# ── Who the agent says it is (AI disclosure at the start of every call) ──
# V2 flow: "an AI assistant calling on behalf of the government of Karnataka". Spoken as
# "<AGENCY_KN> ಪರವಾಗಿ", so AGENCY_KN is the genitive form ("ಕರ್ನಾಟಕ ಸರ್ಕಾರದ").
AGENCY_KN = os.environ.get("SGSP_AGENCY_KN", "ಕರ್ನಾಟಕ ಸರ್ಕಾರದ")
AGENCY_EN = os.environ.get("SGSP_AGENCY_EN", "the Government of Karnataka")

# ── Exotel (phone mode) ────────────────────────────────────────────────────────
# SGSP-specific App Studio flow: Call Start → Stream → wss://<your-domain>/sgsp/stream
# Deliberately no fallback to EXOTEL_FLOW_URL — that flow streams to a different bot.
EXOTEL_FLOW_URL = os.environ.get("SGSP_EXOTEL_FLOW_URL", "")
# TRAI: government / service calls must come from a 160-series number — put it here.
EXOTEL_CALLER_ID = os.environ.get("SGSP_EXOTEL_CALLER_ID") or os.environ.get("EXOTEL_CALLER_ID", "")
EXOTEL_STATUS_CALLBACK_URL = os.environ.get("SGSP_EXOTEL_STATUS_CALLBACK_URL", "")

# Single-call endpoint (POST /sgsp/call): per-number limit.
CALL_RATE_LIMIT_MAX = int(os.environ.get("SGSP_CALL_RATE_LIMIT_MAX", "3"))
CALL_RATE_LIMIT_WINDOW_S = int(os.environ.get("SGSP_CALL_RATE_LIMIT_WINDOW_S", "600"))
# Open Gemini and generate the greeting while the phone rings (~3 s less dead air on answer).
PREWARM = _bool("SGSP_PREWARM", "true")
PREWARM_TTL_S = 120

# ── Campaign dispatcher (bulk calls) ───────────────────────────────────────────
CHANNEL_LIMIT = int(os.environ.get("SGSP_CHANNEL_LIMIT", "5"))          # concurrent calls
DISPATCH_INTERVAL_S = float(os.environ.get("SGSP_DISPATCH_INTERVAL_S", "2"))
CALL_HOURS_START = int(os.environ.get("SGSP_CALL_HOURS_START", "10"))    # IST, inclusive
CALL_HOURS_END = int(os.environ.get("SGSP_CALL_HOURS_END", "19"))        # IST, exclusive
CALL_ON_SUNDAYS = _bool("SGSP_CALL_ON_SUNDAYS", "false")
MAX_ATTEMPTS = int(os.environ.get("SGSP_MAX_ATTEMPTS", "3"))
# Minutes to wait before attempt 2, 3, … (the last value repeats).
RETRY_DELAYS_MIN = [int(x) for x in os.environ.get("SGSP_RETRY_DELAYS_MIN", "60,240").split(",") if x.strip()]
# A dialed call that never streams in (and gets no status callback) is given up after this.
DIAL_TIMEOUT_S = int(os.environ.get("SGSP_DIAL_TIMEOUT_S", "180"))
# A call stuck "in_call" longer than this is assumed dead.
IN_CALL_TIMEOUT_S = int(os.environ.get("SGSP_IN_CALL_TIMEOUT_S", "900"))

# ── SMS follow-up (V2: only when the employee agrees to visit the branch) ──────
# Off until a DLT-registered template and sender ID exist. While off, or when a send fails, the
# agent says it cannot send an SMS and gives the key step verbally instead.
SMS_ENABLED = _bool("SGSP_SMS_ENABLED", "false")
SMS_SENDER_ID = os.environ.get("SGSP_SMS_SENDER_ID", "")
SMS_DLT_ENTITY_ID = os.environ.get("SGSP_SMS_DLT_ENTITY_ID", "")
SMS_DLT_TEMPLATE_ID = os.environ.get("SGSP_SMS_DLT_TEMPLATE_ID", "")
# Must match the DLT template exactly. {name} is filled in.
SMS_TEMPLATE = os.environ.get(
    "SGSP_SMS_TEMPLATE",
    "Dear {name}, to join the State Government Salary Package (SGSP) visit the bank branch where "
    "your salary is credited with Aadhaar, PAN, latest pay slip and Govt employee ID card. "
    "Free accident insurance and other benefits at no cost. - Govt of Karnataka",
)

# ── Results ────────────────────────────────────────────────────────────────────
# When set, every finished call's result is POSTed here (e.g. HRMS / a reporting service).
WEBHOOK_URL = os.environ.get("SGSP_WEBHOOK_URL", "")
# When set, the campaign / dashboard endpoints require header X-Admin-Token: <value>.
# The uploads contain employee phone numbers (DPDP Act) — set this anywhere but localhost.
ADMIN_TOKEN = os.environ.get("SGSP_ADMIN_TOKEN", "")

# ── Costing (per 1M tokens, in USD) ───────────────────────────────────────────
COST_IN_AUDIO_PER_1M = float(os.environ.get("COST_LIVE_IN_AUDIO_PER_1M", "3.00"))
COST_IN_TEXT_PER_1M = float(os.environ.get("COST_LIVE_IN_TEXT_PER_1M", "0.75"))
COST_OUT_AUDIO_PER_1M = float(os.environ.get("COST_LIVE_OUT_AUDIO_PER_1M", "12.00"))
COST_OUT_TEXT_PER_1M = float(os.environ.get("COST_LIVE_OUT_TEXT_PER_1M", "4.50"))

# ── Directories ────────────────────────────────────────────────────────────────
LOGS_DIR = Path(os.environ.get("SGSP_LOGS_DIR", str(ROOT / "sgsp_logs")))
LOGS_DIR.mkdir(exist_ok=True)
DB_PATH = Path(os.environ.get("SGSP_DB_PATH", str(ROOT / "sgsp_data" / "sgsp.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
