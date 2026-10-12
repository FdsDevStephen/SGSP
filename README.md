# SGSP Salary Package Voice Agent

Outbound Gemini Live voice agent that calls Karnataka State Government employees who have not yet
registered their salary account under the **State Government Salary Package (SGSP)**. It confirms
the employee, asks whether they are registered, and, if they are not and are willing, explains the
benefits, the branch visit and the documents. It speaks Kannada and switches to English on request.

> **Source of truth:** `SGSP Voice AI Conversation Flow V2.pdf` (flow, Kannada / English lines,
> guardrails, escalation triggers). Every script line lives in `LINES` in `prompts.py`, marked
> `[V2]`. Lines V2 does not cover are marked `[ASSUMED]`. Benefit claims are marked `[TO VERIFY]`.

It was split out of `Kaams-outbound-service` and is built on the KASP agent's architecture: the same Gemini Live audio bridge, the
pre-warmed greeting, "tool first, then speak" endings and code-level guards. It adds a bulk
**campaign** layer, because the target is about 2.4 lakh employees from HRMS.

```
HRMS .xlsx/.csv ─► POST /sgsp/campaigns ─► SQLite (sgsp_data/sgsp.db)
                                                 │
                     dispatcher (calling hours, channel limit, retries)
                                                 │  pre-warm Gemini + Exotel connect
                                                 ▼
             Exotel "Stream" applet ─► WS /sgsp/stream ─► ExotelSession ⇄ Gemini Live
                                                 │
            record_outcome → SMS (will_visit only) → closing line → hang up → store / webhook / CSV report
```

| File | Role |
|------|------|
| `app.py` | Endpoints, Exotel dialing, pre-warm, stream handler, campaign upload / report |
| `dispatcher.py` | Background loop: calling hours (IST), `SGSP_CHANNEL_LIMIT`, claims due calls |
| `store.py` | SQLite: campaigns, one row per employee, retries, do-not-call list |
| `session.py` | The conversation core (copied from KASP): audio bridge, state machine, barge-in, tools, guards |
| `prompts.py` | Script lines (Kannada / English), scheme facts, system prompt |
| `live.py` | Gemini Live connection that can be opened while the phone rings |
| `progress.py` | Background "was that a usable answer?" check, which forces the unclear ending after 3 non-answers |
| `extract.py` | Classifies calls the agent never recorded (hang-up mid-call) from the transcript |
| `sms.py` | Exotel SMS with the process and documents, for `will_visit` (off until DLT is approved) |
| `static/dashboard.html` | Upload, progress, pause/resume, CSV download |
| `static/web_agent.html` | Talk to the agent in the browser, or have it call your phone |

## The call

1. **Opening + identity:** "ನಮಸ್ಕಾರ. ನಾನು ಕರ್ನಾಟಕ ಸರ್ಕಾರದಿಂದ ರಾಜ್ಯ ಸರ್ಕಾರ ವೇತನ ಪ್ಯಾಕೇಜ್ ಬಗ್ಗೆ ಮಾತಾಡೋಕೆ ಕರೆ ಮಾಡಿದ್ದೀನಿ. ನಾನು {name} ಅವರ ಜೊತೆ ಮಾತಾಡ್ತಿದ್ದೀನಾ?" The agent never calls itself an AI / AI assistant / ಸಹಾಯಕ; asked whether it is a human, it says this is an automated call.
   - The answers below are handled at this step:

     | Answer | Outcome |
     |--------|---------|
     | Wrong person | `wrong_person`: "ಕ್ಷಮಿಸಿ, ತಪ್ಪಾದ ಸಂಖ್ಯೆಗೆ ಕರೆ ಮಾಡಿದ್ದೀನಿ." Nothing about the scheme or the employee is said. |
     | Someone else answers | `not_available` (retried) |
     | Busy or driving | `busy_callback` (retried) |
     | "Hello?" / "ಯಾರು?" | the introduction is repeated (never the name question — it is asked once per call) |
   - The wording of the disclosure is set by `SGSP_AGENCY_KN` / `SGSP_AGENCY_EN`.
2. **SGSP status:** "ಧನ್ಯವಾದ. ಇದು ನಿಮ್ಮ ವೇತನ ಖಾತೆಗೆ ಸಂಬಂಧಿಸಿದೆ. ನಿಮ್ಮ ಖಾತೆ ರಾಜ್ಯ ಸರ್ಕಾರ ವೇತನ ಯೋಜನೆಯಲ್ಲಿ ನೋಂದಣಿ ಆಗಿದ್ಯಾ?" (asked once; skipped if already told)
   - Yes → `already_enrolled`.
   - No → step 3.
   - Not sure → recorded as `NOT_SURE`; the agent asks permission first, and continues only on yes.
3. **Benefits + consent** (one turn): "ನೋಂದಣಿ ಮಾಡಿಸಿಕೊಳ್ಳೋಕೆ ಇದೇ ವಾರ ನಿಮ್ಮ ಹತ್ತಿರದ ಬ್ಯಾಂಕ್ ಶಾಖೆಗೆ ಹೋಗ್ತೀರಾ?"
4. **Answer:**
   - Yes → `will_visit`: the closing starts "ಬ್ಯಾಂಕ್ ಶಾಖೆಗೆ ಹೋಗುವಾಗ ದಯವಿಟ್ಟು ನಿಮ್ಮ ಆಧಾರ್, ಪ್ಯಾನ್, ವೇತನ ಚೀಟಿ ಮತ್ತು ಸರ್ಕಾರದ ಗುರುತಿನ ಚೀಟಿಯನ್ನ ಜೊತೇಲಿ ತಗೊಂಡು ಹೋಗಿ." then the SMS update. The SMS is sent *before* the closing line; if it is off or fails, the agent gives the key step verbally instead and does not mention SMS.
   - No or no time → `not_now`: one short line, then close. No SMS.
   - "Call me later" → `busy_callback` with `callback_time`.
   - Clear refusal, or asks to end the call at any point → `declined` (consent-led).
   - "Don't call me" → `do_not_call`, and the number goes on the do-not-call list.
   - "Can you register me now?" → this call cannot register; registration is at the branch.

The agent also handles these situations at any point in the call:

- **Fraud worry:** it gives the "we never ask for OTP / PIN / account details" line.
- **Questions:** it answers only from `SCHEME_FACTS`. Anything bank-specific gets "the branch will give you the details".
- **Escalations,** which are recorded for an officer to follow up:
  - salary or HR grievance (it points to the office DDO), then continues
  - guaranteed eligibility / legal interpretation / policy exception (`out_of_scope`, V2's limited-role line), then continues
  - "how did you get my number" / consent dispute (`privacy_objection`)
  - fraud or unauthorised transactions on their account (`fraud_report`): contact your bank now, then the call ends
  - fear, harassment, threat or distress (`distress`), then the call ends
  - human or supervisor requested (`human_requested`): the request is recorded, then the call ends
- **Corrections:** a changed answer (e.g. "no" → "already registered") replaces the earlier one.
- **Language barrier:** after a retry, the agent offers the other language once.
- **Silence:** after 2 silent turns → `no_response`.
- **Unintelligible replies:** `unclear`.

**Tool first, then speak**, the same as KASP:
1. The agent calls `record_outcome`.
2. The result carries `say_now`, the exact closing line in the call's language.
3. The agent speaks it.
4. The session hangs up once the line has played, even if the model never calls `end_call`.

**Code-level safeguards** (in `session.py`):
- A `record_outcome` sent in the same turn as the agent's own question is rejected.
- `end_call` is deferred until the closing line has been spoken.
- The same question asked 3 times, or 3 non-answers in a row, forces the `unclear` ending.
- The no-response prompts are timed by the session (`SGSP_NO_RESPONSE_PROMPT_S`).
- Voicemail is detected from the transcription and hung up on (then retried).
- If the call ends without an outcome, it is classified from the transcript (`outcomeSource: "transcript"`).
- If the model leaves out the bank the employee named, `bank_name` is filled from the transcript.

## Campaigns

Upload on the dashboard (<http://localhost:8004/sgsp/dashboard>) or with `POST /sgsp/campaigns`.

- **Required columns** (first row; the names are flexible): `phone` / `mobile`, `name`.
- **Optional columns:** `employee_id` / `kgid`, `gender`.
- **Rows that are rejected:** invalid numbers, duplicates and rows with a missing name.

| Outcome | What happens next |
|---------|-------------------|
| `already_enrolled`, `will_visit`, `not_now`, `declined`, `wrong_person`, `escalated` | Row is `done` |
| `do_not_call` | Row is `done`, and the number is skipped in every campaign from then on |
| `not_available`, `busy_callback`, `no_response`, `unclear`, `voicemail`, `hung_up`, Exotel busy / no-answer / failed | Retried after `SGSP_RETRY_DELAYS_MIN`, up to `SGSP_MAX_ATTEMPTS`, then `failed` |

- Calls are placed only between `SGSP_CALL_HOURS_START` and `SGSP_CALL_HOURS_END` IST (default 10–19, no Sundays).
- At most `SGSP_CHANNEL_LIMIT` calls run at once.
- Busy / no-answer retries need `SGSP_EXOTEL_STATUS_CALLBACK_URL`. Without it, a call that never streams in is given up after `SGSP_DIAL_TIMEOUT_S`.
- An employee who calls the ExoPhone back gets the inbound opening (no name check). The call is logged against their number.

`GET /sgsp/campaigns/{id}/report.csv` gives one row per employee with these columns:

- `outcome`
- `enrollment_status`
- `bank_name`
- `objection`
- `employee_questions`
- `sms_sent`
- `escalations`
- `attempts`
- duration and cost
- log folder

## Run locally

```bash
uvicorn sgsp.app:app --host 0.0.0.0 --port 8004 --reload
```

- **Browser test:** open <http://localhost:8004/sgsp/web>, type an employee name, and click "Start talking".
- **Phone path, without dialing:** `python test_sgsp_call.py 98xxxxxxxx "Ravi Kumar" male`. It simulates an Exotel stream using your mic and speakers.
- **Real calls:**
  1. Set up Exotel (below).
  2. Run `ngrok http 8004`.
  3. Upload a campaign, or `POST /sgsp/call {"phone", "name"}`.
- **Flow compliance tests:** `python -m sgsp.test.run_flow_scenarios`. This runs 28 scenarios covering every branch (including V2's minimum branch test set), with a simulated employee and a judge model. The report goes to `sgsp_logs/flow_tests/<ts>/report.md`. Rerun it after every change to `prompts.py`.
- Set `SGSP_DISPATCHER=false` to run the API without the dialing loop.

Each call writes these files to `sgsp_logs/<timestamp>_<phone>_<callId>/`:
- `call.log`
- `transcript.txt`
- `full_call.wav`
- `session.json` (outcome, details, SMS, escalations, tokens, cost)

## Exotel setup

1. In App Studio, create a flow: Call Start → Stream → `wss://<domain>/sgsp/stream`.
2. Set these in `.env`:
   - `SGSP_EXOTEL_FLOW_URL`
   - `SGSP_EXOTEL_CALLER_ID`: the **160-series** number (TRAI)
   - `SGSP_EXOTEL_STATUS_CALLBACK_URL=https://<domain>/sgsp/call-status`

   The shared `EXOTEL_SID`, `EXOTEL_API_KEY` and `EXOTEL_API_TOKEN` are reused.
3. Set `SGSP_ADMIN_TOKEN` anywhere but localhost. Uploads and reports contain employee phone numbers (DPDP Act).

## Open items for the review meeting (agenda item j)

- **Benefit wording:** confirm every claim in `LINES["benefits"]` and `SCHEME_FACTS` with Finance. V2 states term (life) insurance for everyone, where the earlier note said "in some banks".
- **`[ASSUMED]` lines:** the unsure-permission question, the escalation lines (fraud, distress, human callback) and the language offer are not in V2 and need sign-off.
- **Consent (DPDP):** the call records whether the employee continued after the disclosure. It honours objections (`privacy_objection` escalation → `do_not_call`). The data-source line ("your number was taken from HRMS…") needs Law Department approval. No explicit consent question is asked yet.
- **SMS:** needs a DLT-registered sender ID and template. Then set `SGSP_SMS_ENABLED=true` and `SGSP_SMS_TEMPLATE` to the exact approved text. Until then, the `will_visit` closing gives the key steps verbally and does not mention SMS. The template's document list should match V2 (Aadhaar, PAN, salary slip, government ID card). WhatsApp is not wired.
- **Escalations:** recorded in the report and the webhook only. There is no live transfer or callback queue.
- **Cost:** `session.json` / the report carry the per-call Gemini cost, as input to agenda item (i).

## Project layout (standalone)

```
sgsp-voice-agent/
├── sgsp/                 the agent (FastAPI app, prompts, session, dialer, store, static pages)
│   └── test/             flow compliance tests (python -m sgsp.test.run_flow_scenarios)
├── test_sgsp_call.py     local phone-path test client (mic/speakers, no dialing)
├── sgsp_logs/            per-call logs, transcripts, recordings (git-ignored)
├── sgsp_data/            SQLite: campaigns, calls, do-not-call list (git-ignored)
├── requirements.txt      runtime dependencies (requirements-dev.txt adds the test-client deps)
├── Dockerfile, docker-compose.yml, .dockerignore
└── .env.example          copy to .env
```

Setup:
```bash
python -m venv .venv
.venv\Scripts\activate            # Windows (source .venv/bin/activate on Linux/macOS)
pip install -r requirements-dev.txt
copy .env.example .env             # fill in the keys
uvicorn sgsp.app:app --host 0.0.0.0 --port 8004
```

Docker: `docker compose up -d --build`
