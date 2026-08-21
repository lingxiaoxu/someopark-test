"""payrolls/0.2.0 — every declared key reaches the arithmetic, and the two findings that
motivated 0.2.0 stay pinned.

The first half is the rule test_claims_params.py established: one perturbation per key in
DEFAULT_PARAMS, asserting it MOVES the output. A key that cannot move the output does not
belong in DEFAULT_PARAMS, because the grid spends its width on it regardless.

The second half exists because the two facts behind 0.2.0 point in OPPOSITE directions and
each is easy to "fix" back into a bug:

  * sigma must NOT depend on `period`. Scored on 2010-2026 ex-2020 (1080 pairs, 185
    anchors), the robust sd of the error at h=1..6 months ahead is 1.00/0.96/1.00/0.90/
    0.95/0.93 relative to h=1. cpi.py widens with the horizon and it is right to; copying
    that here would decalibrate a calibrated model. The next reader WILL be tempted, since
    "far contracts are more uncertain" sounds obviously true. It isn't, for NFP at h<=6.
  * sigma must depend on the recent data. 0.1.0 pinned 55k/140k; the same sample says the
    live width sat outside the bootstrap CI of the constant that best fits it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.model import payrolls
from prediction_market_macro.tests.test_pit import _ins, _seed_payems, _seed_weekly_claims


@pytest.fixture()
def seeded(tmp_path):
    conn = init_db(tmp_path / "p.db")
    kt = _seed_payems(conn)
    _seed_weekly_claims(conn)
    asof = kt + timedelta(days=3)
    return conn, asof, (kt + timedelta(days=40)).strftime("%Y-%m")


def _pred(seeded, period=None, **over):
    conn, asof, per = seeded
    return payrolls.predict(conn, asof, period or per, params=over or None)


# perturbation per key -> the inputs field it must move (None = must move the dist)
PERTURB = {
    "base_months": (6, "base_3m"),
    "w_base": (0.1, "mu"),
    "jobs_per_claim": (8.0, "claims_signal"),
    "claims_clip": (1_000, "claims_signal"),
    "sigma_window": (8, "sigma_core"),
    "sigma_mult": (3.0, "sigma_core"),
    "tail_mult": (8.0, "sigma_tail"),
    "sigma_floor": (900_000.0, "sigma_core"),
    "w_tail": (0.6, None),
}


def test_every_declared_param_is_actually_read(seeded):
    assert set(PERTURB) == set(payrolls.DEFAULT_PARAMS), \
        "a param was added or removed without a perturbation case"
    base = _pred(seeded)
    for key, (value, field) in PERTURB.items():
        got = _pred(seeded, **{key: value})
        if field is None:
            assert got.dist.comps != base.dist.comps, f"{key}={value!r} is dead"
        else:
            assert got.inputs[field] != base.inputs[field], \
                f"{key}={value!r} did not move inputs[{field!r}] — the param is dead"


def test_the_claims_leg_is_live_before_the_clip_test_means_anything(seeded):
    """claims_clip=1000 only proves something if the unclipped signal was further off."""
    base = _pred(seeded)
    assert base.inputs["claims_signal"] != base.inputs["base_3m"]


def test_defaults_are_the_registered_behaviour(seeded):
    """params=None, {} and DEFAULT_PARAMS are the same model — the registration and the
    health replay canary both depend on this path not drifting."""
    conn, asof, per = seeded
    a = payrolls.predict(conn, asof, per)
    b = payrolls.predict(conn, asof, per, params={})
    c = payrolls.predict(conn, asof, per, params=dict(payrolls.DEFAULT_PARAMS))
    assert a.inputs == b.inputs == c.inputs
    assert a.dist.comps == b.dist.comps == c.dist.comps


# ── the 0.2.0 findings, pinned ───────────────────────────────────────────────

def test_sigma_is_flat_across_the_horizon_on_purpose(seeded):
    """Measured, not assumed: robust sd ratios 1.00/0.96/1.00/0.90/0.95/0.93 over h=1..6.
    If you are here because you added a horizon term and this failed — re-run the
    measurement first (the study is reproducible from printed_changes + first prints)."""
    conn, asof, per = seeded
    months = [(pd.Period(per, freq="M") + k).strftime("%Y-%m") for k in range(4)]
    preds = [payrolls.predict(conn, asof, m) for m in months]
    assert len({p.dist.comps for p in preds}) == 1, \
        "payrolls sigma must not vary with the contract month — see the docstring"
    assert [p.period for p in preds] == months, "the period must still be carried through"


def _seed_payems_noisy(conn, noise):
    """_seed_payems with a controllable monthly wobble (it hardcodes sigma=60), so two
    otherwise identical histories can differ ONLY in dispersion."""
    rng = np.random.default_rng(10)
    vals, last_kt = [], None
    for i in range(130):
        ev = datetime(2015 + i // 12, i % 12 + 1, 1, tzinfo=timezone.utc)
        v = 150_000.0 + 150.0 * i + rng.normal(0, noise)
        vals.append((ev, round(v, 1)))
        kt = (ev + timedelta(days=35)).replace(hour=12, minute=30)
        _ins(conn, "PAYEMS", ev.date().isoformat(), round(v, 1), kt)
        if i > 0:
            pe, pv = vals[i - 1]
            _ins(conn, "PAYEMS", pe.date().isoformat(), round(pv + 5.0, 1), kt)
        last_kt = kt
    conn.commit()
    return last_kt


def test_sigma_tracks_the_data_rather_than_a_constant(tmp_path):
    """The 0.1.0 defect itself. Under a constant sigma both of these dbs get 55k."""
    got = {}
    for tag, noise in (("calm", 20.0), ("wild", 400.0)):
        db = init_db(tmp_path / f"{tag}.db")
        kt = _seed_payems_noisy(db, noise)
        _seed_weekly_claims(db)
        got[tag] = payrolls.predict(db, kt + timedelta(days=3),
                                    "2026-01").inputs["sigma_core"]
    assert got["wild"] > 3 * got["calm"], \
        f"core sigma did not follow the dispersion: {got}"


def test_retired_absolute_widths_are_accepted_recorded_and_can_only_widen(seeded):
    """An adopted param set still pins sigma_core=45000 on KXPAYROLLS in production.
    Raising on it would take the series down every day until a human edited the adoption,
    so it is honoured as a FLOOR and stamped on every stored pred instead — never dropped
    in silence, which is the failure mode this suite exists to prevent."""
    base = _pred(seeded)
    lo = _pred(seeded, sigma_core=1.0, sigma_tail=2.0)
    assert lo.dist.comps == base.dist.comps, "a retired key made the model NARROWER"
    assert lo.inputs["sigma_core_retired"] == 1.0
    assert lo.inputs["sigma_tail_retired"] == 2.0
    hi = _pred(seeded, sigma_core=9_000_000.0)
    assert hi.inputs["sigma_core"] == 9_000_000.0, "the floor must still be reachable"
    assert "sigma_core_retired" not in base.inputs, "un-passed keys must not be stamped"
