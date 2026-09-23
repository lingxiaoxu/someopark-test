"""Causal source availability and malformed-data tests in isolated directories."""
from datetime import datetime, timezone
import gzip
import json
import math

import pytest

from crypto_trading.crypto_strategies.w10_entry_paper.fv60_features import ValuationTape


T = datetime(2026, 9, 17, 6, 22, tzinfo=timezone.utc).timestamp()
CLOSE = T + 480
TICKER = "KXBTC15M-26SEP170230-30"


def spot(t, **kwargs):
    return {"asset": "BTC", "ts": t, "index": 101, "n_venues": 3,
            "stale": False, "venues": {"a": 100, "b": 101, "c": 102}, **kwargs}


def strip(t, **market):
    return {"recv_ts": t, "markets": [{"ticker": TICKER, "close_time": CLOSE,
            "strike_type": "greater_or_equal", "floor_strike": 100, **market}]}


def write(root, kind, rows, day=None, compressed=False):
    if day is None:
        day = datetime.fromtimestamp(T, timezone.utc).strftime("%Y-%m-%d")
    rel = "index_proxy/live/BTC" if kind == "spot" else "kalshi/event_strips/prod/KXBTC15M/markets"
    path = root / rel / (day + ".jsonl" + (".gz" if compressed else ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(json.dumps(row) + "\n" for row in rows).encode()
    path.write_bytes(gzip.compress(data) if compressed else data)
    return path


def prepared(root):
    write(root, "spot", [spot(T-10), spot(T-5), spot(T+1)])
    write(root, "strike", [strip(T-100), strip(T-40), strip(T+10)])
    tape = ValuationTape(root, assets=("BTC",))
    tape.update(T+12)
    return tape


def evaluate(tape, **kwargs):
    return tape.features(**{"asset": "BTC", "ticker": TICKER, "side": "yes",
        "entry_price": .7, "decision_ts": T, "close_ts": CLOSE,
        "hl_features": {"valid": True, "flow_valid": False, "decision_ts": T,
                        "realized_vol_5m_bp": 50}, **kwargs})


def test_valuation_matches_frozen_formula_and_does_not_require_flow(tmp_path):
    result = evaluate(prepared(tmp_path))
    assert result["valid"]
    # T-5 row only becomes available at T+1, so use T-10 row instead.
    assert result["spot_capture_started_at"] == T-10
    assert result["spot_available_at"] == T-5
    assert result["strike_capture_started_at"] == T-100
    assert result["strike_available_at"] == T-40
    expected = .5 * (1 + math.erf(math.log(101/100)*10000 /
        (50*math.sqrt((CLOSE-T-40)/300)*math.sqrt(2))))
    assert result["probability_proxy"] == pytest.approx(expected)
    assert result["edge_proxy"] == pytest.approx(expected-.7)
    assert result["max_input_available_at"] <= T
    assert evaluate(prepared(tmp_path), side="no")["probability_proxy"] == pytest.approx(1-expected)
    json.dumps(result, allow_nan=False)


def test_reading_future_rows_never_makes_them_available_at_earlier_decision(tmp_path):
    tape = prepared(tmp_path)
    write(tmp_path, "spot", [spot(T-10), spot(T-5, index=999999), spot(T+1)])
    tape = ValuationTape(tmp_path, assets=("BTC",))
    tape.update(T+12)
    assert evaluate(tape)["spot"] == 101
    # In-memory presence itself is not proof that the polling response was ready.
    assert evaluate(tape, decision_ts=T-8, hl_features={"valid": True, "decision_ts": T-8,
        "realized_vol_5m_bp": 50})["errors"] == ["no_conservatively_available_spot"]


def test_custom_strike_preferred_and_round_digits_only_uses_explicit_floor(tmp_path):
    write(tmp_path, "spot", [spot(T-10), spot(T-5)])
    write(tmp_path, "strike", [strip(T-100, custom_strike={"floor_strike": "102", "round_digits": "2"}), strip(T-40)])
    tape = ValuationTape(tmp_path, assets=("BTC",))
    tape.update(T)
    result = evaluate(tape)
    assert result["valid"] and result["strike"] == 102
    assert result["strike_field"] == "custom_strike.floor_strike"
    write(tmp_path, "strike", [strip(T-100, custom_strike={"round_digits": "2"}), strip(T-40)])
    tape = ValuationTape(tmp_path, assets=("BTC",)); tape.update(T)
    assert evaluate(tape)["strike"] == 100


@pytest.mark.parametrize("market,reason", [
    ({"custom_strike": {"floor_strike": None}}, "missing_or_invalid_strike"),
    ({"custom_strike": "100"}, "malformed_custom_strike"),
    ({"floor_strike": float("nan")}, "missing_or_invalid_strike"),
    ({"close_time": CLOSE+900}, "exact_market_close_mismatch"),
    ({"close_time": "2026-09-17T06:30:00"}, "exact_market_close_mismatch"),
    ({"strike_type": "less"}, "unsupported_strike_type"),
    ({"ticker": "KXBTC15M-26SEP170245-45"}, "no_conservatively_available_exact_market"),
])
def test_no_fabricated_or_other_contract_strike(tmp_path, market, reason):
    write(tmp_path, "spot", [spot(T-10), spot(T-5)])
    write(tmp_path, "strike", [strip(T-100, **market), strip(T-40)])
    tape = ValuationTape(tmp_path, assets=("BTC",)); tape.update(T)
    assert evaluate(tape)["errors"] == [reason]


@pytest.mark.parametrize("patch,reason", [
    ({"index": None}, "invalid_or_stale_spot_record"),
    ({"index": float("inf")}, "invalid_or_stale_spot_record"),
    ({"n_venues": 0}, "invalid_or_stale_spot_record"),
    ({"stale": True}, "invalid_or_stale_spot_record"),
    ({"asset": "ETH"}, "no_conservatively_available_spot"),
])
def test_stale_missing_malformed_other_asset_spot_fails_closed(tmp_path, patch, reason):
    write(tmp_path, "spot", [spot(T-10, **patch), spot(T-5)])
    write(tmp_path, "strike", [strip(T-100), strip(T-40)])
    tape = ValuationTape(tmp_path, assets=("BTC",)); tape.update(T)
    assert evaluate(tape)["errors"] == [reason]


def test_stale_capture_is_not_made_fresh_by_recent_availability(tmp_path):
    write(tmp_path, "spot", [spot(T-25), spot(T-5)])
    write(tmp_path, "strike", [strip(T-100), strip(T-40)])
    tape = ValuationTape(tmp_path, assets=("BTC",)); tape.update(T)
    assert evaluate(tape)["errors"] == ["spot_capture_age_over15s"]


def test_duplicate_poll_start_does_not_prove_availability_and_conflict_is_rejected(tmp_path):
    write(tmp_path, "spot", [spot(T-10), spot(T-10)])
    write(tmp_path, "strike", [strip(T-100), strip(T-40)])
    tape = ValuationTape(tmp_path, assets=("BTC",)); tape.update(T)
    assert evaluate(tape)["errors"] == ["no_conservatively_available_spot"]
    write(tmp_path, "spot", [spot(T-10), spot(T-10, index=102), spot(T-5)])
    tape = ValuationTape(tmp_path, assets=("BTC",)); tape.update(T)
    assert evaluate(tape)["errors"] == ["conflicting_spot_poll_timestamp"]


@pytest.mark.parametrize("features,reason", [
    ({"valid": False, "decision_ts": T, "realized_vol_5m_bp": 50}, "invalid_hyperliquid_features"),
    ({"valid": True, "decision_ts": T+1, "realized_vol_5m_bp": 50}, "hyperliquid_decision_timestamp_mismatch"),
    ({"valid": True, "decision_ts": T, "realized_vol_5m_bp": 0}, "invalid_realized_volatility"),
    ({"valid": True, "decision_ts": T, "realized_vol_5m_bp": float("inf")}, "invalid_realized_volatility"),
])
def test_hyperliquid_validity_and_exact_cutoff_required(tmp_path, features, reason):
    assert evaluate(prepared(tmp_path), hl_features=features)["errors"] == [reason]


def test_unread_plain_bytes_survive_gzip_rotation_and_duplicate_loading(tmp_path):
    path = write(tmp_path, "spot", [spot(T-10)])
    write(tmp_path, "strike", [strip(T-100), strip(T-40)])
    tape = ValuationTape(tmp_path, assets=("BTC",)); tape.update(T)
    # New bound appended just before source compression, without another update.
    with path.open("a") as stream:
        stream.write(json.dumps(spot(T-5))+"\n")
    compressed = path.with_suffix(path.suffix+".gz")
    compressed.write_bytes(gzip.compress(path.read_bytes())); path.unlink()
    tape.update(T)
    assert evaluate(tape)["valid"]
    retained = tape.update(T)["retained_rows"]
    assert retained == 4


def test_midnight_previous_gzip_next_day_plain_availability(tmp_path):
    midnight = datetime(2026, 9, 18, tzinfo=timezone.utc).timestamp()
    write(tmp_path, "spot", [spot(midnight-5)], day="2026-09-17", compressed=True)
    write(tmp_path, "spot", [spot(midnight+1)], day="2026-09-18")
    write(tmp_path, "strike", [strip(midnight-100, close_time=midnight+480)], day="2026-09-17", compressed=True)
    write(tmp_path, "strike", [strip(midnight+1, close_time=midnight+480)], day="2026-09-18")
    tape = ValuationTape(tmp_path, assets=("BTC",)); tape.update(midnight+2)
    result = evaluate(tape, decision_ts=midnight+2, close_ts=midnight+480,
        hl_features={"valid": True, "decision_ts": midnight+2, "realized_vol_5m_bp": 50})
    assert result["valid"]
    assert result["spot_capture_started_at"] == midnight-5
    assert result["spot_available_at"] == midnight+1
    tape.update(midnight+1300)
    assert not tape.rows["spot", "BTC"] and not tape.rows["strike", "BTC"]


def test_partial_plain_line_not_consumed_until_complete(tmp_path):
    path = write(tmp_path, "spot", [spot(T-10)])
    write(tmp_path, "strike", [strip(T-100), strip(T-40)])
    encoded = json.dumps(spot(T-5))
    with path.open("a") as stream:
        stream.write(encoded[:20])
    tape = ValuationTape(tmp_path, assets=("BTC",)); tape.update(T)
    assert not evaluate(tape)["valid"]
    with path.open("a") as stream:
        stream.write(encoded[20:]+"\n")
    tape.update(T)
    assert evaluate(tape)["valid"]


def test_malformed_json_does_not_crash_or_manufacture_value(tmp_path):
    path = write(tmp_path, "spot", [spot(T-10), spot(T-5)])
    with path.open("a") as stream:
        stream.write("{broken}\n")
    write(tmp_path, "strike", [strip(T-100), strip(T-40)])
    tape = ValuationTape(tmp_path, assets=("BTC",))
    assert tape.update(T)["read_errors"] == 1
    assert evaluate(tape)["valid"]


def test_final_minute_and_boolean_inputs_are_unclassified(tmp_path):
    tape = prepared(tmp_path)
    assert evaluate(tape, entry_price=True)["errors"] == ["invalid_candidate"]
    assert evaluate(tape, close_ts=T+60)["errors"] == ["outside_frozen_before_final_minute_formula"]
