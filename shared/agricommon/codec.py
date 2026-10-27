"""Compact wire encodings. Every byte on a 2G link has to justify itself.

Price record (compact JSON): {"c":"MZE","m":"NDL","p":28500,"d":20752,"t":32}
  c  commodity code        m  market code
  p  price, ngwee per 50 kg bag (integer: no decimals on the wire)
  d  observation date as days since 1970-01-01
  t  change vs previous observation, tenths of a percent (signed int)
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta

EPOCH = date(1970, 1, 1)
BAG_KG = 50


def day_number(d: date | str) -> int:
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    return (d - EPOCH).days


def from_day_number(n: int) -> date:
    return EPOCH + timedelta(days=n)


def change_tenths(new: int, old: int | None) -> int:
    if not old:
        return 0
    return round((new - old) * 1000 / old)


def price_record(commodity: str, market: str, ngwee_per_kg: int, obs_date: str, prev_ngwee_per_kg: int | None) -> dict:
    return {
        "c": commodity,
        "m": market,
        "p": ngwee_per_kg * BAG_KG,
        "d": day_number(obs_date),
        "t": change_tenths(ngwee_per_kg, prev_ngwee_per_kg),
    }


def dumps(obj) -> str:
    """Minified JSON: no spaces, non-ASCII kept as-is."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def etag(body: str) -> str:
    return '"' + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16] + '"'


def kwacha_per_bag(ngwee_per_kg: int) -> str:
    """Human price for SMS/USSD, e.g. 5700 ngwee/kg -> 'K285'."""
    kw = ngwee_per_kg * BAG_KG / 100
    return f"K{kw:.0f}" if kw >= 100 else f"K{kw:.1f}".replace(".0", "")


def pct(t: int) -> str:
    if t == 0:
        return "="
    v = t / 10
    return f"{v:+.0f}%" if abs(v) >= 10 else f"{v:+.1f}%"
