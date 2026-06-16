"""Tests for the static prior ingestion + validation (plan 10)."""
from __future__ import annotations

import json

import pytest

from prediction_market.ingest.prior_ingest import (
    PriorValidationError,
    canonical_team_name,
    load_prior,
    team_id,
)


def test_load_prior_structure():
    snap = load_prior()
    assert len(snap.teams) == 48
    assert len(snap.groups) == 12
    for members in snap.groups.values():
        assert len(members) == 4


def test_advancement_identity_within_tolerance():
    snap = load_prior()
    for t in snap.teams:
        assert t.identity_residual <= 0.02, f"{t.name} residual {t.identity_residual}"


def test_draw_and_ranks_cover_all_teams():
    snap = load_prior()
    draw = snap.draw()
    ranks = snap.ranks()
    assert sum(len(v) for v in draw.values()) == 48
    assert len(ranks) == 48
    assert all(1 <= r <= 85 for r in ranks.values())


def test_team_id_and_aliases():
    assert team_id("United States") == "united_states"
    assert team_id("Cote d'Ivoire") == "cote_divoire"
    assert canonical_team_name("Türkiye") == "Turkey"
    assert canonical_team_name("South Korea") == "Korea Republic"


def test_validation_rejects_broken_identity(tmp_path):
    snap = load_prior()
    raw = {
        "prior_id": "x", "source": "x", "as_of": "2026-06-10", "is_stale": True,
        "sim_count": 1, "format_rules": "x",
        "teams": [],
    }
    # Build 48 teams but corrupt one's advancement identity badly.
    for i, t in enumerate(snap.teams):
        rec = {
            "group": t.group, "team": t.name, "zh": t.zh, "fifa_rank": t.fifa_rank,
            "exp_points": t.exp_points, "p_first": t.p_first, "p_second": t.p_second,
            "p_third_revive": t.p_third_revive, "p_advance": t.p_advance,
        }
        if i == 0:
            rec["p_advance"] = 0.05  # way off from the components
        raw["teams"].append(rec)

    path = tmp_path / "broken.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(PriorValidationError, match="identity"):
        load_prior(path)
