#!/usr/bin/env python3
"""Fetch the current USD spot gold price and write data/commodities/gold_price.json.

Source is gold-api.com (https://api.gold-api.com/price/XAU) — a free, unauthenticated
public spot-price feed. No API key, matching this project's zero-cost/no-proprietary-
dependency constraint. Run on a schedule via .github/workflows/gold_price.yml, the same
"commit static JSON, no live client-side external call" pattern build_briefing.py uses
for briefing.json, which keeps the site's strict same-origin CSP intact.

This script raises (non-zero exit) on any fetch/parse failure rather than writing partial
or stale-looking data -- a failed run just means gold_price.json keeps its last-good value
until the next scheduled run succeeds.

Run: python3 scripts/build_gold_price.py
"""
import datetime
import json
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT_FILE = ROOT / "data" / "commodities" / "gold_price.json"

SOURCE_URL = "https://api.gold-api.com/price/XAU"
GRAMS_PER_TROY_OZ = 31.1034768
TROY_OZ_PER_KG = 1000.0 / GRAMS_PER_TROY_OZ


def main():
    resp = requests.get(SOURCE_URL, timeout=15, headers={"User-Agent": "global-intelligence-site/1.0"})
    resp.raise_for_status()
    data = resp.json()

    price_per_oz = float(data["price"])
    if not (100.0 < price_per_oz < 100000.0):
        raise ValueError(f"gold price {price_per_oz} outside sane bounds -- refusing to write, likely a bad response")

    out = {
        "schema_version": "1.0.0",
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "usd_per_troy_oz": round(price_per_oz, 2),
        "usd_per_kg": round(price_per_oz * TROY_OZ_PER_KG, 2),
        "usd_per_gram": round(price_per_oz / GRAMS_PER_TROY_OZ, 4),
        "price_as_of": data.get("updatedAt"),
        "source": {"name": "gold-api.com", "url": SOURCE_URL},
    }
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print(f"gold price: ${out['usd_per_troy_oz']}/oz = ${out['usd_per_kg']}/kg (as of {out['price_as_of']})")


if __name__ == "__main__":
    main()
