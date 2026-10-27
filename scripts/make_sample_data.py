#!/usr/bin/env python3
"""Generate SYNTHETIC demo prices in the WFP/HDX CSV format (data/zambia_sample_prices.csv).

These numbers are invented for development and demonstration only. They follow a
plausible Zambian seasonal shape (cheap after the May-July harvest, dear in the
January-February lean season) but are NOT real market observations. Use the
live WFP source (WFP_CSV_URL) for anything presented as real prices.
"""

from __future__ import annotations

import csv
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKETS = json.loads((ROOT / "shared/agricommon/data/markets.json").read_text())
PROVINCE_NAMES = {"CEN": "Central", "CB": "Copperbelt", "EAS": "Eastern", "LUA": "Luapula", "LSK": "Lusaka", "MUC": "Muchinga",
                  "NW": "North-Western", "NOR": "Northern", "SOU": "Southern", "WES": "Western"}
# commodity label (as WFP writes it), base ZMW/kg, seasonal amplitude
COMMODITIES = [
    ("Maize (white)", 5.2, 0.28), ("Soybeans", 13.0, 0.15), ("Groundnuts (shelled)", 26.0, 0.12),
    ("Beans (sugar)", 30.0, 0.10), ("Rice", 32.0, 0.05), ("Cassava (dry)", 4.5, 0.10), ("Maize meal (white, roller)", 11.0, 0.12),
]
SEASON = [1.30, 1.35, 1.20, 1.05, 0.85, 0.80, 0.80, 0.85, 0.95, 1.05, 1.15, 1.25]  # Jan..Dec
REMOTE = {"WES": 1.12, "NW": 1.08, "LUA": 1.08, "MUC": 1.06, "NOR": 1.05}


def main() -> None:
    rng = random.Random(2026)
    out = ROOT / "data" / "zambia_sample_prices.csv"
    out.parent.mkdir(exist_ok=True)
    with out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "admin1", "admin2", "market", "latitude", "longitude", "category", "commodity", "unit", "priceflag", "pricetype", "currency", "price", "usdprice"])
        w.writerow(["#date", "#adm1+name", "#adm2+name", "#loc+market+name", "#geo+lat", "#geo+lon", "#item+type", "#item+name", "#item+unit", "#item+price+flag", "#item+price+type", "#currency", "#value", "#value+usd"])
        for year in (2023, 2024, 2025, 2026):
            inflation = 1 + 0.09 * (year - 2023)
            for month in range(1, 13):
                if year == 2026 and month > 10:
                    break
                for m in MARKETS:
                    for name, base, amp in COMMODITIES:
                        seasonal = 1 + (SEASON[month - 1] - 1) * (amp / 0.28)
                        price = base * inflation * seasonal * REMOTE.get(m["province"], 1.0) * rng.uniform(0.94, 1.06)
                        w.writerow([f"{year}-{month:02d}-15", PROVINCE_NAMES[m["province"]], m["name"], m["name"], m["lat"], m["lon"],
                                    "synthetic demo", name, "KG", "synthetic", "Retail", "ZMW", f"{price:.2f}", ""])
                        if name.startswith("Maize (white)"):
                            w.writerow([f"{year}-{month:02d}-15", PROVINCE_NAMES[m["province"]], m["name"], m["name"], m["lat"], m["lon"],
                                        "synthetic demo", name, "50 KG", "synthetic", "Wholesale", "ZMW", f"{price * 50 * 0.88:.0f}", ""])
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
