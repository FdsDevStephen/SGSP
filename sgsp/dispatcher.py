"""
Campaign dispatcher — background loop that dials due employees from the store.

Every SGSP_DISPATCH_INTERVAL_S: give up on stale rows, and, inside calling hours (IST), claim
as many due rows as there are free channels (SGSP_CHANNEL_LIMIT counts every live call,
including single test calls) and hand each one to app.dial_employee.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from . import store
from .config import CALL_HOURS_END, CALL_HOURS_START, CALL_ON_SUNDAYS, CHANNEL_LIMIT, DISPATCH_INTERVAL_S

log = logging.getLogger("sgsp.dispatcher")
IST = timezone(timedelta(hours=5, minutes=30))


def within_calling_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now(IST)
    if now.weekday() == 6 and not CALL_ON_SUNDAYS:
        return False
    return CALL_HOURS_START <= now.hour < CALL_HOURS_END


async def dispatcher_loop(dial):
    """dial: async fn(call_row) — places the call; must mark the row failed itself on error."""
    log.info(f"[DISPATCH] started — {CHANNEL_LIMIT} channels, calling hours {CALL_HOURS_START}:00–{CALL_HOURS_END}:00 IST")
    was_open = None
    while True:
        try:
            await asyncio.sleep(DISPATCH_INTERVAL_S)
            for call_id in store.reap_stale():
                log.info(f"[DISPATCH] call {call_id} timed out — rescheduled")
            is_open = within_calling_hours()
            if is_open != was_open:
                log.info(f"[DISPATCH] calling hours {'open' if is_open else 'closed'}")
                was_open = is_open
            if not is_open:
                continue
            for row in store.claim_due_calls(CHANNEL_LIMIT - store.active_call_count()):
                log.info(f"[DISPATCH] dialing call {row['id']} (campaign {row['campaign_id']}, attempt {row['attempts']})")
                asyncio.create_task(dial(row))
        except asyncio.CancelledError:
            log.info("[DISPATCH] stopped")
            return
        except Exception as e:
            log.error(f"[DISPATCH] loop error: {e}", exc_info=True)
