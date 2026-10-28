"""Reference data: commodities, provinces, markets and units, plus normalisation helpers.

The bundled JSON files are the baseline. Administrators can add, change or
disable markets and commodities and add unit conversions through the Admin
page (FR10). Those changes are stored in DynamoDB (`CONFIG#catalog`) and merged
on top of the baseline; every service re-reads them at most once a minute.
Dynamic overrides are on when CATALOG_DYNAMIC=1, as set in every Lambda template
and in the local emulator.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

from . import geo

_DATA = Path(__file__).parent / "data"
CACHE_SECONDS = 60
_cache: dict = {"at": 0.0, "doc": {}}


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

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- baseline + admin overrides
@lru_cache(maxsize=1)
def _base_catalog() -> dict:
    return json.loads((_DATA / "catalog.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _base_markets() -> tuple[dict, ...]:
    return tuple(json.loads((_DATA / "markets.json").read_text(encoding="utf-8")))


def overrides() -> dict:
    """Admin overrides from DynamoDB, cached briefly. Empty when dynamic catalog is off."""
    if os.environ.get("CATALOG_DYNAMIC") != "1":
        return {}
    now = time.time()
    if now - _cache["at"] > CACHE_SECONDS:
        try:
            from . import repo  # local import: repo -> config, avoids a cycle at import time

            _cache["doc"] = repo.get_config("catalog") or {}
        except Exception:  # catalog must never take a service down; fall back to the baseline
            _cache["doc"] = _cache.get("doc") or {}
        _cache["at"] = now
    return _cache["doc"]


def invalidate() -> None:
    _cache["at"] = 0.0


def _merge(base: list[dict], extra: list[dict]) -> list[dict]:
    rows = {r["code"]: dict(r) for r in base}
    for r in extra or []:
        if r.get("disabled"):
            rows.pop(r["code"], None)
        else:
            rows[r["code"]] = {**rows.get(r["code"], {}), **{k: v for k, v in r.items() if k != "disabled"}}
    return list(rows.values())


def markets() -> dict[str, Market]:
    rows = _merge(list(_base_markets()), overrides().get("markets", []))
    return {r["code"]: Market(code=r["code"], name=r["name"], province=r["province"], lat=float(r["lat"]), lon=float(r["lon"])) for r in rows}


def commodities() -> list[dict]:
    rows = _merge(_base_catalog()["commodities"], overrides().get("commodities", []))
    for r in rows:
        r.setdefault("short", r["name"][:7])
        r.setdefault("match", [r["name"].lower()])
    return rows


def commodity_codes() -> list[str]:
    return [c["code"] for c in commodities()]


def commodity(code: str) -> dict | None:
    return next((c for c in commodities() if c["code"] == code), None)


def provinces() -> list[dict]:
    return _base_catalog()["provinces"]


def province_codes() -> list[str]:
    return [p["code"] for p in provinces()]


def unit_aliases() -> dict[str, float]:
    """Admin-defined unit conversions, e.g. {"tin (5 l)": 4.5, "bucket": 20}."""
    return {k.strip().lower(): float(v) for k, v in (overrides().get("units") or {}).items()}


# ---------------------------------------------------------------- validation (Admin API)
_CODE = re.compile(r"^[A-Z]{3}$")


def validate_catalog(doc: dict) -> dict:
    """Validate and normalise an admin catalog document. Raises ValueError with a readable message."""
    if not isinstance(doc, dict):
        raise ValueError("catalog must be an object")
    out: dict = {"markets": [], "commodities": [], "units": {}}
    provs = set(province_codes())
    for m in doc.get("markets", []) or []:
        code = str(m.get("code", "")).upper()
        if not _CODE.match(code):
            raise ValueError(f"market code '{code}' must be 3 letters")
        if m.get("disabled"):
            out["markets"].append({"code": code, "disabled": True})
            continue
        if m.get("province") not in provs:
            raise ValueError(f"market {code}: unknown province")
        lat, lon = float(m.get("lat")), float(m.get("lon"))
        if not (-18.5 <= lat <= -8 and 21.5 <= lon <= 34):
            raise ValueError(f"market {code}: coordinates are outside Zambia")
        name = str(m.get("name", "")).strip()[:30]
        if not name:
            raise ValueError(f"market {code}: name required")
        out["markets"].append({"code": code, "name": name, "province": m["province"], "lat": lat, "lon": lon})
    for c in doc.get("commodities", []) or []:
        code = str(c.get("code", "")).upper()
        if not _CODE.match(code):
            raise ValueError(f"commodity code '{code}' must be 3 letters")
        if c.get("disabled"):
            out["commodities"].append({"code": code, "disabled": True})
            continue
        name = str(c.get("name", "")).strip()[:30]
        if not name:
            raise ValueError(f"commodity {code}: name required")
        short = str(c.get("short") or name).strip()[:7]
        match = [str(x).strip().lower() for x in (c.get("match") or [name]) if str(x).strip()]
        out["commodities"].append({"code": code, "name": name, "short": short, "match": match})
    for label, kg in (doc.get("units") or {}).items():
        kg = float(kg)
        if not 0 < kg <= 1000:
            raise ValueError(f"unit '{label}': kilograms must be between 0 and 1000")
        out["units"][str(label).strip().lower()] = kg
    return out


# ---------------------------------------------------------------- normalisation
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
    aliases = unit_aliases()
    if u in aliases:
        return aliases[u]
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
