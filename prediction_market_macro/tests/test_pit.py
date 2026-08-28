"""tests/test_pit.py — the PIT safety matrix (PLAN §5-bis.4): four test families across
every P0 model. tmp db only; production data never touched.

  1. canary            inject FUTURE-dated vintages → predict(asof) bit-identical
  2. release-morning   1 min before a print's knowledge_time the model must NOT see it;
                       1 min after it must (event_time alone never grants visibility)
  3. label-first-vintage  y_first = MIN(knowledge_time) row; later revisions never
                       change the label the replay scores against
  4. monotonicity      every ladder pmf yields a monotone non-increasing survival curve
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.model import claims as m_claims
from prediction_market_macro.model import cpi as m_cpi
from prediction_market_macro.model import energy as m_energy
from prediction_market_macro.model import fed as m_fed
from prediction_market_macro.model import payrolls as m_pay
from prediction_market_macro.model import pce as m_pce
from prediction_market_macro.model import u3 as m_u3
from prediction_market_macro.model.common import grid_pmf, survival


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.db")


# ── fixtures: synthetic vintage histories ────────────────────────────────────

def _ins(conn, sid, ev, val, kt):
    conn.execute("INSERT OR IGNORE INTO fred_obs VALUES(?,?,?,?,?,?)",
                 (sid, ev, val, kt.date().isoformat(), kt.isoformat(), kt.isoformat()))


def _seed_monthly(conn, sid, n=130, base=100.0, mom=0.0025, noise=0.0012,
                  kt_lag_days=12, seed=5, revise=True):
    """Monthly index vintages: first print at month+lag, small revision +30d."""
    rng = np.random.default_rng(seed)
    t0 = datetime(2015, 1, 1, tzinfo=timezone.utc)
    v = base
    last_kt = None
    for i in range(n):
        ev = (t0 + timedelta(days=31 * i)).replace(day=1)
        ev = datetime(t0.year + (t0.month - 1 + i) // 12,
                      (t0.month - 1 + i) % 12 + 1, 1, tzinfo=timezone.utc)
        v = v * (1 + mom + rng.normal(0, noise))
        kt = (ev + timedelta(days=31 + kt_lag_days)).replace(hour=12, minute=30)
        _ins(conn, sid, ev.date().isoformat(), round(v, 3), kt)
        if revise:
            _ins(conn, sid, ev.date().isoformat(), round(v * 1.0004, 3),
                 kt + timedelta(days=30))
        last_kt = kt
    conn.commit()
    return last_kt


def _seed_weekly_claims(conn, n=400, base=220_000):
    rng = np.random.default_rng(7)
    t0 = datetime(2018, 1, 4, tzinfo=timezone.utc)
    v, last_kt = base, None
    for i in range(n):
        wk = t0 + timedelta(weeks=i)
        v = max(150_000, v * (1 + rng.normal(0, 0.02)))
        kt = (wk + timedelta(days=6)).replace(hour=12, minute=30)
        _ins(conn, "ICSA", wk.date().isoformat(), round(v / 250) * 250, kt)
        _ins(conn, "ICSA", wk.date().isoformat(), round(v / 250) * 250 + 1000,
             kt + timedelta(days=7))
        last_kt = kt
    conn.commit()
    return last_kt


def _seed_monthly_level(conn, sid, *, n=130, base, step, noise, seed):
    rng = np.random.default_rng(seed)
    v, last_kt = base, None
    for i in range(n):
        ev = datetime(2015 + i // 12, i % 12 + 1, 1, tzinfo=timezone.utc)
        v = v + step + rng.normal(0, noise)
        kt = (ev + timedelta(days=35)).replace(hour=12, minute=30)
        _ins(conn, sid, ev.date().isoformat(), round(v, 1), kt)
        _ins(conn, sid, ev.date().isoformat(), round(v, 1), kt + timedelta(days=28))
        last_kt = kt
    conn.commit()
    return last_kt


def _seed_fut(conn, root, n=90, s0=70.0, seed=3):
    rng = np.random.default_rng(seed)
    t0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
    v, last_kt = s0, None
    for i in range(n):
        d = t0 + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        v = max(5.0, v * (1 + rng.normal(0, 0.015)))
        kt = d.replace(hour=21)
        conn.execute("INSERT OR IGNORE INTO fut_daily VALUES(?,?,?,?,?,?,?,?,?)",
                     (root, d.date().isoformat(), v, v, v, round(v, 4), 1000.0,
                      kt.isoformat(), kt.isoformat()))
        last_kt = kt
    conn.commit()
    return last_kt


# ── model matrix: (name, seeder → (asof, period, series, fn, sid_to_poison)) ─

def _setup_claims(conn):
    kt = _seed_weekly_claims(conn)
    asof = kt + timedelta(days=1)
    period = (kt + timedelta(days=6)).date().isoformat()
    return asof, period, "KXJOBLESSCLAIMS", m_claims.predict, "ICSA"


def _seed_gasregw(conn, end_year=2026):
    rng = np.random.default_rng(11)
    t0 = datetime(2015, 1, 5, tzinfo=timezone.utc)
    v, kt = 3.2, None
    i = 0
    while True:
        wk = t0 + timedelta(weeks=i)
        if wk.year > end_year:
            break
        v = max(2.0, v + rng.normal(0, 0.03))
        kt = (wk + timedelta(days=1)).replace(hour=16)
        _ins(conn, "GASREGW", wk.date().isoformat(), round(v, 3), kt)
        i += 1
    conn.commit()
    return kt


def _setup_cpi(conn):
    kt = _seed_monthly(conn, "CPIAUCSL", seed=5)
    _seed_monthly(conn, "CPILFESL", seed=6)
    _seed_gasregw(conn, end_year=kt.year)
    asof = kt + timedelta(days=3)
    period = (kt + timedelta(days=40)).strftime("%Y-%m")
    return asof, period, "KXCPI", m_cpi.predict, "CPIAUCSL"


def _setup_pce(conn):
    _seed_monthly(conn, "CPILFESL", seed=6)
    kt = _seed_monthly(conn, "PCEPILFE", seed=8)
    asof = kt + timedelta(days=3)
    period = (kt + timedelta(days=40)).strftime("%Y-%m")
    return asof, period, "KXPCECORE", m_pce.predict, "PCEPILFE"


def _setup_u3(conn):
    kt = _seed_monthly_level(conn, "UNRATE", base=4.0, step=0.0, noise=0.08, seed=9)
    _seed_weekly_claims(conn)
    asof = kt + timedelta(days=3)
    period = (kt + timedelta(days=40)).strftime("%Y-%m")
    return asof, period, "KXU3", m_u3.predict, "UNRATE"


def _seed_payems(conn, n=130):
    """PAYEMS vintages: each first-print vintage also carries the PRIOR month's value
    (as real ALFRED snapshots do) so printed_changes can difference within-vintage."""
    rng = np.random.default_rng(10)
    vals, last_kt = [], None
    for i in range(n):
        ev = datetime(2015 + i // 12, i % 12 + 1, 1, tzinfo=timezone.utc)
        v = 150_000.0 + 150.0 * i + rng.normal(0, 60.0)
        vals.append((ev, round(v, 1)))
        kt = (ev + timedelta(days=35)).replace(hour=12, minute=30)
        _ins(conn, "PAYEMS", ev.date().isoformat(), round(v, 1), kt)
        if i > 0:
            pe, pv = vals[i - 1]
            _ins(conn, "PAYEMS", pe.date().isoformat(), round(pv + 5.0, 1), kt)
        last_kt = kt
    conn.commit()
    return last_kt


def _setup_payrolls(conn):
    kt = _seed_payems(conn)
    _seed_weekly_claims(conn)
    asof = kt + timedelta(days=3)
    period = (kt + timedelta(days=40)).strftime("%Y-%m")
    return asof, period, "KXPAYROLLS", m_pay.predict, "PAYEMS"


def _setup_energy_wti(conn):
    kt = _seed_fut(conn, "CL")
    asof = kt + timedelta(days=1)
    period = (kt + timedelta(days=5)).date().isoformat()
    return asof, period, "KXWTIW", m_energy.predict, None      # poisons fut_daily


def _setup_energy_aaa(conn):
    rng = np.random.default_rng(11)
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    v, kt = 3.2, None
    for i in range(120):
        wk = t0 + timedelta(weeks=i)
        v = max(2.0, v + rng.normal(0, 0.03))
        kt = (wk + timedelta(days=1)).replace(hour=16)
        _ins(conn, "GASREGW", wk.date().isoformat(), round(v, 3), kt)
    conn.commit()
    asof = kt + timedelta(days=1)
    period = (kt + timedelta(days=6)).date().isoformat()
    return asof, period, "KXAAAGASW", m_energy.predict, "GASREGW"


def _setup_fed(conn):
    _seed_monthly_level(conn, "UNRATE", base=4.0, step=0.0, noise=0.08, seed=9)
    _seed_monthly(conn, "CPILFESL", seed=6)
    kt = _seed_monthly_level(conn, "DFEDTARU", base=4.5, step=0.0, noise=0.0, seed=12)
    asof = kt + timedelta(days=3)
    period = (kt + timedelta(days=40)).date().isoformat()
    return asof, period, "KXFEDDECISION", m_fed.predict, "DFEDTARU"


SETUPS = [_setup_claims, _setup_cpi, _setup_pce, _setup_u3, _setup_payrolls,
          _setup_energy_wti, _setup_energy_aaa, _setup_fed]
IDS = ["claims", "cpi", "pce", "u3", "payrolls", "wti", "aaa_gas", "fed"]


def _dist_key(pred):
    return json.dumps(pred.dist.to_json(), sort_keys=True)


# ── 1. canary: future vintages must be invisible ─────────────────────────────

@pytest.mark.parametrize("setup", SETUPS, ids=IDS)
def test_pit_canary(conn, setup):
    asof, period, series, fn, sid = setup(conn)
    p1 = fn(conn, asof, period, series=series)
    fut = asof + timedelta(days=3)
    if sid is not None:
        _ins(conn, sid, (asof - timedelta(days=400)).date().isoformat(), 999_999.0, fut)
        _ins(conn, sid, (asof + timedelta(days=10)).date().isoformat(), 888_888.0, fut)
    else:                                            # futures-driven model
        conn.execute("INSERT OR IGNORE INTO fut_daily VALUES(?,?,?,?,?,?,?,?,?)",
                     ("CL", (asof + timedelta(days=1)).date().isoformat(),
                      999.0, 999.0, 999.0, 999.0, 1.0, fut.isoformat(), fut.isoformat()))
    conn.commit()
    p2 = fn(conn, asof, period, series=series)
    assert _dist_key(p1) == _dist_key(p2), f"{series}: future vintage leaked into predict"
    assert p1.data_horizon <= p1.asof


# ── 2. release-morning: knowledge_time is the visibility clock ───────────────

@pytest.mark.parametrize("setup", SETUPS, ids=IDS)
def test_pit_release_morning(conn, setup):
    asof, period, series, fn, sid = setup(conn)
    # newest print's knowledge_time:
    if sid is not None:
        kt = conn.execute("SELECT MAX(knowledge_time) m FROM fred_obs WHERE sid=?",
                          (sid,)).fetchone()["m"]
    else:
        kt = conn.execute("SELECT MAX(knowledge_time) m FROM fut_daily WHERE root='CL'"
                          ).fetchone()["m"]
    kt_dt = datetime.fromisoformat(kt)
    before = fn(conn, kt_dt - timedelta(minutes=1), period, series=series)
    after = fn(conn, kt_dt + timedelta(minutes=1), period, series=series)
    assert before.data_horizon < kt_dt, f"{series}: pre-release pred saw the print"
    assert after.data_horizon >= kt_dt, f"{series}: post-release pred missed the print"


# ── 3. label uses the FIRST vintage, revisions never rewrite it ──────────────

def test_label_first_vintage(conn):
    ev = "2026-01-08"
    first_kt = datetime(2026, 1, 15, 13, 30, tzinfo=timezone.utc)
    _ins(conn, "ICSA", ev, 210_000, first_kt)
    _ins(conn, "ICSA", ev, 999_000, first_kt + timedelta(days=7))    # big revision
    conn.commit()
    r = conn.execute(
        "SELECT event_time, value, MIN(knowledge_time) kt FROM fred_obs WHERE sid='ICSA'"
        " GROUP BY event_time").fetchone()
    assert r["value"] == 210_000, "label picked a revision, not the first print"
    assert r["kt"] == first_kt.isoformat()


# ── 4. every ladder is a valid pmf with monotone survival ────────────────────

@pytest.mark.parametrize("setup", SETUPS, ids=IDS)
def test_ladder_monotone(conn, setup):
    asof, period, series, fn, sid = setup(conn)
    from prediction_market_macro.config.registry import REGISTRY
    from prediction_market_macro.model.common import Categorical
    pred = fn(conn, asof, period, series=series)
    if isinstance(pred.dist, Categorical):
        tot = sum(pred.dist.probs.values())
        assert abs(tot - 1.0) < 1e-6
        assert all(0 <= v <= 1 for v in pred.dist.probs.values())
        return
    step = REGISTRY[series].round_rule if series in REGISTRY else 0.1
    pmf = grid_pmf(pred.dist, step)
    assert abs(sum(pmf.values()) - 1.0) < 1e-6
    ks = sorted(pmf)
    sv = [survival(pmf, k, strict=False) for k in ks]
    assert all(b <= a + 1e-9 for a, b in zip(sv[:-1], sv[1:])), \
        f"{series}: survival not monotone"


# ── 5. replay scoring: symmetric market filter + asof never crosses the print ─

def test_market_leg_prob_filter_is_symmetric(conn):
    """The empty-book test used to be `a < 1.0`, dropping every leg the market had
    priced as near-certain YES while keeping its NO mirror image — a filter keyed on
    price level, which correlates with the outcome, so it conditioned the scored
    universe on the answer. Only bid=0 AND ask=1 together means nobody is quoting."""
    from prediction_market_macro.research.backtest import _market_leg_prob
    asof = datetime(2026, 8, 1, tzinfo=timezone.utc)
    ts = int(asof.timestamp()) - 60
    for tk, b, a in (("HI", 0.99, 1.00),      # live two-sided book pinned near certainty
                     ("LO", 0.00, 0.01),      # its mirror image on the NO side
                     ("EMPTY", 0.00, 1.00)):  # genuinely no market
        conn.execute("INSERT INTO candles(ticker, end_ts, yes_bid_close, yes_ask_close)"
                     " VALUES(?,?,?,?)", (tk, ts, b, a))
    conn.commit()
    hi, lo = _market_leg_prob(conn, "HI", asof), _market_leg_prob(conn, "LO", asof)
    assert hi is not None and lo is not None, (hi, lo)
    # symmetry: the two mirror books must survive or die together, and mirror in price
    assert abs(hi - (1.0 - lo)) < 1e-9, (hi, lo)
    assert _market_leg_prob(conn, "EMPTY", asof) is None
    assert _market_leg_prob(conn, "NOSUCH", asof) is None


def test_market_leg_bar_accepts_exactly_what_market_leg_prob_accepts(conn):
    """#184b added `_market_leg_bar` so a thinness gate can read spread/volume/staleness
    from the SAME bar the price came from, with `_market_leg_prob` reduced to a wrapper.
    The one thing that must never drift is the acceptance rule: a leg that is scored must
    have metadata and a leg that is dropped must have none, or the gate would be
    conditioning on a different universe than the Brier it is trying to explain."""
    from prediction_market_macro.research.backtest import (_market_leg_bar,
                                                           _market_leg_prob)
    asof = datetime(2026, 8, 1, tzinfo=timezone.utc)
    ts = int(asof.timestamp()) - 120
    for tk, b, a, v in (("BHI", 0.99, 1.00, 7.0),
                        ("BLO", 0.00, 0.01, 0.0),
                        ("BEMPTY", 0.00, 1.00, 0.0),
                        ("BNULL", None, 0.50, 3.0)):
        conn.execute("INSERT INTO candles(ticker, end_ts, yes_bid_close, yes_ask_close,"
                     " volume) VALUES(?,?,?,?,?)", (tk, ts, b, a, v))
    conn.commit()
    for tk in ("BHI", "BLO", "BEMPTY", "BNULL", "BNOSUCH"):
        bar, mp = _market_leg_bar(conn, tk, asof), _market_leg_prob(conn, tk, asof)
        assert (bar is None) == (mp is None), tk
        if bar is not None:
            assert bar["mid"] == mp, tk
    hi = _market_leg_bar(conn, "BHI", asof)
    assert hi["spread"] == pytest.approx(0.01)
    assert hi["volume"] == 7.0
    # staleness is asof MINUS the bar's end, so it is >= 0 for a PIT-legal bar and is the
    # age of the quote, not its timestamp. A sign slip here would invert the gate.
    assert hi["staleness_s"] == 120.0


def test_settle_release_ts_exact_mapping_never_matches_neighbour_day(conn):
    """DCOILWTICO/GASREGW publish DAILY, so a nearest-match window returns the previous
    day's print and manufactures a leak. The mapping must be exact-or-None."""
    from prediction_market_macro.config.registry import REGISTRY
    from prediction_market_macro.research.backtest import _settle_release_ts
    # monthly: '2026-01' -> event_time '2026-01-01'
    kt = datetime(2026, 2, 13, 13, 30, tzinfo=timezone.utc)
    _ins(conn, "CPIAUCSL", "2026-01-01", 300.0, kt)
    assert _settle_release_ts(conn, REGISTRY["KXCPI"], "2026-01") == kt
    # weekly energy: exact day only — the day BEFORE must never be borrowed
    wk = datetime(2026, 4, 2, 22, 0, tzinfo=timezone.utc)
    _ins(conn, "DCOILWTICO", "2026-04-02", 70.0, wk)
    assert _settle_release_ts(conn, REGISTRY["KXWTIW"], "2026-04-03") is None
    _ins(conn, "DCOILWTICO", "2026-04-03", 71.0, wk + timedelta(days=1))
    assert _settle_release_ts(conn, REGISTRY["KXWTIW"], "2026-04-03") == wk + timedelta(days=1)
    # claims: the Thursday release reports the week ending the prior Saturday (key-5d)
    ck = datetime(2026, 7, 30, 12, 30, tzinfo=timezone.utc)
    _ins(conn, "ICSA", "2026-07-25", 220000.0, ck)
    assert _settle_release_ts(conn, REGISTRY["KXJOBLESSCLAIMS"], "2026-07-30") == ck
    # unmappable cadence (FOMC per_event) and unknown period fail OPEN, never guess
    assert _settle_release_ts(conn, REGISTRY["KXFED"], "2026-09") is None
    assert _settle_release_ts(conn, REGISTRY["KXCPI"], "1999-01") is None
    assert _settle_release_ts(conn, REGISTRY["KXCPI"], None) is None


def test_settle_release_ts_uses_first_vintage_not_revision(conn):
    """MIN(knowledge_time): a later revision must not move the release anchor."""
    from prediction_market_macro.config.registry import REGISTRY
    from prediction_market_macro.research.backtest import _settle_release_ts
    first = datetime(2026, 3, 11, 12, 30, tzinfo=timezone.utc)
    _ins(conn, "CPIAUCSL", "2026-02-01", 300.0, first)
    _ins(conn, "CPIAUCSL", "2026-02-01", 300.4, first + timedelta(days=31))
    assert _settle_release_ts(conn, REGISTRY["KXCPI"], "2026-02") == first


def test_candle_404_sentinels_never_become_prices_or_coverage(conn):
    """ingest/kalshi_md.py writes an end_ts=0, all-NULL row when Kalshi 404s a ticker's
    candlesticks, so backfill stops retrying a leg that will never have bars. 6700 of
    14683 stored rows are that sentinel. Two things must hold, and only one of them did.

    The price readers were already safe (they reject NULL bid/ask), but coverage() joined
    `candles` unconditionally and so counted a sentinel-only leg as testable: 61 claimed
    periods for KXCPI against 2 with actual prices.
    """
    from prediction_market_macro.research.backtest import _market_leg_prob
    from prediction_market_macro.research.walkforward import coverage
    asof = datetime(2026, 8, 1, tzinfo=timezone.utc)
    conn.execute("INSERT INTO candles(ticker, end_ts, yes_bid_close, yes_ask_close,"
                 " price_close, volume) VALUES('DEAD',0,NULL,NULL,NULL,NULL)")
    conn.commit()
    assert _market_leg_prob(conn, "DEAD", asof) is None

    settled = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    conn.execute("INSERT INTO contracts(ticker, event_ticker, series, period,"
                 " floor_strike, strike_type, close_time, first_seen_ts)"
                 " VALUES('DEAD','KXCPI-26JUL','KXCPI','2026-07',0.0,'greater',?,?)",
                 (settled, settled))
    conn.execute("INSERT INTO settlements(ticker, series, period, result, settled_ts,"
                 " first_seen_ts) VALUES('DEAD','KXCPI','2026-07','yes',?,?)",
                 (settled, settled))
    conn.commit()
    assert coverage(conn, days=30)["KXCPI"]["events_in_window"] == 0

    # ...and a real bar on the same ticker restores it, so the guard is not just "off"
    conn.execute("INSERT INTO candles(ticker, end_ts, yes_bid_close, yes_ask_close)"
                 " VALUES('DEAD',?,0.40,0.44)", (int(asof.timestamp()) - 60,))
    conn.commit()
    assert _market_leg_prob(conn, "DEAD", asof) == pytest.approx(0.42)
    assert coverage(conn, days=30)["KXCPI"]["events_in_window"] == 1
