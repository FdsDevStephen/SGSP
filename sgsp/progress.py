"""
Non-answer detection — a background check after each employee reply (same as kasp/progress.py).

The live model does not notice when it is stuck (in KASP a caller who only said "thank you" got
the same question 14+ times, reworded each time). After every reply a small fast model judges
whether it was usable; the session counts consecutive non-answers and forces the "unclear"
ending at the limit. It runs in parallel with the conversation, so it adds no latency.
"""

import os

from google import genai
from google.genai import types

PROGRESS_MODEL = os.environ.get("SGSP_PROGRESS_MODEL", "gemini-3.1-flash-lite")
NON_ANSWER_LIMIT = int(os.environ.get("SGSP_NON_ANSWER_LIMIT", "3"))

_PROMPT = """A phone agent asked a government employee a question. Decide whether the reply is a USABLE
response: it answers the question (including "I don't know", yes/no, "later", "not interested"),
asks a question back, or makes a clear request (e.g. switch language, repeat, call later, stop).
Reply NO only if it carries nothing usable — pleasantries like "thank you"/"okay" alone, greetings,
or unintelligible fragments.
Agent: {question}
Employee: {reply}
Answer with exactly YES or NO."""


async def is_usable_reply(client: genai.Client, question: str, reply: str) -> bool | None:
    """True / False, or None if the check itself failed (treated as no information)."""
    try:
        resp = await client.aio.models.generate_content(
            model=PROGRESS_MODEL,
            contents=_PROMPT.format(question=question.strip(), reply=reply.strip()),
            config=types.GenerateContentConfig(temperature=0, max_output_tokens=5),
        )
    except Exception:
        return None
    verdict = (resp.text or "").strip().upper()
    if verdict.startswith("YES"):
        return True
    if verdict.startswith("NO"):
        return False
    return None


def non_answer_instruction() -> str:
    return (f"[SYSTEM] The employee has given {NON_ANSWER_LIMIT} replies in a row that do not answer "
            "your questions. Stop asking. Call record_outcome with outcome 'unclear' now, speak the "
            "line it returns, then end the call.")
