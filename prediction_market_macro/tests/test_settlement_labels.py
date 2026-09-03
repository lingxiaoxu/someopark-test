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


# ── layer 3: a leg with no strike must produce no expectation, not a crash ───
# `_leg_expected` returning None is the answer both callers are written for — they skip
# the leg. Raising is a different outcome entirely: `_settle_label_check` is the 铁律 2
# GLOBAL breaker, so a TypeError there is not a caught mismatch, it is the 06:00 health
# run dying before it checks anything else. The shape is in the live db (604 settled legs
# with strike_type IS NULL and floor_strike IS NULL, from the pre-2025-02 backfill) and
# reachable in strategy/snipe.py, which back-fills `strike` from `cap_strike` for its own
# None-check and then passes the still-None `strike` in here under `greater*` semantics.
@pytest.mark.parametrize("strike_type,floor,cap", [
    ("greater", None, 4.3),            # snipe.py's exact shape: cap set, floor not
    ("greater_or_equal", None, None),
    (None, None, 4.3),                 # strike_type absent -> defaults to a greater*
    ("less", 4.3, None),
    ("less_or_equal", None, None),
    ("between", None, 4.3),
    ("between", 4.3, None),
])
@pytest.mark.parametrize("strict", [True, False])
def test_missing_bound_yields_no_expectation_rather_than_raising(strike_type, floor,
                                                                 cap, strict):
    from prediction_market_macro.research.health import _leg_expected
    assert _leg_expected(4.4, strike_type, floor, cap, strict) is None


@pytest.mark.parametrize("strike_type,floor,cap,y,want", [
    ("greater", 4.3, None, 4.4, "yes"),
    # 2026-09-02: within the greater-family the registry flag decides the tie; this
    # test passes default_strict=False, so a 'greater' contract on the line is YES
    # (KXAAAGASW-26AUG31-4.080 is the settlement that made the rule). The strict
    # reading is pinned separately in test_kxaaagasw_exact_tie_settles_yes_as_kalshi_did.
    ("greater", 4.3, None, 4.3, "yes"),
    ("greater_or_equal", 4.3, None, 4.3, "yes"),
    ("greater_or_equal", 4.3, None, 4.2, "no"),
    ("less", None, 4.3, 4.2, "yes"),
    ("less", None, 4.3, 4.3, "no"),
    ("less_or_equal", None, 4.3, 4.3, "yes"),
    ("less_or_equal", None, 4.3, 4.4, "no"),
    ("between", 4.0, 4.3, 4.3, "yes"),
    ("between", 4.0, 4.3, 4.4, "no"),
])
def test_the_guard_did_not_move_any_leg_that_already_had_an_answer(strike_type, floor,
                                                                  cap, y, want):
    """The other half of the guard: every bounded case, including both open/closed
    ends, must land exactly where it landed before. A None-guard that also flipped a
    boundary would be a settlement-label change wearing a robustness fix's clothes."""
    from prediction_market_macro.research.health import _leg_expected
    assert _leg_expected(y, strike_type, floor, cap, False) == want


def test_the_strike_type_default_still_follows_strict_gt():
    from prediction_market_macro.research.health import _leg_expected
    assert _leg_expected(4.3, None, 4.3, None, True) == "no"      # -> greater
    assert _leg_expected(4.3, None, 4.3, None, False) == "yes"    # -> greater_or_equal


# ── layer 4: a strike is only as precise as the encoding that carried it ─────
# KXU3-25FEB-T4.1 is stored as floor_strike = 4.099999. On a strict-greater ladder that
# ULP flips the exactly-at-strike case from NO to YES. Five KXU3 legs in the live db
# print exactly 4.1 against that strike and settled NO; before the guard, all five read
# as settle_label_mismatch — five false GLOBAL-breaker fires sitting in the history of a
# series that IS in _FUSE_SERIES, held off production only by being outside the window.
# Measured over the whole live book, the guard moves exactly those five legs and nothing
# else (6124 unchanged, 600 previously-crashing now None).
#
# The encoding is historical — KXU3's `.1` rungs are 4.099999 from 23JAN to 25JUN and
# clean 4.1 for all fourteen periods since — so these tests are pinning behaviour on the
# STORED book, which is what the breaker reads and what a re-ingest can move.
def test_a_sub_quantum_strike_offset_is_declined_not_decided():
    from prediction_market_macro.research.health import _leg_expected
    assert _leg_expected(4.1, "greater", 4.099999, None, True) is None
    assert _leg_expected(4.1, "greater_or_equal", 4.099999, None, False) is None
    assert _leg_expected(64.99, "less", None, 64.989999, True) is None
    assert _leg_expected(64.99, "between", 64.0, 64.989999, True) is None


def test_exact_equality_is_still_decided_because_that_is_what_strict_gt_is_for():
    """The band is `0 < |y-bound| <= eps`, open at zero. KXJOBLESSCLAIMS strikes sit on
    a 250 lattice with integer prints, so a print landing exactly on a strike is ordinary
    and `greater_or_equal` calls it YES correctly. A blanket 'near the line' guard would
    swallow it, retire strict_gt altogether, and cost the breaker real coverage on the
    one series it was built for."""
    from prediction_market_macro.research.health import _leg_expected
    assert _leg_expected(235000.0, "greater_or_equal", 235000.0, None, False) == "yes"
    assert _leg_expected(235000.0, "greater", 235000.0, None, True) == "no"


def test_a_deliberate_sub_lattice_offset_is_not_swallowed():
    """KXPAYROLLS writes '>= 100,000' as '> 99,999' — nine such strikes in the live db,
    each exactly 1.0 off the 1000 lattice. That is semantics, not transport, and it is
    six orders of magnitude from the 1e-6 the guard is for. If a future eps ever grows
    past 1.0 this test is the thing that stops it."""
    from prediction_market_macro.research.health import _STRIKE_EPS, _leg_expected
    assert _STRIKE_EPS < 1.0
    assert _leg_expected(100000.0, "greater", 99999.0, None, True) == "yes"
    assert _leg_expected(99999.0, "greater", 99999.0, None, True) == "no"


def test_a_genuine_rounding_disagreement_is_still_reported():
    """The guard must not become a way to make CPI/PCE look clean. Their misses are of
    order 1e-2 — the raw MoM sits above a strike whose published, rounded print does not
    — and those are exactly the disagreements that keep the family OUT of _FUSE_SERIES."""
    from prediction_market_macro.research.health import _leg_expected
    assert _leg_expected(0.2081, "greater", 0.2, None, True) == "yes"     # settled 'no'
    assert _leg_expected(0.2455, "greater", 0.2, None, True) == "yes"     # settled 'no'


# ── layer 5: the fused set, and the window it is read over (#216) ────────────
def test_the_fused_set_is_pinned():
    """_settle_label_check is a GLOBAL breaker. Its membership was widened from 2 to 5
    on measured 100.0% agreement (see the comment on _FUSE_SERIES); a series drifting in
    or out must be a deliberate edit that fails here, not a quiet import-time change."""
    from prediction_market_macro.research.health import _FUSE_SERIES
    assert set(_FUSE_SERIES) == {"KXJOBLESSCLAIMS", "KXU3", "KXPAYROLLS", "KXFED",
                                 "KXAAAGASW"}
    for excluded in ("KXCPI", "KXCPICORE", "KXCPIYOY", "KXCPICOREYOY", "KXPCECORE",
                     "KXWTIW", "KXNATGASW", "KXFEDDECISION", "KXGDP"):
        assert excluded not in _FUSE_SERIES


def test_the_window_is_per_series_so_a_daily_ladder_cannot_evict_a_monthly_one(tmp_path):
    """The reason the widening needed a window change at all. KXAAAGASW settles a ~34-leg
    ladder EVERY DAY; under one shared LIMIT it took 87 of 120 rows on the live db and
    pushed KXU3 to exactly zero — the breaker would have stopped checking a series it
    already guarded. Seeded here at 10x so the eviction is unambiguous."""
    from prediction_market_macro.research.health import _FUSE_PER_SERIES
    conn = init_db(tmp_path / "win.db")
    for i in range(_FUSE_PER_SERIES * 10):        # the loud daily series, newest first
        conn.execute("INSERT INTO settlements(ticker, series, period, result,"
                     " settled_ts, first_seen_ts) VALUES(?,?,?,?,?,?)",
                     (f"KXAAAGASW-X{i}", "KXAAAGASW", "26AUG27", "no",
                      f"2026-08-27T{i // 3600:02d}:00:00Z", "2026-08-27T00:00:00Z"))
    for i in range(5):                            # the quiet monthly one, older
        conn.execute("INSERT INTO settlements(ticker, series, period, result,"
                     " settled_ts, first_seen_ts) VALUES(?,?,?,?,?,?)",
                     (f"KXU3-Y{i}", "KXU3", "26JUL", "no",
                      "2026-07-03T12:25:00Z", "2026-07-03T00:00:00Z"))
    conn.commit()

    shared = [r[0] for r in conn.execute(
        "SELECT series FROM settlements WHERE series IN ('KXAAAGASW','KXU3')"
        " ORDER BY settled_ts DESC LIMIT ?", (_FUSE_PER_SERIES,))]
    assert shared.count("KXU3") == 0, "the shared window is what the fix is about"

    per = []
    for s in ("KXAAAGASW", "KXU3"):
        per += [r[0] for r in conn.execute(
            "SELECT series FROM settlements WHERE series=? ORDER BY settled_ts DESC"
            " LIMIT ?", (s, _FUSE_PER_SERIES))]
    assert per.count("KXU3") == 5, "per-series, the quiet series keeps its own budget"


def test_the_live_breaker_is_silent(live):
    """The end of #216, run against the real db through the real function rather than a
    reimplementation of it: the widened set over the per-series window must produce no
    flags. This is the test that would have caught the five false KXU3 fires, and it is
    the one that fires if a future re-ingest breaks a label."""
    from prediction_market_macro.research.health import _settle_label_check
    from datetime import datetime, timezone
    flags = _settle_label_check(live, datetime.now(timezone.utc))
    assert flags == [], "\n  ".join(flags)


def test_kxaaagasw_exact_tie_settles_yes_as_kalshi_did_on_26aug31():
    """2026-08-31: AAA printed exactly 4.080 and Kalshi settled 'Above 4.080' YES. The
    registry's strict_gt=True predicted NO, the health check called it a mismatch, and
    the GLOBAL breaker tripped two mornings in a row (three positions force-exited, 104
    opens blocked). This pins the observed rule so the flag cannot drift back to the
    build-time assumption."""
    from prediction_market_macro.config.registry import REGISTRY
    from prediction_market_macro.research.health import _leg_expected
    spec = REGISTRY["KXAAAGASW"]
    assert spec.strict_gt is False
    # the live check passes the contract's own nominal type ('greater'); the registry
    # flag must decide the tie, and it must say YES as the settlement did
    assert _leg_expected(4.08, "greater", 4.08, None, spec.strict_gt) == "yes"
    # a series still registered strict keeps calling the same tie NO
    assert _leg_expected(4.08, "greater", 4.08, None, True) == "no"


def test_a_breaker_whose_condition_is_gone_is_released_but_a_pnl_one_is_not():
    """2026-09-02: the tie bug was fixed at 12:57 and the desk stayed blocked anyway,
    because a tripped breaker holds 24h on the alerts row alone and nothing re-read the
    condition. Two mornings of blocked opens and three force-exits were bought by an
    already-fixed bug. This pins the release AND its limit: a PnL drawdown does not
    self-heal just because the next run recomputes it."""
    import sqlite3
    from datetime import datetime, timedelta, timezone
    from prediction_market_macro.ops import risk
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE alerts(ts TEXT, level TEXT, source TEXT, message TEXT,"
                 " acked INTEGER DEFAULT 0)")
    now = datetime(2026, 9, 2, 13, 0, tzinfo=timezone.utc)
    earlier = (now - timedelta(hours=4)).isoformat()
    for msg in ("*: settle_label_mismatch:KXAAAGASW-26AUG31-4.080:label=4.08",
                "*: health_red:pred_stale:40h",
                "*: rolling20 realized -12.3% over 20 closures"):
        conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                     (earlier, "error", "circuit_breaker", msg))
    # this run still reports the stale-prediction condition, but not the label one
    freed = risk.release_resolved(conn, {"health_red:pred_stale:40h"}, now)
    assert len(freed) == 1 and "settle_label_mismatch" in freed[0]
    assert risk.breaker_tripped(conn, "KXNATGASW") is not None      # pred_stale holds
    freed2 = risk.release_resolved(conn, set(), now)
    assert len(freed2) == 1 and "pred_stale" in freed2[0]
    left = risk.breaker_tripped(conn, "KXNATGASW")
    assert left is not None and "rolling20" in left, \
        "the PnL breaker must NEVER be auto-released"
    n_audit = conn.execute("SELECT COUNT(*) FROM alerts WHERE source='circuit_breaker'"
                           " AND level='info'").fetchone()[0]
    assert n_audit == 2, "each auto-release must leave its own audit row"
