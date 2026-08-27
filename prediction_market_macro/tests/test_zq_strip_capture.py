"""The ZQ strip: capture the whole listing, price only the near end of it.

Two separate things are pinned here, and keeping them separate IS the point.

CAPTURE (ingest._ZQ_STRIP_MONTHS). A ZQ contract is listed ~4.5 years before expiry and
carries daily bars for all of it; yfinance 404s it the instant it expires. Measured
2026-08-27 on the live feed: ZQJ27 1087 bars back to 2022-04, ZQZ27 917 back to 2022-12,
while ZQH24 / ZQZ23 / ZQF25 / ZQM26 return nothing at all. The ingest asked for the
current month through +7 only, so the rest was never banked and is now unrecoverable --
which is why model/fed.py's WEIGHT-0.50 ZQ source fired on 1 of 40 settled KXFED events,
36 of them failing with "no ZQ bar for the meeting's own month". Every month of delay
deletes another ~1000 bars, so the request range must stay wide.

REACH (fed._FF_MAX_MONTHS). Widening the request must NOT widen what the model prices.
Far contracts are nearly untraded (ZQU27 207 lots/day, ZQZ27 13), and long-horizon
KXFEDDECISION is the exact surface §27.1 lost money on. Before the widening the reach
was never a decision at all -- it was whatever the strip happened to hold. Pinning the
two numbers apart is what stops a future "just use all the data we have" from quietly
rebuilding that loss.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from prediction_market_macro.ingest import market_data as md
from prediction_market_macro.model import fed as m_fed


def test_strip_request_covers_the_whole_listing_not_just_the_near_end():
    got = md._zq_contracts(date(2026, 8, 27))
    assert md._ZQ_STRIP_MONTHS >= 16, (
        "the live listing reached ZQZ27 (+16) on 2026-08-27; a range shorter than the"
        " listing silently abandons contracts that expiry then deletes for good")
    # the four contracts that were listed-but-unbanked on the day this was found
    for root in ("ZQJ27", "ZQM27", "ZQU27", "ZQZ27"):
        assert root in got, f"{root} was listed with ~1000 bars and never requested"
    assert got["ZQJ27"] == "ZQJ27.CBT"
    assert len(got) == md._ZQ_STRIP_MONTHS + 1


def test_strip_rolls_across_a_year_boundary():
    got = md._zq_contracts(date(2026, 12, 3))
    assert "ZQZ26" in got and "ZQF27" in got and "ZQH27" in got


def test_reach_is_capped_far_below_what_is_captured():
    """The capture/reach split is the whole safety property, so assert the inequality
    rather than either number alone."""
    assert m_fed._FF_MAX_MONTHS < md._ZQ_STRIP_MONTHS
    assert m_fed._FF_MAX_MONTHS == 7, (
        "7 is the reach the old +7 strip actually gave; changing it is a model change"
        " that needs its own preregistration, not an edit")


def test_meetings_past_the_cap_are_not_priced_off_zq(monkeypatch):
    """The freeze must hold even when the bars ARE present -- that is the whole scenario
    the widening creates. Hand _ff_path a store that answers every root, and check it
    still refuses a meeting past the cap while accepting one inside it."""
    from prediction_market_macro.ingest.calendars import CALENDARS
    import pandas as pd

    asof = datetime(2026, 8, 27, tzinfo=timezone.utc)

    class _FS:
        def fred_scalar_latest(self, sid, _asof):
            return 4.0 if sid == "DFEDTARU" else None

        def fut_closes(self, _root, _asof, n=10):        # every contract quotes
            return pd.Series([95.99] * n), asof.isoformat()

    fs = _FS()
    upcoming = [e.scheduled_ts for e in CALENDARS["FOMC"] if e.scheduled_ts > asof]
    inside = [m for m in upcoming
              if (m.year - asof.year) * 12 + m.month - asof.month <= m_fed._FF_MAX_MONTHS]
    beyond = [m for m in upcoming
              if (m.year - asof.year) * 12 + m.month - asof.month > m_fed._FF_MAX_MONTHS]
    assert inside and beyond, "fixture needs meetings on both sides of the cap"

    for mt in beyond:
        pre, mv, _h = m_fed._ff_path(fs, asof, mt)
        assert (pre, mv) == (None, None), f"{mt.date()} is past the cap and was priced"
    # and the cap is not simply refusing everything
    assert any(m_fed._ff_path(fs, asof, mt)[1] is not None for mt in inside)


def test_expired_contracts_are_simply_absent_not_short():
    """The premise that justified excluding ZQ from the backfill lane was that 'max' on a
    dead contract yields 'a handful of bars'. It yields none -- the symbol 404s. This is
    pinned because the false version of it is what made the data loss look acceptable.

    Asserted against the STORE rather than the network: an expired root either has the
    bars we banked before expiry, or it has nothing. What it never has is a short tail
    that a later backfill could top up.
    """
    from prediction_market_macro.config.settings import load_settings
    from prediction_market_macro.ingest.store import init_db
    import sqlite3
    try:
        conn = init_db(load_settings(require_keys=False).db_path)
    except Exception:                                              # noqa: BLE001
        import pytest
        pytest.skip("no live db")
    rows = conn.execute(
        "SELECT root, COUNT(*) n FROM fut_daily WHERE root LIKE 'ZQ%' GROUP BY root"
    ).fetchall()
    if not rows:
        import pytest
        pytest.skip("no ZQ rows yet")
    for r in rows:
        assert r["n"] > 100, (
            f"{r['root']} has {r['n']} bars — a stub like this means a contract was"
            " requested only after it had already started expiring; the strip must be"
            " pulled at period='max' while it is still listed")
