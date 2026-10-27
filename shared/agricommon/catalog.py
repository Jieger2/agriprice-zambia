"""Reference data: commodities, provinces and markets, plus normalisation helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import geo

_DATA = Path(__file__).parent / "data"


@dataclass(frozen=True)
class Market:
    code: str
    name: str
    province: str
    lat: float
    lon: float

    @property
    def geohash(self) -> str:
        return geo.encode(self.lat, self.lon, 7)


@lru_cache(maxsize=1)
def _catalog() -> dict:
    return json.loads((_DATA / "catalog.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def markets() -> dict[str, Market]:
    rows = json.loads((_DATA / "markets.json").read_text(encoding="utf-8"))
    return {r["code"]: Market(**r) for r in rows}


def commodities() -> list[dict]:
    return _catalog()["commodities"]


def commodity_codes() -> list[str]:
    return [c["code"] for c in commodities()]


def commodity(code: str) -> dict | None:
    return next((c for c in commodities() if c["code"] == code), None)


def provinces() -> list[dict]:
    return _catalog()["provinces"]


def province_codes() -> list[str]:
    return [p["code"] for p in provinces()]


def match_commodity(raw_name: str) -> str | None:
    """Map a source commodity label (e.g. 'Maize meal (white, roller)') to a code.

    Longest pattern wins so 'maize meal' is not mistaken for 'maize'.
    """
    name = raw_name.strip().lower()
    best: tuple[int, str] | None = None
    for c in commodities():
        for pattern in c["match"]:
            if name.startswith(pattern) and (best is None or len(pattern) > best[0]):
                best = (len(pattern), c["code"])
    return best[1] if best else None


def match_province(raw: str) -> str | None:
    key = raw.strip().lower()
    for p in provinces():
        if key == p["code"].lower() or key == p["name"].lower() or key in p["aliases"]:
            return p["code"]
    return None


def match_market(raw_name: str) -> Market | None:
    key = raw_name.strip().lower()
    for m in markets().values():
        if key in (m.code.lower(), m.name.lower()):
            return m
    return None


def market_code_for(name: str) -> str:
    """Stable 3-letter code for a market not in the registry."""
    letters = re.sub(r"[^A-Za-z]", "", name).upper()
    return (letters[:3] or "UNK").ljust(3, "X")


_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*kg", re.IGNORECASE)


def unit_to_kg(unit: str) -> float | None:
    """Return kilograms represented by a source unit, or None if not weight-based."""
    u = unit.strip().lower()
    if u in ("kg", "kgs", "kilogram"):
        return 1.0
    m = _UNIT_RE.search(u)
    if m:
        kg = float(m.group(1))
        return kg if kg > 0 else None
    return None


def to_ngwee_per_kg(price: float, unit: str, currency: str = "ZMW") -> int | None:
    if currency.upper() != "ZMW" or price is None or price <= 0:
        return None
    kg = unit_to_kg(unit)
    if not kg:
        return None
    return round(price * 100 / kg)


def nearest_markets(lat: float, lon: float, n: int = 3) -> list[tuple[Market, float]]:
    ranked = sorted(
        ((m, geo.haversine_km(lat, lon, m.lat, m.lon)) for m in markets().values()),
        key=lambda t: t[1],
    )
    return ranked[:n]


def markets_in_province(province: str) -> list[Market]:
    return [m for m in markets().values() if m.province == province]
