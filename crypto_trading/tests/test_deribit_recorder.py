"""Deribit IV recorder: ATM selection is pure and bounded; no expired picks."""
from crypto_trading.crypto_common.refdata.deribit import (
    EXPIRIES_PER_CUR, STRIKES_PER_EXPIRY, pick_atm)


def inst(name, exp, strike, typ="call"):
    return dict(instrument_name=name, expiration_timestamp=exp,
                strike=strike, option_type=typ)


def test_pick_atm_two_nearest_expiries_three_strikes_calls_only():
    now = 1_000_000.0
    rows = []
    for e, exp in enumerate((now + 3.6e6, now + 8.64e7, now + 6.05e8)):
        for s in (80, 90, 100, 110, 120, 200):
            rows.append(inst(f"C-{e}-{s}", exp, s))
            rows.append(inst(f"P-{e}-{s}", exp, s, "put"))
    rows.append(inst("C-expired-100", now - 1, 100))
    picked = pick_atm(rows, index_price=102, now_ms=now)
    assert len(picked) == EXPIRIES_PER_CUR * STRIKES_PER_EXPIRY
    assert {p["expiration_timestamp"] for p in picked} == {now + 3.6e6, now + 8.64e7}
    for p in picked:
        assert p["option_type"] == "call"
        assert p["strike"] in (90, 100, 110)          # the three nearest to 102
    assert all("expired" not in p["instrument_name"] for p in picked)
