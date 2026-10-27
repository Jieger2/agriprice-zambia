"""Phone-number handling (Zambia Data Protection Act No. 3 of 2021).

Phone numbers are personal data: they are stored only where needed to send an
SMS (DynamoDB is KMS-encrypted at rest), and everywhere else, including keys,
logs and traces, they are replaced by a keyed hash.
"""

from __future__ import annotations

import hashlib
import hmac
import re

from . import config


def normalise_phone(raw: str) -> str:
    """Return E.164 for Zambian numbers: 0977123456 / 977123456 / +260977123456 -> +260977123456."""
    digits = re.sub(r"[^\d+]", "", raw or "")
    if digits.startswith("+"):
        num = digits
    elif digits.startswith("260"):
        num = "+" + digits
    elif digits.startswith("0") and len(digits) == 10:
        num = "+260" + digits[1:]
    elif len(digits) == 9:
        num = "+260" + digits
    else:
        num = "+" + digits.lstrip("+")
    if not re.fullmatch(r"\+\d{9,15}", num):
        raise ValueError("invalid phone number")
    return num


def phone_id(phone: str) -> str:
    pepper = config.secret("privacy/pepper").encode()
    return hmac.new(pepper, normalise_phone(phone).encode(), hashlib.sha256).hexdigest()[:20]


def mask(phone: str) -> str:
    p = normalise_phone(phone)
    return p[:-6] + "***" + p[-3:]
