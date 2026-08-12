"""Build data/world_intelligence_latest.json — MIRROR of
`pvt/global-intelligence/pipeline/build_world_intelligence.py` (private repo), same convention
already used for `scripts/world_intelligence_match.py` / `data/aspects.json` /
`data/ripple_effects.json`: kept in sync with the private repo's copy, only the data-path
constants differ since this repo's data files live flat under `data/` rather than split across
`taxonomy/`/`pipeline/`. See that file's own docstring for full design rationale, and
`docs/world-intelligence/README.md` in the private repo for the project plan + schema
documentation (the schema written here is byte-for-byte the same shape).

STATUS: this is the heavier, higher-outlet-count sibling of `build_briefing.py` — it fetches all
1,149 country-outlet feeds in `data/country_feeds.json` (vs. build_briefing.py's curated
~299-source `data/sources.json`), which is exactly why this new job lives in the PUBLIC repo
(free/unlimited Actions minutes) rather than the private repo (2,000 min/month cap). Runs via its
own new workflow, `.github/workflows/world_intelligence.yml` — does NOT touch `briefing.yml` or
`build_briefing.py`.

    python scripts/build_world_intelligence.py

Real full-scale run stats (first live run, 2026-08-12, from the private repo before this mirror
was scheduled): 1,149 outlets attempted, ~1,098 ok (~95.6%), ~13,000 fresh items, ~50
ripple-effect playbook matches, 29 signals emitted, ~80s total wall-clock. See
docs/world-intelligence/README.md (private repo) for the full write-up.

SCRAPED-FALLBACK SOURCE INTEGRATION (added 2026-08-11 -- see docs/world-intelligence/README.md
in the private repo for the full writeup): `data/scraped_headlines_fallback.json` holds headline
scrapes (no RSS) for 567 of the 993 `data/country_feeds.json` outlets that have no discoverable
feed. These items have NO reliable publish timestamp -- a scraped homepage can show evergreen/
pinned content that looks identical run after run, unlike an RSS item's genuine `pubDate`. To
avoid manufacturing false signals out of stale/pinned content, scraped items are integrated under
one hard rule: **a scraped item may only ADD to the source_count/country_count of a signal that
RSS items have already independently established as real; a scraped item can never
single-handedly create a new signal.** Concretely:
- Every item is tagged `source_type: "rss"` or `"scraped_fallback"` (see `_fetch_one()` /
  `scraped_items_from_fallback()`), carried through to each `sources[]` entry in the output.
- Scraped items are matched against aspects/playbooks exactly like RSS items (same
  world_intelligence_match functions), but are EXCLUDED from `corroboration_input` in `main()` --
  `detect_corroboration()` runs on RSS items only, so cluster existence/gating is byte-for-byte
  the same computation as before this change (verifiable equivalence, not just an approximation).
- Scraped items are given a sentinel `epoch=0` (oldest possible), so they can never become a
  signal's representative/headline item and never masquerade as fresh -- `matched_at` always
  reflects a genuine RSS item's timestamp.
- In `build_signals()`, once a signal already exists (an RSS-derived corroboration cluster, or a
  solo RSS item that matched a playbook), any scraped items sharing that same matched aspect/
  playbook id are looked up and their distinct outlets/countries are ADDED on top of the
  RSS-derived `source_count`/`country_count` (never replacing them) -- see the
  `scraped_fallback_sources_added` field on each signal's `corroboration` block, and the
  `outlets_scraped_fallback_contributing` stat.
"""

from __future__ import annotations

import calendar
import hashlib
import html
import json
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import world_intelligence_match as wim  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "data" / "world_intelligence_latest.json"
SCRAPED_FALLBACK_PATH = ROOT / "data" / "scraped_headlines_fallback.json"
USER_AGENT = "GlobalIntelligenceBot/0.1 (+https://equalinformation.com/global-intelligence-site/)"
FETCH_TIMEOUT = 12
MAX_WORKERS = 40
MAX_ITEMS_PER_SOURCE = 25
FRESH_HOURS = 26  # matches the live build_briefing.py freshness window
MAX_SOURCES_PER_SIGNAL = 10
SCHEMA_VERSION = "1.0.0"

_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str, limit: int = 400) -> str:
    text = html.unescape(_TAG_RE.sub(" ", text or ""))
    return " ".join(text.split())[:limit]


def _safe_url(url: str) -> str:
    """Only http(s) survives — a malicious/compromised feed could set <link> to a javascript:
    URI, which the app's in-app WKWebView/WebView would otherwise happily load. Same guard as
    build_briefing.py's _safe_url."""
    url = (url or "").strip()
    return url if url.lower().startswith(("http://", "https://")) else ""


def _entry_epoch(entry) -> float | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            return calendar.timegm(parsed)
    return None


def _fetch_one(outlet: dict) -> tuple[dict, list[dict], str | None]:
    url = outlet["feed_url"]
    try:
        resp = requests.get(url, timeout=FETCH_TIMEOUT, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        if feed.bozo and not feed.entries:
            return outlet, [], f"parse-error: {feed.bozo_exception}"
        cutoff = time.time() - FRESH_HOURS * 3600
        items = []
        for entry in feed.entries[:MAX_ITEMS_PER_SOURCE]:
            epoch = _entry_epoch(entry)
            if epoch is not None and epoch < cutoff:
                continue
            title = _clean(entry.get("title", ""), 300)
            if not title:
                continue
            items.append(
                {
                    "title": title,
                    "summary": _clean(entry.get("summary", "")),
                    "url": _safe_url(entry.get("link", "")),
                    "epoch": epoch or time.time(),
                    "source_id": outlet["id"],
                    "source_name": outlet["outlet"],
                    "country": outlet["country"],
                    "language": outlet["language"],
                    "state_affiliated": False,  # unknown for country_feeds outlets, not scored down
                    "source_type": "rss",  # genuine pubDate/updated-based freshness, see epoch above
                }
            )
        return outlet, items, None
    except Exception as exc:  # noqa: BLE001 — one bad feed must never kill the run
        return outlet, [], f"{type(exc).__name__}: {exc}"


def load_scraped_fallback() -> dict:
    """Load data/scraped_headlines_fallback.json (pre-scraped headlines for the 567 of 993
    feed-less country_feeds.json outlets that the private repo's `scrape_fallback.py` could
    extract content from -- a static snapshot, not re-fetched live by this script). Missing file
    is tolerated (returns an empty outlet list) so this integration degrades gracefully rather
    than crashing the whole pipeline if the file is ever absent."""
    if not SCRAPED_FALLBACK_PATH.exists():
        return {"outlets": []}
    return json.loads(SCRAPED_FALLBACK_PATH.read_text())


def scraped_items_from_fallback(data: dict) -> list[dict]:
    """Turn scraped_headlines_fallback.json's outlets into the same item shape _fetch_one()
    produces for RSS entries, tagged source_type="scraped_fallback" and given a sentinel
    epoch of 0.0 (the Unix epoch) rather than a fabricated "now" timestamp.

    WHY epoch=0.0, not time.time(): these headlines have NO reliable publish timestamp -- a
    scraped homepage can show evergreen/pinned content that looks identical run after run. Giving
    them the oldest-possible epoch means they always sort last wherever items are ordered by
    recency (build_signals()'s cluster reconstruction), so a scraped item can never become a
    signal's representative/headline item and never claims a fresh `matched_at`. It also means
    they are safe to exclude from `corroboration_input` in main() without any special-casing
    there -- see the module docstring's "SCRAPED-FALLBACK SOURCE INTEGRATION" section for the
    full design rationale."""
    items: list[dict] = []
    for outlet in data.get("outlets", []):
        for h in outlet.get("headlines", [])[:MAX_ITEMS_PER_SOURCE]:
            title = _clean(h.get("title", ""), 300)
            if not title:
                continue
            items.append(
                {
                    "title": title,
                    "summary": "",  # scrape_fallback.py extracts headline text only, no body/summary
                    "url": _safe_url(h.get("url", "")),
                    "epoch": 0.0,  # sentinel: unknown/no publish timestamp, see docstring above
                    "source_id": outlet["id"],
                    "source_name": outlet["outlet"],
                    "country": outlet["country"],
                    "language": outlet["language"],
                    "state_affiliated": False,
                    "source_type": "scraped_fallback",
                }
            )
    return items


def fetch_all(outlets: list[dict]) -> tuple[list[dict], dict]:
    items: list[dict] = []
    report = {"ok": [], "failed": {}}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(_fetch_one, o) for o in outlets]
        for fut in as_completed(futures):
            outlet, found, err = fut.result()
            if err:
                report["failed"][outlet["id"]] = err
            else:
                report["ok"].append(outlet["id"])
                items.extend(found)
    return items, report


def format_why_it_matters(playbook: dict) -> str:
    """Format a short 'why it matters' string purely from existing curated fields — no LLM.
    Uses the most recent historical_precedent (last in the list; playbooks are authored in
    chronological order) + market_impact, plus the first possibility_chains entry."""
    parts = []
    precedents = playbook.get("historical_precedents") or []
    if precedents:
        hp = precedents[-1]
        event = hp.get("event", "")
        period = hp.get("period", "")
        impact = hp.get("market_impact", "")
        label = f"{event} ({period})" if period else event
        if label and impact:
            parts.append(f"{label}: {impact}")
        elif label:
            parts.append(label)
    chains = playbook.get("possibility_chains") or []
    if chains:
        parts.append(chains[0])
    return " ".join(parts)


def _signal_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _source_entry(item: dict) -> dict:
    source_type = item.get("source_type", "rss")
    entry = {
        "title": item["title"],
        "source_name": item["source_name"],
        "country": item["country"],
        "language": item["language"],
        "url": item["url"],
        "source_type": source_type,
    }
    if source_type == "rss":
        entry["age_hours"] = round(max(0.0, (time.time() - item["epoch"]) / 3600), 1)
    else:
        # No reliable publish timestamp for scraped-fallback items (see module docstring) --
        # reporting a fabricated age would be dishonest, so this is explicitly null rather than
        # a huge/misleading number derived from the epoch=0.0 sentinel.
        entry["age_hours"] = None
    return entry


def build_signals(
    matched_items: list[dict],
    corroboration: dict[str, dict],
    aspects_by_id: dict[str, dict],
    category_context_lookup: dict[str, dict],
    playbooks_by_id: dict[str, dict],
) -> tuple[list[dict], set[str]]:
    """Turn matched items + corroboration clusters into the public 'signals' list. Two sources:
    (1) playbook_corroboration clusters (specific ripple-effects trigger, >=2 distinct outlets),
    and (2) every item that matched a ripple-effects playbook but did NOT land in any
    corroboration cluster — surfaced solo as a 'playbook_single_source' signal, since a curated
    playbook match is valuable even from a single outlet. Plain aspect-only matches with no
    corroboration are NOT surfaced as individual signals (too voluminous/noisy at 1000+ outlet
    scale).

    `matched_items` contains BOTH rss and scraped_fallback items (tagged `_source_type`), but a
    scraped item may only ADD to a signal's source_count/country_count -- it can never establish
    a signal's existence on its own. That gate is already enforced upstream: (a) `corroboration`
    (passed in) was computed from RSS items only, so cluster existence is untouched by anything
    here; (b) the solo-match loop below explicitly requires `_source_type == "rss"` before it will
    create a new `playbook_single_source` signal. Scraped items only ever get folded in via
    `_scraped_additions()`, additively, on top of an already-decided-real signal.

    Returns (signals, scraped_outlets_contributing) -- the latter is the set of distinct
    scraped-fallback outlet ids that ended up padding at least one real signal's corroboration,
    used for the `outlets_scraped_fallback_contributing` stat."""
    items_by_matched_id: dict[str, list[dict]] = defaultdict(list)
    for m in matched_items:
        key = m["ripple_effect"]["id"] if m["ripple_effect"] else (m["aspects"][0]["id"] if m["aspects"] else None)
        if key:
            items_by_matched_id[key].append(m)

    signals: list[dict] = []
    covered_signal_keys: set[str] = set()
    scraped_outlets_contributing: set[str] = set()

    def _scraped_additions(matched_id: str, existing_source_ids: set[str]) -> tuple[set[str], set[str]]:
        """Distinct scraped-fallback source_ids (+ their countries) hitting `matched_id` that are
        NOT already counted among `existing_source_ids` -- the only mechanism by which a scraped
        item is allowed to influence a signal's reported counts. Never used to decide whether a
        signal exists, only to pad one that already does."""
        new_ids: set[str] = set()
        new_countries: set[str] = set()
        for c in items_by_matched_id.get(matched_id, []):
            if c.get("_source_type") != "scraped_fallback":
                continue
            sid = c.get("_source_id")
            if not sid or sid in existing_source_ids:
                continue
            new_ids.add(sid)
            country = c["_source_entry"].get("country")
            if country:
                new_countries.add(country)
        return new_ids, new_countries

    def _playbook_block(playbook: dict) -> dict:
        linked = []
        for linked_id in playbook.get("linked_playbooks", []):
            lp = playbooks_by_id.get(linked_id)
            if lp is None:
                continue
            linked.append(
                {
                    "id": lp["id"],
                    "name": lp["name"],
                    "affected_domains": lp.get("affected_domains", []),
                    "why_it_matters": format_why_it_matters(lp),
                }
            )
        return {
            "id": playbook["id"],
            "name": playbook["name"],
            "affected_domains": playbook.get("affected_domains", []),
            "historical_precedents": playbook.get("historical_precedents", []),
            "possibility_chains": playbook.get("possibility_chains", []),
            "why_it_matters": format_why_it_matters(playbook),
            "linked_playbooks": linked,
        }

    # NOTE: `topic_momentum` corroboration clusters (broad aspect ids) are deliberately EXCLUDED
    # from the shipped `signals` list — a real full-scale run showed they are not selective at
    # 1,149-outlet scale (nearly every broad aspect trivially clears the volume threshold tuned
    # for a much smaller validation sample, producing misleading "signals" like 171 sources
    # across 94 countries for a single broad topic). Their counts are still reported in `stats`.
    # See docs/world-intelligence/README.md (private repo) for the full real-data writeup.
    for sig_key, sig in corroboration.items():
        if sig["signal_type"] == "topic_momentum":
            continue
        matched_id = sig["matched_id"]
        window_start_epoch = sig_key.rsplit(":", 1)[-1]
        candidates = items_by_matched_id.get(matched_id, [])
        cluster_items = sorted(candidates, key=lambda c: -c["_epoch"])[:MAX_SOURCES_PER_SIGNAL]

        playbook = playbooks_by_id.get(matched_id)
        aspect = aspects_by_id.get(matched_id)
        representative = cluster_items[0] if cluster_items else None

        sources_out = [c["_source_entry"] for c in cluster_items[:MAX_SOURCES_PER_SIGNAL]] if cluster_items else []
        for c in cluster_items:
            covered_signal_keys.add((matched_id, c.get("_url")))

        # Scraped-fallback augmentation: ADD to the RSS-derived (sig["source_count"]/
        # ["country_count"]) counts, never replace them -- this cluster's existence was already
        # decided from RSS items alone, before this function even ran (see `corroboration` in
        # main()). See module docstring / _scraped_additions() above.
        new_scraped_ids, new_scraped_countries = _scraped_additions(matched_id, set(sig["sources"]))
        scraped_outlets_contributing |= new_scraped_ids
        source_count = sig["source_count"] + len(new_scraped_ids)
        combined_countries = set(sig["countries"]) | new_scraped_countries
        country_count = len(combined_countries)
        label = (
            f"{source_count} sources across {country_count} countries"
            if country_count > 1 else f"{source_count} sources"
        )

        signal = {
            "signal_id": _signal_id(sig["signal_type"], matched_id, window_start_epoch),
            "signal_type": sig["signal_type"],
            "headline": representative["title"] if representative else (sig["example_titles"][0] if sig["example_titles"] else matched_id),
            "matched_at": representative["_iso"] if representative else None,
            "matched_language": representative.get("matched_language") if representative else None,
            "aspects": ([{"id": a["id"], "score": a["score"]} for a in representative["aspects"][:3]] if representative else []),
            "corroboration": {
                "tier": sig["signal_type"],
                "source_count": source_count,
                "country_count": country_count,
                "countries": sorted(combined_countries),
                "label": label,
                "scraped_fallback_sources_added": len(new_scraped_ids),
            },
            "sources": sources_out,
        }
        if playbook is not None:
            signal["ripple_effect"] = _playbook_block(playbook)
            signal["category_context"] = None
        elif aspect is not None:
            signal["ripple_effect"] = None
            signal["category_context"] = category_context_lookup.get(aspect["category"])
        signals.append(signal)

    # A scraped item may never create this kind of signal by itself -- only an RSS item (whose
    # freshness is genuinely verified via pubDate) can. Scraped items sharing the same playbook
    # id are folded in AFTER as pure addition, same as the cluster path above.
    for m in matched_items:
        if m.get("_source_type") != "rss":
            continue
        ripple = m["ripple_effect"]
        if not ripple:
            continue
        key = (ripple["id"], m.get("_url"))
        if key in covered_signal_keys:
            continue
        playbook = playbooks_by_id.get(ripple["id"])
        if playbook is None:
            continue

        base_source = m["_source_entry"]
        new_scraped_ids, new_scraped_countries = _scraped_additions(ripple["id"], {m.get("_source_id")})
        scraped_outlets_contributing |= new_scraped_ids
        source_count = 1 + len(new_scraped_ids)
        combined_countries = {base_source["country"]} | new_scraped_countries
        country_count = len(combined_countries)

        seen_scraped_ids: set[str] = set()
        extra_sources = []
        for c in items_by_matched_id.get(ripple["id"], []):
            sid = c.get("_source_id")
            if c.get("_source_type") != "scraped_fallback" or sid not in new_scraped_ids or sid in seen_scraped_ids:
                continue
            seen_scraped_ids.add(sid)
            extra_sources.append(c["_source_entry"])
        sources_out = ([base_source] + extra_sources)[:MAX_SOURCES_PER_SIGNAL]

        signal = {
            "signal_id": _signal_id("playbook_single_source", ripple["id"], m.get("_url") or m["title"]),
            "signal_type": "playbook_single_source",
            "headline": m["title"],
            "matched_at": m["_iso"],
            "matched_language": m.get("matched_language"),
            "aspects": [{"id": a["id"], "score": a["score"]} for a in m["aspects"][:3]],
            "ripple_effect": _playbook_block(playbook),
            "category_context": None,
            "corroboration": {
                "tier": "single_source",
                "source_count": source_count,
                "country_count": country_count,
                "countries": sorted(combined_countries),
                "label": (
                    f"{source_count} sources across {country_count} countries"
                    if country_count > 1
                    else (f"{source_count} sources" if source_count > 1 else "1 source")
                ),
                "scraped_fallback_sources_added": len(new_scraped_ids),
            },
            "sources": sources_out,
        }
        signals.append(signal)

    signals.sort(key=lambda s: s["matched_at"] or "", reverse=True)
    return signals, scraped_outlets_contributing


def main() -> None:
    t0 = time.time()
    country_feeds = wim.load_country_feeds()
    outlets = [o for o in country_feeds["outlets"] if o.get("feed_url")]
    print(f"Outlets with a discovered feed: {len(outlets)}")

    print("Fetching all feeds...")
    fetch_t0 = time.time()
    items, report = fetch_all(outlets)
    fetch_elapsed = time.time() - fetch_t0
    print(f"outlets: ok={len(report['ok'])} failed={len(report['failed'])} "
          f"(fetch took {fetch_elapsed:.1f}s)")
    print(f"fetched items (<= {FRESH_HOURS}h old): {len(items)}")
    if not items:
        print("ERROR: 0 items fetched — aborting without writing output (avoids overwriting the "
              "last good file with an empty one).", file=sys.stderr)
        sys.exit(1)

    print("Loading scraped-fallback headlines (static snapshot, no live fetch)...")
    scraped_data = load_scraped_fallback()
    scraped_items = scraped_items_from_fallback(scraped_data)
    scraped_outlets_total = len(scraped_data.get("outlets", []))
    print(f"scraped-fallback outlets available: {scraped_outlets_total}, "
          f"headline items loaded: {len(scraped_items)}")

    aspects_en = wim.load_aspects_en()
    aspects_ml = wim.load_aspects_multilingual()
    ripple_effects = wim.load_ripple_effects()
    ripple_ml = wim.load_ripple_multilingual()
    category_context = json.loads((ROOT / "data" / "category_context.json").read_text())
    category_context_lookup = {c["id"]: c for c in category_context["categories"]}
    aspects_by_id = {a["id"]: a for a in aspects_en["aspects"]}
    playbooks_by_id = {p["id"]: p for p in ripple_effects["playbooks"]}
    compiled_playbooks = wim.compile_playbooks_multilingual(ripple_effects, ripple_ml)

    # Scraped-fallback items participate in matching (below) alongside RSS items -- so they CAN
    # be attached to an already-real signal -- but are excluded from `corroboration_input` and
    # every RSS-only stat counter (matched_aspect_item_count, ripple_hit_count, lang_counter,
    # etc.), gated via `is_rss` in the loop below. This is what makes the RSS-only path of this
    # run byte-for-byte the same computation as before this integration existed. See the module
    # docstring's "SCRAPED-FALLBACK SOURCE INTEGRATION" section.
    all_items = items + scraped_items

    print("Matching against multilingual aspects...")
    buckets = wim.match_aspects_multilingual(all_items, aspects_en, aspects_ml)
    item_aspect_hits: dict[int, list[tuple[str, float]]] = defaultdict(list)
    for aspect_id, scored in buckets.items():
        for score, item in scored:
            item_aspect_hits[id(item)].append((aspect_id, score))

    print("Matching against ripple-effect playbooks (+ one-hop linked_playbooks)...")
    matched_items_out = []
    corroboration_input: list[tuple[str, dict]] = []
    ripple_hit_count = 0
    linked_chain_count = 0
    matched_aspect_item_count = 0
    lang_counter: dict[str, int] = defaultdict(int)
    scraped_items_matched = 0  # informational: scraped items that matched *something* at all

    for item in all_items:
        is_rss = item.get("source_type", "rss") == "rss"
        lang = item.get("_matched_language") or wim.normalize_language(item.get("language"))
        aspect_hits = sorted(item_aspect_hits.get(id(item), []), key=lambda p: -p[1])
        ripple = wim.match_ripple_effect_with_links(item, compiled_playbooks, playbooks_by_id)

        if is_rss:
            lang_counter[lang] += 1
            if aspect_hits:
                matched_aspect_item_count += 1
            if ripple:
                ripple_hit_count += 1
                if ripple["linked_playbooks"]:
                    linked_chain_count += 1
            matched_id = ripple["id"] if ripple else (aspect_hits[0][0] if aspect_hits else None)
            if matched_id:
                corroboration_input.append((matched_id, item))
        elif aspect_hits or ripple:
            scraped_items_matched += 1

        if aspect_hits or ripple:
            iso = datetime.fromtimestamp(item["epoch"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            matched_items_out.append(
                {
                    "title": item["title"],
                    "source": item["source_name"],
                    "matched_language": lang,
                    "aspects": [{"id": aid, "score": score} for aid, score in aspect_hits[:5]],
                    "ripple_effect": ripple,
                    "_url": item["url"],
                    "_epoch": item["epoch"],
                    "_iso": iso,
                    "_source_entry": _source_entry(item),
                    "_source_id": item["source_id"],
                    "_source_type": item.get("source_type", "rss"),
                }
            )

    print("Detecting cross-source corroboration (RSS items only -- see module docstring)...")
    corroboration = wim.detect_corroboration(
        corroboration_input, narrow_ids=set(playbooks_by_id.keys())
    )
    playbook_signals = {k: v for k, v in corroboration.items() if v["signal_type"] == "playbook_corroboration"}
    topic_signals = {k: v for k, v in corroboration.items() if v["signal_type"] == "topic_momentum"}

    print("Building signals (scraped-fallback items may only ADD corroboration to these)...")
    signals, scraped_outlets_contributing = build_signals(
        matched_items_out, corroboration, aspects_by_id, category_context_lookup, playbooks_by_id
    )

    elapsed = time.time() - t0
    print("\n===== SUMMARY =====")
    print(f"outlets fetched ok: {len(report['ok'])}/{len(outlets)}")
    print(f"fresh items: {len(items)}")
    print(f"items matched >=1 aspect: {matched_aspect_item_count}")
    print(f"items matched a ripple-effect playbook: {ripple_hit_count} "
          f"(with linked chain: {linked_chain_count})")
    print(f"corroboration clusters: {len(corroboration)} "
          f"({len(playbook_signals)} playbook_corroboration, {len(topic_signals)} topic_momentum)")
    print(f"signals emitted: {len(signals)}")
    print(f"scraped-fallback: {scraped_outlets_total} outlets available, {len(scraped_items)} "
          f"items loaded, {scraped_items_matched} matched an aspect/playbook, "
          f"{len(scraped_outlets_contributing)} outlets actually added corroboration to a real "
          f"(RSS-established) signal")
    print(f"fetch elapsed: {fetch_elapsed:.1f}s, total elapsed: {elapsed:.1f}s")

    out = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": (
            "World Intelligence signals: multilingual aspect + ripple-effect playbook matching "
            "(with one-hop linked_playbooks chains) and cross-source corroboration, run against "
            "every country-outlet feed discovered in data/country_feeds.json, PLUS "
            "scraped_headlines_fallback.json headline scrapes for feed-less outlets (source_type "
            "'scraped_fallback' vs 'rss' -- scraped items can only ADD corroboration to a signal "
            "an RSS item already established as real, never create one on their own, since "
            "scraped headlines have no reliable publish timestamp). Zero-cost, zero-LLM, "
            "keyword-matching + curated playbook data only. NOT yet wired into "
            "data/briefing.json / the live Briefing tab — a standalone, additive dataset for now."
        ),
        "stats": {
            "outlets_total_with_feed": len(outlets),
            "outlets_ok": len(report["ok"]),
            "outlets_failed": len(report["failed"]),
            "fresh_items": len(items),
            "items_matched_aspect": matched_aspect_item_count,
            "items_matched_ripple_playbook": ripple_hit_count,
            "items_with_linked_playbook_chain": linked_chain_count,
            "corroboration_clusters_total": len(corroboration),
            "corroboration_clusters_playbook": len(playbook_signals),
            "corroboration_clusters_topic_momentum": len(topic_signals),
            "signals_total": len(signals),
            "matched_language_distribution": dict(sorted(lang_counter.items(), key=lambda p: -p[1])),
            "fetch_elapsed_seconds": round(fetch_elapsed, 1),
            "total_elapsed_seconds": round(elapsed, 1),
            "outlets_total_scraped_fallback": scraped_outlets_total,
            "outlets_total_combined": len(outlets) + scraped_outlets_total,
            "scraped_fallback_items_loaded": len(scraped_items),
            "scraped_fallback_items_matched": scraped_items_matched,
            "outlets_scraped_fallback_contributing": len(scraped_outlets_contributing),
        },
        "signals": signals,
    }
    # Write to a temp file then rename into place — same atomic-write pattern as
    # build_briefing.py, so a runner killed mid-write can never leave a truncated file behind.
    tmp = OUT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    os.replace(tmp, OUT_PATH)
    print(f"\nWrote {OUT_PATH} ({OUT_PATH.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
