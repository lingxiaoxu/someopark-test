"""analysis/news_tags.py — deterministic macro tagging of headlines (PLAN §28, §22-18).

Why this exists instead of just calling the LLM tagger. `analysis/llm.py::news_risk_tags`
asks the 120B to tag the last 36h of headlines. That is fine as a live overlay and
useless as history: it is serial on a slow model, it is not reproducible (the tagger is
whatever weights were loaded that day), and PLAN §5-bis will not let an unreproducible
feature into a pre-launch backtest. A regex over the title is reproducible by
construction, so it is what the backfilled 2021→ archive gets scored with.

The two layers are meant to coexist: this one gives a walk-forward-testable *intensity*
series over five years; the LLM gives a same-day read on what the headlines mean.

FILTER vs TAG. `is_macro()` is the ingest-time gate — a deliberately broad union used to
decide which of Polygon's ~780k articles are worth storing at all. `families()` is the
feature-time classifier and may be tightened later without re-running the backfill, as
long as it stays a subset of the union. Families match `llm._TAG_FAMILY` so the two
layers land on the same downstream keys.
"""
from __future__ import annotations

import re

# Word-boundary anchored: "CPI" must not fire on "CPIx", and "oil" alone is far too
# common in equity copy ("Oil States International") so only price-bearing phrases count.
_FAMILY_PATTERNS: dict[str, str] = {
    "labor": r"\b(layoffs?|job cuts?|jobless claims?|unemployment(?! rate is low)"
             r"|nonfarm payrolls?|hiring freeze|workforce reduction|initial claims)\b",
    "fed": r"\b(federal reserve|the fed|fomc|powell|rate (?:cut|hike|decision)s?"
           r"|interest[- ]rate decision|fed chair|dot plot|quantitative (?:easing|tightening))\b",
    "inflation": r"\b(inflation|cpi|consumer price|pce price|core (?:cpi|pce|inflation)"
                 r"|price pressures|deflation)\b",
    "energy": r"\b(oil price|crude|opec|gasoline|natural gas|refiner(?:y|ies)"
              r"|per barrel|wti|brent|diesel price)\b",
}

_FAMILY_RE = {k: re.compile(v, re.I) for k, v in _FAMILY_PATTERNS.items()}
_ANY_RE = re.compile("|".join(f"(?:{v})" for v in _FAMILY_PATTERNS.values()), re.I)


def is_macro(title: str | None) -> bool:
    """Ingest gate: does this headline mention anything macro at all?"""
    return bool(title) and _ANY_RE.search(title) is not None


def families(title: str | None) -> list[str]:
    """Feature-time classifier: which macro families a headline touches (may be several —
    'OPEC cut lifts gasoline, complicating the Fed's inflation fight' is three)."""
    if not title:
        return []
    return sorted(k for k, r in _FAMILY_RE.items() if r.search(title))
