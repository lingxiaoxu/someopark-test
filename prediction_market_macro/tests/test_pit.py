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
