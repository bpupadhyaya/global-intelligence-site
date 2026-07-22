# Global Intelligence

**Live site:** https://equalinformation.com/global-intelligence-site/

Global Intelligence reads the world's major news media and distills what's happening across 41
world "aspects" — economy, markets, politics, geopolitics, energy, technology, and more — grouped
into 12 categories. It's 100% open pipeline, zero paid dependencies: public RSS feeds in, a
deterministic Python script out, no AI/LLM cost anywhere in this repo.

**Principles**

- We summarize in our own words and always attribute — we never republish article text.
- We prefer RSS feeds and official APIs over scraping.
- State-affiliated outlets are flagged in the registry and weighted accordingly.
- Coverage grows continuously toward all countries and regions — see the live, current count and
  full list at [`data/sources.json`](data/sources.json) or the
  [live sources page](https://equalinformation.com/global-intelligence-site/sources/).

## How it works

```
data/sources.json          the registry of news sources (RSS feed per source, region/country/
                            language/state-affiliation metadata)
        │
        ▼
scripts/build_briefing.py  fetches every feed, matches items to aspects by word-boundary keyword
                            scoring against data/aspects.json, ranks the most influential current
                            stories, tags matches with historical "ripple effect" context from
                            data/ripple_effects.json (or a general fallback from
                            data/category_context.json)
        │
        ▼
data/briefing.json         the output every page and both native apps read
```

`.github/workflows/briefing.yml` runs the pipeline every 4 hours via GitHub Actions (free on public
repos) and commits the refreshed `data/briefing.json`.

## Pages

- `/` — homepage, live aspect previews + top-10 preview
- `/briefing/` — the full live briefing, every aspect
- `/top10/` — the 10 most influential current stories, ranked by breadth × recency
- `/sources/` — the full, live source registry

## Running the pipeline locally

```
pip install -r requirements.txt
python scripts/build_briefing.py
```

Writes `data/briefing.json`. No API keys or paid services required.

## Related

The native iOS and Android apps that also read `data/briefing.json` live in a separate private
repository (not part of this site).

To suggest a source, open an issue.
