"""#152 — the live book's KXPCECORE 2026-11 stack reached $13 against a $5 per-event cap.

Thirteen `kind='open'` rows, same series, same period, same structure, $1.00 each, one per
tick between 2026-07-28T01:44Z and 2026-07-30T09:13Z. `per_event_usd` has been 5.0 since
before any of them, so the cap that broke is one that already existed — this is NOT #151/F7
(the per-release-day cap was only added 2026-07-31 08:38 EDT, after every one of these rows).

What actually ran on 07-28 cannot be recovered: `git log` for `ops/risk.py` starts at
2026-07-31 07:46 EDT, so the code of the three breaching days was never under version
control. Rather than guess at it, this test answers the question that still has consequences
— **is the breach reachable under today's code?** — by replaying the exact live sequence
against the current `risk.check`.

It is not: the sixth open is refused. Those rows also belong to the disavowed pre-cutover
book (#121), which is excluded from every displayed figure, so nothing published depends on
them. The value here is the pin: a $1 flat-size stream that ticks 13 times must not be able
to walk past a per-event cap again.
"""
from __future__ import annotations

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ops import risk

S, P = "KXPCECORE", "2026-11"

# the live ts_utc of all 13 rows (ids 39, 255, 312, 486, 546, 606, 666, 729, 790, 851, 912,
# 973, 1034 / 1095), each $1.00 — spanning three calendar days, so no day-scoped cap can be
# what stops the replay
LIVE_TS = ["2026-07-28T01:44:41+00:00", "2026-07-28T01:59:17+00:00",
           "2026-07-28T06:26:54+00:00", "2026-07-28T09:08:30+00:00",
           "2026-07-28T18:11:42+00:00", "2026-07-28T18:12:49+00:00",
           "2026-07-29T09:08:25+00:00", "2026-07-29T12:44:28+00:00",
           "2026-07-29T12:44:35+00:00", "2026-07-29T17:02:19+00:00",
           "2026-07-29T17:02:57+00:00", "2026-07-29T18:19:03+00:00",
           "2026-07-29T18:19:37+00:00", "2026-07-30T09:13:10+00:00"]


@pytest.fixture()
def conn(tmp_path):
    return init_db(str(tmp_path / "t.db"))


def _open(conn, ts: str, usd: float = 1.0) -> None:
    conn.execute(
        "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, size_usd,"
        " inputs_json, model_version, gate_snapshot, note)"
        " VALUES(?,?,?,'{\"desc\":\"YES T\"}','open',?,'{}','pce/0.1.0','{}','')",
        (ts, S, P, usd))
    conn.commit()


def test_todays_risk_check_refuses_the_live_sequence_at_the_sixth_open(conn):
    """Replayed one row at a time, exactly as the live ticks arrived. `per_event_usd` is
    5.0, so five $1 opens fit and the sixth must not."""
    placed = 0
    for ts in LIVE_TS:
        if risk.check(conn, S, P, 1.0) is not None:
            break
        _open(conn, ts)
        placed += 1
    assert placed == 5, f"{placed} of the 13 live opens got through today's cap"
    veto = risk.check(conn, S, P, 1.0)
    assert veto is not None and veto.reason.startswith("risk_per_event"), veto


def test_the_refusal_is_the_per_event_cap_and_not_a_day_cap(conn):
    """The breach spans three calendar days, so a reader might assume the day cap is what
    now catches it. It is not — and it must not be, because a per-event cap that only holds
    within a day is not a per-event cap."""
    for ts in LIVE_TS[:5]:
        _open(conn, ts)
    # a brand-new calendar day, far from any of the live timestamps: the day budget is
    # untouched, and the per-event stock is still $5
    veto = risk.check(conn, S, P, 1.0)
    assert veto is not None and veto.reason.startswith("risk_per_event"), veto


def test_a_closed_stack_frees_the_event_budget_again(conn):
    """The other half: the cap is on exposure held, not on trades ever made. Anti-vacuity
    for the two tests above — if `check` simply always vetoed, they would pass anyway."""
    for ts in LIVE_TS[:5]:
        _open(conn, ts)
    assert risk.check(conn, S, P, 1.0) is not None
    for row in conn.execute("SELECT id FROM decisions WHERE kind='open'").fetchall():
        conn.execute(
            "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, size_usd,"
            " inputs_json, model_version, gate_snapshot, note, closes_decision_id)"
            " VALUES('2026-07-30T00:00:00+00:00',?,?,'{}','exit',0,'{}','pce/0.1.0','{}','',?)",
            (S, P, row["id"]))
    conn.commit()
    assert risk.check(conn, S, P, 1.0) is None
