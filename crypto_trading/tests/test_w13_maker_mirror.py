"""W13 = W11 + maker execution: resting orders at touch-1c chased on the recorded book, fills by crossing, fee 0."""
import json, time
import pytest
from crypto_trading.crypto_common.execution_events import EventExecutionRouter
from crypto_trading.crypto_strategies.w7_scenarios import live_plan, inputs
from crypto_trading.crypto_strategies.w13_maker_mirror import paper_router as pr, config as cfg

LIVE_CFG = {"w7_noisefade": {"enabled": True, "window_cap_mult": 1.5}}
PROD_BAND = {"w7_noisefade": (0.79, 0.98)}
OB = {"orderbook_fp": None, "yes_dollars": [["0.80", "50"], ["0.79", "100"]], "no_dollars": [["0.15", "30"], ["0.14", "40"], ["0.10", "500"]]}   # YES costs 0.85 (30), 0.86 (40), 0.90 (500)
PROBS = {"p_fair": 0.90, "p_lgbm": 0.80, "p_lgbm_np": 0.88, "p_gru": 0.90, "p_gru_np": None, "p_chronos": None, "p_chronos_base": 0.86, "p_blend1": 0.86, "p_blend2": 0.84, "z": 0.5}


def _observer(monkeypatch, tmp_path):
    from crypto_trading.crypto_strategies.w13_maker_mirror import observer as ob
    monkeypatch.setattr(ob, "OUT", tmp_path); monkeypatch.setattr(ob, "STATE", tmp_path / "state.json"); monkeypatch.setattr(ob, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(ob, "SLOTS", tmp_path / "slots.json"); monkeypatch.setattr(ob, "latest_stamp", lambda: None)
    monkeypatch.setattr(EventExecutionRouter, "TABLE_LIVE", LIVE_CFG); monkeypatch.setattr(EventExecutionRouter, "PROD_BAND", PROD_BAND)
    o = ob.Observer(chronos=False, use_worker=False)
    monkeypatch.setattr(o.router, "_utc_hour", lambda: 15); monkeypatch.setattr(o.router, "_trend_bp", lambda c, lb: 0.0)
    monkeypatch.setattr(o.router, "_flow_gate_decision", lambda t, s: {"decision": "accept"})
    o.models = {"meta": {"z_terciles": [1.0, 1.5]}}
    return ob, o


def test_maker_router_rests_at_touch_minus_1c_and_keeps_the_taker_counterfactual(monkeypatch, tmp_path):
    monkeypatch.setattr(EventExecutionRouter, "TABLE_LIVE", LIVE_CFG); monkeypatch.setattr(EventExecutionRouter, "PROD_BAND", PROD_BAND)
    monkeypatch.setattr(live_plan, "SLOTS_FILE", tmp_path / "slots.json"); live_plan.reset_ledger()
    monkeypatch.setattr("crypto_trading.crypto_common.kalshi.rest_event.KalshiEventOrderClient", lambda *a, **k: (_ for _ in ()).throw(AssertionError("venue call")))
    r = pr.MakerPaperRouter(strategy="w7_noisefade"); r.book = OB
    monkeypatch.setattr(r, "_utc_hour", lambda: 15); monkeypatch.setattr(r, "_trend_bp", lambda c, lb: 0.0); monkeypatch.setattr(r, "_flow_gate_decision", lambda t, s: {"decision": "accept"})
    out = r.submit(ticker="KXBTC15M-26OCT021945-45", side="yes", entry_price=0.85, contracts=25, armed=True, size_mult=1.5,
                   entry_source="table_top1_T-9.75", table_decision={"action": "trade", "slot": "top1", "mult": 1.5, "scenario": "S"})
    assert out["status"] == "live_sent" and out["paper"] and out["contracts"] == 40          # 2026-10-05: x1.5 capped at x1
    rs = out["resting"]; assert rs["price"] == pytest.approx(0.84) and rs["touch"] == pytest.approx(0.85) and rs["contracts"] == 40
    assert rs["taker_filled"] == 40 and rs["taker_cost"] == pytest.approx((30 * 0.85 + 10 * 0.86) / 40) and rs["taker_fee"] > 0
    assert json.loads(out["response"])["fill_count"] == "0.00" and out["window_cap"]["bought"] == 0.0 and live_plan.exposure("KXBTC15M-26OCT021945-45") == 0.0   # nothing bought at send time
    assert pr.top_cost(OB, "no") == pytest.approx(0.20) and pr.top_cost({"yes_dollars": [], "no_dollars": []}, "yes") is None


def test_chase_reposts_fills_on_crossing_expires_and_settles(monkeypatch, tmp_path):
    ob, o = _observer(monkeypatch, tmp_path)
    close = 1_790_000_000; now = close - 585; tk = inputs.ticker("BTC", close); tk2 = inputs.ticker("ETH", close)
    d = {"action": "trade", "slot": "top1", "mult": 1.5, "scenario": "横盘·低波动"}
    out = o._order(tk, "yes", 0.85, OB, "T-9.75", d, PROBS, close, now, entry_source="table_top1_T-9.75", size_mult=1.5)
    assert out["status"] == "live_sent" and o.st["fills"] == [] and len(o.st["resting"]) == 1
    p = o.st["resting"][0]; assert p["price"] == pytest.approx(0.84) and p["contracts"] == 40 and p["expires"] == close - 45 and p["plan"]["base"]["n"] == 40 and p["plan"]["eq_size"]["n"] == 40
    assert live_plan.exposure(tk) == 40.0 and o.st["maker"]["attempted"] == 1                       # a resting order holds the window cap
    # the ask moves away -> re-post at the new touch - 1c; then the ask comes down to our price -> filled at OUR price
    snaps = {"BTC": [(now + 10, tk, {"yes_dollars": [["0.80", "50"]], "no_dollars": [["0.14", "40"], ["0.10", "500"]]}),      # touch 0.86
                     (now + 20, tk, {"yes_dollars": [["0.80", "50"]], "no_dollars": [["0.15", "30"], ["0.10", "500"]]})]}     # touch 0.85
    calls = {"BTC": 0}
    def latest(coin, t):
        if coin != "BTC": return None
        i = min(calls["BTC"], len(snaps["BTC"]) - 1); calls["BTC"] += 1; return snaps["BTC"][i]
    monkeypatch.setattr(o, "_latest_strip", latest)
    o._chase(now + 11); p = o.st["resting"][0]; assert p["price"] == pytest.approx(0.85) and p["reposts"] == 1 and o.st["fills"] == []
    o._chase(now + 21); assert o.st["resting"] == [] and len(o.st["fills"]) == 1
    f = o.st["fills"][0]; v = f["var"]
    assert v["base"]["n"] == 40 and v["base"]["cost"] == pytest.approx(0.85) and v["base"]["fee"] == 0.0 and v["eq_size"]["n"] == 40 and v["f_lgbm"]["take"] is False and v["f_gru"]["take"]
    assert f["maker"]["reposts"] == 1 and f["maker"]["ttf_s"] == pytest.approx(21) and f["maker"]["improve_c"] == pytest.approx(((30 * 0.85 + 10 * 0.86) / 40 - 0.85) * 100)
    assert live_plan.exposure(tk) == 40.0 and o.st["maker"]["filled"] == 1
    # a second order that never gets crossed expires 45 s before the close and releases the cap
    o._order(tk2, "yes", 0.85, OB, "T-9.75", d, PROBS, close, now, entry_source="table_top1_T-9.75", size_mult=1.0)
    assert len(o.st["resting"]) == 1 and live_plan.exposure(tk2) == 30.0
    monkeypatch.setattr(o, "_latest_strip", lambda coin, t: None)
    o._chase(close - 46); assert len(o.st["resting"]) == 1                                            # still resting
    o._chase(close - 45); assert o.st["resting"] == [] and len(o.st["unfilled"]) == 1 and live_plan.exposure(tk2) == 0.0 and o.st["maker"]["unfilled"] == 1
    # settlement: YES wins both windows -> the fill books 40 x (1 - 0.85) with no fee; the unfilled order is settled hypothetically
    monkeypatch.setattr(ob.inputs, "official_outcomes", lambda: {tk: 1.0, tk2: 1.0})
    o._settle(close + 120)
    f = o.st["fills"][0]; assert f["settled"] and f["won"] == 1.0 and f["pnl"]["base"] == pytest.approx(40 * 0.15) and f["pnl"]["f_lgbm"] == 0.0
    assert o.st["ledgers"]["base"] == {"trades": 1, "contracts": 40.0, "pnl": pytest.approx(6.0), "won": 1, "lost": 0, "fees": 0.0}
    tq = f["maker"]["taker"]; assert f["taker_equiv_pnl"] == pytest.approx(40 * (1 - tq["cost"] - tq["fee"]), abs=1e-3)
    u = o.st["unfilled"][0]; assert u["settled"] and u["won"] == 1.0 and u["taker_equiv_pnl"] > 0
    m = o.st["maker"]; assert m["filled_settled"] == 1 and m["filled_won"] == 1 and m["unfilled_settled"] == 1 and m["unfilled_would_won"] == 1 and m["taker_equiv_n"] == 2
    assert m["taker_equiv_pnl"] == pytest.approx(f["taker_equiv_pnl"] + u["taker_equiv_pnl"], abs=1e-3)
    acts = [json.loads(l)["action"] for l in open(tmp_path / "logs" / f"log_{time.strftime('%Y-%m-%d', time.gmtime())}.jsonl")]
    assert "w13_order" in acts and "w13_fill" in acts and "w13_unfilled" in acts and "w13_settle" in acts and "w13_unfilled_settle" in acts


def test_macro_guard_clamps_resting_size_and_every_variant(monkeypatch, tmp_path):
    from crypto_trading.crypto_common import macro_calendar as mc
    ob, o = _observer(monkeypatch, tmp_path)
    monkeypatch.setattr(mc, "active", lambda now=None, after_s=2700, before_s=0: {"key": "cpi", "name": "CPI", "t_utc": time.time() - 60, "until": time.time() + 2640, "source": "fred"})
    close = 1_790_000_000; tk = inputs.ticker("SOL", close)
    out = o._order(tk, "yes", 0.85, OB, "T-9.75", {"action": "trade", "slot": "top1", "mult": 1.5, "scenario": "S"}, PROBS, close, close - 585, entry_source="table_top1_T-9.75", size_mult=1.5)
    assert out["contracts"] == 5 and out["macro"]["event"] == "cpi"
    p = o.st["resting"][0]; assert p["contracts"] == 5 and all(x["n"] <= 5 for x in p["plan"].values()) and p["plan"]["eq_size"]["n"] == 5 and p["plan"]["night_full"]["n"] == 5


def test_w13_paths_models_and_no_training(monkeypatch, tmp_path):
    assert str(cfg.OUT).endswith("w13_maker_mirror") and cfg.SLOTS.parent == cfg.OUT and cfg.STATE.parent == cfg.OUT and cfg.LOG_DIR.parent == cfg.OUT
    assert str(cfg.MODELS).endswith("w11_prod_mirror/models") and cfg.TRAIN_ENABLED is False and cfg.CHASE["improve_c"] == 0.01 and cfg.CHASE["stop_before_close_s"] == 45
    ob, o = _observer(monkeypatch, tmp_path)
    monkeypatch.setattr(ob.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("W13 must not train")))
    o._maybe_train(time.time(), 12.0); assert o.training is None
    assert isinstance(o.router, pr.MakerPaperRouter)


def test_capped_leg_never_chases_above_the_decision_limit(monkeypatch, tmp_path):
    ob, o = _observer(monkeypatch, tmp_path)
    close = 1_790_000_000; now = close - 585; tk = inputs.ticker("BTC", close)
    o._order(tk, "yes", 0.85, OB, "T-9.75", {"action": "trade", "slot": "top1", "mult": 1.0, "scenario": "S"}, PROBS, close, now, entry_source="table_top1_T-9.75", size_mult=1.0)
    p = o.st["resting"][0]; assert p["price"] == pytest.approx(0.84) and p["price_cap"] == pytest.approx(0.84) and p["limit"] == pytest.approx(0.86)
    snaps = [(now + 10, tk, {"yes_dollars": [["0.80", "50"]], "no_dollars": [["0.05", "40"], ["0.03", "500"]]}),      # touch 0.95: MAIN chases to 0.94, CAPPED stays at the 0.86 limit
             (now + 20, tk, {"yes_dollars": [["0.80", "50"]], "no_dollars": [["0.06", "40"], ["0.03", "500"]]}),      # touch 0.94: MAIN filled at 0.94, CAPPED still waits
             (now + 30, tk, {"yes_dollars": [["0.80", "50"]], "no_dollars": [["0.15", "40"], ["0.03", "500"]]})]      # touch 0.85: CAPPED filled at 0.86... at its resting price
    it = iter(snaps); monkeypatch.setattr(o, "_latest_strip", lambda coin, t: next(it, None))
    o._chase(now + 11); p = o.st["resting"][0]; assert p["price"] == pytest.approx(0.94) and p["price_cap"] == pytest.approx(0.86) and not p["main_done"]
    o._chase(now + 21); p = o.st["resting"][0]; assert p["main_done"] and not p["cap_done"] and len(o.st["fills"]) == 1 and o.st["fills"][0]["var"]["base"]["cost"] == pytest.approx(0.94)
    o._chase(now + 31); assert o.st["resting"] == [] and len(o.st["cap_fills"]) == 1 and o.st["cap_fills"][0]["price"] == pytest.approx(0.86)
    monkeypatch.setattr(ob.inputs, "official_outcomes", lambda: {tk: 1.0}); o._settle(close + 120)
    assert o.st["ledgers"]["maker_capped"]["pnl"] == pytest.approx(40 * (1 - 0.86)) and o.st["ledgers"]["base"]["pnl"] == pytest.approx(40 * (1 - 0.94))   # BTC day base 40
    # expiry with only the capped leg open counts as a capped unfilled
    o._order(inputs.ticker("ETH", close), "yes", 0.85, OB, "T-9.75", {"action": "trade", "slot": "top1", "mult": 1.0, "scenario": "S"}, PROBS, close, now, entry_source="table_top1_T-9.75", size_mult=1.0)
    monkeypatch.setattr(o, "_latest_strip", lambda coin, t: None); o._chase(close - 45)
    assert o.st["maker"]["unfilled"] == 1 and o.st["maker_capped"]["unfilled"] == 1 and o.st["resting"] == []


def test_chase_uses_freshly_fetched_bin_books_too(monkeypatch, tmp_path):
    ob, o = _observer(monkeypatch, tmp_path)
    close = 1_790_000_000; now = close - 585; tk = inputs.ticker("BTC", close)
    o._order(tk, "yes", 0.85, OB, "T-9.75", {"action": "trade", "slot": "top1", "mult": 1.0, "scenario": "S"}, PROBS, close, now, entry_source="table_top1_T-9.75", size_mult=1.0)
    monkeypatch.setattr(o, "_latest_strip", lambda coin, t: None)                    # the recorder has nothing new
    crossing = {"yes_dollars": [["0.80", "50"]], "no_dollars": [["0.16", "40"], ["0.10", "500"]]}    # touch 0.84 = our resting price
    o._chase(now + 90, books={"BTC": (now + 89, tk, crossing)})
    assert o.st["resting"] == [] and len(o.st["fills"]) == 1 and o.st["fills"][0]["var"]["base"]["cost"] == pytest.approx(0.84)


def test_resting_orders_poll_the_live_book_between_bins_but_never_at_a_bin(monkeypatch, tmp_path):
    ob, o = _observer(monkeypatch, tmp_path)
    close = 1_790_000_100; now = close - 585; tk = inputs.ticker("BTC", close)          # a real quarter-hour boundary: the poll guard works off the wall clock
    o._order(tk, "yes", 0.85, OB, "T-9.75", {"action": "trade", "slot": "top1", "mult": 1.0, "scenario": "S"}, PROBS, close, now, entry_source="table_top1_T-9.75", size_mult=1.0)
    calls = []; crossing = {"yes_dollars": [["0.80", "50"]], "no_dollars": [["0.16", "40"], ["0.10", "500"]]}
    monkeypatch.setattr(ob, "fetch_orderbook", lambda t: calls.append(t) or crossing); monkeypatch.setattr(o, "_latest_strip", lambda coin, t: None)
    assert o._poll_books(close - 495) == {} and calls == []                            # T-8.25 bin centre: production is reading, we stay away
    books = o._poll_books(close - 540); assert calls == [tk] and "BTC" in books       # rem 9.0 min: between bins
    assert o._poll_books(close - 530) == {}                                            # 10 s later: inside the 15 s poll interval
    o._chase(close - 530, books=books); assert o.st["resting"] == [] and len(o.st["fills"]) == 1
