#!/usr/bin/env python3
"""Compute $ valuations for the Global Dashboard taxonomy and write
data/dashboard/taxonomy.json from the hand-curated
data/dashboard/taxonomy_structure.json.

The structure file defines WHICH nodes exist and where a leaf's valuation comes from
(`valuation_source`); this script does the arithmetic against each item's real, already-
published data files (reserves + live spot price for commodities, etc.) and aggregates parent
nodes bottom-up. This mirrors this repo's existing supply_chain_paths.json ->
build_supply_chain_geo.py -> supply_chain_geo.json pattern: curated shape in, computed detail
out, so numbers never manually drift out of sync with the real per-item data.

Valuation honesty rules (no fabricated numbers, ever):
  - "commodity_reserves": sum of reserve tonnes across every listed country x current spot
    price. Real data, real price, real arithmetic.
  - "published_total": a directly cited published $ figure, taken as-is from the structure file
    (must already carry real sources there -- this script does not invent or look anything up).
  - "unavailable": usd stays null. Never backfilled with a guess.
  - A parent node's valuation is the sum of whichever children have a real number; if ANY child
    is missing a value the parent is flagged "partial" (never silently understated as complete);
    if EVERY child is unavailable, the parent itself is "unavailable", not a false $0.

Run: python3 scripts/build_dashboard_taxonomy.py
"""
import datetime
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASH_DIR = ROOT / "data" / "dashboard"
STRUCTURE_FILE = DASH_DIR / "taxonomy_structure.json"
OUT_FILE = DASH_DIR / "taxonomy.json"
COUNTRY_OUT_FILE = DASH_DIR / "country_totals.json"
COMMODITIES_DIR = ROOT / "data" / "commodities"

# Which commodities currently have real per-country reserve data -- extend this list as new
# geographic items are added (e.g. a future item with its own per-country breakdown).
COUNTRY_METALS = ["gold", "silver", "copper"]


def build_country_totals():
    """Aggregate gold+silver+copper reserve value by country -- the Global Dashboard's country
    map wants ONE combined bubble per country, not three separate metal maps stacked on top of
    each other. Only items with real per-country data are included (Supply Chain and Banana
    aren't -- Supply Chain has no per-path $ figure and Banana's $10B is a single global total
    with no country split published anywhere, so neither can honestly be attributed to a
    country; they stay off this map rather than being guessed at)."""
    per_country = {}  # country name -> {"usd": total, "breakdown": {metal: {...}}}
    for metal in COUNTRY_METALS:
        reserves = json.loads((COMMODITIES_DIR / f"{metal}_reserves.json").read_text())
        price = json.loads((COMMODITIES_DIR / f"{metal}_price.json").read_text())
        for c in reserves["countries"]:
            usd = c["tonnes"] * 1000 * price["usd_per_kg"]
            entry = per_country.setdefault(c["country"], {"usd": 0.0, "breakdown": {}})
            entry["usd"] += usd
            # Carry the SAME per-country citation already published on that metal's own page --
            # never invent a new one here, just point at the real source behind this number.
            entry["breakdown"][metal] = {
                "usd": round(usd, 2),
                "tonnes": c["tonnes"],
                "as_of": c.get("as_of"),
                "sources": c.get("sources", []),
            }

    countries = [
        {"country": name, "usd": round(v["usd"], 2), "breakdown": v["breakdown"]}
        for name, v in per_country.items()
    ]
    countries.sort(key=lambda c: c["usd"], reverse=True)
    for i, c in enumerate(countries, start=1):
        c["rank"] = i

    out = {
        "schema_version": "1.0.0",
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_from": "scripts/build_dashboard_taxonomy.py",
        "unit": "usd",
        "note": "Combined value of gold + silver + copper reserves held in each country, at current spot prices. Only items with a real, published per-country breakdown are included -- Supply Chain and Banana are tracked in the taxonomy (see taxonomy.json) but have no per-country $ split to attribute, so they're intentionally absent from this map rather than guessed at.",
        "included_items": COUNTRY_METALS,
        "countries": countries,
    }
    COUNTRY_OUT_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print(f"country totals: {len(countries)} countries, top holder {countries[0]['country']} at ${countries[0]['usd']:,.0f}" if countries else "country totals: no countries")


def valuation_commodity_reserves(vs):
    reserves = json.loads((COMMODITIES_DIR / vs["reserves_file"]).read_text())
    price = json.loads((COMMODITIES_DIR / vs["price_file"]).read_text())
    total_tonnes = sum(c["tonnes"] for c in reserves["countries"])
    usd = total_tonnes * 1000 * price["usd_per_kg"]
    return {
        "usd": round(usd, 2),
        "method": "spot_price",
        "as_of": price["generated"],
        "partial": False,
        "sources": [
            {"title": f"Live spot price ({price['source']['name']})", "url": price["source"]["url"]}
        ],
        "note": vs.get("note", ""),
        "basis_tonnes": round(total_tonnes, 2),
    }


def valuation_published_total(vs):
    return {
        "usd": vs["usd"],
        "method": "published_total",
        "as_of": vs["as_of"],
        "partial": False,
        "sources": vs.get("sources", []),
        "note": vs.get("note", ""),
    }


def valuation_unavailable(vs):
    return {
        "usd": None,
        "method": "unavailable",
        "as_of": None,
        "partial": False,
        "sources": [],
        "note": vs.get("note", ""),
    }


LEAF_HANDLERS = {
    "commodity_reserves": valuation_commodity_reserves,
    "published_total": valuation_published_total,
    "unavailable": valuation_unavailable,
}


def aggregate(children_valuations):
    available = [v["usd"] for v in children_valuations if v["usd"] is not None]
    any_missing = any(v["usd"] is None for v in children_valuations)
    if not available:
        return {"usd": None, "method": "unavailable", "as_of": None, "partial": False, "sources": [], "note": ""}
    return {
        "usd": round(sum(available), 2),
        "method": "aggregated_children",
        "as_of": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"),
        "partial": any_missing,
        "sources": [],
        "note": "",
    }


def walk(node):
    out = {"id": node["id"], "name": node["name"]}
    if "page" in node:
        out["page"] = node["page"]
    if "color" in node:
        out["color"] = node["color"]

    if "children" in node:
        out["children"] = [walk(child) for child in node["children"]]
        out["valuation"] = aggregate([c["valuation"] for c in out["children"]])
    else:
        vs = node["valuation_source"]
        handler = LEAF_HANDLERS[vs["type"]]
        out["valuation"] = handler(vs)
    return out


def count_nodes(node):
    total = 1
    valued = 1 if node["valuation"]["usd"] is not None and "children" not in node else 0
    for child in node.get("children", []):
        t, v = count_nodes(child)
        total += t
        valued += v
    return total, valued


def main():
    structure = json.loads(STRUCTURE_FILE.read_text())
    root_out = walk(structure["root"])
    total_nodes, valued_leaves = count_nodes(root_out)

    out = {
        "schema_version": "1.0.0",
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_from": "scripts/build_dashboard_taxonomy.py",
        "world_gdp": structure["world_gdp"],
        "tracked_total": root_out["valuation"],
        "coverage": {"total_nodes": total_nodes, "leaf_nodes_with_valuation": valued_leaves},
        "root": root_out,
    }
    OUT_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")

    print(f"nodes: {total_nodes}, leaf nodes with a real valuation: {valued_leaves}")
    print(f"tracked total: {out['tracked_total']}")

    build_country_totals()


if __name__ == "__main__":
    main()
