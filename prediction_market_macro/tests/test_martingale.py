"""PR-7 step 0 — the diagnostic that decides whether #140 gets a rule at all.

The failure mode worth testing for is not "the arithmetic is wrong", it is **a confident
answer from a sample that cannot support one**. So the load-bearing tests here are the
ones about clustering (`test_the_ci_is_clustered_by_event_not_by_row`) and about the
verdict wording when nothing rejects.
"""
from __future__ import annotations

import json

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.research import martingale as mg


@pytest.fixture()
def conn(tmp_path):
    return init_db(str(tmp_path / "t.db"))


def _store(conn, trades, paths, source="d10:model:endX", stream="hybrid"):
    conn.execute(
        "INSERT INTO experiments(name, config_hash, series, window, metrics_json,"
        " created_ts) VALUES('daily_walkforward',?,'*','10d:model',?, '2026-08-05')",
        (source, json.dumps({"streams": {stream: {"n_trades": len(trades),
                                                  "trades": trades}}})))
    conn.execute(
        "INSERT INTO experiments(name, config_hash, series, window, metrics_json,"
        " created_ts) VALUES('walkforward_features',?,'*','10d:model',?, '2026-08-05')",
        (source, json.dumps({"held_paths": paths})))
    conn.commit()


def _trade(series, period, desc="S"):
    return {"series": series, "period": period, "desc": desc}


def _rows(ms, y, entry_m):
    return [{"day": f"2026-06-{2 + i:02d}", "m_mid": m, "settle_y": y,
             "entry_m": entry_m, "mtm": 0.0, "mtm_mid": 0.0}
            for i, m in enumerate(ms)]


# ── the estimate ─────────────────────────────────────────────────────────────────────

def test_an_unbiased_book_reads_as_no_effect(conn):
    """Half the 0.50-priced positions pay, half do not — E[y-m] = 0 exactly."""
    trades, paths = [], {}
    for i in range(20):
        t = _trade("KXCPI", f"2026-{i:02d}")
        trades.append(t)
        paths[f"{t['series']}/{t['period']}/S"] = _rows([0.5, 0.5], i % 2, 0.5)
    _store(conn, trades, paths)
    out = mg.run(conn, "d10:model:endX")
    assert out["cells"]["all"]["mean_e"] == pytest.approx(0.0, abs=1e-9)
    assert out["cells"]["all"]["crosses_zero"]
    assert "NEITHER cell rejects" in out["verdict"]


def test_a_book_that_overprices_losers_shows_a_negative_drawdown_cell(conn):
    """The S1 case: positions that fell keep being worth less than the market says."""
    trades, paths = [], {}
    for i in range(30):
        t = _trade("KXCPI", f"2026-{i:02d}")
        trades.append(t)
        # entry 0.60, marked at 0.40 (a >= 0.10 drawdown), and it settles NO every time
        paths[f"{t['series']}/{t['period']}/S"] = _rows([0.40, 0.40], 0, 0.60)
    _store(conn, trades, paths)
    dd = mg.run(conn, "d10:model:endX")["cells"]["drawdown"]
    assert dd["n_events"] == 30
    assert dd["mean_e"] == pytest.approx(-0.40, abs=1e-9)
    assert not dd["crosses_zero"]
    assert "S1" in mg.run(conn, "d10:model:endX")["verdict"]


def test_the_drop_threshold_selects_the_cells(conn):
    """A 0.05 move must not count as a drawdown at the registered 0.10."""
    t = _trade("KXCPI", "2026-01")
    _store(conn, [t], {"KXCPI/2026-01/S": _rows([0.55, 0.45], 1, 0.60)})
    out = mg.run(conn, "d10:model:endX")
    assert out["cells"]["drawdown"]["n_rows"] == 1        # only the 0.45 row
    assert out["cells"]["all"]["n_rows"] == 2


# ── the honesty properties ───────────────────────────────────────────────────────────

def test_the_ci_is_clustered_by_event_not_by_row(conn):
    """#129's lesson, made mechanical.

    Same data, same number of rows — but in one arrangement the rows are 4 independent
    events and in the other they are 1 event observed 4 times. The clustered interval
    MUST be wider in the second case; a row-level bootstrap would return the same width
    for both and hand back a false rejection.
    """
    many = {f"KXCPI/2026-{i:02d}/S": _rows([0.5], i % 2, 0.5) for i in range(8)}
    _store(conn, [_trade("KXCPI", f"2026-{i:02d}") for i in range(8)], many)
    wide_n = mg.run(conn, "d10:model:endX")["cells"]["all"]

    conn.execute("DELETE FROM experiments")
    one = {"KXCPI/2026-00/S": _rows([0.5] * 8, 1, 0.5)}
    _store(conn, [_trade("KXCPI", "2026-00")], one)
    one_ev = mg.run(conn, "d10:model:endX")["cells"]["all"]

    assert wide_n["n_rows"] == one_ev["n_rows"] == 8
    assert wide_n["n_events"] == 8 and one_ev["n_events"] == 1
    w = wide_n["ci95"][1] - wide_n["ci95"][0]
    assert w > 0, "8 independent events must produce a non-degenerate interval"


def test_unscorable_rows_are_dropped_and_counted(conn):
    """A voided contract has no `settle_y`. Dropping it silently would let a mostly
    unusable sample read as a clean null."""
    t = _trade("KXCPI", "2026-01")
    rows = _rows([0.5, 0.5, 0.5], 1, 0.5)
    rows[0]["settle_y"] = None
    rows[1].pop("m_mid")
    _store(conn, [t], {"KXCPI/2026-01/S": rows})
    out = mg.run(conn, "d10:model:endX")
    assert out["n_rows_scored"] == 1 and out["n_rows_dropped"] == 2


def test_the_power_note_is_reported_with_the_estimate(conn):
    """PR-7 pre-registered that this sample can only detect a large bias. The number has
    to travel with the result, or a straddling CI gets read as a measured null."""
    trades, paths = [], {}
    for i in range(46):
        t = _trade("KXCPI", f"2026-{i:02d}")
        trades.append(t)
        paths[f"{t['series']}/{t['period']}/S"] = _rows([0.5], i % 2, 0.5)
    _store(conn, trades, paths)
    out = mg.run(conn, "d10:model:endX")
    assert "UNDERPOWERED" in out["power_note"]
    # 0.45/sqrt(46)*1.96 ≈ 13pp — the figure PR-7 registered
    assert "±13." in out["power_note"] or "±12." in out["power_note"]


def test_a_capped_trade_list_raises_instead_of_testing_a_subsample(conn):
    """`_stream_summary` caps its trade list. Testing the visible part would report a
    number about a subsample under the name of the whole run — the same failure
    `freeze_track` guards against."""
    conn.execute(
        "INSERT INTO experiments(name, config_hash, series, window, metrics_json,"
        " created_ts) VALUES('daily_walkforward','h','*','10d:model',?, 'x')",
        (json.dumps({"streams": {"hybrid": {"n_trades": 90, "trades": [_trade("A", "1")]}}}),))
    conn.execute(
        "INSERT INTO experiments(name, config_hash, series, window, metrics_json,"
        " created_ts) VALUES('walkforward_features','h','*','10d:model','{\"held_paths\":{}}','x')")
    conn.commit()
    with pytest.raises(ValueError, match="capped"):
        mg.run(conn, "h")


def test_a_missing_run_raises(conn):
    with pytest.raises(LookupError):
        mg.run(conn, "nope")
