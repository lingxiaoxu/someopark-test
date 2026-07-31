"""tests/test_ensemble_calibration.py — Step-3 units: calibration map, log-pool,
inverse-MSPE weight learning, Almon weights, bridge math. tmp/in-memory db only."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.model.bridge import almon_weights
from prediction_market_macro.model.ensemble import PRIOR, learn_weights, log_pool
from prediction_market_macro.strategy import calibration as cal


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.db")


# ── calibration ──────────────────────────────────────────────────────────────

def test_fit_map_too_thin_returns_none():
    assert cal.fit_map([(0.5, 1.0)] * 100) is None


def test_isotonic_corrects_overconfidence(conn):
    rng = np.random.RandomState(0)
    pairs = []
    for _ in range(4000):
        f = rng.uniform(0.05, 0.95)
        true_p = 0.5 + 0.5 * (f - 0.5)            # model overconfident by 2x around 0.5
        pairs.append((f, 1.0 if rng.rand() < true_p else 0.0))
    m = cal.fit_map(pairs)
    assert m is not None
    cal.store_map(conn, "KXTEST", m)
    hi = cal.apply(conn, "KXTEST", 0.90)          # true ≈ 0.70
    lo = cal.apply(conn, "KXTEST", 0.10)          # true ≈ 0.30
    assert hi < 0.80
    assert lo > 0.20
    # monotone
    xs = [cal.apply(conn, "KXTEST", p) for p in np.linspace(0, 1, 21)]
    assert all(b >= a - 1e-9 for a, b in zip(xs[:-1], xs[1:]))


def test_identity_without_map(conn):
    assert cal.apply(conn, "KXNONE", 0.42) == 0.42


def test_calibrate_structs_rebuilds(conn):
    from prediction_market_macro.strategy.edge import Leg, Struct
    pairs = [(0.9, 0.0)] * 150 + [(0.9, 1.0)] * 150 + [(0.1, 0.0)] * 300
    cal.store_map(conn, "KXS", cal.fit_map(pairs))
    st = Struct("single", (Leg("T", "yes", 0.5, 100.0),), fair=0.9, cost=0.5,
                max_loss=0.5, desc="x")
    out = cal.calibrate_structs(conn, "KXS", [st])
    assert out[0].fair < 0.9                       # 0.9-bucket realized only 50%


# ── ensemble ─────────────────────────────────────────────────────────────────

def test_log_pool_agreement_sharpens():
    p = {100.0: 0.2, 101.0: 0.6, 102.0: 0.2}
    pooled = log_pool({"a": p, "b": p}, {"a": 0.5, "b": 0.5})
    assert abs(sum(pooled.values()) - 1) < 1e-9
    assert pooled[101.0] >= 0.6                    # agreement never dilutes the mode


def test_log_pool_veto_property():
    a = {100.0: 0.5, 101.0: 0.5, 102.0: 1e-9}
    b = {100.0: 1 / 3, 101.0: 1 / 3, 102.0: 1 / 3}
    pooled = log_pool({"a": a, "b": b}, {"a": 0.5, "b": 0.5})
    assert pooled[102.0] < 0.01                    # near-zero from one source crushes it


def test_learn_weights_prior_fallback(conn):
    assert learn_weights(conn, "KXCPI") == PRIOR


def test_learn_weights_inverse_mspe(conn):
    now = datetime.now(timezone.utc).isoformat()
    for i in range(20):
        conn.execute("INSERT INTO source_scores VALUES(?,?,?,?,?,?,?)",
                     ("KXCPI", f"2026-{i:02d}", "model", "-1h", 0.10, 5, now))
        conn.execute("INSERT INTO source_scores VALUES(?,?,?,?,?,?,?)",
                     ("KXCPI", f"2026-{i:02d}", "market", "-1h", 0.05, 5, now))
    conn.commit()
    w = learn_weights(conn, "KXCPI")
    assert w["market"] > w["model"]                # better source gets more weight
    assert abs(sum(w.values()) - 1) < 1e-6
    assert w["model"] >= 0.10 - 1e-9               # floor holds


def test_learn_weights_trims_catastrophic(conn):
    now = datetime.now(timezone.utc).isoformat()
    for i in range(20):
        conn.execute("INSERT INTO source_scores VALUES(?,?,?,?,?,?,?)",
                     ("KXU3", f"2026-{i:02d}", "model", "-1h", 0.50, 5, now))
        conn.execute("INSERT INTO source_scores VALUES(?,?,?,?,?,?,?)",
                     ("KXU3", f"2026-{i:02d}", "market", "-1h", 0.05, 5, now))
        conn.execute("INSERT INTO source_scores VALUES(?,?,?,?,?,?,?)",
                     ("KXU3", f"2026-{i:02d}", "bridge", "-1h", 0.06, 5, now))
    conn.commit()
    w = learn_weights(conn, "KXU3")
    assert "model" not in w                        # 10x worse than best → trimmed out


# ── bridge ───────────────────────────────────────────────────────────────────

def test_almon_weights_decay_and_normalize():
    w = almon_weights(12)
    assert abs(w.sum() - 1) < 1e-9
    assert w[0] > w[5] > w[11]                     # recency-weighted
