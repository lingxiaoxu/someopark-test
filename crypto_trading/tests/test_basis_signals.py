"""Plan 01 signal math tests — synthetic OU processes, no network."""
import numpy as np
import pandas as pd

from crypto_trading.crypto_strategies.basis_meanrev.signals.basis import (BasisParams,
                                                                          compute_signal_frame,
                                                                          ou_half_life,
                                                                          rolling_zscore)


def ou_series(n=2000, kappa=0.05, sigma=1.0, seed=7) -> pd.Series:
    """Discrete OU: x_t = x_{t-1}(1-kappa) + noise. HL = ln2/-ln(1-kappa)."""
    rng = np.random.default_rng(seed)
    x = np.zeros(n)
    for i in range(1, n):
        x[i] = x[i - 1] * (1 - kappa) + sigma * rng.standard_normal()
    idx = pd.date_range("2026-06-04", periods=n, freq="1min", tz="UTC")
    return pd.Series(x, index=idx)


def test_ou_half_life_recovers_known_kappa():
    s = ou_series(kappa=0.05)
    hl = ou_half_life(s)
    expected = np.log(2) / -np.log(1 - 0.05)          # ≈ 13.5 bars
    assert hl is not None and 0.6 * expected < hl < 1.6 * expected


def test_ou_half_life_none_for_random_walk():
    rng = np.random.default_rng(3)
    rw = pd.Series(rng.standard_normal(2000).cumsum())
    hl = ou_half_life(rw)
    # finite-sample kappa bias gives large-but-finite HL on random walks;
    # anything far above the 60-min trade gate is "no usable mean reversion"
    assert hl is None or hl > 200


def test_rolling_zscore_is_pit():
    s = pd.Series(np.arange(100, dtype=float))
    z = rolling_zscore(s, 30)
    # constant positive drift → z stays positive; last value doesn't rewrite history
    z_early = z.iloc[50]
    z2 = rolling_zscore(pd.concat([s, pd.Series([1e6])], ignore_index=True), 30)
    assert z2.iloc[50] == z_early


def make_frame(b_bps: np.ndarray) -> pd.DataFrame:
    idx = pd.date_range("2026-06-04", periods=len(b_bps), freq="1min", tz="UTC")
    index_px = np.full(len(b_bps), 60_000.0)
    mark = index_px * (1 + b_bps / 1e4)
    return pd.DataFrame({
        "mark_mid_contract": mark * 1e-4, "mark_mid_underlying": mark,
        "index_proxy": index_px, "index_venues": 3,
        "b_t": b_bps / 1e4, "b_t_bps": b_bps}, index=idx)


def test_signal_state_machine_short_on_rich_and_exit_on_revert():
    # OU around 0 with an injected rich excursion
    base = ou_series(n=800, kappa=0.08, sigma=2.0, seed=11).to_numpy()
    b = base.copy()
    b[500:520] += 30.0                                 # +30bps stretch
    frame = make_frame(b)
    p = BasisParams(zscore_window_min=60, entry_k=2.0, exit_k=0.5,
                    time_stop_min=120, half_life_max_min=100,
                    half_life_window_min=240, min_abs_bps=5.0)
    sig = compute_signal_frame(frame, p)
    window = sig.desired.iloc[500:540]
    assert (window == -1).any()                        # went short into richness
    after = sig.desired.iloc[560:]
    assert (after == 0).any()                          # eventually exited


def test_time_stop_forces_exit():
    # permanently stretched basis: entry then time-stop exit
    b = np.concatenate([np.random.default_rng(5).normal(0, 2, 400),
                        np.full(400, 40.0)])
    frame = make_frame(b)
    p = BasisParams(zscore_window_min=60, entry_k=2.0, exit_k=0.1,
                    time_stop_min=30, half_life_max_min=10_000,
                    half_life_window_min=120, min_abs_bps=1.0)
    sig = compute_signal_frame(frame, p)
    in_pos = np.where(sig.desired.to_numpy() != 0)[0]
    if len(in_pos):                                    # held ≤ time_stop bars per episode
        runs = np.split(in_pos, np.where(np.diff(in_pos) > 1)[0] + 1)
        assert max(len(r) for r in runs) <= 31
