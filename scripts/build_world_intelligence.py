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
                }
            )
        return outlet, items, None
    except Exception as exc:  # noqa: BLE001 — one bad feed must never kill the run
        return outlet, [], f"{type(exc).__name__}: {exc}"


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
    return {
        "title": item["title"],
        "source_name": item["source_name"],
        "country": item["country"],
        "language": item["language"],
        "url": item["url"],
        "age_hours": round(max(0.0, (time.time() - item["epoch"]) / 3600), 1),
    }


def build_signals(
    matched_items: list[dict],
    corroboration: dict[str, dict],
    aspects_by_id: dict[str, dict],
    category_context_lookup: dict[str, dict],
    playbooks_by_id: dict[str, dict],
) -> list[dict]:
    """Turn matched items + corroboration clusters into the public 'signals' list. Two sources:
    (1) playbook_corroboration clusters (specific ripple-effects trigger, >=2 distinct outlets),
    and (2) every item that matched a ripple-effects playbook but did NOT land in any
    corroboration cluster — surfaced solo as a 'playbook_single_source' signal, since a curated
    playbook match is valuable even from a single outlet. Plain aspect-only matches with no
    corroboration are NOT surfaced as individual signals (too voluminous/noisy at 1000+ outlet
    scale)."""
    items_by_matched_id: dict[str, list[dict]] = defaultdict(list)
    for m in matched_items:
        key = m["ripple_effect"]["id"] if m["ripple_effect"] else (m["aspects"][0]["id"] if m["aspects"] else None)
        if key:
            items_by_matched_id[key].append(m)

    signals: list[dict] = []
    covered_signal_keys: set[str] = set()

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

        signal = {
            "signal_id": _signal_id(sig["signal_type"], matched_id, window_start_epoch),
            "signal_type": sig["signal_type"],
            "headline": representative["title"] if representative else (sig["example_titles"][0] if sig["example_titles"] else matched_id),
            "matched_at": representative["_iso"] if representative else None,
            "matched_language": representative.get("matched_language") if representative else None,
            "aspects": ([{"id": a["id"], "score": a["score"]} for a in representative["aspects"][:3]] if representative else []),
            "corroboration": {
                "tier": sig["signal_type"],
                "source_count": sig["source_count"],
                "country_count": sig["country_count"],
                "countries": sig["countries"],
                "label": sig["label"],
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

    for m in matched_items:
        ripple = m["ripple_effect"]
        if not ripple:
            continue
        key = (ripple["id"], m.get("_url"))
        if key in covered_signal_keys:
            continue
        playbook = playbooks_by_id.get(ripple["id"])
        if playbook is None:
            continue
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
                "source_count": 1,
                "country_count": 1,
                "countries": [m["_source_entry"]["country"]],
                "label": "1 source",
            },
            "sources": [m["_source_entry"]],
        }
        signals.append(signal)

    signals.sort(key=lambda s: s["matched_at"] or "", reverse=True)
    return signals


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

    aspects_en = wim.load_aspects_en()
    aspects_ml = wim.load_aspects_multilingual()
    ripple_effects = wim.load_ripple_effects()
    ripple_ml = wim.load_ripple_multilingual()
    category_context = json.loads((ROOT / "data" / "category_context.json").read_text())
    category_context_lookup = {c["id"]: c for c in category_context["categories"]}
    aspects_by_id = {a["id"]: a for a in aspects_en["aspects"]}
    playbooks_by_id = {p["id"]: p for p in ripple_effects["playbooks"]}
    compiled_playbooks = wim.compile_playbooks_multilingual(ripple_effects, ripple_ml)

    print("Matching against multilingual aspects...")
    buckets = wim.match_aspects_multilingual(items, aspects_en, aspects_ml)
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

    for item in items:
        lang = item.get("_matched_language") or wim.normalize_language(item.get("language"))
        lang_counter[lang] += 1
        aspect_hits = sorted(item_aspect_hits.get(id(item), []), key=lambda p: -p[1])
        ripple = wim.match_ripple_effect_with_links(item, compiled_playbooks, playbooks_by_id)

        if aspect_hits:
            matched_aspect_item_count += 1
        if ripple:
            ripple_hit_count += 1
            if ripple["linked_playbooks"]:
                linked_chain_count += 1

        matched_id = ripple["id"] if ripple else (aspect_hits[0][0] if aspect_hits else None)
        if matched_id:
            corroboration_input.append((matched_id, item))

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
                }
            )

    print("Detecting cross-source corroboration...")
    corroboration = wim.detect_corroboration(
        corroboration_input, narrow_ids=set(playbooks_by_id.keys())
    )
    playbook_signals = {k: v for k, v in corroboration.items() if v["signal_type"] == "playbook_corroboration"}
    topic_signals = {k: v for k, v in corroboration.items() if v["signal_type"] == "topic_momentum"}

    print("Building signals...")
    signals = build_signals(
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
    print(f"fetch elapsed: {fetch_elapsed:.1f}s, total elapsed: {elapsed:.1f}s")

    out = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "note": (
            "World Intelligence signals: multilingual aspect + ripple-effect playbook matching "
            "(with one-hop linked_playbooks chains) and cross-source corroboration, run against "
            "every country-outlet feed discovered in data/country_feeds.json. Zero-cost, "
            "zero-LLM, keyword-matching + curated playbook data only. NOT yet wired into "
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
