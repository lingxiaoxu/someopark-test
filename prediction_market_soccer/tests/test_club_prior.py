"""Club prior ingestion + validation (TRANSFORM_PLAN §3.2).

Replaces the WC module's single 48-team / 12-group snapshot: there is now ONE prior per
competition (plus a merged cross-league one), anchored on last season's final table and
ClubElo instead of FIFA rank and an external tournament sim.

The iron rule is inherited verbatim: a prior is a STALE STARTING LINE, never a tradable
signal — with 34-38 rounds a season the result-update path takes over quickly.
"""
from __future__ import annotations

import json

import pytest

from prediction_market_soccer.config.leagues import active, get
from prediction_market_soccer.ingest.club_prior import (
    ClubPrior,
    ClubPriorSnapshot,
    PriorValidationError,
    canonical_club_name,
    load_prior,
    team_id,
)


def test_every_enabled_competition_has_a_prior():
    for comp in active():
        snap = load_prior(comp.key)
        assert snap.league == comp.key
        assert snap.teams, comp.key
        # A domestic league's roster is exact; a cup/swiss prior is a pre-draw superset
        # (UCL is 52 clubs now and becomes 36 after the draw), so only a drastically
        # short roster is wrong there.
        if comp.kind == "league":
            assert len(snap.teams) == comp.n_teams, comp.key
        else:
            assert len(snap.teams) >= comp.n_teams // 2, comp.key


def test_merged_prior_spans_every_competition():
    """clubs_all.json is what the cross-league paths (confidence tiers, the global
    backtest model) price with — it must actually cover the field."""
    snap = load_prior()
    assert snap.league == "all"
    leagues = {t.league for t in snap.teams}
    assert leagues == {c.key for c in active()}
    ids = [t.club_id for t in snap.teams]
    assert len(set(ids)) == len(ids)        # a club appears once, under one competition


def test_prior_is_marked_stale():
    """The staleness flag is the guard against a prior ever reading as live signal."""
    assert load_prior("epl").is_stale is True
    assert load_prior().is_stale is True


def test_anchor_points_are_per_round_and_ordered():
    """Anchors are POINTS PER ROUND (not a season total), so they stay comparable across
    a 38-round league, a 34-round one and a cup — the reverse-fit consumes them directly."""
    snap = load_prior("epl")
    for t in snap.teams:
        assert t.anchor_points is None or 0.0 <= t.anchor_points <= 3.0
    table = snap.league_table()
    assert table[0] == "arsenal"
    anchors = [snap.by_id[c].anchor_points for c in table]
    assert anchors == sorted(anchors, reverse=True)


def test_promoted_clubs_carry_a_rebuilt_anchor():
    snap = load_prior("epl")
    promoted = [t for t in snap.teams if t.promoted]
    assert promoted, "a top-flight prior should know who came up"
    # Promoted sides have no top-flight record to average, so they must not outrank the
    # established field on an anchor they never earned.
    assert max(t.anchor_points for t in promoted) < max(
        t.anchor_points for t in snap.teams if not t.promoted)


def test_elo_ranks_are_cross_league():
    """ClubElo is the one anchor comparable ACROSS competitions (South America is
    outside its coverage and is neutral-filled, so a rank may legitimately be absent)."""
    snap = load_prior()
    ranks = snap.ranks()
    assert ranks, "the merged prior should carry Elo ranks"
    assert all(r >= 1 for r in ranks.values())
    assert len(set(ranks.values())) == len(ranks)      # ranks are distinct


def test_wc_dropin_field_names_still_resolve():
    """Copied WC consumers read ``team_id`` / ``fifa_rank`` / ``group``; the club prior
    keeps those names as properties so those call sites work unchanged."""
    t = load_prior("epl").by_id["arsenal"]
    assert t.team_id == t.club_id == "arsenal"
    assert t.group == t.league == "epl"
    assert t.fifa_rank == (t.elo_rank if t.elo_rank is not None else 999)
    # …and the snapshot class itself is exported under the WC name.
    assert ClubPriorSnapshot.__name__ == "ClubPriorSnapshot"
    from prediction_market_soccer.ingest.club_prior import PriorSnapshot
    assert PriorSnapshot is ClubPriorSnapshot


def test_team_id_and_aliases():
    assert team_id("Manchester City") == "manchester_city"
    assert team_id("Man City") == "manchester_city"          # ClubElo short name
    assert team_id("Nott'm Forest") == "nott_m_forest"       # punctuation normalised
    assert team_id("Brighton & Hove Albion") == "brighton_and_hove_albion"
    assert canonical_club_name("Bayern") == "bayern_munich"
    assert canonical_club_name("Real Madrid") == "Real Madrid"   # unknown → left alone


def test_validation_rejects_duplicate_club_ids(tmp_path, monkeypatch):
    from prediction_market_soccer.ingest import club_prior as cp
    snap = load_prior("epl")
    rows = [{k: getattr(t, k) for k in ClubPrior.__dataclass_fields__} for t in snap.teams]
    rows[1] = dict(rows[1], club_id=rows[0]["club_id"])       # two clubs, one id
    path = tmp_path / "clubs_epl.json"
    path.write_text(json.dumps({"prior_id": "x", "source": "x", "as_of": "2026-08-26",
                                "is_stale": True, "league": "epl", "clubs": rows}),
                    encoding="utf-8")
    monkeypatch.setattr(cp, "_PRIORS", tmp_path)
    with pytest.raises(PriorValidationError, match="duplicate"):
        cp.load_prior("epl")


def test_validation_rejects_a_league_with_the_wrong_number_of_clubs(tmp_path, monkeypatch):
    """A domestic league's size is known exactly; a short roster means a broken build,
    and a broken build must never reach the strength fit silently."""
    from prediction_market_soccer.ingest import club_prior as cp
    snap = load_prior("epl")
    rows = [{k: getattr(t, k) for k in ClubPrior.__dataclass_fields__} for t in snap.teams][:-1]
    path = tmp_path / "clubs_epl.json"
    path.write_text(json.dumps({"prior_id": "x", "source": "x", "as_of": "2026-08-26",
                                "is_stale": True, "league": "epl", "clubs": rows}),
                    encoding="utf-8")
    monkeypatch.setattr(cp, "_PRIORS", tmp_path)
    with pytest.raises(PriorValidationError, match="expected 20"):
        cp.load_prior("epl")


def test_missing_prior_names_the_rebuild_command(tmp_path, monkeypatch):
    from prediction_market_soccer.ingest import club_prior as cp
    monkeypatch.setattr(cp, "_PRIORS", tmp_path)
    with pytest.raises(PriorValidationError, match="--build"):
        cp.load_prior("epl")


def test_registry_and_prior_agree_on_the_competition_set():
    """Adding a competition is one registry entry + one prior file; this pins the two
    halves together so a half-added league fails loudly rather than silently pricing
    nothing."""
    for comp in active():
        assert get(comp.key).api_football_id > 0
        assert load_prior(comp.key).league == comp.key
