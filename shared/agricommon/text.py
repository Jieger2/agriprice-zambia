"""Low-bandwidth text: GSM-7 safety and USSD/SMS length budgets."""

from __future__ import annotations

USSD_MAX = 182
SMS_MAX = 160

# GSM 03.38 basic character set (single-septet characters only).
_GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_REPLACE = {"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", "…": "...", " ": " "}


def to_gsm7(s: str) -> str:
    out = []
    for ch in s:
        ch = _REPLACE.get(ch, ch)
        for c in ch:
            out.append(c if c in _GSM7 else "?")
    return "".join(out)


def is_gsm7(s: str) -> bool:
    return all(c in _GSM7 for c in s)


def fit(s: str, limit: int) -> str:
    """Make text GSM-7 safe and trim to the budget (keeps whole lines where possible)."""
    s = to_gsm7(s)
    if len(s) <= limit:
        return s
    lines, out = s.split("\n"), []
    for line in lines:
        candidate = "\n".join(out + [line])
        if len(candidate) > limit:
            break
        out.append(line)
    joined = "\n".join(out)
    return joined if joined else s[:limit]


def ussd(kind: str, body: str) -> str:
    """kind is 'CON' (continue session) or 'END' (close session)."""
    if kind not in ("CON", "END"):
        raise ValueError(kind)
    return f"{kind} " + fit(body, USSD_MAX - 4)


def sms(body: str) -> str:
    return fit(body, SMS_MAX)
