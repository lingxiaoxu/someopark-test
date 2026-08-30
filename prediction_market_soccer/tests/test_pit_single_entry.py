"""Both replay paths must get their prior from the same point-in-time entry.

The module has two of them — the frozen bet ledger (ops/performance_report._pit_strength)
and the walk-forward (model/pit_strength.WalkForwardStrength) — and the first round of PIT
work fixed only the second. The ledger kept caching `load_prior(league)` under the LEAGUE
alone, so its model memo varied by date while its anchor never did: every settled match in
the report window was priced with TODAY's prior. Measured on Brasileirão's 58 settled
matches, Brier 0.6364 with today's prior against 0.6688 with the match-day one (paired
t = -4.02); on Argentina the argmax pick flipped on 29 of 90.

Separately, the market champion anchor was a LIVE venue read with no date parameter, so a
prior stamped in July carried the title book's August opinion — a 2026-07-14 prior and
today's held identical market_p_champion for 20 of 20 Brasileirão clubs.
"""
from __future__ import annotations

import inspect


def test_both_replay_paths_use_the_shared_entry():
    """One function, so a future fix cannot land on one caller and miss the other."""
    from prediction_market_soccer.model import pit_strength
    from prediction_market_soccer.ops import performance_report

    assert hasattr(pit_strength, "pit_prior"), "the shared entry point must exist"
    led = inspect.getsource(performance_report._pit_strength)
    assert "pit_prior" in led, "the ledger path must obtain its prior through pit_prior"
    wf = inspect.getsource(pit_strength.WalkForwardStrength._prior)
    assert "pit_prior" in wf, "the walk-forward must obtain its prior through pit_prior"


def test_the_ledger_prior_cache_is_keyed_by_date():
    """Keyed on the league alone, the anchor could not move with the calendar."""
    led = inspect.getsource(__import__(
        "prediction_market_soccer.ops.performance_report", fromlist=["x"])._pit_strength)
    assert "_pit_prior_cache[_pk]" in led
    assert "_pit_prior_cache[league]" not in led, "league-only key reintroduces the leak"


def test_the_market_anchor_is_gated_on_the_date():
    """A live venue read has no history; for a past date the anchor must be OMITTED rather
    than approximated with today's book."""
    from prediction_market_soccer.ingest import club_prior
    src = inspect.getsource(club_prior.build_all)
    assert "_is_today(as_of)" in src, "the market anchor must be gated on as_of"
    assert club_prior._is_today(None) is True
    assert club_prior._is_today("2020-01-01") is False


def test_a_past_prior_carries_no_market_anchor():
    """The invariant stated as data rather than as source text."""
    import json

    from prediction_market_soccer.config import CONFIG
    p = CONFIG.paths.priors / "clubs_brasileirao_pit.json"
    if not p.exists():
        return
    doc = json.loads(p.read_text(encoding="utf-8"))
    import datetime as _dt
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    if str(doc.get("as_of", ""))[:10] >= today:
        return          # a _pit file rebuilt for today legitimately has one
    assert all(c.get("market_p_champion") is None for c in doc["clubs"]), (
        "a point-in-time prior must not carry the title book's opinion from after its date")
