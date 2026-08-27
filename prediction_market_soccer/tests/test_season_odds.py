"""Season-odds board export (ops/season_odds_export) — the club mirror of the WC
reach-round board.

The contract this pins is the HONEST EMPTY STATE. A pre-draw swiss competition and a cup
with no live bracket have no champion distribution at all; publishing 0% for all 153 clubs
would read as a confident "nobody can win it". The model emits ``null`` for those
probabilities plus an ``odds_state`` saying WHY, and this export must keep the competition
visible with that reason instead of quietly dropping it — a vanished league reads as
missing data, which is a different (and wrong) message.
"""
from __future__ import annotations

import json
import types

import pytest

from prediction_market_soccer.ops import season_odds_export as soe


@pytest.fixture
def output_dir(tmp_path, monkeypatch):
    """Point the export at a scratch output dir — a test must not read (or race) the
    live soccer_model.json, and must never write into data/output."""
    monkeypatch.setattr(soe, "CONFIG",
                        types.SimpleNamespace(paths=types.SimpleNamespace(output=tmp_path)))
    return tmp_path


def _write_model(output_dir, leagues):
    (output_dir / "soccer_model.json").write_text(
        json.dumps({"meta": {}, "leagues": leagues}, ensure_ascii=False), encoding="utf-8")


def _row(club_id, **p):
    base = {"club_id": club_id, "name": club_id.title(), "zh": "", "logo": None,
            "p_champion": None, "p_top_n": None, "p_relegation": None,
            "p_last": None, "p_qual_direct": None, "p_qual_playoff": None}
    base.update(p)
    return base


def _no_venue(monkeypatch, cents=None):
    """Stub the Kalshi side: the export resolves season_odds_cents at call time, so the
    unit test never touches the network."""
    from prediction_market_soccer.venues import champion_prices
    monkeypatch.setattr(champion_prices, "season_odds_cents",
                        lambda comp_key: (cents or {}).get(comp_key, {}))


def test_priced_league_builds_one_board_per_family(output_dir, monkeypatch):
    _no_venue(monkeypatch, {"epl": {"champion": {"arsenal": 30.0}}})
    _write_model(output_dir, [{
        "league": "epl", "name": "Premier League", "zh": "英超", "kind": "league",
        "odds_state": "ok",
        "season_odds": [_row("arsenal", p_champion=0.42, p_top_n=0.90, p_relegation=0.0),
                        _row("ipswich", p_champion=0.01, p_top_n=0.05, p_relegation=0.55)],
    }])
    doc = soe.build(conn=object())
    epl = next(lg for lg in doc["leagues"] if lg["league"] == "epl")
    assert epl["state"] == "ok"
    assert [b["family"] for b in epl["boards"]] == ["champion", "top_n", "relegation"]
    champ = epl["boards"][0]
    assert champ["kalshi_series"] == "KXPREMIERLEAGUE"
    top_row = champ["rows"][0]
    assert top_row["club_id"] == "arsenal"
    assert top_row["model_c"] == 42.0 and top_row["kalshi_c"] == 30.0
    assert top_row["edge_vs_kalshi"] == 12.0          # model¢ − kalshi¢
    assert top_row["poly_c"] is None                  # Phase-3b Global slugs pending


def test_pending_draw_competition_stays_visible_with_its_reason(output_dir, monkeypatch):
    """Pre-draw UCL: every probability is null, so no board can be built — but the
    competition must still appear, carrying ``pending_draw``."""
    _no_venue(monkeypatch)
    _write_model(output_dir, [{
        "league": "ucl", "name": "UEFA Champions League", "zh": "欧冠", "kind": "swiss_ucl",
        "odds_state": "pending_draw",
        "season_odds": [_row("lyon"), _row("celtic")],
    }])
    doc = soe.build(conn=object())
    ucl = next(lg for lg in doc["leagues"] if lg["league"] == "ucl")
    assert ucl["state"] == "pending_draw"
    assert ucl["boards"] == []
    assert ucl["kind"] == "swiss_ucl"


def test_cup_without_a_bracket_reports_pending_bracket(output_dir, monkeypatch):
    _no_venue(monkeypatch)
    _write_model(output_dir, [{
        "league": "libertadores", "name": "Copa Libertadores", "zh": "解放者杯",
        "kind": "cup_two_leg", "odds_state": "pending_bracket",
        "season_odds": [_row("palmeiras"), _row("flamengo")],
    }])
    doc = soe.build(conn=object())
    lib = next(lg for lg in doc["leagues"] if lg["league"] == "libertadores")
    assert lib["state"] == "pending_bracket" and lib["boards"] == []


def test_a_null_probability_is_never_rendered_as_zero(output_dir, monkeypatch):
    """The crux of the empty state: a null p_champion is dropped from the board, not
    coerced to 0.0 — otherwise the card shows a confident 0% for the whole field."""
    _no_venue(monkeypatch)
    _write_model(output_dir, [{
        "league": "epl", "name": "Premier League", "zh": "英超", "kind": "league",
        "odds_state": "ok",
        "season_odds": [_row("arsenal", p_champion=0.42),
                        _row("ipswich")],          # champion unknown for this club
    }])
    doc = soe.build(conn=object())
    rows = next(lg for lg in doc["leagues"] if lg["league"] == "epl")["boards"][0]["rows"]
    assert [r["club_id"] for r in rows] == ["arsenal"]


def test_swiss_boards_are_the_qualification_cuts_not_relegation(output_dir, monkeypatch):
    """A swiss league phase has no relegation and no European cut — its boards are the
    top-8 direct and the 9-24 play-off slots."""
    _no_venue(monkeypatch)
    _write_model(output_dir, [{
        "league": "ucl", "name": "UEFA Champions League", "zh": "欧冠", "kind": "swiss_ucl",
        "odds_state": "ok",
        "season_odds": [_row("lyon", p_champion=0.08, p_qual_direct=0.55, p_qual_playoff=0.35),
                        _row("celtic", p_champion=0.02, p_qual_direct=0.20, p_qual_playoff=0.50)],
    }])
    doc = soe.build(conn=object())
    ucl = next(lg for lg in doc["leagues"] if lg["league"] == "ucl")
    assert [b["family"] for b in ucl["boards"]] == ["champion", "qual_direct", "qual_playoff"]
    assert ucl["boards"][1]["kalshi_series"] == "KXUCLTOP8"
    assert ucl["boards"][2]["kalshi_series"] == ""     # no venue series for the 9-24 band


def test_venue_failure_never_blanks_the_board(output_dir, monkeypatch):
    """A venue hiccup costs the ¢ column, not the model column."""
    from prediction_market_soccer.venues import champion_prices

    def boom(comp_key):
        raise RuntimeError("venue down")

    monkeypatch.setattr(champion_prices, "season_odds_cents", boom)
    _write_model(output_dir, [{
        "league": "epl", "name": "Premier League", "zh": "英超", "kind": "league",
        "odds_state": "ok",
        "season_odds": [_row("arsenal", p_champion=0.42)],
    }])
    doc = soe.build(conn=object())
    row = next(lg for lg in doc["leagues"] if lg["league"] == "epl")["boards"][0]["rows"][0]
    assert row["model_c"] == 42.0
    assert row["kalshi_c"] is None and row["edge_vs_kalshi"] is None


def test_missing_model_document_yields_no_leagues(output_dir, monkeypatch):
    _no_venue(monkeypatch)
    doc = soe.build(conn=object())
    assert doc["leagues"] == []
    assert doc["note_key"] == "notes.seasonOdds"
