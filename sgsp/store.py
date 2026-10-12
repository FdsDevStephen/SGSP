"""
Campaign store — SQLite (stdlib, one file at SGSP_DB_PATH), so bulk campaigns survive restarts
without extra infrastructure. All access goes through one connection guarded by a lock; every
query is small, so it is called straight from the event loop.

Call row lifecycle:
    pending ──claim──► dialing ──stream──► in_call ──finish──► done
       ▲                  │                    │
       └──── retry ◄──────┴── no answer / busy ┴── retryable outcome (not_available, no_response, …)
    retry rows past SGSP_MAX_ATTEMPTS → failed;  numbers on the do-not-call list → skipped
"""

import json
import sqlite3
import threading
import time

from .config import DB_PATH, DIAL_TIMEOUT_S, IN_CALL_TIMEOUT_S, MAX_ATTEMPTS, RETRY_DELAYS_MIN

# Outcomes worth another attempt — the employee never really heard the message.
RETRY_OUTCOMES = {
    "not_available", "busy_callback", "no_response", "unclear", "voicemail",
    "hung_up", "silence_timeout", "session_failed", "no_answer", "busy", "failed", "canceled", "no_stream",
}
ACTIVE_STATUSES = ("dialing", "in_call")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    file_name   TEXT,
    status      TEXT NOT NULL DEFAULT 'active',      -- active | paused
    total       INTEGER NOT NULL DEFAULT 0,
    rejected    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS calls (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id       INTEGER REFERENCES campaigns(id),   -- NULL: single / inbound call
    phone             TEXT NOT NULL,
    name              TEXT,
    employee_id       TEXT,
    gender            TEXT,
    status            TEXT NOT NULL DEFAULT 'pending',
    attempts          INTEGER NOT NULL DEFAULT 0,
    next_attempt_at   REAL NOT NULL DEFAULT 0,
    call_sid          TEXT,
    dialed_at         REAL,
    outcome           TEXT,
    outcome_source    TEXT,
    enrollment_status TEXT,
    details           TEXT,
    sms_sent          INTEGER,
    escalations       TEXT,
    call_dir          TEXT,
    last_error        TEXT,
    updated_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calls_due ON calls(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_calls_phone ON calls(phone);
CREATE INDEX IF NOT EXISTS idx_calls_sid ON calls(call_sid);
CREATE TABLE IF NOT EXISTS dnc (
    phone     TEXT PRIMARY KEY,
    reason    TEXT,
    added_at  REAL NOT NULL
);
"""

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


def init():
    global _conn
    if _conn:
        return
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.executescript(_SCHEMA)


def _q(sql: str, params=()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _conn.execute(sql, params).fetchall()]


def _x(sql: str, params=()) -> int:
    with _lock:
        cur = _conn.execute(sql, params)
        return cur.lastrowid


def _retry_delay_s(attempts: int) -> float:
    delays = RETRY_DELAYS_MIN or [60]
    return delays[min(attempts - 1, len(delays) - 1)] * 60


# ── Campaigns ──────────────────────────────────────────────────────────────────

def create_campaign(name: str, file_name: str, rows: list[dict], rejected: int) -> int:
    """rows: validated {"phone", "name", "employee_id"?, "gender"?}. Numbers on the do-not-call
    list are stored as skipped so the report shows them."""
    now = time.time()
    dnc = {r["phone"] for r in _q("SELECT phone FROM dnc")}
    with _lock:
        _conn.execute("BEGIN")
        try:
            cid = _conn.execute(
                "INSERT INTO campaigns (name, file_name, total, rejected, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, file_name, len(rows), rejected, now)).lastrowid
            _conn.executemany(
                "INSERT INTO calls (campaign_id, phone, name, employee_id, gender, status, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(cid, r["phone"], r.get("name"), r.get("employee_id"), r.get("gender"),
                  "skipped" if r["phone"] in dnc else "pending", now) for r in rows])
            _conn.execute("COMMIT")
        except Exception:
            _conn.execute("ROLLBACK")
            raise
    return cid


def set_campaign_status(campaign_id: int, status: str) -> bool:
    with _lock:
        return _conn.execute("UPDATE campaigns SET status=? WHERE id=?", (status, campaign_id)).rowcount > 0


def campaign_stats(campaign_id: int) -> dict | None:
    rows = _q("SELECT * FROM campaigns WHERE id=?", (campaign_id,))
    if not rows:
        return None
    c = rows[0]
    by_status = {r["status"]: r["n"] for r in _q(
        "SELECT status, COUNT(*) n FROM calls WHERE campaign_id=? GROUP BY status", (campaign_id,))}
    by_outcome = {r["outcome"]: r["n"] for r in _q(
        "SELECT outcome, COUNT(*) n FROM calls WHERE campaign_id=? AND status IN ('done','failed') "
        "AND outcome IS NOT NULL GROUP BY outcome", (campaign_id,))}
    cost = _q("SELECT COALESCE(SUM(json_extract(details, '$.costUsd')), 0) c FROM calls WHERE campaign_id=?",
              (campaign_id,))[0]["c"]
    open_rows = sum(by_status.get(s, 0) for s in ("pending", "retry", "dialing", "in_call"))
    return {
        **c,
        "state": "finished" if open_rows == 0 else c["status"],
        "byStatus": by_status,
        "byOutcome": by_outcome,
        "costUsd": round(cost or 0, 2),
    }


def list_campaigns() -> list[dict]:
    return [campaign_stats(r["id"]) for r in _q("SELECT id FROM campaigns ORDER BY id DESC")]


def campaign_calls(campaign_id: int) -> list[dict]:
    return _q("SELECT * FROM calls WHERE campaign_id=? ORDER BY id", (campaign_id,))


# ── Dispatching ────────────────────────────────────────────────────────────────

def active_call_count() -> int:
    return _q(f"SELECT COUNT(*) n FROM calls WHERE status IN {ACTIVE_STATUSES}")[0]["n"]


def claim_due_calls(limit: int) -> list[dict]:
    """Atomically move up to `limit` due rows of active campaigns to 'dialing'. A number that is
    already on a call is skipped this round."""
    if limit <= 0:
        return []
    now = time.time()
    with _lock:
        _conn.execute("BEGIN IMMEDIATE")
        try:
            rows = [dict(r) for r in _conn.execute(
                f"""SELECT calls.* FROM calls JOIN campaigns ON campaigns.id = calls.campaign_id
                    WHERE campaigns.status='active' AND calls.status IN ('pending','retry')
                      AND calls.next_attempt_at <= ?
                      AND calls.phone NOT IN (SELECT phone FROM calls WHERE status IN {ACTIVE_STATUSES})
                      AND calls.phone NOT IN (SELECT phone FROM dnc)
                    ORDER BY calls.attempts, calls.id LIMIT ?""", (now, limit * 3)).fetchall()]
            claimed, phones = [], set()
            for r in rows:
                if r["phone"] in phones:
                    continue
                phones.add(r["phone"])
                _conn.execute("UPDATE calls SET status='dialing', attempts=attempts+1, dialed_at=?, call_sid=NULL, "
                              "updated_at=? WHERE id=?", (now, now, r["id"]))
                claimed.append({**r, "attempts": r["attempts"] + 1})
                if len(claimed) == limit:
                    break
            _conn.execute("COMMIT")
        except Exception:
            _conn.execute("ROLLBACK")
            raise
    return claimed


def create_direct_call(phone: str, name: str | None, employee_id: str | None, gender: str | None,
                       status: str = "dialing") -> int:
    """A single call (POST /sgsp/call) or an inbound call — no campaign."""
    now = time.time()
    return _x("INSERT INTO calls (campaign_id, phone, name, employee_id, gender, status, attempts, dialed_at, updated_at) "
              "VALUES (NULL, ?, ?, ?, ?, ?, 1, ?, ?)", (phone, name, employee_id, gender, status, now, now))


def get_call(call_id: int) -> dict | None:
    rows = _q("SELECT * FROM calls WHERE id=?", (call_id,))
    return rows[0] if rows else None


def find_call_by_sid(call_sid: str) -> dict | None:
    rows = _q("SELECT * FROM calls WHERE call_sid=? ORDER BY id DESC LIMIT 1", (call_sid,))
    return rows[0] if rows else None


def latest_employee_for_phone(phone: str) -> dict | None:
    """For an inbound call: the employee this number belongs to, from any campaign."""
    rows = _q("SELECT name, employee_id, gender FROM calls WHERE phone=? AND campaign_id IS NOT NULL "
              "ORDER BY id DESC LIMIT 1", (phone,))
    return rows[0] if rows else None


def set_call_sid(call_id: int, call_sid: str):
    _x("UPDATE calls SET call_sid=?, updated_at=? WHERE id=?", (call_sid, time.time(), call_id))


def mark_in_call(call_id: int):
    _x("UPDATE calls SET status='in_call', updated_at=? WHERE id=?", (time.time(), call_id))


def _schedule(call: dict, outcome: str, error: str | None = None) -> str:
    """Retry or close a row after an attempt that ended with `outcome`."""
    now = time.time()
    if outcome in RETRY_OUTCOMES and call["campaign_id"] and call["attempts"] < MAX_ATTEMPTS:
        status, next_at = "retry", now + _retry_delay_s(call["attempts"])
    else:
        status, next_at = ("failed" if outcome in RETRY_OUTCOMES else "done"), call["next_attempt_at"]
    _x("UPDATE calls SET status=?, next_attempt_at=?, outcome=?, last_error=COALESCE(?, last_error), updated_at=? WHERE id=?",
       (status, next_at, outcome, error, now, call["id"]))
    return status


def dial_failed(call_id: int, reason: str) -> str | None:
    """The call never connected (busy, no answer, Exotel error). Only acts on rows still dialing."""
    call = get_call(call_id)
    if not call or call["status"] != "dialing":
        return None
    outcome = {"no-answer": "no_answer"}.get(reason, reason if reason in RETRY_OUTCOMES else "failed")
    return _schedule(call, outcome, error=reason)


def finish_call(call_id: int, result: dict) -> str | None:
    call = get_call(call_id)
    if not call:
        return None
    details = {**(result.get("details") or {}), "turns": result.get("turns"),
               "durationSeconds": result.get("durationSeconds"), "costUsd": result.get("costUsd")}
    _x("UPDATE calls SET outcome_source=?, enrollment_status=?, details=?, sms_sent=?, escalations=?, call_dir=?, "
       "call_sid=COALESCE(call_sid, ?), updated_at=? WHERE id=?",
       (result.get("outcomeSource"), (result.get("details") or {}).get("enrollment_status"),
        json.dumps(details, ensure_ascii=False), int(bool((result.get("sms") or {}).get("sent"))),
        json.dumps(result.get("escalations") or [], ensure_ascii=False), result.get("callDir"),
        result.get("callSid"), time.time(), call_id))
    if result.get("outcome") == "do_not_call":
        add_dnc(call["phone"], "requested on call")
    return _schedule(call, result.get("outcome") or "hung_up")


def reap_stale() -> list[int]:
    """Dialed rows that never streamed in, and in-call rows that never finished."""
    now = time.time()
    reaped = []
    for r in _q("SELECT id FROM calls WHERE status='dialing' AND dialed_at < ?", (now - DIAL_TIMEOUT_S,)):
        dial_failed(r["id"], "no_stream")
        reaped.append(r["id"])
    for r in _q("SELECT * FROM calls WHERE status='in_call' AND updated_at < ?", (now - IN_CALL_TIMEOUT_S,)):
        _schedule(r, "session_failed", error="stuck in_call")
        reaped.append(r["id"])
    return reaped


def recover_after_restart():
    """Rows left dialing / in_call by a previous process can never finish — retry them."""
    for r in _q(f"SELECT * FROM calls WHERE status IN {ACTIVE_STATUSES}"):
        _schedule(r, "session_failed", error="server restarted")


# ── Do-not-call list ───────────────────────────────────────────────────────────

def add_dnc(phone: str, reason: str):
    _x("INSERT OR IGNORE INTO dnc (phone, reason, added_at) VALUES (?, ?, ?)", (phone, reason, time.time()))
    _x("UPDATE calls SET status='skipped', updated_at=? WHERE phone=? AND status IN ('pending','retry')",
       (time.time(), phone))


def is_dnc(phone: str) -> bool:
    return bool(_q("SELECT 1 FROM dnc WHERE phone=?", (phone,)))
