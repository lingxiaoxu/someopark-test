"""Fee-defeat research tests — synthetic frames, no network."""
import numpy as np
import pandas as pd

from crypto_trading.crypto_strategies.event_perp.research_fee_defeat import (
    BASE_Z, Z_BUCKETS, bucket_table, conv_bps, episode_table)


def synth_gap(z_path, spot_path, close_time="2026-07-20T12:00:00Z", start=0):
    n = len(z_path)
    ts = np.arange(n) * 90.0 + 1e9 + start
    idx = pd.to_datetime(ts, unit="s", utc=True)
    df = pd.DataFrame({"recv_ts": ts, "close_time": close_time,
                       "implied_mean": np.array(spot_path) * 1.001,
                       "perp_spot": spot_path, "gap": 0.001,
                       "gap_z": z_path}, index=idx)
    df.index.name = "dt"
    return df


def test_conv_bps_sign_convention():
    # z>0 (long) and spot rises → positive convergence
    g = synth_gap([2.0] * 30, list(np.linspace(100, 101, 30)))
    c = conv_bps(g, 30).dropna()
    assert len(c) and (c > 0).all()
    # z<0 (short) and spot rises → negative convergence
    g2 = synth_gap([-2.0] * 30, list(np.linspace(100, 101, 30)))
    c2 = conv_bps(g2, 30).dropna()
    assert len(c2) and (c2 < 0).all()


def test_conv_respects_horizon_boundary():
    # two interleaved horizons: forward spot must come from the SAME horizon
    a = synth_gap([2.0] * 5, [100] * 5, close_time="H1")
    b = synth_gap([2.0] * 5, [999] * 5, close_time="H2", start=45)
    g = pd.concat([a, b]).sort_index()
    c = conv_bps(g, 3)
    # H1 rows with enough same-horizon future have conv computed off 100s, not 999s
    h1 = c[g["close_time"] == "H1"].dropna()
    assert (h1.abs() < 1).all()          # flat 100→100, never crosses to 999


def test_bucket_table_assignment():
    z = [1.2] * 10 + [2.2] * 10 + [3.5] * 10
    g = synth_gap(z, [100.0] * 30)
    t = bucket_table(g, "gap_z", Z_BUCKETS, "t")
    n_by = dict(zip(t["bucket"], t["n"]))
    assert n_by["[1.0,1.5)"] == 10 and n_by["[2.0,2.5)"] == 10
    assert n_by["[3.0,inf)"] == 10 and n_by["[1.5,2.0)"] == 0


def test_episode_grouping_and_causal_entry():
    # one excursion ramping 1.1→2.5 (crosses hi_k=2 midway), then decay; spot
    # converges after the crossing → positive causal gross
    z = [1.1, 1.4, 1.8, 2.1, 2.5, 1.6, 1.2, 0.4, 0.2, 0.1]
    spot = [100, 100, 100, 100, 100.1, 100.3, 100.5, 100.6, 100.6, 100.6]
    g = synth_gap(z, spot)
    r = episode_table(g, hi_k=2.0)
    assert r["episodes_total"] == 1 and r["traded (hit hi_k)"] == 1
    assert r["mean_gross_bps"] > 0        # entered at z=2.1 (spot 100) → 100.5+


def test_episode_never_hits_hi_k_not_traded():
    z = [1.2, 1.4, 1.3, 1.1, 0.3]
    g = synth_gap(z, [100] * 5)
    r = episode_table(g, hi_k=2.0)
    assert r["episodes_total"] == 1 and r["traded (hit hi_k)"] == 0
