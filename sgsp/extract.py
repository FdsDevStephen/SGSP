"""
Classify a call from its transcript when the agent never recorded an outcome — the employee
hung up mid-call, the connection dropped, or the model ended without calling record_outcome.
Every answered call should still land in the campaign report with its best-known outcome.
"""

import json
import os

from google import genai
from google.genai import types

EXTRACT_MODEL = os.environ.get("SGSP_EXTRACT_MODEL", "gemini-3.8-flash")

_PROMPT = """Below is a transcript of a call in which an AI agent told a Karnataka government employee about
the State Government Salary Package (SGSP). The call ended before the agent recorded a result.
Using ONLY what the EMPLOYEE actually said, return JSON with:
- "outcome": one of {outcomes} — the best fit for how far the call got; use "hung_up" if the
  employee never gave a usable answer before the call ended.
- "enrollment_status": one of ENROLLED, NOT_ENROLLED, NOT_SURE, UNKNOWN.
- "bank_name": the bank, only if the employee named it, else null.
- "notes": one short English sentence on what the employee said, or null.

Transcript:
<<<
{transcript}
>>>"""


async def classify_transcript(client: genai.Client, transcript: str, outcomes: list[str]) -> dict:
    resp = await client.aio.models.generate_content(
        model=EXTRACT_MODEL,
        contents=_PROMPT.format(outcomes=", ".join(outcomes + ["hung_up"]), transcript=transcript),
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0),
    )
    data = json.loads(resp.text or "{}")
    if data.get("outcome") not in outcomes + ["hung_up"]:
        data["outcome"] = "hung_up"
    return data
