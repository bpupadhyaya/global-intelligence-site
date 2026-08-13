#!/usr/bin/env python3
"""Resolve data/supply_chain_paths.json's origin/destination/chokepoint text into
map coordinates, and write data/supply_chain_geo.json for the /supply-chain/ map.

Countries are matched by name (with an alias table for common variants) against
data/world-atlas-countries-110m.json's 177 country names, then geolocated at
runtime in the browser via d3.geoCentroid() on that country's actual geometry --
this script only resolves NAMES, not coordinates, for countries. Composite
regions ("Middle East", "Southeast Asia", ...) and chokepoints (straits, canals,
named ports/pipelines) get hand-curated fixed coordinates below, since they have
no single polygon to take a centroid from.

A large fraction of origin/destination strings in this dataset are not places at
all ("global EV and grid-storage battery manufacturing", "170 countries via
COVAX") -- those are left unresolved on purpose. An unresolved entry just gets no
arc endpoint; it stays fully visible as text in the existing detail cards.

Run: python3 scripts/build_supply_chain_geo.py
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATHS_FILE = ROOT / "data" / "supply_chain_paths.json"
ATLAS_FILE = ROOT / "data" / "world-atlas-countries-110m.json"
OUT_FILE = ROOT / "data" / "supply_chain_geo.json"

# --- Country name aliases -> the exact name string used in world-atlas-countries-110m.json ---
# Longer/more specific keys are matched first (see COUNTRY_PATTERNS build below), so a
# specific variant like "Democratic Republic of the Congo" wins over a bare "Congo".
COUNTRY_ALIASES = {
    "United States of America": ["United States of America", "United States", "USA", "U.S.", "American"],
    "Russia": ["Russia", "Russian Federation"],
    "South Korea": ["South Korea", "Republic of Korea"],
    "North Korea": ["North Korea"],
    "Czechia": ["Czechia", "Czech Republic"],
    "Dem. Rep. Congo": ["Democratic Republic of the Congo", "Democratic Republic of Congo", "Dem. Rep. Congo", "DR Congo", "DRC"],
    "Congo": ["Republic of the Congo", "Congo"],
    "Côte d'Ivoire": ["Côte d'Ivoire", "Cote d'Ivoire", "Ivory Coast"],
    "Macedonia": ["North Macedonia", "Macedonia"],
    "Bosnia and Herz.": ["Bosnia and Herzegovina", "Bosnia and Herz."],
    "Dominican Rep.": ["Dominican Republic", "Dominican Rep."],
    "Central African Rep.": ["Central African Republic", "Central African Rep."],
    "Eq. Guinea": ["Equatorial Guinea", "Eq. Guinea"],
    "S. Sudan": ["South Sudan", "S. Sudan"],
    "W. Sahara": ["Western Sahara", "W. Sahara"],
    "eSwatini": ["eSwatini", "Eswatini", "Swaziland"],
    "Myanmar": ["Myanmar", "Burma"],
    "Vietnam": ["Vietnam", "Viet Nam", "Vietnamese"],
    "Laos": ["Laos", "Lao PDR"],
    "Brunei": ["Brunei", "Brunei Darussalam"],
    "United Arab Emirates": ["United Arab Emirates", "UAE"],
    "United Kingdom": ["United Kingdom", "U.K.", "Britain", "Great Britain", "British"],
    # Demonyms/adjectives -- the dataset often says "Chinese manufacturing" or "Brazilian
    # soybean-growing states" rather than the bare country name. Kept to the countries that
    # actually show up as origins/destinations/company HQs in this dataset; extend as new
    # themes are added and new unresolved strings turn up (see /tmp/unresolved_*.txt after
    # running this script).
    "China": ["China", "Chinese", "Pearl River Delta", "Yangtze River Delta", "Xinjiang", "Bohai Rim"],
    "India": ["India", "Indian"],
    "Australia": ["Australia", "Australian"],
    "Brazil": ["Brazil", "Brazilian"],
    "Argentina": ["Argentina", "Argentine", "Argentinian", "Argentine Pampas"],
    "Canada": ["Canada", "Canadian"],
    "Mexico": ["Mexico", "Mexican"],
    "Indonesia": ["Indonesia", "Indonesian"],
    "Nigeria": ["Nigeria", "Nigerian"],
    "Egypt": ["Egypt", "Egyptian"],
    "Kenya": ["Kenya", "Kenyan"],
    "Thailand": ["Thailand", "Thai"],
    "Malaysia": ["Malaysia", "Malaysian"],
    "Japan": ["Japan", "Japanese"],
    "Germany": ["Germany", "German"],
    "France": ["France", "French"],
    "Saudi Arabia": ["Saudi Arabia", "Saudi"],
    "Qatar": ["Qatar", "Qatari"],
    "Chile": ["Chile", "Chilean"],
    "Peru": ["Peru", "Peruvian"],
    "Morocco": ["Morocco", "Moroccan"],
    "South Africa": ["South Africa", "South African"],
    "New Zealand": ["New Zealand", "North Island pasture", "South Island pasture"],
    # All other entries below match the Natural Earth name against itself, no alias needed --
    # they're added programmatically from the atlas file.
}

# --- Composite / macro region aliases: no single polygon exists, so these carry a
# hand-picked representative point instead (roughly the geographic/economic center
# of the named area). Checked only when NO country name matched inside the string. ---
REGION_POINTS = {
    "Sub-Saharan Africa": [2.0, 20.0],
    "West Africa": [9.5, -3.0],
    "East Africa": [1.0, 37.0],
    "North Africa": [28.0, 12.0],
    "Middle East and North Africa": [26.0, 30.0],
    "Middle East": [27.0, 44.0],
    "Persian Gulf": [26.5, 51.5],
    "Gulf states": [24.5, 52.0],
    "Mediterranean basin": [37.0, 18.0],
    "Mediterranean and North African markets": [35.0, 15.0],
    "Africa": [2.0, 20.0],
    "Americas": [10.0, -80.0],
    "Latin America": [-8.0, -60.0],
    "West Coast South America": [-15.0, -73.0],
    "North America": [45.0, -100.0],
    "Caribbean": [18.0, -66.0],
    "Asia-Pacific": [10.0, 120.0],
    "Asia and Oceania": [10.0, 120.0],
    "Asia (broadly)": [30.0, 90.0],
    "Asia": [30.0, 90.0],
    "East Asia": [33.0, 118.0],
    "Southeast Asia": [5.0, 108.0],
    "South and Southeast Asia": [12.0, 100.0],
    "South Asia": [22.0, 78.0],
    "Oceania": [-25.0, 140.0],
    "Europe": [50.0, 15.0],
    "Western Europe": [48.5, 4.0],
    "Southern Europe": [40.0, 15.0],
    "Eastern Europe": [50.0, 25.0],
    "Russia and Eastern Europe": [55.0, 40.0],
    "European Union": [50.0, 10.0],
    "Caspian Sea": [41.5, 51.0],
    "other Caspian Sea region producers": [41.5, 51.0],
    "Black Sea region grain-growing areas": [46.5, 32.0],
    "US Gulf Coast": [29.5, -94.5],
    "United States Gulf Coast": [29.5, -94.5],
    "US Midwest": [41.8, -87.6],
    "United States Midwest": [41.8, -87.6],
    "United States Northeast": [40.7, -74.0],
    "United States Mid-Atlantic": [39.0, -77.0],
    "United States Southeast": [33.7, -84.4],
    "US Cotton Belt": [32.0, -95.0],
    "Western Canada": [53.5, -114.0],
    "Eastern Canada": [45.4, -75.7],
}

CATEGORY_NAMES = {
    "energy": "Energy",
    "minerals-metals": "Minerals & Metals",
    "fertilizer": "Fertilizer",
    "agriculture": "Agriculture",
    "industrial": "Industrial",
    "pharmaceuticals-medical-supplies": "Pharmaceuticals & Medical Supplies",
    "chemicals-plastics": "Chemicals & Plastics",
    "coal": "Coal",
    "renewable-energy-equipment": "Renewable Energy Equipment",
}

# --- Curated, genuinely physical chokepoints (straits, canals, named ports/pipelines) ---
# Matched by keyword against the free-text chokepoints[] strings. Everything else in that
# field (market concentration, licensing regimes, corporate concentration...) is real and
# stays visible as text in the path's detail panel -- it just isn't a point on a map.
CHOKEPOINTS = [
    ("Strait of Hormuz", ["Strait of Hormuz", "Hormuz"], [26.55, 56.35]),
    ("Strait of Malacca", ["Strait of Malacca", "Malacca"], [2.8, 101.4]),
    ("Bab-el-Mandeb", ["Bab-el-Mandeb", "Bab el-Mandeb"], [12.6, 43.4]),
    ("Suez Canal", ["Suez Canal", "Suez"], [30.55, 32.35]),
    ("Panama Canal", ["Panama Canal"], [9.08, -79.68]),
    ("Bosphorus / Turkish Straits", ["Bosphorus", "Turkish Straits", "Dardanelles"], [41.12, 29.06]),
    ("Danish Straits", ["Danish Straits", "Øresund", "Oresund"], [55.7, 12.6]),
    ("Taiwan Strait", ["Taiwan Strait"], [24.5, 119.6]),
    ("Strait of Gibraltar", ["Strait of Gibraltar", "Gibraltar"], [35.96, -5.6]),
    ("Cape of Good Hope", ["Cape of Good Hope"], [-34.35, 18.47]),
    ("Cape Horn / Drake Passage", ["Cape Horn", "Drake Passage"], [-56.0, -67.3]),
    ("Strait of Dover", ["Strait of Dover", "Dover Strait", "English Channel"], [51.0, 1.4]),
    ("Druzhba Pipeline", ["Druzhba Pipeline", "Druzhba"], [52.05, 27.5]),
    ("Trans-Alaska Pipeline", ["Trans-Alaska Pipeline", "TAPS"], [64.8, -147.7]),
    ("Keystone Pipeline", ["Keystone Pipeline", "Keystone XL"], [49.0, -97.1]),
    ("Baku-Tbilisi-Ceyhan (BTC) Pipeline", ["Baku-Tbilisi-Ceyhan", "BTC pipeline", "Ceyhan"], [36.9, 34.6]),
    ("Trans Mountain Pipeline", ["Trans Mountain"], [53.9, -119.5]),
    ("Yamal Pipeline", ["Yamal Pipeline", "Yamal-Europe"], [66.0, 70.0]),
    ("Port of Rotterdam", ["Rotterdam"], [51.9, 4.14]),
    ("Port of Singapore", ["Port of Singapore", "Singapore Strait"], [1.26, 103.82]),
    ("Port of Shanghai", ["Shanghai"], [31.36, 121.5]),
    ("Ningbo-Zhoushan Port", ["Ningbo"], [29.87, 121.9]),
    ("Port of Busan", ["Busan"], [35.1, 129.04]),
    ("Jebel Ali Port", ["Jebel Ali"], [25.02, 55.06]),
    ("Richards Bay Coal Terminal", ["Richards Bay"], [-28.8, 32.09]),
    ("Ras Laffan Industrial City", ["Ras Laffan"], [25.9, 51.55]),
    ("Houston Ship Channel", ["Houston Ship Channel"], [29.73, -95.0]),
    ("Port of Los Angeles / Long Beach", ["Los Angeles", "Long Beach"], [33.74, -118.25]),
    ("Port of Santos", ["Santos"], [-23.96, -46.33]),
    ("Kiel Canal", ["Kiel Canal"], [54.32, 10.14]),
    ("Turkish Straits (Boron mining, western Turkey)", ["Bigadic", "western Turkey"], [39.4, 28.1]),
]

WORD_RE_CACHE = {}


def word_pattern(term):
    if term not in WORD_RE_CACHE:
        WORD_RE_CACHE[term] = re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)
    return WORD_RE_CACHE[term]


def build_country_patterns(atlas_names):
    """One (alias_text, canonical_name) pair per candidate, longest alias first so
    'Democratic Republic of the Congo' matches before the bare 'Congo' entry."""
    pairs = []
    aliased = set(COUNTRY_ALIASES.keys())
    for canonical, aliases in COUNTRY_ALIASES.items():
        for a in aliases:
            pairs.append((a.strip(), canonical))
    for name in atlas_names:
        if name in aliased or name == "Antarctica":
            continue
        pairs.append((name, name))
    pairs.sort(key=lambda p: -len(p[0]))
    return pairs


def resolve_countries(text, patterns):
    found = []
    remaining = text
    for alias, canonical in patterns:
        if canonical in found:
            continue
        if word_pattern(alias).search(remaining):
            found.append(canonical)
    return found


def resolve_region_point(text):
    # Longest region name first, so "Middle East and North Africa" beats "Middle East".
    for name in sorted(REGION_POINTS, key=len, reverse=True):
        if word_pattern(re.escape(name)).search(text) or name in text:
            return name, REGION_POINTS[name]
    return None, None


def resolve_entries(strings, patterns):
    """Returns {original_string: {"countries": [...canonical names]} |
    {"region": name, "point": [lat,lng]} | None (unresolved)}."""
    out = {}
    for s in strings:
        countries = resolve_countries(s, patterns)
        if countries:
            out[s] = {"countries": countries}
            continue
        region, point = resolve_region_point(s)
        if region:
            out[s] = {"region": region, "point": point}
            continue
        out[s] = None
    return out


def match_chokepoints(text):
    matches = []
    for name, keywords, point in CHOKEPOINTS:
        for kw in keywords:
            if kw.lower() in text.lower():
                matches.append({"name": name, "lat": point[0], "lng": point[1]})
                break
    return matches


def main():
    atlas = json.loads(ATLAS_FILE.read_text())
    atlas_names = [g["properties"]["name"] for g in atlas["objects"]["countries"]["geometries"]]
    patterns = build_country_patterns(atlas_names)

    data = json.loads(PATHS_FILE.read_text())
    all_origin_strs, all_dest_strs, all_choke_strs = set(), set(), set()
    for p in data["paths"]:
        all_origin_strs.update(p.get("origin_regions", []))
        all_dest_strs.update(p.get("destination_regions", []))
        all_choke_strs.update(p.get("chokepoints", []))

    origin_resolved = resolve_entries(all_origin_strs, patterns)
    dest_resolved = resolve_entries(all_dest_strs, patterns)

    out_paths = []
    unresolved_origins, unresolved_dests = [], []
    matched_choke_count, total_choke_count = 0, 0
    paths_with_geo = 0

    for p in data["paths"]:
        origin_countries, origin_regions = [], []
        for s in p.get("origin_regions", []):
            r = origin_resolved.get(s)
            if r is None:
                unresolved_origins.append(s)
            elif "countries" in r:
                origin_countries.extend(r["countries"])
            else:
                origin_regions.append({"name": r["region"], "lat": r["point"][0], "lng": r["point"][1]})

        dest_countries, dest_regions = [], []
        for s in p.get("destination_regions", []):
            r = dest_resolved.get(s)
            if r is None:
                unresolved_dests.append(s)
            elif "countries" in r:
                dest_countries.extend(r["countries"])
            else:
                dest_regions.append({"name": r["region"], "lat": r["point"][0], "lng": r["point"][1]})

        choke_points = []
        for s in p.get("chokepoints", []):
            total_choke_count += 1
            cps = match_chokepoints(s)
            if cps:
                matched_choke_count += 1
                for cp in cps:
                    if cp not in choke_points:
                        choke_points.append(cp)

        # Dedupe country lists, preserve order.
        origin_countries = list(dict.fromkeys(origin_countries))
        dest_countries = list(dict.fromkeys(dest_countries))

        has_geo = bool(origin_countries or origin_regions) and bool(dest_countries or dest_regions)
        if has_geo:
            paths_with_geo += 1

        out_paths.append({
            "id": p["id"],
            "name": p["name"],
            "category": p["category"],
            "commodities": p.get("commodities", []),
            "origin_countries": origin_countries,
            "origin_regions": origin_regions,
            "destination_countries": dest_countries,
            "destination_regions": dest_regions,
            "chokepoint_points": choke_points,
            "has_geo": has_geo,
        })

    out = {
        "schema_version": "1.0.0",
        "source_version": data.get("version"),
        "generated_from": "scripts/build_supply_chain_geo.py",
        "paths": out_paths,
    }
    OUT_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")

    print(f"paths: {len(out_paths)}, with both origin+destination geo: {paths_with_geo}")
    print(f"chokepoints matched: {matched_choke_count}/{total_choke_count} occurrences "
          f"({len(CHOKEPOINTS)} curated physical chokepoints)")
    print(f"unresolved origin strings: {len(unresolved_origins)}/{len(all_origin_strs)} distinct")
    print(f"unresolved destination strings: {len(unresolved_dests)}/{len(all_dest_strs)} distinct")
    Path("/tmp/unresolved_origins.txt").write_text("\n".join(sorted(set(unresolved_origins))))
    Path("/tmp/unresolved_dests.txt").write_text("\n".join(sorted(set(unresolved_dests))))


if __name__ == "__main__":
    main()
