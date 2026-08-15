#!/usr/bin/env python3
"""Fetch current USD spot prices for gold, silver, and copper and write
data/commodities/{gold,silver,copper}_price.json.

Source is gold-api.com (https://api.gold-api.com/price/<SYMBOL>) -- a free, unauthenticated
public spot-price feed covering XAU (gold, $/troy oz), XAG (silver, $/troy oz), and HG (COMEX
copper futures, $/lb). No API key, matching this project's zero-cost/no-proprietary-dependency
constraint -- one source covers all three metals. Run on a schedule via
.github/workflows/metal_prices.yml, the same "commit static JSON, no live client-side external
call" pattern build_briefing.py uses for briefing.json, which keeps the site's strict
same-origin CSP intact.

This script raises (non-zero exit) on any fetch/parse failure for a given metal rather than
writing partial or stale-looking data -- a failed run just means that metal's price.json keeps
its last-good value until the next scheduled run succeeds. Each metal is independent: one
metal's failure doesn't block the others from updating.

Run: python3 scripts/build_metal_price.py
"""
import datetime
import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
GRAMS_PER_TROY_OZ = 31.1034768
TROY_OZ_PER_KG = 1000.0 / GRAMS_PER_TROY_OZ
KG_PER_LB = 0.45359237

# unit: "troy_oz" (quoted $/oz, like gold/silver bullion) or "lb" (quoted $/lb, like COMEX
# copper futures) -- bounds are a sanity check on the raw API price, not a prediction.
METALS = [
    {"key": "gold", "symbol": "XAU", "unit": "troy_oz", "bounds": (100.0, 100000.0)},
    {"key": "silver", "symbol": "XAG", "unit": "troy_oz", "bounds": (1.0, 5000.0)},
    {"key": "copper", "symbol": "HG", "unit": "lb", "bounds": (0.1, 100.0)},
]


def fetch_price(symbol):
    url = f"https://api.gold-api.com/price/{symbol}"
    resp = requests.get(url, timeout=15, headers={"User-Agent": "global-intelligence-site/1.0"})
    resp.raise_for_status()
    return resp.json(), url


def build_one(metal):
    data, url = fetch_price(metal["symbol"])
    price = float(data["price"])
    lo, hi = metal["bounds"]
    if not (lo < price < hi):
        raise ValueError(f"{metal['key']} price {price} outside sane bounds ({lo}-{hi}) -- refusing to write, likely a bad response")

    if metal["unit"] == "troy_oz":
        usd_per_kg = price * TROY_OZ_PER_KG
    else:  # lb
        usd_per_kg = price / KG_PER_LB

    out = {
        "schema_version": "1.0.0",
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "usd_per_kg": round(usd_per_kg, 2),
        "usd_per_gram": round(usd_per_kg / 1000.0, 6),
        "price_as_of": data.get("updatedAt"),
        "source": {"name": "gold-api.com", "url": url},
    }
    if metal["unit"] == "troy_oz":
        out["usd_per_troy_oz"] = round(price, 2)
    else:
        out["usd_per_lb"] = round(price, 4)

    out_file = ROOT / "data" / "commodities" / f"{metal['key']}_price.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    return out


def main():
    failures = []
    for metal in METALS:
        try:
            out = build_one(metal)
            per_unit = out.get("usd_per_troy_oz", out.get("usd_per_lb"))
            unit_label = "/oz" if metal["unit"] == "troy_oz" else "/lb"
            print(f"{metal['key']}: ${per_unit}{unit_label} = ${out['usd_per_kg']}/kg (as of {out['price_as_of']})")
        except Exception as e:
            failures.append(metal["key"])
            print(f"{metal['key']}: FAILED -- {e}", file=sys.stderr)

    if failures:
        raise SystemExit(f"failed to update: {', '.join(failures)}")


if __name__ == "__main__":
    main()
