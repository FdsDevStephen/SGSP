"""
SMS follow-up (agenda annexure step 4): the documents to take to the bank.

Sent through Exotel's SMS API once a DLT-registered sender ID and template exist
(SGSP_SMS_ENABLED=true). Indian commercial SMS is rejected unless the body matches the DLT
template exactly, so SGSP_SMS_TEMPLATE must be the approved text. WhatsApp is not wired yet.
"""

import os

import requests

from .config import SMS_DLT_ENTITY_ID, SMS_DLT_TEMPLATE_ID, SMS_ENABLED, SMS_SENDER_ID, SMS_TEMPLATE


def render_sms(employee: dict | None) -> str:
    return SMS_TEMPLATE.replace("{name}", (employee or {}).get("name") or "Sir/Madam")


def send_sms(to_number: str, body: str) -> dict:
    """Blocking — call from an executor. Returns {"sent": bool, ...}."""
    if not SMS_ENABLED:
        return {"sent": False, "reason": "disabled"}
    sid = os.environ.get("EXOTEL_SID")
    api_key = os.environ.get("EXOTEL_API_KEY")
    api_token = os.environ.get("EXOTEL_API_TOKEN")
    domain = os.environ.get("EXOTEL_API_DOMAIN", "api.in.exotel.com")
    if not all([sid, api_key, api_token, SMS_SENDER_ID]):
        return {"sent": False, "reason": "Exotel SMS not configured (EXOTEL_* / SGSP_SMS_SENDER_ID)"}
    data = {"From": SMS_SENDER_ID, "To": to_number, "Body": body, "SmsType": "transactional"}
    if SMS_DLT_ENTITY_ID:
        data["DltEntityId"] = SMS_DLT_ENTITY_ID
    if SMS_DLT_TEMPLATE_ID:
        data["DltTemplateId"] = SMS_DLT_TEMPLATE_ID
    if any(ord(ch) > 127 for ch in body):
        data["EncodingType"] = "unicode"  # Kannada template
    resp = requests.post(
        f"https://{domain}/v1/Accounts/{sid}/Sms/send.json",
        auth=(api_key, api_token), data=data, timeout=15,
    )
    resp.raise_for_status()
    sms = resp.json().get("SMSMessage", {})
    return {"sent": True, "sid": sms.get("Sid"), "status": sms.get("Status")}
