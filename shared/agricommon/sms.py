"""Outbound SMS through Africa's Talking (sandbox by default).

SMS_MODE:
  log      - do not call the provider; log the message (CI, cost-free)
  outbox   - mock provider: store the message in DynamoDB (SMSOUT#<phone>) so the
             feature-phone simulator and scripts/sms_outbox.py can display it
  sandbox  - api.sandbox.africastalking.com (messages appear in the AT simulator)
  live     - api.africastalking.com (charged per message; needs a live account)
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

from aws_lambda_powertools import Logger

from . import config, text

logger = Logger(child=True)

ENDPOINTS = {
    "sandbox": "https://api.sandbox.africastalking.com/version1/messaging",
    "live": "https://api.africastalking.com/version1/messaging",
}


class SmsError(RuntimeError):
    pass


def send(to: str, body: str, timeout: float = 5.0) -> dict:
    mode = os.environ.get("SMS_MODE", "log")
    message = text.sms(body)
    if mode == "log":
        logger.info("sms.logged", extra={"chars": len(message), "sms_mode": mode})
        return {"status": "logged", "chars": len(message)}
    if mode == "outbox":
        from . import repo

        repo.put_outbox(to, message)
        logger.info("sms.outbox", extra={"chars": len(message), "sms_mode": mode})
        return {"status": "outbox", "chars": len(message)}
    if mode not in ENDPOINTS:
        raise SmsError(f"unknown SMS_MODE {mode}")
    data = {"username": config.secret("at/username", "sandbox"), "to": to, "message": message}
    sender = os.environ.get("SMS_SENDER_ID", "")
    if sender:
        data["from"] = sender
    req = urllib.request.Request(
        ENDPOINTS[mode],
        data=urllib.parse.urlencode(data).encode(),
        headers={"apiKey": config.secret("at/api_key"), "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https endpoint)
            payload = json.loads(resp.read() or b"{}")
    except Exception as exc:  # network / HTTP errors are retried by the SQS consumer
        raise SmsError(str(exc)) from exc
    recipients = payload.get("SMSMessageData", {}).get("Recipients", [])
    ok = any(r.get("status") == "Success" for r in recipients)
    logger.info("sms.sent", extra={"chars": len(message), "sms_mode": mode, "ok": ok})
    if not ok:
        raise SmsError("provider did not accept the message")
    return {"status": "sent", "chars": len(message)}
