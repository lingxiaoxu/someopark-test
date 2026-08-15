"""cleveland_nowcast: the parsing judgment calls that could silently corrupt vintages.

1. December targets: MM/DD labels roll into the NEXT year when the label month is
   below the target month — a 12-target's 01/05 point is January of year+1.
2. vline objects inside the category array are markers, not days — they must not
   shift the label↔value zip (the off-by-one would misdate every later nowcast).
3. 'Actual ...' series are skipped (fred_obs owns actuals).
4. knowledge_time is the nowcast day 18:00 UTC; store is idempotent and preserves
   first_seen_ts (weather.py contract).
5. Quarterly targets carry no month — year inference rolls on the label sequence.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from prediction_market_macro.ingest import cleveland_nowcast as cn


def _blob(subcaption, labels, series):
    cats = []
    for lab in labels:
        cats.append({"label": lab} if lab != "VLINE" else
                    {"vline": "true", "color": "#000"})
    return json.dumps([{
        "chart": {"subcaption": subcaption},
        "categories": [{"category": cats}],
        "dataset": [{"seriesname": name, "data": [{"value": v} for v in vals]}
                    for name, vals in series],
    }]).encode()


def test_december_target_rolls_labels_into_next_year():
    blob = _blob("2025-12", ["12/30", "12/31", "01/02"],
                 [("CPI Inflation", ["0.1", "0.2", "0.3"])])
    rows = cn.parse(blob, "mom")
    assert [r[3] for r in rows] == ["2025-12-30", "2025-12-31", "2026-01-02"]


def test_vlines_do_not_shift_the_zip():
    # 3 labels + 1 vline in the middle; 3 data points must land on the 3 labels
    blob = _blob("2026-08", ["08/11", "VLINE", "08/12", "08/13"],
                 [("Core CPI Inflation", ["0.1", "0.2", "0.3"])])
    rows = cn.parse(blob, "mom")
    assert [(r[3], r[4]) for r in rows] == [
        ("2026-08-11", 0.1), ("2026-08-12", 0.2), ("2026-08-13", 0.3)]


def test_actual_series_skipped_and_measures_mapped():
    blob = _blob("2026-08", ["08/11"],
                 [("CPI Inflation", ["0.5"]), ("Actual CPI Inflation", ["0.4"]),
                  ("Core PCE Inflation", ["0.2"])])
    rows = cn.parse(blob, "yoy")
    assert {(r[0], r[4]) for r in rows} == {("cpi", 0.5), ("corepce", 0.2)}


def test_quarterly_year_rollover_from_sequence():
    blob = _blob("2025-Q4", ["11/28", "12/31", "01/03"],
                 [("PCE Inflation", ["2.0", "2.1", "2.2"])])
    rows = cn.parse(blob, "q")
    assert [r[3] for r in rows] == ["2025-11-28", "2025-12-31", "2026-01-03"]


def test_store_idempotent_pit_accessor(monkeypatch):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    blob = _blob("2026-08", ["08/11", "08/12"],
                 [("CPI Inflation", ["0.30", "0.35"])])
    monkeypatch.setattr(cn, "_fetch", lambda kind, timeout=120: blob)
    out1 = cn.refresh(c, kinds=("month",))
    first = c.execute("SELECT first_seen_ts FROM cleveland_nowcast LIMIT 1").fetchone()[0]
    out2 = cn.refresh(c, kinds=("month",))
    assert out1 == out2 == {"mom": 2}
    assert c.execute("SELECT COUNT(*) FROM cleveland_nowcast").fetchone()[0] == 2
    assert c.execute("SELECT first_seen_ts FROM cleveland_nowcast LIMIT 1"
                     ).fetchone()[0] == first
    # PIT: at 08-12 17:00Z only the 08-11 nowcast (kt 18:00Z) is known
    asof = datetime(2026, 8, 12, 17, 0, tzinfo=timezone.utc)
    assert cn.latest(c, "cpi", "mom", "2026-08", asof) == ("2026-08-11", 0.30)
    asof2 = datetime(2026, 8, 12, 19, 0, tzinfo=timezone.utc)
    assert cn.latest(c, "cpi", "mom", "2026-08", asof2) == ("2026-08-12", 0.35)
