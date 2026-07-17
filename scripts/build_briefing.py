"""Build data/briefing.json — live headlines + top stories per aspect for the site's cards.

100% open source, zero cost: fetches the public RSS feeds in data/sources.json,
matches items to the aspects in data/aspects.json by WORD-BOUNDARY keyword scoring
(mirrors pvt/global-intelligence/pipeline/match.py — keep both in sync), ranks the
most influential current stories by cross-aspect/category breadth, tags headlines/top
stories with a "ripple effect" playbook when one matches (data/ripple_effects.json —
historical precedent + hedged possibilities, zero AI cost, curated offline), and writes
the top headlines + top_stories per run. Runs every 4 hours via GitHub Actions (free on
public repos).

    python scripts/build_briefing.py
"""

import calendar
import html
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).resolve().parent.parent
USER_AGENT = "GlobalIntelligenceBot/0.1 (+https://equalinformation.com/global-intelligence-site/)"
FRESH_HOURS = 26
FETCH_TIMEOUT = 20
MAX_ITEMS_PER_SOURCE = 40
HEADLINES_PER_ASPECT = 3
MIN_SCORE = 1.5  # a single title hit (3.0) qualifies alone; a lone summary hit (1.0) doesn't
TOP_STORIES_COUNT = 10

_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str, limit: int = 300) -> str:
    text = html.unescape(_TAG_RE.sub(" ", text or ""))
    return " ".join(text.split())[:limit]


def _safe_url(url: str) -> str:
    """Only http(s) links survive — a malicious or compromised RSS feed could otherwise set
    <link> to a javascript: URI, which every consumer of this url (this site's plain <a href>,
    and the native apps' Custom Tabs/Safari View Controller) would treat as a real, clickable
    link. Dropping anything else to "" is safe: both apps and this site already treat an empty
    url as a legitimate "no link" state (e.g. Headline.id falls back to the title)."""
    url = (url or "").strip()
    return url if url.lower().startswith(("http://", "https://")) else ""


def _entry_epoch(entry) -> float | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            return calendar.timegm(parsed)
    return None


def _fetch_one(source: dict) -> tuple[dict, list[dict], str | None]:
    url = source.get("rss")
    if not url:
        return source, [], "no-rss"
    try:
        resp = requests.get(url, timeout=FETCH_TIMEOUT, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        if feed.bozo and not feed.entries:
            return source, [], "parse-error"
        cutoff = time.time() - FRESH_HOURS * 3600
        items = []
        for entry in feed.entries[:MAX_ITEMS_PER_SOURCE]:
            epoch = _entry_epoch(entry)
            if epoch is not None and epoch < cutoff:
                continue
            title = _clean(entry.get("title", ""))
            if not title:
                continue
            items.append(
                {
                    "title": title,
                    "summary": _clean(entry.get("summary", ""), 400),
                    "url": _safe_url(entry.get("link", "")),
                    "epoch": epoch or time.time(),
                    "source": source["name"],
                    "state_affiliated": source.get("state_affiliated", False),
                }
            )
        return source, items, None
    except Exception:  # noqa: BLE001 — one bad feed must never kill the build
        return source, [], "fetch-error"


def _pattern(keyword: str) -> re.Pattern:
    return re.compile(r"\b" + re.escape(keyword.strip()) + r"\b")


def _compile_taxonomy(taxonomy: dict) -> dict:
    compiled = {}
    for aspect in taxonomy["aspects"]:
        compiled[aspect["id"]] = {
            "keywords": [_pattern(kw) for kw in aspect["keywords"]],
            "exclude": [_pattern(kw) for kw in aspect.get("exclude_keywords", [])],
        }
    return compiled


def _compile_playbooks(ripple_effects: dict) -> list[dict]:
    """Precompile each playbook's trigger keywords once per run."""
    compiled = []
    for pb in ripple_effects["playbooks"]:
        compiled.append({**pb, "_patterns": [_pattern(kw) for kw in pb["keywords"]]})
    return compiled


def _match_ripple_effect(item: dict, compiled_playbooks: list[dict]) -> dict | None:
    """Best-matching playbook for one item (title + summary), or None. "Best" = most
    distinct trigger keywords hit — ties broken by playbook order in the source file.
    Keeps a single, unambiguous ripple-effect panel per story rather than stacking several.
    """
    text = f"{item['title'].lower()} {item['summary'].lower()}"
    best, best_hits = None, 0
    for pb in compiled_playbooks:
        hits = sum(1 for pat in pb["_patterns"] if pat.search(text))
        if hits > best_hits:
            best, best_hits = pb, hits
    if best is None:
        return None
    return {
        "id": best["id"],
        "name": best["name"],
        "historical_precedents": best["historical_precedents"],
        "possibility_chains": best["possibility_chains"],
    }


def _category_context_lookup(category_context: dict) -> dict:
    """id -> {why_it_matters, typical_channels} for the fallback tier."""
    return {c["id"]: c for c in category_context["categories"]}


def _category_context_for(category_id: str, lookup: dict) -> dict | None:
    """Fallback context for a headline/story whose category didn't match a specific
    ripple_effects playbook — general, structural "why this matters" content, never
    framed as a specific historical claim. Guarantees every item has some context.
    """
    entry = lookup.get(category_id)
    if entry is None:
        return None
    return {
        "id": entry["id"],
        "why_it_matters": entry["why_it_matters"],
        "typical_channels": entry["typical_channels"],
    }


def _score_all(items: list[dict], taxonomy: dict) -> dict[str, list[tuple[float, dict]]]:
    """One matching pass, word-boundary regex — mirrors pipeline/match.py exactly."""
    compiled = _compile_taxonomy(taxonomy)
    buckets: dict[str, list[tuple[float, dict]]] = {a["id"]: [] for a in taxonomy["aspects"]}
    for item in items:
        title = item["title"].lower()
        summary = item["summary"].lower()
        text = f"{title} {summary}"
        for aspect_id, rules in compiled.items():
            if any(pat.search(text) for pat in rules["exclude"]):
                continue
            score = 0.0
            for pat in rules["keywords"]:
                if pat.search(title):
                    score += 3.0
                elif pat.search(summary):
                    score += 1.0
            if score >= MIN_SCORE:
                if item["state_affiliated"]:
                    score *= 0.8
                buckets[aspect_id].append((score, item))
    return buckets


def _rank_top_stories(
    buckets: dict,
    taxonomy: dict,
    compiled_playbooks: list[dict],
    category_context_lookup: dict,
    top_n: int = TOP_STORIES_COUNT,
) -> list[dict]:
    """Most influential current stories: breadth (aspects + 2x categories touched) x recency."""
    aspect_to_category = {a["id"]: a["category"] for a in taxonomy["aspects"]}
    category_names = {c["id"]: c["name"] for c in taxonomy["categories"]}

    by_key: dict[str, dict] = {}
    for aspect_id, scored in buckets.items():
        for score, item in scored:
            key = item["url"] or item["title"]
            entry = by_key.setdefault(key, {"item": item, "aspect_ids": set(), "category_ids": set()})
            entry["aspect_ids"].add(aspect_id)
            entry["category_ids"].add(aspect_to_category[aspect_id])

    now = time.time()
    ranked = []
    for entry in by_key.values():
        item = entry["item"]
        age_hours = max(0.0, (now - item["epoch"]) / 3600)
        recency_weight = max(0.15, 1 - age_hours / FRESH_HOURS)
        breadth = len(entry["aspect_ids"]) + 2 * len(entry["category_ids"])
        ranked.append((breadth * recency_weight, entry))
    ranked.sort(key=lambda pair: (-pair[0], -pair[1]["item"]["epoch"]))

    out = []
    for rank, (_, entry) in enumerate(ranked[:top_n], start=1):
        item = entry["item"]
        ripple_effect = _match_ripple_effect(item, compiled_playbooks)
        category_context = None
        if not ripple_effect:
            primary_category_id = min(entry["category_ids"], key=lambda cid: category_names[cid])
            category_context = _category_context_for(primary_category_id, category_context_lookup)
        out.append(
            {
                "rank": rank,
                "title": item["title"],
                "source": item["source"],
                "url": item["url"],
                "age_hours": round(max(0.0, (now - item["epoch"]) / 3600), 1),
                "categories": sorted(category_names[cid] for cid in entry["category_ids"]),
                "aspect_count": len(entry["aspect_ids"]),
                **({"ripple_effect": ripple_effect} if ripple_effect else {}),
                **({"category_context": category_context} if category_context else {}),
            }
        )
    return out


def main() -> None:
    sources = json.loads((ROOT / "data" / "sources.json").read_text())["sources"]
    taxonomy = json.loads((ROOT / "data" / "aspects.json").read_text())
    ripple_effects = json.loads((ROOT / "data" / "ripple_effects.json").read_text())
    compiled_playbooks = _compile_playbooks(ripple_effects)
    category_context = json.loads((ROOT / "data" / "category_context.json").read_text())
    category_context_lookup = _category_context_lookup(category_context)

    items: list[dict] = []
    ok = failed = 0
    with ThreadPoolExecutor(max_workers=16) as pool:
        for fut in as_completed([pool.submit(_fetch_one, s) for s in sources]):
            _, found, err = fut.result()
            if err:
                failed += 1
            else:
                ok += 1
                items.extend(found)

    # Dedupe wire copies by near-identical title.
    seen: set[str] = set()
    unique = []
    for item in sorted(items, key=lambda i: -i["epoch"]):
        key = item["title"].lower()[:120]
        if key not in seen:
            seen.add(key)
            unique.append(item)

    # A total (or near-total) fetch failure — e.g. a transient network outage on the runner —
    # used to still write and get committed as data/briefing.json, silently replacing a fully
    # populated briefing with an empty one live for every visitor. Aborting before writing
    # leaves the previous good commit in place and fails the CI job loudly instead.
    if ok == 0 or not unique:
        print(f"ERROR: 0 sources succeeded (or 0 fresh items found) — aborting without writing "
              f"{ROOT / 'data' / 'briefing.json'} to avoid overwriting the last good briefing. "
              f"ok={ok} failed/no-rss={failed} items={len(unique)}", file=sys.stderr)
        sys.exit(1)

    buckets = _score_all(unique, taxonomy)
    top_stories = _rank_top_stories(buckets, taxonomy, compiled_playbooks, category_context_lookup)

    now = time.time()
    categories = []
    covered = 0
    for cat in taxonomy["categories"]:
        cat_aspects = []
        for aspect in taxonomy["aspects"]:
            if aspect["category"] != cat["id"]:
                continue
            scored = sorted(buckets[aspect["id"]], key=lambda p: (-p[0], -p[1]["epoch"]))
            heads = []
            for _, i in scored[:HEADLINES_PER_ASPECT]:
                ripple_effect = _match_ripple_effect(i, compiled_playbooks)
                category_context = (
                    None if ripple_effect else _category_context_for(cat["id"], category_context_lookup)
                )
                heads.append(
                    {
                        "title": i["title"],
                        "source": i["source"],
                        "url": i["url"],
                        "age_hours": round(max(0.0, (now - i["epoch"]) / 3600), 1),
                        **({"ripple_effect": ripple_effect} if ripple_effect else {}),
                        **({"category_context": category_context} if category_context else {}),
                    }
                )
            if heads:
                covered += 1
            cat_aspects.append(
                {
                    "id": aspect["id"],
                    "name": aspect["name"],
                    "influences": aspect.get("influences", []),
                    "headlines": heads,
                }
            )
        categories.append({"id": cat["id"], "name": cat["name"], "aspects": cat_aspects})

    briefing = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stats": {
            "sources_ok": ok,
            "sources_failed_or_no_rss": failed,
            "fresh_items": len(unique),
            "aspects_covered": covered,
            "aspects_total": len(taxonomy["aspects"]),
        },
        "top_stories": top_stories,
        "categories": categories,
    }
    out = ROOT / "data" / "briefing.json"
    out.write_text(json.dumps(briefing, ensure_ascii=False, indent=1))
    print(f"ok={ok} failed/no-rss={failed} items={len(unique)} covered={covered}/41 top_stories={len(top_stories)} → {out}")


if __name__ == "__main__":
    main()
