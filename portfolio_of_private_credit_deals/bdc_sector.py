"""
bdc_sector.py — D4 §6.2: normalise raw SOI industry strings to a canonical sector +
credit risk multiplier (keyword rules in bdc_sector_map.yaml).

Used by bdc_credit (SOI-mode sector adjustment) and bdc_lookthrough (clean sector
exposure). Unmatched industries fall to 'Other' and are recorded so the rule set can be
extended — never silently mis-bucketed.
"""

from __future__ import annotations

import os
import functools

_YAML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bdc_sector_map.yaml")
_UNMATCHED: set[str] = set()


@functools.lru_cache(maxsize=1)
def _rules():
    import yaml
    cfg = yaml.safe_load(open(_YAML))
    return cfg["rules"], cfg.get("default", {"canonical": "Other", "multiplier": 1.0})


@functools.lru_cache(maxsize=4096)
def classify(industry) -> tuple[str, float]:
    """raw SOI industry -> (canonical_sector, risk_multiplier). First-rule-wins."""
    rules, default = _rules()
    if not industry or str(industry).lower() in ("nan", "none", ""):
        return default["canonical"], float(default["multiplier"])
    text = str(industry).lower()
    for r in rules:
        if any(kw in text for kw in r["keywords"]):
            return r["canonical"], float(r["multiplier"])
    _UNMATCHED.add(str(industry))
    return default["canonical"], float(default["multiplier"])


def canonical_sector(industry) -> str:
    return classify(industry)[0]


def risk_multiplier(industry) -> float:
    return classify(industry)[1]


def unmatched() -> list[str]:
    return sorted(_UNMATCHED)
