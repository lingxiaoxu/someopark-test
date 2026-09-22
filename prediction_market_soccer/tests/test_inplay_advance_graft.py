"""The live "Advances" lens must actually carry data.

SoccerMatchCard offers the lens off `caps.advance` but renders it off `m.advance`.
The in-play export set the first and never emitted the second, so during a knockout
tie the toggle appeared and clicking it changed nothing — worse than not offering it.
These tests pin the contract the card reads.
"""
from __future__ import annotations

from prediction_market_soccer.ops.inplay_export import graft_advance


def _three_way_doc():
    return {"ts": "2026-08-27T19:00:00+00:00", "n_live": 2, "matches": [
        {"fixture_id": 111, "caps": {"advance": True, "two_leg": True, "leg": 2},
         "model": {"home": 0.5, "draw": 0.25, "away": 0.25}},
        {"fixture_id": 222, "caps": {"advance": False}, "model": {"home": 0.4, "draw": 0.3, "away": 0.3}},
    ]}


def _advance_kalshi():
    """Receipted 2-way kalshi book, shaped exactly as the advance export ships it:
    qualify_source_block keeps 'receipt' inside each side and _q2c adds the *_c
    display keys. graft_advance prices the edge through _best_buy_edge_2way, which
    since the leak-correction commit rejects any side without a valid, FRESH
    QuoteReceiptV1 — so the receipt clocks must derive from the real clock."""
    from datetime import datetime, timezone

    from prediction_market_soccer.ops.inplay_export_advance import _q2c
    from prediction_market_soccer.util.market_identity import make_binding
    from prediction_market_soccer.util.quote_evidence import make_receipt, quote_from_receipt
    now = datetime.now(timezone.utc).isoformat()
    fixture = {"fixture_api_id": 111, "comp": "ucl", "season": 2026,
               "home_api_id": 80, "away_api_id": 247, "home_id": "lyon", "away_id": "celtic",
               "kickoff_ts": "2026-08-27T19:00:00+00:00", "tie_id": "t111", "leg": 2}

    def q(side, ask, bid):
        b = make_binding(fixture=fixture, provider="kalshi", environment="public",
                         event_id="KXUCLADV-TEST", market_id=f"KXUCLADV-TEST-{side.upper()}",
                         side=side, market_kind="advance")
        return quote_from_receipt(make_receipt(b, ask=ask, bid=bid, raw={"side": side},
                                               request_started_at=now, received_at=now))

    return _q2c({"home": q("home", 0.55, 0.53), "away": q("away", 0.48, 0.46)})


def _advance_doc():
    return {"ts": "2026-08-27T19:00:00+00:00", "n_live": 1, "matches": [
        {"fixture_id": 111,
         "model": {"home": 0.72, "away": 0.28,
                   "p_reg_decides": 0.6, "p_et_decides": 0.25, "p_pens_decides": 0.15},
         "prices": {
             "model_c": {"home": 72.0, "away": 28.0},
             "kalshi": _advance_kalshi()},
         "opportunities": [{"reason_key": "relative_value"}],
         "hedge_advance": None},
    ]}


def test_graft_attaches_block_the_card_can_render():
    doc = graft_advance(_three_way_doc(), _advance_doc())
    row = doc["matches"][0]
    adv = row["advance"]
    # SoccerMatchCard: twoWay = mode==='advance' && caps.advance && adv.model
    assert row["caps"]["advance"] and adv["model"]
    assert adv["model"]["home"] == 0.72 and adv["model"]["away"] == 0.28
    # It must NOT carry a draw — the whole point of the 2-way product.
    assert "draw" not in adv["model"]
    assert adv["model"]["cents"]["home"] == 72.0


def test_graft_prices_the_edge_against_the_venue_ask():
    adv = graft_advance(_three_way_doc(), _advance_doc())["matches"][0]["advance"]
    best = adv["edge"]["best"]
    # Model 0.72 vs a 0.55 ask is the tradable side; the away side is model 0.28 vs 0.48.
    assert best["side"] == "home" and best["venue"] == "kalshi"
    assert best["net_edge"] > 0
    # De-vig of a 0.55/0.48 book sums to 1 and keeps home as the favourite.
    dv = adv["kalshi"]["devig"]
    assert abs(dv["home"] + dv["away"] - 1.0) < 1e-6
    assert dv["home"] > dv["away"]


def test_graft_leaves_non_tie_rows_alone():
    doc = graft_advance(_three_way_doc(), _advance_doc())
    assert "advance" not in doc["matches"][1]


def test_graft_is_a_noop_without_advance_data():
    """The advance builder is failure-tolerant and may hand back None — the 3-way
    export must survive that untouched rather than write a half-formed block."""
    for empty in (None, {}, {"matches": []}):
        doc = graft_advance(_three_way_doc(), empty)
        assert all("advance" not in m for m in doc["matches"])


def test_graft_handles_a_tie_with_no_venue_quote():
    """No quote → no edge, but the model block must still render (that is the lens)."""
    adv_doc = _advance_doc()
    adv_doc["matches"][0]["prices"] = {"model_c": {"home": 72.0, "away": 28.0}}
    adv = graft_advance(_three_way_doc(), adv_doc)["matches"][0]["advance"]
    assert adv["model"]["home"] == 0.72
    assert adv["kalshi"] is None and adv["poly_us"] is None and adv["edge"] is None
