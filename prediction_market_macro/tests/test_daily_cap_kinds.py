"""#151/F7 — the per-day exposure cap must count every kind of open, not just `kind='open'`.

`risk.check` applies five caps. Four of them read `_open_exposure` -> `ledger.open_positions`,
which counts all four `OPEN_KINDS` (open, argmax, arb, snipe). The fifth — the $30 per-release-day
cap — ran its own SQL against `kind='open'` alone, so an argmax/arb/snipe leg consumed none of
the day's budget. One question, two kind-sets, inside a single function: the exact split #149
and #150 fixed on the close side.

It fails OPEN — it under-reads today's exposure and therefore lets trades through — which is the
dangerous direction for a cap.

The fixture books the prior exposure and then CLOSES it in the same day. That is not incidental:
it isolates the day cap from the other four (a closed position leaves `_open_exposure` empty, so
gross/family/cluster/event all read zero) and it pins the semantic the cap actually has — it
limits the day's FLOW of new exposure, not the stock still standing at the end of it.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ops import risk

S, P = "KXWTIW", "2026-08-07"


@pytest.fixture()
def conn(tmp_path):
    return init_db(str(tmp_path / "t.db"))


def _today_ts() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _open_and_close(conn, kind: str, usd: float, ts: str | None = None) -> None:
    """Book `usd` of new exposure under `kind` at `ts` (default today), then retire it."""
    ts = ts or _today_ts()
    cur = conn.execute(
        "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, size_usd,"
        " inputs_json, model_version, gate_snapshot, note)"
        " VALUES(?,?,?,'{\"desc\":\"YES T\"}',?,?,'{}','m/1.0','{}','')",
        (ts, S, P, kind, usd))
    conn.execute(
        "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, size_usd,"
        " inputs_json, model_version, gate_snapshot, note, closes_decision_id)"
        " VALUES(?,?,?,'{}','exit',0,'{}','m/1.0','{}','',?)",
        (ts, S, P, cur.lastrowid))
    conn.commit()


def _open_and_close_today(conn, kind: str, usd: float) -> None:
    _open_and_close(conn, kind, usd)


@pytest.mark.parametrize("kind", ["open", "argmax", "arb", "snipe"])
def test_every_open_kind_consumes_the_day_budget(conn, kind):
    """Parametrised over the whole of `OPEN_KINDS` rather than just the argmax case that
    exists today. `arb` and `snipe` have never fired on the live book — which is precisely
    why a bug on them would go unnoticed until the day they do."""
    _open_and_close_today(conn, kind, risk.LIMITS["per_release_day_usd"] - 0.5)
    veto = risk.check(conn, S, P, 1.0)
    assert veto is not None, f"{kind} did not consume the per-release-day budget"
    assert veto.reason.startswith("risk_release_day"), veto.reason


def test_the_day_cap_is_the_only_one_this_fixture_can_trip(conn):
    """Guards the test above from passing for the wrong reason: with the prior exposure
    closed, the four stock caps see an empty book, so a veto here can only be the day cap."""
    _open_and_close_today(conn, "argmax", risk.LIMITS["per_release_day_usd"] - 0.5)
    assert risk._open_exposure(conn) == []


def test_a_day_under_the_cap_still_passes(conn):
    """The fix tightens a cap, so the other half of the claim has to be pinned too:
    it must not start vetoing days that are genuinely under budget. $3.45 is the live
    2026-08-06 mixed-kind total ($2.55 open + $0.90 argmax)."""
    _open_and_close_today(conn, "open", 2.55)
    _open_and_close_today(conn, "argmax", 0.90)
    assert risk.check(conn, S, P, 1.0) is None


def test_yesterdays_exposure_does_not_count_against_today(conn):
    """`ts_utc>=today` is a string compare against a date prefix. Cheap to get wrong, and
    wrong in the direction that freezes the desk. $48 on 2026-07-28 is the live book's
    real first-day total, which is over the cap — so if the date bound ever broke, this is
    the row that would wedge the desk shut."""
    _open_and_close(conn, "argmax", 48.0, ts="2026-07-28T01:44:41+00:00")
    assert risk.check(conn, S, P, 1.0) is None
