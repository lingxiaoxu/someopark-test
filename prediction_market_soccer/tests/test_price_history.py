

def test_causal_never_takes_a_bar_from_the_future():
    """The nearest-bar rule returns a price that already reflects events the target moment had
    not seen. On the production Polymarket path the chosen bar sat a mean 2.7 minutes from the
    target and could fall on either side; on a 10-minute-bar series the old 900s tolerance
    silently accepted a quote a quarter of an hour away."""
    from prediction_market_soccer.util.price_history import price_at
    ser = [{"ts": 100, "price": 0.1}, {"ts": 160, "price": 0.2}, {"ts": 220, "price": 0.9}]
    assert price_at(ser, 200, max_gap_s=900)[0] == 0.9                 # old: reaches forward
    assert price_at(ser, 200, max_gap_s=900, causal=True)[0] == 0.2    # new: last bar at or before
    assert price_at(ser, 220, causal=True) == (0.9, 220)               # exact hit is allowed
    assert price_at(ser, 90, causal=True) == (None, None)              # nothing before the target
    assert price_at(ser, 500, max_gap_s=180, causal=True) == (None, None)   # too stale


def test_backfill_samples_the_wall_time_of_each_match_minute():
    """Consumers read a milestone label as a MATCH minute (smart_exit._MILESTONE_MIN, the live
    capture's thresholds), so a backfilled row must carry the price from that match minute.
    A ~15-minute half-time puts match minute 60 at wall minute 75; sampling at wall 60 handed
    the model a price from ~match 45 while scoring it at match 60."""
    from prediction_market_soccer.ops import backfill_milestones as B
    from prediction_market_soccer.strategy.smart_exit import _HALFTIME_WALL_MIN, _MILESTONE_MIN
    walls = {c: w for c, _m, w in B._MILESTONES}
    matches = {c: m for c, m, _w in B._MILESTONES}
    assert B._HALFTIME_WALL_MIN is _HALFTIME_WALL_MIN, "one owner for the half-time constant"
    for code, mn in _MILESTONE_MIN.items():
        assert matches[code] == mn, f"{code} must mean the same match minute as smart_exit"
    assert walls["T15"] == 15 and walls["T30"] == 30          # first half: clocks agree
    assert walls["T60"] == 75 and walls["T75"] == 90          # second half: +half-time
    assert 45 < walls["HT"] < 60                              # sampled inside the break
