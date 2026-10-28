"""Weather signal for price forecasts (FR12 stretch, thesis M12: Open-Meteo, free, no API key).

WEATHER_MODE:
  live  - Open-Meteo forecast API over HTTPS (needs outbound internet via the NAT)
  mock  - deterministic built-in values per province (local emulator, tests, offline demos)
  off   - no adjustment

The adjustment is a deliberately simple, documented heuristic, not an agronomic
model: during the growing season (November-April), forecast rainfall well below
the normal for that month raises expected prices (supply risk); well above
normal lowers them slightly. The effect is capped at +/-8%.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import date

from . import catalog

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
# Approximate long-run mean daily rainfall (mm/day) by month for central Zambia (Nov-Apr rainy season).
NORMAL_MM_PER_DAY = {1: 8.0, 2: 7.0, 3: 5.0, 4: 1.5, 5: 0.2, 6: 0.0, 7: 0.0, 8: 0.0, 9: 0.1, 10: 0.8, 11: 3.5, 12: 7.0}
GROWING = {11, 12, 1, 2, 3, 4}
MAX_EFFECT = 0.08
_MOCK_RATIO = {"CEN": 0.7, "CB": 1.1, "EAS": 0.9, "LUA": 1.2, "LSK": 0.8, "MUC": 1.0, "NW": 1.15, "NOR": 1.05, "SOU": 0.6, "WES": 0.75}


def province_centroid(province: str) -> tuple[float, float] | None:
    pts = [(m.lat, m.lon) for m in catalog.markets_in_province(province)] or [(m.lat, m.lon) for m in catalog.markets().values()]
    if not pts:
        return None
    return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)


def rainfall_ratio(province: str, today: date | None = None) -> float | None:
    """Forecast 16-day mean rainfall divided by the monthly normal (1.0 = normal). None if unavailable."""
    mode = os.environ.get("WEATHER_MODE", "off")
    today = today or date.today()
    if mode == "off":
        return None
    if mode == "mock":
        return _MOCK_RATIO.get(province, 1.0)
    centre = province_centroid(province)
    if centre is None:
        return None
    q = urllib.parse.urlencode({"latitude": f"{centre[0]:.3f}", "longitude": f"{centre[1]:.3f}", "daily": "precipitation_sum", "forecast_days": 16, "timezone": "Africa/Lusaka"})
    try:
        with urllib.request.urlopen(f"{OPEN_METEO}?{q}", timeout=8) as resp:  # noqa: S310 (fixed https endpoint)
            daily = json.loads(resp.read())["daily"]["precipitation_sum"]
    except Exception:
        return None
    values = [v for v in daily if v is not None]
    normal = NORMAL_MM_PER_DAY[today.month]
    if not values or normal < 0.5:
        return None
    return (sum(values) / len(values)) / normal


def price_adjustment(ratio: float | None, month: int) -> float:
    """Multiplicative factor for forecast prices (1.0 = no change)."""
    if ratio is None or month not in GROWING:
        return 1.0
    if ratio < 0.8:
        return 1 + min(MAX_EFFECT, (0.8 - ratio) * 0.2)
    if ratio > 1.3:
        return 1 - min(MAX_EFFECT / 2, (ratio - 1.3) * 0.05)
    return 1.0
