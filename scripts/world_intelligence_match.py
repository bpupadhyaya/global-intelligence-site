"""World-Intelligence extension: multilingual aspect/ripple-effect matching, one-hop
`linked_playbooks` graph traversal, and cross-source corroboration for the 1,149 country-outlet
feeds discovered in `data/country_feeds.json`.

MIRROR of `pvt/global-intelligence/pipeline/world_intelligence_match.py` -- same convention this
repo already uses for `data/aspects.json` / `data/ripple_effects.json` (kept in sync with the
private repo's `taxonomy/` copies; see that file's own docstring for the full design rationale
and `docs/world-intelligence/README.md` in the private repo for the project plan). Only the
data-path constants differ, since this repo's data files live flat under `data/` rather than
split across `taxonomy/`/`pipeline/`.

STATUS: standalone, NOT imported by `scripts/build_briefing.py` (the live, scheduled pipeline)
and NOT wired into any GitHub Actions workflow. Wiring this in is a deliberate follow-up task.
Validated for real against live RSS feeds in the private repo (see
`pipeline/world_intelligence_sample_output.json` there) -- the matching/traversal/corroboration
logic here is identical, just re-pointed at this repo's flat `data/` layout, so it was not
re-validated against the network a second time; re-run `pipeline/world_intelligence_validate.py`
(private repo) if you want to see it exercised against fresh live data again.

Three pieces of new logic, each reusing build_briefing.py's proven word-boundary
keyword-scoring mechanism rather than inventing something new:

1. `match_aspects_multilingual()` — extends build_briefing.py's `_score_all` to consume
   `data/aspects_keywords_multilingual.json`, picking the keyword list for an article's own
   declared source language (`normalize_language()` maps the free-text `language` field found in
   both `data/country_feeds.json` and `data/sources.json` to one of the 17 covered ISO codes),
   falling back to the English `data/aspects.json` keywords for English sources or languages
   outside the 17-language set. This is what makes non-English country-outlet articles matchable
   against the aspect taxonomy at all (today's `aspects.json` matching is English-only).

2. `match_ripple_effect_with_links()` — the same best-playbook scoring build_briefing.py already
   uses (most distinct trigger keywords hit, ties broken by playbook order), made language-aware
   via `data/ripple_keywords_multilingual.json` (covers only the original 25 playbooks — the 189
   Phase A/B/C playbooks fall back to their English `keywords` field per language, a known,
   documented gap), PLUS a one-hop `linked_playbooks` lookup so a matched playbook also surfaces
   its directly-connected playbooks. Depth is hard-capped at 1: a linked playbook's own
   `linked_playbooks` are never expanded further (avoids one trigger surfacing an unbounded chain
   of tangentially related playbooks).

3. `detect_corroboration()` — given a batch of matched items, clusters items that hit the same
   playbook/aspect id within a rolling time window and reports a corroboration signal for any
   cluster with enough distinct outlets. Two honestly-labeled tiers: `playbook_corroboration`
   (a specific ripple-effects playbook fired -- >=2 distinct outlets is already meaningful) vs
   `topic_momentum` (a broad aspect fired -- requires >=3 distinct outlets AND >=2 distinct
   countries, since validation showed a handful of outlets sharing a broad aspect like
   "geopolitics.diplomacy" in one day is usually several unrelated stories, not one corroborated
   event). Clustering by taxonomy id rather than raw title text is deliberately cross-lingual:
   this is exactly how validation caught a real Colombia earthquake reported by outlets in
   Bangladesh, Antigua, Azerbaijan, Belgium and Kazakhstan -- languages whose headlines share no
   characters, so raw text-similarity alone could never have linked them.
"""

from __future__ import annotations

import json
import re
import time
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
ASPECTS_PATH = DATA_DIR / "aspects.json"
ASPECTS_ML_PATH = DATA_DIR / "aspects_keywords_multilingual.json"
RIPPLE_EFFECTS_PATH = DATA_DIR / "ripple_effects.json"
RIPPLE_ML_PATH = DATA_DIR / "ripple_keywords_multilingual.json"
COUNTRY_FEEDS_PATH = DATA_DIR / "country_feeds.json"

# The 17 languages actually translated in aspects_keywords_multilingual.json /
# ripple_keywords_multilingual.json (2026-08-11 authoring pass in the private repo).
SUPPORTED_LANGS = {
    "es", "fr", "ar", "pt", "ru", "zh", "de", "hi", "ja", "ko",
    "it", "tr", "id", "vi", "bn", "ur", "sw",
}

# Languages whose script has no reliable whitespace word-segmentation. Python's \b (Unicode
# word boundary) only delimits at a transition between a \w char and a non-\w char; in
# unsegmented CJK text a multi-character keyword sitting mid-sentence (the common case) has no
# such transition on either side, so \b-anchored matching silently misses most real hits.
# Plain substring search is used for these instead — looser, but actually finds the keyword.
NO_BOUNDARY_LANGS = {"zh", "ja"}

MIN_SCORE = 1.5  # same threshold as build_briefing.py: one title hit (3.0) qualifies alone
LINK_HOP_DEPTH = 1  # hard cap: a linked playbook's own linked_playbooks are never expanded
CORROBORATION_WINDOW_HOURS = 48
SAME_STORY_TITLE_RATIO = 0.55  # difflib ratio threshold for the same_story_hint refinement

# Free-text `language` field (country_feeds.json) -> ISO code covered by the multilingual
# taxonomy files, or "en". Multi-language fields (e.g. "Arabic/French", "English/Swahili") are
# split on common separators; the FIRST segment that names a recognized language wins, so
# "English/Swahili" outlets are treated as English-first (usually the outlet's primary/
# international edition) while "Swahili/English" outlets are treated as Swahili-first.
_LANGUAGE_NAME_TO_CODE = {
    "english": "en", "spanish": "es", "french": "fr", "arabic": "ar", "portuguese": "pt",
    "russian": "ru", "mandarin chinese": "zh", "mandarin": "zh", "chinese": "zh",
    "german": "de", "hindi": "hi", "japanese": "ja", "korean": "ko", "italian": "it",
    "turkish": "tr", "indonesian": "id", "vietnamese": "vi", "bengali": "bn", "urdu": "ur",
    "swahili": "sw",
}
_SEGMENT_SPLIT_RE = re.compile(r"[/,&()]")


def normalize_language(raw: str | None) -> str:
    """Map a free-text language field (as found in country_feeds.json / sources.json) to one of
    the 17 multilingual-taxonomy codes, or "en" if unrecognized/absent -- the documented fallback
    for "the language isn't covered"."""
    if not raw:
        return "en"
    lowered = raw.lower()
    for segment in _SEGMENT_SPLIT_RE.split(lowered):
        segment = segment.strip()
        if not segment:
            continue
        for name in sorted(_LANGUAGE_NAME_TO_CODE, key=len, reverse=True):
            if re.search(rf"\b{re.escape(name)}\b", segment):
                return _LANGUAGE_NAME_TO_CODE[name]
    return "en"


def load_aspects_en() -> dict:
    return json.loads(ASPECTS_PATH.read_text())


def load_aspects_multilingual() -> dict:
    return json.loads(ASPECTS_ML_PATH.read_text())


def load_ripple_effects() -> dict:
    return json.loads(RIPPLE_EFFECTS_PATH.read_text())


def load_ripple_multilingual() -> dict:
    return json.loads(RIPPLE_ML_PATH.read_text())


def load_country_feeds() -> dict:
    return json.loads(COUNTRY_FEEDS_PATH.read_text())


def _pattern(keyword: str, lang: str) -> re.Pattern:
    kw = keyword.strip()
    if lang in NO_BOUNDARY_LANGS:
        return re.compile(re.escape(kw))
    return re.compile(r"\b" + re.escape(kw) + r"\b")


# ---------------------------------------------------------------------------
# 1. Multilingual aspect matching
# ---------------------------------------------------------------------------

def compile_multilingual_aspects(aspects_en: dict, aspects_ml: dict) -> dict[str, dict]:
    """{lang_code: {aspect_id: {"keywords": [pattern...], "exclude": [pattern...]}}} for every
    lang_code in SUPPORTED_LANGS | {"en"}. Any aspect missing a translated entry for a given
    language (shouldn't happen today -- all 41 aspects are translated -- but kept defensive for
    future aspect additions) falls back to its English keyword/exclude lists for that language."""
    en_by_id = {a["id"]: a for a in aspects_en["aspects"]}
    ml_by_id = aspects_ml["aspects"]

    compiled: dict[str, dict] = {
        "en": {
            aid: {
                "keywords": [_pattern(kw, "en") for kw in a["keywords"]],
                "exclude": [_pattern(kw, "en") for kw in a.get("exclude_keywords", [])],
            }
            for aid, a in en_by_id.items()
        }
    }
    for lang in SUPPORTED_LANGS:
        compiled[lang] = {}
        for aid, a in en_by_id.items():
            entry = ml_by_id.get(aid, {})
            keywords = entry.get(lang) or a["keywords"]
            exclude = (entry.get("exclude_keywords") or {}).get(lang) or a.get("exclude_keywords", [])
            compiled[lang][aid] = {
                "keywords": [_pattern(kw, lang) for kw in keywords],
                "exclude": [_pattern(kw, lang) for kw in exclude],
            }
    return compiled


def match_aspects_multilingual(
    items: list[dict], aspects_en: dict, aspects_ml: dict
) -> dict[str, list[tuple[float, dict]]]:
    """Language-aware sibling of build_briefing.py's `_score_all`. Each item is scored against
    the keyword set for ITS OWN declared language (item["language"], normalized), not a single
    global English set. Returns {aspect_id: [(score, item), ...]}, untrimmed, same shape as
    build_briefing.py so downstream sort/trim logic (HEADLINES_PER_ASPECT etc.) can be reused
    as-is."""
    compiled = compile_multilingual_aspects(aspects_en, aspects_ml)
    aspect_ids = [a["id"] for a in aspects_en["aspects"]]
    buckets: dict[str, list[tuple[float, dict]]] = {aid: [] for aid in aspect_ids}

    for item in items:
        lang = normalize_language(item.get("language"))
        item["_matched_language"] = lang  # record what was actually used, for validation/debug
        rules_by_aspect = compiled.get(lang, compiled["en"])
        title = item["title"].lower()
        summary = (item.get("summary") or "").lower()
        text = f"{title} {summary}"
        for aid, rules in rules_by_aspect.items():
            if any(pat.search(text) for pat in rules["exclude"]):
                continue
            score = 0.0
            for pat in rules["keywords"]:
                if pat.search(title):
                    score += 3.0
                elif pat.search(summary):
                    score += 1.0
            if score >= MIN_SCORE:
                if item.get("state_affiliated"):
                    score *= 0.8
                buckets[aid].append((score, item))
    return buckets


# ---------------------------------------------------------------------------
# 2. Ripple-effect playbook matching + one-hop linked_playbooks traversal
# ---------------------------------------------------------------------------

def compile_playbooks_multilingual(ripple_effects: dict, ripple_ml: dict) -> dict[str, list[dict]]:
    """{lang_code: [{**playbook, "_patterns": [...]}]}. Playbooks with no translated entry for a
    language (all 189 Phase A/B/C playbooks today, per the documented multilingual-coverage gap
    -- ripple_keywords_multilingual.json only covers the original 25) fall back to the
    playbook's English `keywords` field for every language -- still matchable on an English
    term embedded in non-English text (company names, tickers, place names), just not as
    reliably as a fully-translated playbook."""
    ml_by_id = ripple_ml["playbooks"]
    compiled: dict[str, list[dict]] = {
        "en": [
            {**p, "_patterns": [_pattern(kw, "en") for kw in p["keywords"]]}
            for p in ripple_effects["playbooks"]
        ]
    }
    for lang in SUPPORTED_LANGS:
        compiled[lang] = []
        for p in ripple_effects["playbooks"]:
            keywords = (ml_by_id.get(p["id"]) or {}).get(lang) or p["keywords"]
            compiled[lang].append({**p, "_patterns": [_pattern(kw, lang) for kw in keywords]})
    return compiled


def _best_playbook_match(item: dict, compiled_playbooks_for_lang: list[dict]) -> dict | None:
    """Identical selection rule to build_briefing.py's _match_ripple_effect: most distinct
    trigger keywords hit wins; ties broken by playbook order in ripple_effects.json."""
    text = f"{item['title'].lower()} {(item.get('summary') or '').lower()}"
    best, best_hits = None, 0
    for pb in compiled_playbooks_for_lang:
        hits = sum(1 for pat in pb["_patterns"] if pat.search(text))
        if hits > best_hits:
            best, best_hits = pb, hits
    return best


def match_ripple_effect_with_links(
    item: dict,
    compiled_playbooks_by_lang: dict[str, list[dict]],
    playbooks_by_id: dict[str, dict],
) -> dict | None:
    """Best-matching playbook for one item (language-aware), plus its directly linked
    playbooks (LINK_HOP_DEPTH=1 -- linked playbooks' own linked_playbooks are NOT expanded)."""
    lang = normalize_language(item.get("language"))
    compiled = compiled_playbooks_by_lang.get(lang, compiled_playbooks_by_lang["en"])
    best = _best_playbook_match(item, compiled)
    if best is None:
        return None

    linked = []
    for linked_id in best.get("linked_playbooks", []):
        lp = playbooks_by_id.get(linked_id)
        if lp is None:
            continue  # schema was pre-validated with zero dangling refs, but stay defensive
        linked.append(
            {
                "id": lp["id"],
                "name": lp["name"],
                "affected_domains": lp.get("affected_domains", []),
            }
        )

    return {
        "id": best["id"],
        "name": best["name"],
        "historical_precedents": best["historical_precedents"],
        "possibility_chains": best["possibility_chains"],
        "affected_domains": best.get("affected_domains", []),
        "matched_language": lang,
        "linked_playbooks": linked,  # depth-1 only, see LINK_HOP_DEPTH
    }


# ---------------------------------------------------------------------------
# 3. Cross-source corroboration
# ---------------------------------------------------------------------------

def _split_into_windows(group: list[dict], window_hours: float) -> list[list[dict]]:
    """Greedily split a time-sorted group into sub-clusters no wider than window_hours,
    starting a new sub-cluster whenever an item is more than window_hours past the sub-cluster's
    first item."""
    if not group:
        return []
    windows: list[list[dict]] = [[group[0]]]
    for it in group[1:]:
        if (it["epoch"] - windows[-1][0]["epoch"]) / 3600 <= window_hours:
            windows[-1].append(it)
        else:
            windows.append([it])
    return windows


def _has_similar_titles(group: list[dict], threshold: float = SAME_STORY_TITLE_RATIO) -> bool:
    """Cheap same-story refinement: True if two titles from DIFFERENT outlets in the group are
    textually similar (difflib ratio). Mainly useful within a single language/script -- titles
    in different languages will rarely clear the threshold even when reporting the identical
    story, so this is a confidence BONUS on top of the shared-taxonomy-id clustering, never a
    requirement.

    Deliberately compares only cross-outlet pairs, never same-outlet pairs: a single prolific
    outlet publishing many similarly-templated headlines (observed for real during validation --
    one outlet ran 8 near-identical "President X inspected/visited/laid foundation stone..."
    headlines in one day) would otherwise self-trigger a false "same story" signal from its own
    house style, with no actual second source involved."""
    for i in range(len(group)):
        for j in range(i + 1, len(group)):
            a, b = group[i], group[j]
            source_a = a.get("source_id") or a.get("source")
            source_b = b.get("source_id") or b.get("source")
            if source_a == source_b:
                continue
            if SequenceMatcher(None, a["title"].lower(), b["title"].lower()).ratio() >= threshold:
                return True
    return False


MIN_SOURCES_NARROW = 2   # playbook-level: a specific trigger firing twice is already meaningful
MIN_SOURCES_BROAD = 3    # aspect-level: raise the bar, since a broad topic gets daily baseline
MIN_COUNTRIES_BROAD = 2  # coverage from a single country's outlets alone doesn't count as broad
                          # "topic momentum" -- it could just be one country's news cycle


def detect_corroboration(
    matched: list[tuple[str, dict]],
    window_hours: float = CORROBORATION_WINDOW_HOURS,
    narrow_ids: set[str] | None = None,
) -> dict[str, dict]:
    """matched: list of (matched_id, item) pairs -- matched_id is a ripple-playbook id or aspect
    id an item scored against (the caller decides which; playbook id is preferred where present
    since it's more specific than an aspect id). Clustering by taxonomy id rather than raw title
    text is deliberately cross-lingual: two articles in different languages/scripts that both
    trigger the same playbook/aspect cluster together even though their headlines share no
    tokens -- this is exactly how real validation (in the private repo) caught a genuine
    multi-country, multi-language story (a Colombia earthquake reported by outlets in
    Bangladesh, Antigua, Azerbaijan, Belgium and Kazakhstan) that raw title-similarity alone
    could never have linked, since a Bengali headline and a Russian headline about the same
    quake share zero characters.

    Two honestly-labeled signal tiers, since a playbook match and an aspect match carry very
    different confidence about "is this really the same story":
    - `playbook_corroboration` (narrow_ids match, e.g. a ripple-effects playbook id like
      "hormuz-gulf-oil-shock"): the keyword set that triggered it is specific enough that
      >=MIN_SOURCES_NARROW distinct outlets hitting it really do mean the same underlying event.
      Always surfaced.
    - `topic_momentum` (anything else, typically a broad aspect id like "geopolitics.diplomacy"):
      NOT claimed to be the same specific story -- confirmed empirically during validation that a
      handful of outlets hitting a broad aspect in one day is usually several unrelated stories
      sharing a topic, not one corroborated event. Only surfaced once the volume itself is
      unusual (>=MIN_SOURCES_BROAD distinct outlets AND >=MIN_COUNTRIES_BROAD distinct
      countries), which is the volume signature the real earthquake cluster actually had.
    `same_story_hint` (difflib title similarity, cross-outlet pairs only) is still computed and
    attached to every signal as a bonus confidence indicator, never as a gate -- gating on it
    was tried and discarded in the private repo's validation: it reliably fires for
    same-language wire-copy variants but reliably MISSES genuine cross-lingual matches like the
    earthquake, so gating on it would silently drop the single best real example found. Pass
    narrow_ids=None to treat every id as narrow (useful for exploration).

    Returns {signal_key: {matched_id, signal_type, source_count, country_count, countries,
    sources, same_story_hint, example_titles, label}}.
    """
    clusters: dict[str, list[dict]] = {}
    for matched_id, item in matched:
        clusters.setdefault(matched_id, []).append(item)

    signals: dict[str, dict] = {}
    for matched_id, group in clusters.items():
        group = sorted(group, key=lambda it: it["epoch"])
        is_narrow = narrow_ids is None or matched_id in narrow_ids
        min_sources = MIN_SOURCES_NARROW if is_narrow else MIN_SOURCES_BROAD
        for sub in _split_into_windows(group, window_hours):
            sources = {it.get("source_id") or it.get("source") for it in sub}
            countries = {it.get("country") for it in sub if it.get("country")}
            if len(sources) < min_sources:
                continue
            if not is_narrow and len(countries) < MIN_COUNTRIES_BROAD:
                continue
            signal_key = f"{matched_id}:{min(it['epoch'] for it in sub):.0f}"
            signals[signal_key] = {
                "matched_id": matched_id,
                "signal_type": "playbook_corroboration" if is_narrow else "topic_momentum",
                "source_count": len(sources),
                "country_count": len(countries),
                "countries": sorted(countries),
                "sources": sorted(str(s) for s in sources),
                "same_story_hint": _has_similar_titles(sub),
                "example_titles": [it["title"] for it in sub[:5]],
                "label": (
                    f"{len(sources)} sources across {len(countries)} countries"
                    if len(countries) > 1
                    else f"{len(sources)} sources"
                ),
            }
    return signals


if __name__ == "__main__":
    # Quick self-check with no network calls: confirms the taxonomy files load, compile, and
    # normalize_language() behaves sanely. Full end-to-end validation against real feeds was run
    # from the private repo's pipeline/world_intelligence_validate.py (same logic, same data).
    t0 = time.time()
    aspects_en = load_aspects_en()
    aspects_ml = load_aspects_multilingual()
    ripple_effects = load_ripple_effects()
    ripple_ml = load_ripple_multilingual()

    compiled_aspects = compile_multilingual_aspects(aspects_en, aspects_ml)
    compiled_playbooks = compile_playbooks_multilingual(ripple_effects, ripple_ml)

    print(f"aspects: {len(aspects_en['aspects'])}, languages compiled: {sorted(compiled_aspects)}")
    print(f"playbooks: {len(ripple_effects['playbooks'])}")
    for lang in sorted(compiled_playbooks):
        n_translated = sum(1 for p in compiled_playbooks[lang] if p["id"] in ripple_ml["playbooks"])
        print(f"  lang={lang}: {n_translated}/{len(compiled_playbooks[lang])} playbooks have real translations")

    test_cases = [
        "English", "Arabic/French", "Swahili/English", "English/Swahili", "Mandarin Chinese",
        "Dutch", "Persian", None, "Tetum, Portuguese, Indonesian, English",
    ]
    for raw in test_cases:
        print(f"normalize_language({raw!r}) = {normalize_language(raw)!r}")
    print(f"self-check done in {time.time() - t0:.2f}s")
