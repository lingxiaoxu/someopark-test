"""The owner-authorised leak correction: the legacy-rule port, the bundle, and its guards.

Context: the frozen book's legacy rows were priced off milestone rows a backfill had
sampled on the wall clock (15 minutes ahead of the score it stored). On 2026-09-11 the
owner ordered the freeze lifted, the prices corrected, and the result frozen as a new
version. These tests pin three things: the ported rules equal the f2fe563 originals on
the data they read, the bundle validator cannot be satisfied by relabelled generic
research, and the marks renderer works for any version, active or not."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from prediction_market_soccer.ops import leak_corrected_book as L
from prediction_market_soccer.ops import owner_authorized_correction as O


def _conn():
    from prediction_market_soccer.ingest import store
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(store._SCHEMA)
    return c


def _fixture(c, fid=101, *, home=1, away=2, goals=((52, 2),)):
    c.execute("INSERT INTO fixture (api_id, league_id, season, home_api_id, away_api_id, kickoff_ts, status_short, "
              "home_goals, away_goals, round, raw_json) VALUES (?,848,2026,?,?,'2026-08-20T18:00:00+00:00','FT',0,1,'League Phase','{}')",
              (fid, home, away))
    for seq, (mn, team) in enumerate(goals):
        c.execute("INSERT INTO fixture_event (fixture_api_id, seq, minute, team_api_id, type, detail) VALUES (?,?,?,?,'Goal','Normal Goal')",
                  (fid, seq, mn, team))
    for code, wall in (("PRE", -5), ("T15", 15), ("T30", 30), ("HT", 47), ("T60", 60), ("T75", 75)):
        c.execute("INSERT INTO milestone_snapshot (fixture_api_id, milestone, ts, elapsed, price_source, "
                  "poly_home_ask, poly_home_bid, poly_draw_ask, poly_draw_bid, poly_away_ask, poly_away_bid) "
                  "VALUES (?,?,?,?, 'candlestick', 0.6,0.6, 0.25,0.25, 0.135,0.135)",
                  (fid, code, f"2026-08-20T{18 + (wall + 5) // 60:02d}:{(wall + 5) % 60:02d}:00+00:00", max(wall, 0)))
    c.commit()


def test_corrected_poly_quote_replaces_a_reconstructed_row_and_unavailable_yields_none():
    c = _conn(); _fixture(c)
    quotes = {(101, "T60"): {"home": 0.235, "draw": 0.315, "away": 0.445, "ts": "x", "status": {"away": "ok"}},
              (101, "T75"): {"home": None, "draw": None, "away": None, "ts": "x", "status": {"away": "unavailable"}}}
    rows = {r["milestone"]: r for r in L._milestone_rows(c, 101)}
    assert L._poly(rows["T60"], quotes, "away") == 0.445          # corrected candidate value wins
    assert L._poly(rows["T75"], quotes, "away") is None           # unavailable never falls back to the leaky 0.135
    assert L._poly(rows["T15"], quotes, "away") is None           # no quote at all → nothing, not the old price
    rows["T15"]["price_source"] = "live"
    assert L._poly(rows["T15"], quotes, "away") == 0.135          # a LIVE row keeps its own stored value


def test_legacy_ticks_keep_kalshi_precedence_and_match_minutes():
    c = _conn(); _fixture(c)
    c.execute("UPDATE milestone_snapshot SET kalshi_away_bid=0.5 WHERE fixture_api_id=101 AND milestone='T30'")
    c.commit()
    quotes = {(101, m): {"home": 0.6, "draw": 0.25, "away": 0.15, "ts": "x", "status": {}} for m in ("T15", "T30", "HT", "T60", "T75")}
    ticks = L._ticks(c, 101, "away", 0, quotes)
    assert [m for m, _ in ticks] == [15, 30, 45, 60, 75], "match minutes, never wall minutes"
    assert dict(ticks)[30] == 0.5, "Kalshi bid outranks the Poly quote, exactly as f2fe563 did"
    assert dict(ticks)[60] == 0.15


def test_event_timeline_matches_the_legacy_accounting():
    c = _conn(); _fixture(c, goals=((10, 1), (52, 2)))
    c.execute("INSERT INTO fixture_event (fixture_api_id, seq, minute, team_api_id, type, detail) VALUES (101,9,60,2,'Goal','Missed Penalty')")
    c.execute("INSERT INTO fixture_event (fixture_api_id, seq, minute, team_api_id, type, detail) VALUES (101,10,70,1,'Goal','Own Goal')")
    c.commit()
    goals, _reds = L._event_timelines(c, 101, 1)
    assert L._state_at(goals, 9) == (0, 0) and L._state_at(goals, 10) == (1, 0)
    assert L._state_at(goals, 55) == (1, 1)
    assert L._state_at(goals, 65) == (1, 1), "a missed penalty is not a goal"
    assert L._state_at(goals, 75) == (1, 2), "an own goal credits the other side"


def test_match_minute_is_the_f2fe563_mapping():
    assert [L._match_minute(m) for m in (30, 45, 50, 60, 75, 90)] == [30, 45, 45, 45, 60, 75]


def _records():
    legacy = {"fixture_id": 101, "bet": True, "pick": "away", "won": True, "stake_usd": 1.0, "entry_cents": 13.5,
              "entry_source": "poly", "realized_pnl_cents": 640.7, "pnl_cents": 640.7, "inplay_side": None,
              "inplay_pnl_cents": None, "evidence_level": "leak_corrected_approx_pit_paper",
              "leak_correction": {"run_id": "run-1", "milestone_source": "candlestick"},
              "pre_cum_pnl_cents": 640.7, "inplay_cum_pnl_cents": 0.0, "combined_cum_pnl_cents": 640.7, "combined_pnl_cents": 640.7}
    forward = {"fixture_id": 202, "bet": True, "pick": "home", "won": False, "stake_usd": 1.0, "entry_cents": 40.0,
               "realized_pnl_cents": -100.0, "inplay_side": None, "inplay_pnl_cents": None,
               "evidence_level": "forward_observed_paper", "book_version_id": "v0", "pre_decision_id": "d1",
               "pre_cum_pnl_cents": 540.7, "inplay_cum_pnl_cents": 0.0, "combined_cum_pnl_cents": 540.7, "combined_pnl_cents": -100.0}
    return [legacy, forward]


def test_forward_signature_ignores_only_cumulative_fields():
    recs = _records()
    sig = O.forward_signature(recs)
    moved = json.loads(json.dumps(recs)); moved[1]["pre_cum_pnl_cents"] = 1.0; moved[1]["argmax_cum_pnl_cents"] = 5.0; moved[1]["cum_pnl_cents"] = 9.0
    assert O.forward_signature(moved) == sig, "running totals may move when earlier rows change"
    changed = json.loads(json.dumps(recs)); changed[1]["entry_cents"] = 41.0
    assert O.forward_signature(changed) != sig, "a forward leg value must never change"


def test_eligibility_covers_every_fixture_and_track_with_honest_statuses():
    rows = O.eligibility_rows(_records())
    assert {(r["fixture_id"], r["track"]) for r in rows} == {(101, "pre"), (101, "inplay"), (202, "pre"), (202, "inplay")}
    by = {(r["fixture_id"], r["track"]): r["status"] for r in rows}
    assert by[(101, "pre")] == "corrected" and by[(202, "pre")] == "forward_unchanged"
    assert all(s in O.ELIGIBILITY for s in by.values())


def test_the_generic_certification_gate_still_refuses_and_only_the_owner_kind_dispatches(tmp_path, monkeypatch):
    """Relabelling a bundle as the owner kind without its authorisation document, or with
    strict_pit_certified flipped to True, must be rejected — the path is narrow on purpose."""
    root = tmp_path / "root"; root.mkdir(); (root / ".soccer-isolated").write_text("soccer-isolated-v1")
    b = root / "bundle"; b.mkdir()
    inputs = {"analysis_purpose": O.KIND, "candidate_input": {"kind": O.KIND}}
    for name, doc in (("input_manifest.json", inputs), ("candidate_report.json", {}), ("completed_manifest.json", {"strict_pit_certified": True})):
        (b / name).write_text(json.dumps(doc))
    (b / "certification.json").write_text(json.dumps({"kind": O.KIND, "documents": {}, "certification_id": "x"}))
    with pytest.raises(Exception):
        O.validate_bundle(b, root=root)            # no authorisation.json → refused
    (b / "authorization.json").write_text(json.dumps({"kind": O.KIND, "owner": "o", "authorized_at": "t", "statement": "s"}))
    with pytest.raises(ValueError):
        O.validate_bundle(b, root=root)            # certification does not cover the documents → refused
    from prediction_market_soccer.ops import version_workflow as VW
    called = {}
    def _fake(d, root):
        called["root"] = root
        return {"ok": True}
    monkeypatch.setattr(O, "validate_bundle", _fake)
    assert VW.validate_backtest_bundle(b, root=root) == {"ok": True}
    assert called["root"] == root


def test_marks_for_book_serves_a_non_active_baseline_version():
    """A rollback renders the previous version while another is active; _build refuses
    that, marks_for_book must not."""
    c = _conn(); _fixture(c)
    from prediction_market_soccer.ops import milestone_export as M
    from prediction_market_soccer.util.strategy_ledger import build_strategy_ledger
    recs = [{**_records()[0], "evidence_level": "legacy_live_marked_paper", "leak_correction": None}]
    ledger = build_strategy_ledger({"bet_log": recs, "as_of": "2026-09-11T00:00:00+00:00"})
    ledger["book_version"] = {"version_id": "vX"}
    doc = M.marks_for_book(c, ledger, {"version_id": "vX", "origin": "published_baseline"})
    assert doc["n"] == 1 and [m["strategy_record"] for m in doc["matches"]] == recs
    assert [k["milestone"] for k in doc["matches"][0]["marks"]] == ["PRE", "T15", "T30", "HT", "T60", "T75"]
    with pytest.raises(ValueError):
        M.marks_for_book(c, ledger, {"version_id": "other", "origin": "published_baseline"})


def test_a_live_pre_row_keeps_its_observed_entry_even_when_the_candidate_has_a_pre_quote():
    """Review 2026-09-11: 36 entries whose PRE row the live loop captured itself were being
    replaced by a historical reference sample. The observed ask is the executable one."""
    c = _conn(); _fixture(c)
    c.execute("UPDATE milestone_snapshot SET price_source='live' WHERE fixture_api_id=101 AND milestone='PRE'"); c.commit()
    rows = L._milestone_rows(c, 101)
    rec = {"fixture_id": 101, "pick": "away", "entry_source": "poly", "entry_cents": 40.0}
    quotes = {(101, "PRE"): {"home": 0.6, "draw": 0.25, "away": 0.375, "ts": "x", "status": {"away": "ok"}}}
    assert L._corrected_entry(rec, quotes, rows) == (40.0, "kept:pre_row_live")
    c.execute("UPDATE milestone_snapshot SET price_source='candlestick' WHERE fixture_api_id=101 AND milestone='PRE'"); c.commit()
    assert L._corrected_entry(rec, quotes, L._milestone_rows(c, 101)) == (37.5, "corrected")


def test_ticks_keep_the_poly_bid_step_on_live_rows():
    c = _conn(); _fixture(c)
    c.execute("UPDATE milestone_snapshot SET price_source='live', poly_away_bid=0.30, poly_away_ask=0.34 WHERE fixture_api_id=101 AND milestone='T30'"); c.commit()
    ticks = dict(L._ticks(c, 101, "away", 0, {(101, m): {"home": 0.6, "draw": 0.25, "away": 0.15, "ts": "x", "status": {}} for m in ("T15", "T30", "HT", "T60", "T75")}))
    assert ticks[30] == 0.30, "f2fe563 precedence: Kalshi bid, Kalshi ask, Poly BID, Poly ask"


def test_accumulate_recomputes_the_three_cumulatives_in_order():
    cums = {"pre": 0.0, "ip": 0.0}
    a = {"bet": True, "realized_pnl_cents": 10.0, "inplay_side": "home", "inplay_pnl_cents": -4.0}
    b = {"bet": False, "inplay_side": None}
    L._accumulate(a, cums); L._accumulate(b, cums)
    assert (a["pre_cum_pnl_cents"], a["inplay_cum_pnl_cents"], a["combined_cum_pnl_cents"], a["combined_pnl_cents"]) == (10.0, -4.0, 6.0, 6.0)
    assert (b["pre_cum_pnl_cents"], b["combined_cum_pnl_cents"], b["combined_pnl_cents"]) == (10.0, 6.0, 0.0)


def test_method_version_is_unique_per_attempt():
    assert L.method_version_for("run-1", "a") != L.method_version_for("run-1", "b")
    assert L.method_version_for("run-1", "a").startswith("hybrid-smart-timing-v1+leakfix-approxpit-")


def test_unsealed_paper_entries_counts_entries_without_a_sealed_completion():
    c = _conn()
    assert L.unsealed_paper_entries(c) == 0, "no paper tables at all → nothing can be unsealed"
    c.executescript("CREATE TABLE paper_entry (decision_id TEXT, book_version_id TEXT, fixture_api_id INTEGER, track TEXT);"
                    "CREATE TABLE paper_completion (source_id TEXT, book_version_id TEXT, fixture_api_id INTEGER);")
    c.execute("INSERT INTO paper_entry VALUES ('d1','v0',7,'pre')"); c.commit()
    assert L.unsealed_paper_entries(c) == 1
    c.execute("INSERT INTO paper_completion VALUES ('s1','v0',7)"); c.commit()
    assert L.unsealed_paper_entries(c) == 0


def test_realized_cum_follows_pre_cum_on_non_bet_rows():
    """An in-play-only row (bet=False) must not keep its pre-correction realized cumulative.

    Production book ordinal 352 / fixture 1635708 is the one such row; before the fix it
    carried 3370.2c through the rebuild while the surrounding rows sat at 2615.0c.
    """
    from prediction_market_soccer.ops.leak_corrected_book import _accumulate
    rows = [
        {"bet": True, "realized_pnl_cents": 100.0, "pnl": 1.0, "pnl_cents": 100.0},
        {"bet": False, "inplay_side": "away", "inplay_pnl_cents": 50.0,
         "realized_cum_pnl_cents": 3370.2, "cum_pnl_cents": 999.9, "cum_pnl": 9.999},
        {"bet": True, "realized_pnl_cents": 25.0, "pnl": 0.25, "pnl_cents": 25.0},
    ]
    cums = {"pre": 0.0, "ip": 0.0}
    for row in rows:
        _accumulate(row, cums)
    assert [r["realized_cum_pnl_cents"] for r in rows] == [100.0, 100.0, 125.0]
    assert all(r["pre_cum_pnl_cents"] == r["realized_cum_pnl_cents"] for r in rows)
    # the held/usd cumulatives stay bet-only: the non-bet row keeps its stored values
    assert rows[1]["cum_pnl_cents"] == 999.9 and rows[1]["cum_pnl"] == 9.999
    assert rows[2]["cum_pnl_cents"] == 125.0


def test_evidence_tiers_recomputed_from_records():
    """The frozen metadata's tier array goes stale (old book: forward tier frozen at
    zeros despite 12 forward rows; corrected book inherited posthoc 208/170 rows that
    no longer exist). Tiers are derived data — recomputed per read from the records,
    with the semantics that reproduce the v7-era frozen array bit-for-bit."""
    from prediction_market_soccer.ops.performance_report import _evidence_tiers
    records = [
        {"evidence_level": "legacy_live_marked_paper", "bet": True,
         "realized_pnl_cents": -586.4, "inplay_side": "home", "inplay_pnl_cents": 558.3},
        {"evidence_level": "leak_corrected_approx_pit_paper", "bet": True,
         "realized_pnl_cents": 100.0},
        {"evidence_level": "forward_observed_paper", "bet": False,
         "inplay_side": "away", "inplay_pnl_cents": -50.0},
    ]
    tiers = {t["level"]: t for t in _evidence_tiers(records)}
    assert tiers["legacy_live_marked_paper"] == {"level": "legacy_live_marked_paper",
        "n_pre": 1, "n_inplay": 1, "pre_gross_usd": -5.864, "inplay_gross_usd": 5.583}
    assert tiers["leak_corrected_approx_pit_paper"]["n_pre"] == 1
    assert tiers["leak_corrected_approx_pit_paper"]["pre_gross_usd"] == 1.0
    # a bet=False row contributes no pre leg but keeps its in-play leg
    fwd = tiers["forward_observed_paper"]
    assert fwd["n_pre"] == 0 and fwd["n_inplay"] == 1 and fwd["inplay_gross_usd"] == -0.5
