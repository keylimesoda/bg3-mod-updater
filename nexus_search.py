"""Search Nexus Mods by name and score matches against local mod metadata.

Uses the Nexus Mods GraphQL API (v2) with the ``nameStemmed`` filter for
fuzzy text search, plus an author-based search when the local mod's
author is known.  If neither produces a confident match, a DuckDuckGo
web search is used as a last-resort fallback.

Results are scored against local metadata (name, author, description,
popularity) to find the best match.
"""

import re
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Optional
from urllib.parse import unquote

import requests
log = logging.getLogger("nexus_search")
# ── Constants ───────────────────────────────────────────────────────

GRAPHQL_URL = "https://api.nexusmods.com/v2/graphql"

# Nexus internal game IDs (used by the search API)
_GAME_IDS = {
    "baldursgate3": 3474,
}

# Auto-accept matches at or above this score
AUTO_MATCH_THRESHOLD = 0.75

# Minimum score to even show a candidate in the dialog
MIN_CANDIDATE_SCORE = 0.20

# Delay between search requests to avoid rate-limiting (seconds)
SEARCH_DELAY = 0.4

# Number of concurrent worker threads for Nexus lookups
LOOKUP_WORKERS = 4


# ── Data types ──────────────────────────────────────────────────────


@dataclass
class NexusSearchResult:
    """A single Nexus Mods search result."""

    mod_id: int
    name: str
    author: str = "Unknown"
    summary: str = ""
    version: str = ""
    url: str = ""
    endorsements: int = 0
    unique_downloads: int = 0


@dataclass
class ScoredMatch:
    """A search result paired with a confidence score."""

    result: NexusSearchResult
    score: float  # 0.0 – 1.0
    breakdown: dict = field(default_factory=dict)


# ── GraphQL search ──────────────────────────────────────────────────

_SEARCH_QUERY = """
query SearchMods($game: String!, $name: String!, $count: Int!) {
  mods(
    filter: {
      gameDomainName: { value: $game }
      nameStemmed: { value: $name }
    }
    count: $count
  ) {
    nodes {
      modId
      name
      author
      summary
      version
      endorsements
      downloads
    }
  }
}
"""


def search_nexus_mods(
    query: str,
    api_key: str,
    game_domain: str = "baldursgate3",
    max_results: int = 10,
) -> list[NexusSearchResult]:
    """Search Nexus Mods by name via the GraphQL API.

    Returns up to *max_results* results sorted by relevance.
    Requires a valid Nexus Mods API key.
    """
    if not api_key or not query.strip():
        return []

    variables = {
        "game": game_domain,
        "name": query.strip(),
        "count": max_results,
    }

    headers = {
        "apikey": api_key,
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(
            GRAPHQL_URL,
            json={"query": _SEARCH_QUERY, "variables": variables},
            headers=headers,
            timeout=15,
        )
        if resp.status_code != 200:
            return []

        data = resp.json()
        nodes = (
            data.get("data", {}).get("mods", {}).get("nodes", [])
        )

        results: list[NexusSearchResult] = []
        for node in nodes:
            mod_id = node.get("modId")
            if not mod_id:
                continue
            results.append(
                NexusSearchResult(
                    mod_id=int(mod_id),
                    name=node.get("name", f"Mod {mod_id}"),
                    author=node.get("author", "Unknown"),
                    summary=node.get("summary", ""),
                    version=node.get("version", ""),
                    url=f"https://www.nexusmods.com/{game_domain}/mods/{mod_id}",
                    endorsements=int(node.get("endorsements", 0) or 0),
                    unique_downloads=int(node.get("downloads", 0) or 0),
                )
            )
        return results

    except (requests.RequestException, ValueError, KeyError):
        return []


# ── Author-based search ────────────────────────────────────────────

_AUTHOR_SEARCH_QUERY = """
query SearchByAuthor($game: String!, $author: String!, $count: Int!) {
  mods(
    filter: {
      gameDomainName: { value: $game }
      author: { value: $author }
    }
    count: $count
  ) {
    nodes {
      modId
      name
      author
      summary
      version
      endorsements
      downloads
    }
  }
}
"""


def search_nexus_by_author(
    author: str,
    api_key: str,
    game_domain: str = "baldursgate3",
    max_results: int = 20,
) -> list[NexusSearchResult]:
    """Search Nexus Mods by author name via the GraphQL API."""
    if not api_key or not author.strip():
        return []
    if author.strip().lower() in ("unknown", "", "\u2014"):
        return []

    try:
        resp = requests.post(
            GRAPHQL_URL,
            json={
                "query": _AUTHOR_SEARCH_QUERY,
                "variables": {
                    "game": game_domain,
                    "author": author.strip(),
                    "count": max_results,
                },
            },
            headers={"apikey": api_key, "Content-Type": "application/json"},
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        nodes = data.get("data", {}).get("mods", {}).get("nodes", [])
        results: list[NexusSearchResult] = []
        for node in nodes:
            mod_id = node.get("modId")
            if not mod_id:
                continue
            results.append(
                NexusSearchResult(
                    mod_id=int(mod_id),
                    name=node.get("name", f"Mod {mod_id}"),
                    author=node.get("author", "Unknown"),
                    summary=node.get("summary", ""),
                    version=node.get("version", ""),
                    url=f"https://www.nexusmods.com/{game_domain}/mods/{mod_id}",
                    endorsements=int(node.get("endorsements", 0) or 0),
                    unique_downloads=int(node.get("downloads", 0) or 0),
                )
            )
        return results
    except (requests.RequestException, ValueError, KeyError):
        return []


# ── DuckDuckGo web-search fallback ──────────────────────────────────

_NEXUS_URL_RE = re.compile(
    r"nexusmods\.com/baldursgate3/mods/(\d+)", re.IGNORECASE
)

_SEARCH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Score threshold below which web-search fallback is triggered
WEB_FALLBACK_THRESHOLD = 0.55


def _extract_nexus_ids_from_html(html: str) -> list[int]:
    """Pull unique Nexus mod IDs from raw HTML."""
    ids = dict.fromkeys(int(m) for m in _NEXUS_URL_RE.findall(html))
    return list(ids)


def _search_ddg(query: str) -> list[int]:
    """Search DuckDuckGo for Nexus mod pages and return mod IDs."""
    try:
        resp = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query, "b": ""},
            headers={"User-Agent": _SEARCH_UA},
            timeout=8,
        )
        if resp.status_code != 200:
            return []

        ids: list[int] = []
        seen: set[int] = set()
        for m in _NEXUS_URL_RE.finditer(resp.text):
            mid = int(m.group(1))
            if mid not in seen:
                seen.add(mid)
                ids.append(mid)
        # Also check decoded hrefs
        for href in re.findall(r'href="([^"]+)"', resp.text):
            decoded = unquote(href)
            for m2 in _NEXUS_URL_RE.finditer(decoded):
                mid2 = int(m2.group(1))
                if mid2 not in seen:
                    seen.add(mid2)
                    ids.append(mid2)
        return ids[:15]
    except (requests.RequestException, Exception):
        return []


def _fetch_nexus_mod_info(
    mod_id: int, api_key: str, game_domain: str = "baldursgate3"
) -> Optional[NexusSearchResult]:
    """Fetch a single mod's info from the Nexus v1 REST API."""
    try:
        resp = requests.get(
            f"https://api.nexusmods.com/v1/games/{game_domain}/mods/{mod_id}.json",
            headers={"apikey": api_key, "accept": "application/json"},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        d = resp.json()
        return NexusSearchResult(
            mod_id=int(d.get("mod_id", mod_id)),
            name=d.get("name", f"Mod {mod_id}"),
            author=d.get("author", "Unknown"),
            summary=d.get("summary", ""),
            version=d.get("version", ""),
            url=f"https://www.nexusmods.com/{game_domain}/mods/{mod_id}",
            endorsements=int(d.get("endorsement_count", 0) or 0),
            unique_downloads=int(d.get("mod_unique_downloads", 0) or 0),
        )
    except (requests.RequestException, ValueError, KeyError):
        return None


# ── Tavily web-search (preferred over DDG) ──────────────────────────


def _search_tavily(query: str, tavily_api_key: str) -> list[int]:
    """Search Tavily for Nexus mod pages and return mod IDs.

    Tavily returns structured JSON results – much more reliable than
    scraping DDG HTML.  Free tier gives 1 000 searches/month.
    """
    if not tavily_api_key:
        return []
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": tavily_api_key,
                "query": query,
                "search_depth": "basic",
                "max_results": 10,
                "include_domains": ["nexusmods.com"],
            },
            timeout=12,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        ids: list[int] = []
        seen: set[int] = set()
        for result in data.get("results", []):
            url = result.get("url", "")
            for m in _NEXUS_URL_RE.finditer(url):
                mid = int(m.group(1))
                if mid not in seen:
                    seen.add(mid)
                    ids.append(mid)
            # Also check the snippet / content
            for field in ("content", "title"):
                text = result.get(field, "")
                for m in _NEXUS_URL_RE.finditer(text):
                    mid = int(m.group(1))
                    if mid not in seen:
                        seen.add(mid)
                        ids.append(mid)
        return ids[:15]
    except (requests.RequestException, ValueError, KeyError):
        return []


def search_web_for_nexus_mods(
    query: str,
    api_key: str,
    game_domain: str = "baldursgate3",
    tavily_api_key: str = "",
) -> list[NexusSearchResult]:
    """Search the web for Nexus mod pages, then fetch mod info.

    Prefers Tavily (structured JSON API) when a key is provided.
    Falls back to DuckDuckGo HTML scraping otherwise.
    """
    search_query = f"BG3 Nexus {query}"
    mod_ids = _search_tavily(search_query, tavily_api_key)
    if mod_ids:
        log.info("[web] Tavily returned %d IDs for '%s'", len(mod_ids), query)
    if not mod_ids:
        # Fallback to DuckDuckGo
        ddg_query = f"site:nexusmods.com/baldursgate3/mods {query}"
        mod_ids = _search_ddg(ddg_query)
        if mod_ids:
            log.info("[web] DDG fallback returned %d IDs for '%s'", len(mod_ids), query)
        else:
            log.info("[web] No web results for '%s'", query)
    if not mod_ids:
        return []

    results: list[NexusSearchResult] = []
    with ThreadPoolExecutor(max_workers=LOOKUP_WORKERS) as pool:
        futs = {
            pool.submit(_fetch_nexus_mod_info, mid, api_key, game_domain): mid
            for mid in mod_ids
        }
        for fut in as_completed(futs):
            info = fut.result()
            if info:
                results.append(info)
    return results


def _expand_query(query: str) -> list[str]:
    """Generate search query variations for broader coverage.

    For example, "5eSpells" produces ["5eSpells", "5e Spells"].
    CamelCase and digit-letter boundaries are split with spaces.
    """
    queries = [query.strip()]

    # Split on camelCase / digit-letter boundaries:
    # "5eSpells" → "5e Spells", "BetterHotbar2" → "Better Hotbar 2"
    expanded = re.sub(r"([a-z])([A-Z])", r"\1 \2", query)      # camelCase
    expanded = re.sub(r"(\d[a-z])([A-Z])", r"\1 \2", expanded) # 5eSpells → 5e Spells
    expanded = re.sub(r"([A-Za-z])(\d)", r"\1 \2", expanded)   # Hotbar2 → Hotbar 2
    expanded = re.sub(r"[_\-]+", " ", expanded)                 # underscores
    expanded = re.sub(r"\s+", " ", expanded).strip()

    if expanded != queries[0]:
        queries.append(expanded)

    return queries


def search_all_sources(
    query: str,
    api_key: str,
    game_domain: str = "baldursgate3",
    max_results: int = 10,
    author: str = "",
    local_name: str = "",
    local_author: str = "",
    local_description: str = "",
    local_version: str = "",
    tavily_api_key: str = "",
) -> list[NexusSearchResult]:
    """Author-first search strategy with name-search and web fallback.

    Resolution order:
    1. **Author search** – fetch all mods by this author and try to
       resolve within that small catalog.  If the best candidate
       exceeds ``WEB_FALLBACK_THRESHOLD`` we return immediately.
    2. **Name search** – only if author is unknown/empty or the author
       catalog didn't produce a confident match.
    3. **Web search** (Tavily → DDG) – last resort, only if the best
       score from steps 1–2 is still below the threshold.
    """
    seen_ids: set[int] = set()
    merged: list[NexusSearchResult] = []
    _ln = local_name or query
    _la = local_author or author

    def _add(results: list[NexusSearchResult]):
        for r in results:
            if r.mod_id not in seen_ids:
                seen_ids.add(r.mod_id)
                merged.append(r)

    def _best_score() -> float:
        if not merged:
            return 0.0
        return max(
            score_mod_match(_ln, _la, local_description, r,
                            local_version=local_version).score
            for r in merged
        )

    # ── Step 1: Author-first resolution ─────────────────────
    has_author = (author and author.strip().lower()
                  not in ("unknown", "", "\u2014"))
    if has_author:
        _add(search_nexus_by_author(
            author, api_key, game_domain, max_results=20))
        best = _best_score()
        log.info("[%s] Step 1 author-search: %d candidates, best=%.3f",
                 _ln, len(merged), best)
        if best >= WEB_FALLBACK_THRESHOLD:
            return merged

    # ── Step 2: Name search (only if author didn't solve it) ───
    queries = _expand_query(query)
    for q in queries:
        _add(search_nexus_mods(q, api_key, game_domain, max_results=20))
    best = _best_score()
    log.info("[%s] Step 2 name-search: %d candidates, best=%.3f",
             _ln, len(merged), best)
    if best >= WEB_FALLBACK_THRESHOLD:
        return merged

    # ── Step 3: Web search fallback (Tavily → DDG) ─────────
    log.info("[%s] Step 3 web-search fallback triggered (best=%.3f < %.2f)",
             _ln, best, WEB_FALLBACK_THRESHOLD)
    for q in queries:
        _add(search_web_for_nexus_mods(
            q, api_key, game_domain, tavily_api_key=tavily_api_key))

    return merged  # let rank_matches handle final selection/trimming# ── Name normalisation ──────────────────────────────────────────────

_STRIP_TERMS = [
    "bg3",
    "baldur's gate 3",
    "baldurs gate 3",
    "baldur's gate",
    "for bg3",
    "for baldur's gate 3",
    "(bg3)",
    "[bg3]",
    " - ",
]


def _normalize_name(name: str) -> str:
    """Normalise a mod name for fuzzy comparison."""
    name = name.lower().strip()
    for term in _STRIP_TERMS:
        name = name.replace(term, " ")
    # Separators → spaces
    name = re.sub(r"[_\-\.]+", " ", name)
    # Drop trailing version-like tokens: v1.2.3, 1.0, etc.
    name = re.sub(r"\bv?\d+(?:\.\d+)+\b", "", name)
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _tokenize(text: str) -> set[str]:
    """Split into lowercase word tokens, ignoring very short ones."""
    return {w for w in re.findall(r"\w+", text.lower()) if len(w) > 1}


# ── Scoring ─────────────────────────────────────────────────────────

_STOPWORDS = frozenset(
    "the a an and or is in to of for with this that it mod mods are on at by"
    .split()
)

# ── Translation / language-variant detection ────────────────────────

# Common language codes and translation-related suffixes that appear in
# Nexus mod names for translated versions of a mod.  If the Nexus result
# contains one of these but the local mod does not, we penalise the
# score so that the original English mod is preferred.
_TRANSLATION_SUFFIXES = re.compile(
    r"\b(?:"
    r"TCN|SCN|CN|ZH|ZHS|ZHT|CHS|CHT|TW"   # Chinese
    r"|RU|UA|Rus"                            # Russian / Ukrainian
    r"|FR|DE|GER|ES|SPA|IT|ITA|PT|BR|PTBR"  # Western European
    r"|JP|JA|JPN|KO|KR"                     # Japanese / Korean
    r"|PL|CZ|TR|TH|VN|ID"                   # Polish / Czech / Turkish / etc.
    r"|NL|FI|SV|DA|NO|HU|RO|BG|HR|SK"       # Other European
    r"|translation|traducao|traduction|traduccion|traduzione"
    r"|übersetzung|tradução|перевод"
    r")\b",
    re.IGNORECASE,
)


def _looks_like_translation(
    local_name: str, nexus_name: str
) -> bool:
    """Return True if *nexus_name* looks like a language translation
    that the *local_name* does NOT reference."""
    # If the local name already contains the tag, it's intentional
    if _TRANSLATION_SUFFIXES.search(local_name):
        return False
    return bool(_TRANSLATION_SUFFIXES.search(nexus_name))


# ── Version similarity ───────────────────────────────────────────────

_VERSION_DIGITS_RE = re.compile(r"[\d]+")


def _normalize_version(v: str) -> str:
    """Collapse a version string to just its numeric parts.

    "v1.2.3-beta" → "1.2.3", "36028797018963968" (int64) → handle below.
    """
    parts = _VERSION_DIGITS_RE.findall(v)
    return ".".join(parts) if parts else ""


def _version_similarity(local_ver: str, nexus_ver: str) -> float:
    """Score how similar two version strings are (0.0–1.0).

    Handles:
    - Exact match → 1.0
    - Same format & close numbers → high score
    - Major version matches, minor differs → moderate
    - Completely different shape → low
    """
    if not local_ver or not nexus_ver:
        return 0.0

    lv = _normalize_version(local_ver)
    nv = _normalize_version(nexus_ver)
    if not lv or not nv:
        return 0.0

    # Exact normalised match
    if lv == nv:
        return 1.0

    l_parts = lv.split(".")
    n_parts = nv.split(".")

    # BG3 mods sometimes store version as a single int64
    # (e.g. "36028797018963968") while Nexus shows "1.0.0".
    # If one side is a single huge number and the other is dotted,
    # they're just different formats – give a small nudge.
    if (len(l_parts) == 1 and len(l_parts[0]) > 6
            and len(n_parts) > 1):
        return 0.15  # same mod, different version encoding
    if (len(n_parts) == 1 and len(n_parts[0]) > 6
            and len(l_parts) > 1):
        return 0.15

    # Compare element-by-element
    max_len = max(len(l_parts), len(n_parts))
    matches = 0
    for i in range(max_len):
        lp = l_parts[i] if i < len(l_parts) else "0"
        np = n_parts[i] if i < len(n_parts) else "0"
        if lp == np:
            matches += 1

    if max_len == 0:
        return 0.0

    ratio = matches / max_len
    # Same format (same number of parts) is a good sign
    format_bonus = 0.1 if len(l_parts) == len(n_parts) else 0.0
    return min(ratio + format_bonus, 1.0)


def _compute_name_score(local_name: str, nexus_name: str) -> float:
    """Compute a 0.0–1.0 name similarity score using both sequence
    matching and token overlap, returning the best of the two."""
    norm_local = _normalize_name(local_name)
    norm_nexus = _normalize_name(nexus_name)

    seq_score = SequenceMatcher(None, norm_local, norm_nexus).ratio()

    local_tokens = _tokenize(norm_local)
    nexus_tokens = _tokenize(norm_nexus)
    token_score = 0.0
    if local_tokens and nexus_tokens:
        overlap = local_tokens & nexus_tokens
        token_score = (2 * len(overlap)) / (len(local_tokens) + len(nexus_tokens))

    return max(seq_score, token_score)


def _compute_author_score(local_author: str, nexus_author: str) -> float:
    """Compute a 0.0–1.0 author similarity score.

    Returns:
        1.0  – exact (case-insensitive) match
        0.7  – one name is a substring of the other
        0.4+ – fuzzy SequenceMatcher similarity
        0.0  – no meaningful similarity or missing data
    """
    if not local_author or local_author.lower() in ("unknown", "", "\u2014"):
        return 0.0
    if not nexus_author or nexus_author.lower() in ("unknown", ""):
        return 0.0

    la = local_author.lower().strip()
    na = nexus_author.lower().strip()
    if la == na:
        return 1.0
    if la in na or na in la:
        return 0.7
    ratio = SequenceMatcher(None, la, na).ratio()
    return ratio if ratio >= 0.4 else 0.0


def score_mod_match(
    local_name: str,
    local_author: str,
    local_description: str,
    result: NexusSearchResult,
    *,
    local_version: str = "",
) -> ScoredMatch:
    """Score how well a Nexus search result matches a local mod.

    Uses a **cascade / gating model** instead of a flat weighted sum.
    Author + name are the primary signals that gate the confidence
    level.  Version, description, and popularity only act as small
    tiebreakers *within* the confidence tier established by the
    primary signals.

    Cascade tiers (before tiebreakers):
        1. Author exact  + name ≥ 0.45  →  base 0.92
        2. Author fuzzy  + name ≥ 0.45  →  base 0.82
        3. No author info + name ≥ 0.85 →  base 0.75
        4. No author info + name ≥ 0.60 →  base 0.60
        5. Otherwise                     →  name * 0.55  (low-confidence)

    Tiebreaker budget adds up to ≤ 0.08:
        version     ≤ 0.03
        description ≤ 0.02
        popularity  ≤ 0.03

    A translation penalty (×0.3) is applied when the Nexus result
    looks like a language variant that the local mod does not reference.
    """
    import math

    # ── Primary signals ────────────────────────────────────────
    name_score = _compute_name_score(local_name, result.name)
    author_score = _compute_author_score(local_author, result.author)

    has_author = author_score > 0.0
    author_exact = author_score >= 1.0
    author_fuzzy = 0.4 <= author_score < 1.0

    # ── Cascade / gating ───────────────────────────────────────
    if author_exact and name_score >= 0.45:
        base = 0.92
    elif author_fuzzy and name_score >= 0.45:
        base = 0.82
    elif not has_author and name_score >= 0.85:
        base = 0.75
    elif not has_author and name_score >= 0.60:
        base = 0.60
    else:
        # Low-confidence zone – score is primarily driven by name
        base = name_score * 0.55

    # ── Tiebreakers (≤ 0.08 total) ─────────────────────────────
    ver_score = _version_similarity(local_version, result.version)
    ver_bonus = ver_score * 0.03

    desc_score = 0.0
    if local_description and result.summary:
        d_local = _tokenize(local_description) - _STOPWORDS
        d_nexus = _tokenize(result.summary) - _STOPWORDS
        if d_local and d_nexus:
            overlap = d_local & d_nexus
            desc_score = len(overlap) / min(len(d_local), len(d_nexus))
            desc_score = min(desc_score, 1.0)
    desc_bonus = desc_score * 0.02

    pop_raw = max(result.unique_downloads, result.endorsements * 10)
    pop_score = min(math.log10(max(pop_raw, 1)) / 8.0, 1.0) if pop_raw > 0 else 0.0
    pop_bonus = pop_score * 0.03

    total = min(base + ver_bonus + desc_bonus + pop_bonus, 1.0)

    # ── Translation penalty ────────────────────────────────────
    is_translation = _looks_like_translation(local_name, result.name)
    if is_translation:
        total *= 0.3

    return ScoredMatch(
        result=result,
        score=round(total, 3),
        breakdown={
            "name": round(name_score, 3),
            "author": round(author_score, 3),
            "version": round(ver_score, 3),
            "desc": round(desc_score, 3),
            "popularity": round(pop_score, 3),
            "base_tier": round(base, 3),
            "translation_penalty": is_translation,
        },
    )


def rank_matches(
    local_name: str,
    local_author: str,
    local_description: str,
    results: list[NexusSearchResult],
    min_score: float = MIN_CANDIDATE_SCORE,
    local_version: str = "",
) -> list[ScoredMatch]:
    """Score and rank search results, returning the top candidates.

    Results below *min_score* are filtered out.
    """
    scored = [
        score_mod_match(local_name, local_author, local_description, r,
                        local_version=local_version)
        for r in results
    ]
    scored = [s for s in scored if s.score >= min_score]
    scored.sort(key=lambda s: s.score, reverse=True)
    return scored[:5]
