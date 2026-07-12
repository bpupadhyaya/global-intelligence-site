"""Build data/briefing.json — live headlines per aspect for the site's cards.

100% open source, zero cost: fetches the public RSS feeds in data/sources.json,
matches items to the aspects in data/aspects.json by keyword, and writes the top
headlines per aspect. Runs every 4 hours via GitHub Actions (free on public repos).

    python scripts/build_briefing.py
"""

import calendar
import html
import json
import re
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

_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str, limit: int = 300) -> str:
    text = html.unescape(_TAG_RE.sub(" ", text or ""))
    return " ".join(text.split())[:limit]


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
                    "url": entry.get("link", ""),
                    "epoch": epoch or time.time(),
                    "source": source["name"],
                    "state_affiliated": source.get("state_affiliated", False),
                }
            )
        return source, items, None
    except Exception:  # noqa: BLE001 — one bad feed must never kill the build
        return source, [], "fetch-error"


def main() -> None:
    sources = json.loads((ROOT / "data" / "sources.json").read_text())["sources"]
    taxonomy = json.loads((ROOT / "data" / "aspects.json").read_text())

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

    # Match to aspects by keyword score (title hits weigh 3x).
    buckets: dict[str, list[tuple[float, dict]]] = {a["id"]: [] for a in taxonomy["aspects"]}
    for item in unique:
        title = item["title"].lower()
        text = f"{title} {item['summary'].lower()}"
        for aspect in taxonomy["aspects"]:
            score = sum(3.0 if kw in title else 1.0 for kw in aspect["keywords"] if kw in text)
            if score > 0:
                if item["state_affiliated"]:
                    score *= 0.8
                buckets[aspect["id"]].append((score, item))

    now = time.time()
    categories = []
    covered = 0
    for cat in taxonomy["categories"]:
        cat_aspects = []
        for aspect in taxonomy["aspects"]:
            if aspect["category"] != cat["id"]:
                continue
            scored = sorted(buckets[aspect["id"]], key=lambda p: (-p[0], -p[1]["epoch"]))
            heads = [
                {
                    "title": i["title"],
                    "source": i["source"],
                    "url": i["url"],
                    "age_hours": round(max(0.0, (now - i["epoch"]) / 3600), 1),
                }
                for _, i in scored[:HEADLINES_PER_ASPECT]
            ]
            if heads:
                covered += 1
            cat_aspects.append({"id": aspect["id"], "name": aspect["name"], "headlines": heads})
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
        "categories": categories,
    }
    out = ROOT / "data" / "briefing.json"
    out.write_text(json.dumps(briefing, ensure_ascii=False, indent=1))
    print(f"ok={ok} failed/no-rss={failed} items={len(unique)} covered={covered}/41 → {out}")


if __name__ == "__main__":
    main()
