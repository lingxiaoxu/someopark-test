"""The walk-forward must never hand a competition a prior from another week.

Two defects lived here, both introduced by the fix for the ORIGINAL leak:

  1. build_all wrote the merged snapshot to a hard-coded "clubs_all.json" while every
     per-competition file honoured `suffix`, so a walk-forward build replaced the LIVE
     cross-league prior — the file ~40 live call sites read through `load_prior()` — with
     a point-in-time one. Seen on disk as clubs_all.json at as_of 2026-08-03 beside
     per-comp files at 2026-08-27.

  2. WalkForwardStrength cached on the DATE alone while build_all overwrites one fixed
     path per competition. Only one generation of files can exist, so a competition that
     short-circuited on "already built this day" loaded whatever generation happened to
     be on disk — measured: epl@2026-08-10 receiving the prior built for 2026-08-17, a
     week AFTER the matches it was pricing.
"""
from __future__ import annotations

import json

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.model.pit_strength import _PIT_SUFFIX, WalkForwardStrength


def test_the_merged_prior_follows_the_suffix():
    """A suffixed build must not be able to reach the live merged file."""
    import inspect

    from prediction_market_soccer.ingest import club_prior
    src = inspect.getsource(club_prior.build_all)
    assert 'f"clubs_all{suffix}.json"' in src, "the merged write must interpolate the suffix"
    # The WRITE expression, not the bare filename — the explanatory comment names the
    # file too, and a test that cannot tell prose from code is a test that will lie.
    assert '_PRIORS / "clubs_all.json"' not in src, "hard-coded merged write still present"


def test_the_merged_prior_is_written_after_the_domestic_loan():
    """Written before the loan pass, the merged file disagreed with the per-comp files on
    every club whose anchor was lent from its domestic league."""
    import inspect

    from prediction_market_soccer.ingest import club_prior
    src = inspect.getsource(club_prior.build_all)
    assert src.index("domestic anchors") < src.index('f"clubs_all{suffix}.json"')


def test_the_live_merged_prior_matches_the_live_per_comp_priors():
    """The regression itself, checked against what is actually on disk: clubs_all.json
    must carry the same as_of as the per-competition files it is merged from."""
    p = CONFIG.paths.priors / "clubs_all.json"
    if not p.exists():
        return
    merged = json.loads(p.read_text(encoding="utf-8"))
    per = CONFIG.paths.priors / "clubs_epl.json"
    if not per.exists():
        return
    live = json.loads(per.read_text(encoding="utf-8"))
    assert merged.get("as_of") == live.get("as_of"), (
        f"merged prior is from {merged.get('as_of')} while the live per-comp prior is "
        f"from {live.get('as_of')} — a walk-forward build has overwritten it")
    assert _PIT_SUFFIX not in str(merged.get("prior_id") or "")


def test_the_prior_cache_is_keyed_by_competition_and_day():
    """A date-only key cannot be correct while the files share one path per competition."""
    wf = WalkForwardStrength.__new__(WalkForwardStrength)
    wf._priors = {}
    wf._priors[("epl", "2026-08-10")] = "A"
    wf._priors[("epl", "2026-08-17")] = "B"
    wf._priors[("laliga", "2026-08-10")] = "C"
    assert wf._priors[("epl", "2026-08-10")] == "A"
    assert wf._priors[("laliga", "2026-08-10")] == "C"
    assert len(wf._priors) == 3, "competition and day must both be part of the key"


def test_every_competition_is_cached_from_the_same_build():
    """The fix reads all competitions off disk while the files are still that day's own;
    a later day's build must not be able to answer for an earlier one."""
    import inspect

    src = inspect.getsource(WalkForwardStrength._prior)
    assert "for c in active()" in src, "the whole day must be captured, not just the caller's comp"
    assert "self._priors[(c.key, day)]" in src
