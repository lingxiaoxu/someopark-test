"""ops/pnl._realized_print must agree with the ladder that actually paid out.

The settled ladder is ground truth: it is the only record of what Kalshi decided the
print WAS. Until 2026-08-27 `_realized_print` disagreed with it on five of fourteen
series, and nothing caught it, because every consumer trusted the label instead of
checking it:

  strategy/snipe.py:136   buys the "certain" side of a ladder off this number. The worst
                          case found was KXPAYROLLS 2026-01, label -899,000 against a
                          ladder that settled above +125,000 — a maximally confident
                          order on the wrong side.
  ops/pnl.py:201          writes the z-attribution note on every settlement from it.
  research/health.py:184  fuses the global breaker on it (_FUSE_SERIES, which happens to
                          contain only label-clean series, which is why it never fired).

The bugs were all "the label is a plausible number from the wrong source": PAYEMS
differenced across two independent first prints (so every revision landed in it), the
DAILY DFEDTARU keyed by month returning the PRE-meeting rate, EIA Cushing SPOT standing
in for the NYMEX front-month settle, the EIA weekly pump average standing in for the AAA
daily national average, and CPI YoY computed on the seasonally-adjusted index.

Two layers here:
  1. Hermetic branch tests, on a tmp db, that pin the exact discrimination each fix
     makes. These fail on any machine if the branch is reverted.
  2. A live-db agreement test that re-measures every series against its ladders and
     holds it to the rate measured on 2026-08-27. This is the layer that catches a
     regression introduced upstream — a re-ingest under a different sid, a vintage
     policy change — which the hermetic tests cannot see. It skips when the db has no
     settled ladders for a series rather than passing vacuously.

Rates are floors, not targets. Where a rate is below 100% the residual is a KNOWN,
named gap, recorded next to the number; a rate that RISES is a fix and should be
promoted here, a rate that falls is a regression.
"""
from __future__ import annotations

import sqlite3

import pytest

from prediction_market_macro.config.registry import REGISTRY
from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ops.pnl import _realized_print
from prediction_market_macro.util.periods import kalshi_period_to_key

INF = float("inf")


# ── layer 1: hermetic branch tests ──────────────────────────────────────────
@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.db")


def _fred(conn, sid, event_time, value, vintage):
    kt = f"{vintage}T13:30:00+00:00"
    conn.execute("INSERT OR REPLACE INTO fred_obs VALUES(?,?,?,?,?,?)",
                 (sid, event_time, value, vintage, kt, kt))
    conn.commit()


def _fut(conn, root, day, close):
    kt = f"{day}T20:30:00+00:00"
    conn.execute("INSERT OR REPLACE INTO fut_daily VALUES(?,?,?,?,?,?,?,?,?)",
                 (root, day, close, close, close, close, 1000.0, kt, kt))
    conn.commit()


def test_payrolls_differences_within_one_vintage(conn):
    """The 2026-01 failure, reconstructed. PAYEMS is a LEVEL; the contract is the
    CHANGE. Differencing the Jan print against the Dec FIRST print puts December's
    revision (and January's annual benchmark revision) into the label."""
    # December: first printed at 160000k, revised UP to 160900k in the January vintage.
    _fred(conn, "PAYEMS", "2025-12-01", 160_000.0, "2026-01-09")
    _fred(conn, "PAYEMS", "2025-12-01", 160_900.0, "2026-02-06")
    _fred(conn, "PAYEMS", "2026-01-01", 160_150.0, "2026-02-06")

    # Both legs come from January's own vintage (2026-02-06): 160150 - 160900.
    got = _realized_print(conn, "KXPAYROLLS", "2026-01")
    assert got == pytest.approx(-750_000.0)
    # Mixing vintages (Jan's print against December's FIRST print) gives +150,000 —
    # opposite sign, and the sign is what decides which side snipe.py calls certain.
    assert got != pytest.approx(150_000.0)


def test_payrolls_returns_none_rather_than_mixing_vintages(conn):
    """No same-vintage prior month => no honest label. The old code fell back to two
    independent first prints; that fallback is what produced -899,000."""
    _fred(conn, "PAYEMS", "2026-01-01", 160_150.0, "2026-02-06")
    assert _realized_print(conn, "KXPAYROLLS", "2026-01") is None


def test_fed_takes_the_month_end_daily_not_the_first(conn):
    """DFEDTARU is DAILY. Keyed by month, the first row is the PRE-meeting rate: six of
    28 FOMC periods came out exactly one 25bp step high."""
    _fred(conn, "DFEDTARU", "2025-09-01", 4.50, "2025-09-02")   # pre-meeting
    _fred(conn, "DFEDTARU", "2025-09-18", 4.25, "2025-09-19")   # post-meeting
    _fred(conn, "DFEDTARU", "2025-09-30", 4.25, "2025-10-01")
    assert _realized_print(conn, "KXFED", "2025-09") == pytest.approx(4.25)


def test_wti_reads_the_futures_settle_not_the_spot(conn):
    """KXWTIW settles on the NYMEX front-month CL settle. DCOILWTICO is the EIA Cushing
    SPOT series and sat outside the ladder on 61 of 141 periods."""
    _fut(conn, "CL", "2026-05-30", 89.31)
    _fred(conn, "DCOILWTICO", "2026-05-30", 91.74, "2026-06-02")
    assert _realized_print(conn, "KXWTIW", "2026-05-30") == pytest.approx(89.31)


def test_wti_without_a_futures_bar_has_no_label(conn):
    """A holiday/missing bar must NOT silently fall through to the spot series."""
    _fred(conn, "DCOILWTICO", "2026-05-30", 91.74, "2026-06-02")
    assert _realized_print(conn, "KXWTIW", "2026-05-30") is None


def test_aaa_uses_the_daily_average(conn):
    _fred(conn, "AAA_DAILY", "2026-08-07", 3.152, "2026-08-07")
    _fred(conn, "GASREGW", "2026-08-03", 3.089, "2026-08-04")
    assert _realized_print(conn, "KXAAAGASW", "2026-08-07") == pytest.approx(3.152)


def test_aaa_before_the_scrape_starts_has_no_label(conn):
    """AAA_DAILY only exists from 2026-07-31 and cannot be backfilled. Older periods get
    None, not the weekly GASREGW proxy — that proxy sat a mean 3.1 grid steps LOW and
    disagreed with the ladder on 27 of 73 periods."""
    _fred(conn, "GASREGW", "2026-03-02", 3.089, "2026-03-03")
    assert _realized_print(conn, "KXAAAGASW", "2026-03-06") is None


def test_cpi_yoy_uses_one_vintage_for_both_legs(conn):
    _fred(conn, "CPIAUCSL", "2025-07-01", 320.000, "2025-08-12")
    _fred(conn, "CPIAUCSL", "2025-07-01", 320.400, "2026-08-12")   # revised
    _fred(conn, "CPIAUCSL", "2026-07-01", 330.000, "2026-08-12")
    got = _realized_print(conn, "KXCPIYOY", "2026-07")
    assert got == pytest.approx(round((330.0 / 320.4 - 1) * 100, 4))
    assert got != pytest.approx(round((330.0 / 320.0 - 1) * 100, 4))


# ── layer 2: live-db ladder agreement ───────────────────────────────────────
def ladder_interval(legs):
    """(lo, hi] implied by the settled legs, with strike_type as the authority.

    cap_strike is NOT a "this leg is a bucket" flag — KXCPICORE's 123 'greater' rungs
    carry cap_strike == floor_strike, and reading those as buckets yields the degenerate
    interval (0.1, 0.1], which manufactures a 15% miss rate out of nothing. Only
    'between' is a genuine two-sided bucket.
    """
    for l in legs:
        if (l["strike_type"] == "between" and l["result"] == "yes"
                and l["floor_strike"] is not None and l["cap_strike"] is not None):
            return float(l["floor_strike"]), float(l["cap_strike"])
    yes, no = [], []
    for l in legs:
        st = l["strike_type"] or "greater"
        if st in ("greater", "greater_or_equal") and l["floor_strike"] is not None:
            (yes if l["result"] == "yes" else no).append(float(l["floor_strike"]))
        elif st in ("less", "less_or_equal") and l["cap_strike"] is not None:
            (no if l["result"] == "yes" else yes).append(float(l["cap_strike"]))
    if not yes and not no:
        return None
    lo = max(yes) if yes else -INF
    hi = min(no) if no else INF
    return (lo, hi) if hi > lo else None


# series -> (minimum agreement rate, why it is not 1.0)
AGREEMENT_FLOOR = {
    "KXCPI": (1.00, ""),
    "KXCPICORE": (1.00, ""),
    "KXFED": (1.00, ""),
    "KXJOBLESSCLAIMS": (1.00, ""),
    "KXPAYROLLS": (1.00, ""),
    "KXU3": (1.00, ""),
    "KXAAAGASW": (1.00, ""),      # only 4 periods exist; every one must be right
    "KXWTIW": (0.95, "CL front-month roll near expiry; 139/143 on 2026-08-27"),
    "KXPCECORE": (0.90, "one boundary period; 21/22 on 2026-08-27"),
    "KXCPICOREYOY": (0.90, "published YoY is NSA, CPILFESL is SA; 40/43 on 2026-08-27"),
    "KXCPIYOY": (0.85, "published YoY is NSA, CPIAUCSL is SA; 36/41 on 2026-08-27"),
}
MIN_EVENTS = 4


@pytest.fixture(scope="module")
def live():
    from prediction_market_macro.config.settings import load_settings
    s = load_settings(require_keys=False)
    if not s.db_path.exists():
        pytest.skip("no live db")
    c = sqlite3.connect(f"file:{s.db_path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


@pytest.mark.parametrize("series", sorted(AGREEMENT_FLOOR))
def test_label_agrees_with_the_settled_ladder(live, series):
    floor, why = AGREEMENT_FLOOR[series]
    step = float(REGISTRY[series].round_rule)
    periods = [r["period"] for r in live.execute(
        "SELECT DISTINCT period FROM settlements WHERE series=? AND result IN"
        " ('yes','no')", (series,)).fetchall()]

    n = agree = 0
    misses = []
    for p in periods:
        key = kalshi_period_to_key(p)
        if not key:
            continue
        legs = live.execute(
            "SELECT c.floor_strike, c.cap_strike, c.strike_type, s.result FROM contracts c"
            " JOIN settlements s ON s.ticker=c.ticker WHERE c.series=? AND s.period=?"
            " AND s.result IN ('yes','no')", (series, p)).fetchall()
        iv = ladder_interval(legs)
        if iv is None:
            continue
        y = _realized_print(live, series, key)
        if y is None:                      # no honest label — not a disagreement
            continue
        lo, hi = iv
        n += 1
        if lo - step / 2 - 1e-9 <= y <= hi + step / 2 + 1e-9:
            agree += 1
        else:
            off = (y - lo) / step if y < lo else (y - hi) / step
            misses.append(f"{key}: print={y} ladder=({lo}, {hi}] off={off:+.2f} steps")

    if n < MIN_EVENTS:
        pytest.skip(f"{series}: only {n} labelled+settled periods in this db")
    rate = agree / n
    assert rate >= floor, (
        f"{series}: {agree}/{n} = {rate:.1%} agree with the settled ladder, floor "
        f"{floor:.0%}{' (' + why + ')' if why else ''}. A label that disagrees with the "
        f"ladder is what snipe.py calls a certainty.\n  " + "\n  ".join(misses[:6]))
