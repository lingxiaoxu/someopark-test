"""Plan 02 implied-distribution tests — synthetic strips, no network."""
import numpy as np

from crypto_trading.crypto_strategies.event_perp.signals.implied_dist import (
    StrikeQuote, event_fee, find_violations, implied_distribution, parse_strip,
    pav_decreasing)


def synth_quotes(mean=60_000.0, sd=2_000.0, strikes=None, spread=0.02):
    """Gaussian survival curve quoted with symmetric spreads."""
    from math import erf
    strikes = strikes if strikes is not None else np.arange(54_000, 66_001, 1_000)
    out = []
    for k in strikes:
        p = 0.5 * (1 - erf((k - mean) / (sd * 2 ** 0.5)))     # P(S ≥ k)
        out.append(StrikeQuote(float(k), max(0.0, p - spread / 2),
                               min(1.0, p + spread / 2), 100, 100))
    return out


def test_parse_strip_drops_unquoted_and_sorts():
    markets = [
        {"strike_type": "greater", "floor_strike": 62000,
         "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.45"},
        {"strike_type": "greater", "floor_strike": 60000,
         "yes_bid_dollars": "0.0000", "yes_ask_dollars": "1.0000"},   # empty book
        {"strike_type": "between", "floor_strike": 61000,
         "yes_bid_dollars": "0.10", "yes_ask_dollars": "0.20"},       # not a threshold
    ]
    qs = parse_strip(markets)
    assert len(qs) == 1 and qs[0].strike == 62000


def test_pav_monotonizes_decreasing():
    y = np.array([0.9, 0.7, 0.75, 0.5, 0.55, 0.2])
    z = pav_decreasing(y)
    assert all(z[i] >= z[i + 1] - 1e-12 for i in range(len(z) - 1))
    assert abs(z.mean() - y.mean()) < 1e-9          # least-squares pooling preserves mean


def test_implied_moments_recover_gaussian():
    qs = synth_quotes(mean=60_000, sd=2_000)
    d = implied_distribution(qs)
    assert d is not None
    assert abs(d.mean - 60_000) < 300               # within a strike gap
    assert 1_200 < d.sd < 3_000                     # discretisation-tolerant
    assert abs(d.skew) < 0.5


def test_clean_curve_has_no_violations():
    qs = synth_quotes()
    assert find_violations(qs) == []


def test_injected_crossing_is_flagged_and_fee_netted():
    qs = synth_quotes(spread=0.01)
    # corrupt one strike: higher-strike bid ABOVE lower-strike ask
    bad = StrikeQuote(61_000.0, 0.80, 0.85, 50, 50)
    qs = [q for q in qs if q.strike != 61_000.0] + [bad]
    qs.sort(key=lambda q: q.strike)
    viols = find_violations(qs)
    assert viols, "crossing must be detected"
    top = viols[0]
    assert top.k_hi == 61_000.0 and top.gross_credit > 0
    assert top.net_credit < top.gross_credit        # fees subtracted
    # with a huge fee rate the same crossing dies
    assert find_violations(qs, fee_rate=5.0) == []


def test_event_fee_shape():
    assert event_fee(0.5) >= event_fee(0.05)        # max at p=0.5
    assert event_fee(0.0) == 0.0 and event_fee(1.0) == 0.0
