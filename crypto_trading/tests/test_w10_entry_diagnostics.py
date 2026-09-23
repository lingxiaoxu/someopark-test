"""Prospective quote diagnostics use supplied snapshots, never production state."""
from copy import deepcopy
from datetime import datetime

import pytest

from crypto_trading.crypto_strategies.w10_entry_paper.diagnostics import collect_quote_diagnostics


TICKER = "KXBTC15M-26SEP151430-30"
AT = datetime.fromisoformat("2026-09-15T18:21:54+00:00").timestamp()
CLOSE = datetime.fromisoformat("2026-09-15T18:30:00+00:00").timestamp()


def state():
    order = {"id": TICKER+":1", "side": "yes", "price": .71, "quantity": 1,
             "created_ts": AT, "activate_ts": AT+.5, "expires_ts": AT+20,
             "remaining": 1, "queue_ahead": 5}
    return {"version": "w8_test", "registered_at": "2026-09-14T05:17:35+00:00",
            "books": {"tilted": {"complete_sets": False, "positions": {
                TICKER: {"ticker": TICKER, "series": "KXBTC15M", "close_ts": CLOSE,
                         "orders": [order], "fills": []}}}}}


def market(s):
    return s["books"]["tilted"]["positions"][TICKER]


def features(asset, at):
    assert asset == "BTC"
    return {"decision_ts": at, "valid": True, "flow_valid": True,
            "momentum_1m_bp": 1.5, "observed_flow_imbalance_1m": .25}


def run(s=None, existing=None, fn=features, **kwargs):
    args = dict(parent_state=s or state(), excluded_tickers=set(), since_ts=AT-1,
                until_ts=AT+10, observed_ts=AT+11, existing_records=existing or {}, feature_fn=fn)
    args.update(kwargs)
    return collect_quote_diagnostics(**args)


def remember(result):
    return {r["id"]: r for r in result["records"]}


def test_first_quote_gate_and_source_creation_time_not_poll_time():
    s = state(); before = deepcopy(s)
    r = run(s)
    assert not r["errors"] and len(r["records"]) == 1
    q = r["records"][0]
    assert q["classification"] == "accept" and q["quote_stage"] == "first"
    assert q["features"]["decision_ts"] == AT
    assert q["observation_lag_seconds"] == 11
    assert q["diagnostic_only"] is True and q["pnl_eligible"] is False
    assert "not_pretrade" in q["execution_claim"]
    assert s == before


@pytest.mark.parametrize("side,momentum,flow,expected", [
    ("yes", 1, .1, "accept"), ("no", -1, -.1, "accept"),
    ("yes", 0, .1, "skip"), ("yes", 1, 0, "skip"),
    ("yes", 1, -.1, "skip"), ("yes", -1, .1, "skip"),
    ("no", 1, .1, "skip"), ("no", -1, .1, "skip")])
def test_strict_gate_and_direction(side, momentum, flow, expected):
    s = state(); market(s)["orders"][0]["side"] = side
    def fn(asset, at):
        return {**features(asset, at), "momentum_1m_bp": momentum, "observed_flow_imbalance_1m": flow}
    assert run(s, fn=fn)["records"][0]["classification"] == expected


@pytest.mark.parametrize("change", [
    {"valid": False}, {"flow_valid": False}, {"momentum_1m_bp": float("nan")},
    {"observed_flow_imbalance_1m": 1.1}, {"decision_ts": AT+1},
    {"momentum_1m_bp": True}])
def test_unqualified_or_future_features_never_accept(change):
    assert run(fn=lambda a, t: {**features(a, t), **change})["records"][0]["classification"] == "unclassified"


def test_feature_exception_is_unclassified():
    def broken(*args):
        raise RuntimeError("feature failed")
    q = run(fn=broken)["records"][0]
    assert q["classification"] == "unclassified"
    assert q["features"]["errors"] == ["feature_evaluation_failed:RuntimeError"]


def test_future_old_and_startup_markets_not_admitted():
    assert not run(until_ts=AT-.1, observed_ts=AT)["records"]
    assert not run(since_ts=AT+.1)["records"]
    assert not run(excluded_tickers={TICKER})["records"]


def test_replacements_are_independently_evaluated_without_pnl():
    s = state(); first = market(s)["orders"][0]
    market(s)["orders"].append({**first, "id": TICKER+":2", "created_ts": AT+5,
                                 "activate_ts": AT+5.5, "expires_ts": AT+25, "side": "no"})
    result = run(s)
    assert [q["classification"] for q in result["records"]] == ["accept", "skip"]
    assert [q["quote_stage"] for q in result["records"]] == ["first", "replacement"]
    assert all("net_usd" not in q and not q["pnl_eligible"] for q in result["records"])


def test_idempotent_does_not_rerun_features_or_rewrite_first_observation():
    s = state(); saved = remember(run(s)); before = deepcopy(saved)
    def forbidden(*args):
        pytest.fail("must not recompute an existing quote")
    assert run(s, saved, fn=forbidden, observed_ts=AT+30) == {"records": [], "errors": []}
    assert saved == before


def test_mutable_queue_cancel_fill_remaining_are_not_source_revisions():
    s = state(); saved = remember(run(s))
    market(s)["orders"][0].update(remaining=0, queue_ahead=0, cancel_ts=AT+5)
    assert not run(s, saved)["records"]


def test_source_revision_append_preserves_original_and_deduplicates():
    s = state(); saved = remember(run(s)); original = deepcopy(saved)
    market(s)["orders"][0]["price"] = .72
    revision = run(s, saved)["records"][0]
    assert revision["event_type"] == "quote_source_revision"
    assert revision["revised_source"]["price"] == .72
    assert saved == original
    saved[revision["id"]] = revision
    assert not run(s, saved)["records"]


def test_known_quote_timestamp_changed_outside_interval_is_revision_not_hidden():
    s = state(); saved = remember(run(s))
    market(s)["orders"][0]["created_ts"] = AT-2
    r = run(s, saved)
    assert len(r["records"]) == 1
    assert r["records"][0]["event_type"] == "quote_source_revision"
    assert r["records"][0]["revised_source"]["created_ts"] == AT-2


def fill(at=AT+1):
    return {"order_id": TICKER+":1", "ts": at, "side": "yes", "price": .71,
            "quantity": .5, "fee_usd": 0, "liquidity": "maker_model", "source": "venue-trade-1"}


def test_already_reported_fill_explicitly_observed_after_fill():
    s = state(); market(s)["fills"] = [fill()]
    quote, report = run(s)["records"]
    assert quote["classification"] == "accept"
    assert report["fill_precedes_or_equals_first_quote_observation"] is True
    assert report["first_quote_observation_minus_fill_seconds"] == 10
    assert not report["pnl_eligible"]


def test_late_arriving_old_fill_cannot_claim_initial_observation_was_pretrade():
    s = state(); saved = remember(run(s)); before = deepcopy(saved)
    market(s)["fills"] = [fill()]
    r = run(s, saved, until_ts=AT+20, observed_ts=AT+21)
    assert len(r["records"]) == 1
    report = r["records"][0]
    assert report["event_type"] == "quote_fill_reported"
    assert report["fill_precedes_or_equals_first_quote_observation"]
    assert report["first_quote_observed_ts"] == AT+11
    assert report["observed_ts"] == AT+21 and saved == before
    saved.update(remember(r))
    assert not run(s, saved, observed_ts=AT+22)["records"]


def test_fill_later_than_first_observation_still_diagnostic_only():
    s = state(); saved = remember(run(s))
    market(s)["fills"] = [fill(AT+15)]
    report = run(s, saved, until_ts=AT+20, observed_ts=AT+21)["records"][0]
    assert not report["fill_precedes_or_equals_first_quote_observation"]
    assert report["diagnostic_only"] and not report["pnl_eligible"]


def test_future_fill_ignored_and_taker_risk_exit_not_entry_order():
    s = state(); market(s)["fills"] = [fill(AT+50), {**fill(), "order_id": None,
        "source": "risk_flatten", "liquidity": "taker_depth_model"}]
    assert len(run(s)["records"]) == 1
    market(s)["orders"].append({**market(s)["orders"][0], "id": TICKER+":2", "source": "risk_flatten"})
    assert len(run(s)["records"]) == 1


def test_reject_duplicate_ids_and_invalid_market_identity():
    s = state(); market(s)["orders"] *= 2
    r = run(s)
    assert not r["records"] and r["errors"][0]["error"] == "duplicate source order ID"
    s = state(); market(s)["close_ts"] += 1
    assert not run(s)["records"] and run(s)["errors"]


def test_paired_book_never_included_or_accepted_as_parent():
    s = state(); s["books"]["paired"] = deepcopy(s["books"]["tilted"])
    assert len(run(s)["records"]) == 1
    s["books"]["tilted"]["complete_sets"] = True
    with pytest.raises(ValueError, match="non-completion"):
        run(s)


def test_invalid_clock_order_rejected():
    with pytest.raises(ValueError, match="interval"):
        run(until_ts=AT+20, observed_ts=AT+19)
