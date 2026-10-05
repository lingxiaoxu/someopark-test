"""W11 = W7 prod rules as a paper mirror (+ overlays). No network: books are faked."""
import json, time
import numpy as np, pandas as pd, pytest
from crypto_trading.crypto_common.execution_events import EventExecutionRouter
from crypto_trading.crypto_strategies.w7_scenarios import live_plan
from crypto_trading.crypto_strategies.w11_prod_mirror import paper_router as pr, features as ft, config as cfg

LIVE_CFG = {"w7_noisefade": {"enabled": True, "window_cap_mult": 1.5}}
PROD_BAND = {"w7_noisefade": (0.79, 0.98)}
OB = {"orderbook_fp": None, "yes_dollars": [["0.80", "50"], ["0.79", "100"]], "no_dollars": [["0.15", "30"], ["0.14", "40"], ["0.10", "500"]]}   # YES costs 0.85 (30), 0.86 (40), 0.90 (500)


@pytest.fixture
def paper(monkeypatch, tmp_path):
    monkeypatch.setattr(EventExecutionRouter, "TABLE_LIVE", LIVE_CFG); monkeypatch.setattr(EventExecutionRouter, "PROD_BAND", PROD_BAND)
    monkeypatch.setattr(live_plan, "SLOTS_FILE", tmp_path / "w11_slots.json"); live_plan.reset_ledger()
    r = pr.PaperProdRouter(strategy="w7_noisefade")
    monkeypatch.setattr(r, "_utc_hour", lambda: 15); monkeypatch.setattr(r, "_trend_bp", lambda coin, lb: 0.0)
    monkeypatch.setattr(r, "_flow_gate_decision", lambda t, s: {"decision": "accept"})
    r.book = OB; return r


def test_walk_with_limit_respects_the_limit_and_depth():
    got, c = pr.walk_with_limit(OB, "yes", 25, 0.86); assert got == 25 and c == pytest.approx(0.85)          # all at the touch
    got, c = pr.walk_with_limit(OB, "yes", 60, 0.86); assert got == 60 and c == pytest.approx((30 * 0.85 + 30 * 0.86) / 60)
    got, c = pr.walk_with_limit(OB, "yes", 100, 0.86); assert got == 70 and c == pytest.approx((30 * 0.85 + 40 * 0.86) / 70)   # 0.90 level is above the limit
    assert pr.walk_with_limit(OB, "yes", 10, 0.80) == (0.0, 0.0)                         # nothing at or below the limit
    got, c = pr.walk_with_limit(OB, "no", 10, 0.25); assert got == 10 and c == pytest.approx(0.20)          # NO costs 1 - yes bid


def test_paper_router_runs_the_production_rules_and_fills_from_the_book(paper, monkeypatch):
    t = lambda slot, mult: {"action": "trade", "slot": slot, "mult": mult, "scenario": "S"}
    out = paper.submit(ticker="KXBTC15M-26OCT021945-45", side="yes", entry_price=0.85, contracts=25, armed=True, size_mult=1.5,
                       entry_source="table_top1_T-9.75", table_decision=t("top1", 1.5))
    # 2026-10-05: x1.5 is capped at x1 (W7 prod / W11 / W13) -> 40, +1c limit
    assert out["status"] == "live_sent" and out["paper"] and out["contracts"] == 40 and out["price_dollars"] == 0.86
    assert out["size_mult"] == 1.0 and out["size_mult_table"] == 1.5
    assert out["paper_fill"]["filled"] == 40 and abs(out["paper_fill"]["cost"] - (30 * 0.85 + 10 * 0.86) / 40) < 1e-9
    assert out["window_cap"] == {"cap": 60, "already": 0.0, "bought": 40.0, "total": 40.0}
    # the cap, the slot ledger and the prod band are the production code
    out2 = paper.submit(ticker="KXBTC15M-26OCT021945-45", side="yes", entry_price=0.85, contracts=25, armed=True, size_mult=1.0,
                        entry_source="table_top2_T-6.75", table_decision=t("top2", 1.0))
    assert out2["status"] == "live_sent" and out2["contracts"] == 20 and out2["window_cap"]["total"] == 60.0   # trimmed to the cap
    out3 = paper.submit(ticker="KXETH15M-26OCT021945-45", side="no", entry_price=0.785, contracts=25, armed=True, size_mult=1.0,
                        entry_source="table_top1_T-6.75", table_decision=t("top1", 1.0))
    assert out3["status"] == "skipped_by_prod_band"
    assert live_plan.used_slots("KXBTC15M-26OCT021945-45") == {"top1", "top2"}
    # a thin book: partial fill is what the ledger records
    paper.book = {"yes_dollars": [], "no_dollars": [["0.12", "7"]]}
    out4 = paper.submit(ticker="KXSOL15M-26OCT021945-45", side="yes", entry_price=0.88, contracts=25, armed=True, size_mult=1.0,
                        entry_source="table_top2_T-9.75", table_decision=t("top2", 1.0))
    assert out4["paper_fill"]["filled"] == 7 and out4["window_cap"]["bought"] == 7.0 and live_plan.exposure("KXSOL15M-26OCT021945-45") == 7.0
    # the T-8 production path (entry_source=None) consults the table inside submit
    monkeypatch.setattr(live_plan, "decide", lambda *a, **k: {"action": "skip", "reason": "table_not_this_bin"})
    paper.book = OB
    assert paper.submit(ticker="KXXRP15M-26OCT021945-45", side="yes", entry_price=0.85, contracts=25, armed=True)["status"] == "skipped_by_table"


def test_paper_router_never_touches_the_venue_or_users(paper, monkeypatch):
    monkeypatch.setattr("crypto_trading.crypto_common.kalshi.rest_event.KalshiEventOrderClient", lambda *a, **k: (_ for _ in ()).throw(AssertionError("venue call")))
    assert paper.gate_status()["live_open"] and paper.gate_status()["paper"]
    out = paper.submit(ticker="KXBTC15M-26OCT021945-45", side="yes", entry_price=0.85, contracts=25, armed=True, size_mult=1.0,
                       entry_source="table_top1_T-9.75", table_decision={"action": "trade", "slot": "top1", "mult": 1.0, "scenario": "S"})
    assert out["status"] == "live_sent" and out["paper"]
    assert paper._spawn_user_mirror({}) is None and paper._journal_user_skips({}) is None


def _synthetic(n=6, k=180):
    rng = np.random.default_rng(0); X = np.full((n, k, ft.NCH), np.nan, np.float32)
    for i in range(n):
        px = 100 * np.exp(np.cumsum(rng.normal(0, 1e-4, k)))
        X[i, :, 0] = px; X[i, :, 1] = px * 1.0001; X[i, :, 2] = px; X[i, :, 3] = px; X[i, :, 4] = 1e-4; X[i, :, 5] = 1.25e-5; X[i, :, 6] = 1000 + np.arange(k)
        X[i, :, 7] = px - 0.01; X[i, :, 8] = px + 0.01; X[i, :, 9] = 50; X[i, :, 10] = 40; X[i, :, 11] = 10; X[i, :, 12] = 8
        X[i, :, 13] = rng.normal(0, 100, k); X[i, :, 14] = np.abs(X[i, :, 13]) + 50; X[i, :, 15] = 3; X[i, :, 16] = 60; X[i, :, 17] = 0.8 + i * 0.01; X[i, :, 18] = 40
    M = pd.DataFrame(dict(coin=list(cfg.COINS) + ["BTC"], close_ts=[1_790_000_000] * 5 + [1_790_000_900], strike=100.0, strike_next=[100.5, 99.5, 100.2, 100.0, 101, 99],
                          rv60_prior=30.0, rv15_prior=15.0, hour=12, dow=2, dev8h=0.0))
    return X, M


def test_features_are_causal_and_complete():
    X, M = _synthetic()
    F = ft.feats(X, M, 63)
    assert list(F.coin) == list(M.coin) and set(ft.ALL) <= set(F.columns) and F[ft.ALL].iloc[:5].notna().all().all() and "z" in ft.NOPRICE
    assert F.x_dist.iloc[5] != F.x_dist.iloc[5]                                                    # a lone window has no cross-coin context
    X2 = X.copy(); X2[:, 63:, :] = 777.0                                               # the future must not matter
    F2 = ft.feats(X2, M, 63); pd.testing.assert_frame_equal(F[ft.ALL], F2[ft.ALL])
    assert np.isnan(F.y_yes.iloc[3]) and F.y_yes.iloc[0] == 1.0 and F.y_yes.iloc[1] == 0.0         # tie -> no label
    assert abs(F.x_dist.iloc[0] - F.dist.iloc[1:5].mean()) < 1e-6                                   # cross-coin = the other 4 coins of the same window
    C, med = ft.channels(X, M); assert C.shape == (6, 180, 8) and np.isfinite(C).all()
    assert ft.static(F).shape == (6, 11)


def test_window_array_buckets_trades_and_masks_future(tmp_path):
    o = 1_790_000_000 - 900; I = np.array([[o + 5 * i, 100 + i * 0.01] for i in range(1, 181)]); C = np.zeros((0, 7)); B = np.zeros((0, 7)); D = np.zeros((0, 2))
    T = np.array([[o + 7.0, 10.0, 10.0], [o + 8.0, -4.0, 4.0], [o + 899.0, 3.0, 3.0]])
    Sx = np.array([[o + 1.0, 0.8, 1_790_000_000, 100.0], [o + 400.0, 0.9, 1_790_000_000, 100.0]])
    X = ft.window_array(1_790_000_000, I, C, T, B, Sx, D)
    assert X.shape == (180, 19) and X[1, 13] == 6.0 and X[1, 14] == 14.0 and X[1, 15] == 2 and X[179, 13] == 3.0   # bucket 2 = (open+5, open+10]
    assert X[0, 17] == pytest.approx(0.8) and X[90, 17] == pytest.approx(0.9) and np.isnan(X[100, 17]) and np.isnan(X[0, 18])   # 100 s max age


def test_live_arrays_match_training_builder_for_a_recorded_window():
    """The live tail reader and the training builder must produce the same channels for the same window."""
    import os, glob
    from crypto_trading.crypto_strategies.w11_prod_mirror.config import STRIPS, INDEX
    files = sorted(glob.glob(str(STRIPS / "KXBTC15M" / "orderbook" / "*.jsonl")))
    if not files: pytest.skip("no live strips recording")
    day = os.path.basename(files[-1])[:-6]
    rows = ft.load_jsonl(STRIPS / "KXBTC15M" / "orderbook" / f"{day}.jsonl", ft.STRIP_FN, 400_000)
    closes = sorted({ct for _, _, ct, _ in rows}); now = time.time()
    done = [c for c in closes if c + 120 < now]
    if len(done) < 1: pytest.skip("no completed window today")
    close = done[-1]
    X, M = ft.live_arrays(close, close + 60, cfg.COINS)
    assert X.shape == (5, 180, 19) and list(M.coin) == list(cfg.COINS)
    F = ft.feats(X, M, 63); assert F[ft.NOPRICE].drop(columns=["dvol"]).notna().mean().mean() > 0.9
    assert (F.dist.abs() < 500).all()


def test_variant_logic_and_settlement(monkeypatch, tmp_path):
    from crypto_trading.crypto_strategies.w11_prod_mirror import observer as ob
    monkeypatch.setattr(ob, "OUT", tmp_path); monkeypatch.setattr(ob, "STATE", tmp_path / "state.json"); monkeypatch.setattr(ob, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(ob, "SLOTS", tmp_path / "slots.json"); monkeypatch.setattr(ob.cfg, "MODELS", tmp_path / "models")
    monkeypatch.setattr(ob, "latest_stamp", lambda: None)
    monkeypatch.setattr(EventExecutionRouter, "TABLE_LIVE", LIVE_CFG); monkeypatch.setattr(EventExecutionRouter, "PROD_BAND", PROD_BAND)
    o = ob.Observer(chronos=False, use_worker=False)
    monkeypatch.setattr(o.router, "_utc_hour", lambda: 15); monkeypatch.setattr(o.router, "_trend_bp", lambda c, lb: 0.0)
    monkeypatch.setattr(o.router, "_flow_gate_decision", lambda t, s: {"decision": "accept"})
    from crypto_trading.crypto_strategies.w7_scenarios import inputs
    close = 1_790_000_000; tk = inputs.ticker("BTC", close)
    probs = {"p_fair": 0.90, "p_lgbm": 0.80, "p_lgbm_np": 0.88, "p_gru": 0.90, "p_gru_np": None, "p_chronos": None, "p_chronos_base": 0.86, "p_blend1": 0.86, "p_blend2": 0.84, "z": 0.5}
    o.models = {"meta": {"z_terciles": [1.0, 1.5]}}
    d = {"action": "trade", "slot": "top1", "mult": 1.5, "scenario": "横盘·低波动"}
    out = o._order(tk, "yes", 0.85, OB, "T-9.75", d, probs, close, close - 585, entry_source="table_top1_T-9.75", size_mult=1.5)
    assert out["status"] == "live_sent"; f = o.st["fills"][-1]; v = f["var"]
    assert v["base"]["n"] == 40 and v["f_lgbm"]["take"] is False and v["f_gru"]["take"] and v["f_chronos"]["take"] is False and v["f_fair"]["take"]
    assert v["f_lgbm_np"]["take"] and v["f_gru_np"]["take"] is False and v["f_chronos_base"]["take"] and v["f_blend1"]["take"] and v["f_blend2"]["take"] is False
    assert v["eq_size"]["n"] == 40 and v["floor82"]["take"] and v["f_hour"]["take"] and v["f_zrule"]["take"]         # price 0.85: neither z rule applies
    assert v["size_kelly_mkt"]["n"] == 40 and v["size_edge"]["n"] == 20 and v["cap_total"]["n"] == 40                 # 0.85 -> x1; edge -5c -> x0.5; 40 <= 120
    assert v["night_full"]["n"] == 40                                                                                 # day band: identical to base
    # night band (ETH): base 20 (x1.5 capped at x1); night_full uses the day base 30 x1 -> 30 (cap 45)
    monkeypatch.setattr(o.router, "_utc_hour", lambda: 3)
    tk2 = inputs.ticker("ETH", close)
    out2 = o._order(tk2, "yes", 0.85, OB, "T-9.75", d, probs, close, close - 585, entry_source="table_top1_T-9.75", size_mult=1.5)
    v2 = o.st["fills"][-1]["var"]; assert v2["base"]["n"] == 20 and v2["night_full"]["n"] == 30 and v2["eq_size"]["n"] == 20   # ETH: night 20; day base 30 (no x1.5)
    # settlement: YES wins -> every taken variant books n*(1-cost-fee)
    monkeypatch.setattr(ob.inputs, "official_outcomes", lambda: {tk: 1.0})
    o._settle(close + 120)
    f = next(x for x in o.st["fills"] if x["ticker"] == tk); assert f["settled"] and f["won"] == 1.0 and f["pnl"]["f_lgbm"] == 0.0 and f["pnl"]["base"] > 0
    assert o.st["ledgers"]["base"]["trades"] == 1 and o.st["ledgers"]["f_lgbm"]["trades"] == 0 and o.st["ledgers"]["eq_size"]["contracts"] == 40
    assert abs(f["pnl"]["base"] - 40 * (1 - v["base"]["cost"] - v["base"]["fee"])) < 1e-6


def test_t8_leg_follows_w7_paper_entries_only_in_main(monkeypatch, tmp_path):
    from crypto_trading.crypto_strategies.w11_prod_mirror import observer as ob
    monkeypatch.setattr(ob, "OUT", tmp_path); monkeypatch.setattr(ob, "STATE", tmp_path / "state.json"); monkeypatch.setattr(ob, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(ob, "SLOTS", tmp_path / "slots.json"); monkeypatch.setattr(ob, "W7_LOG_DIR", tmp_path / "w7"); (tmp_path / "w7").mkdir()
    monkeypatch.setattr(ob, "latest_stamp", lambda: None)
    monkeypatch.setattr(EventExecutionRouter, "TABLE_LIVE", LIVE_CFG); monkeypatch.setattr(EventExecutionRouter, "PROD_BAND", PROD_BAND)
    o = ob.Observer(chronos=False, use_worker=False)
    monkeypatch.setattr(o.router, "_utc_hour", lambda: 15); monkeypatch.setattr(o.router, "_trend_bp", lambda c, lb: 0.0)
    monkeypatch.setattr(o.router, "_flow_gate_decision", lambda t, s: {"decision": "accept"})
    monkeypatch.setattr(o, "_book", lambda tk: ({"yes_bid": 0.84, "yes_ask": 0.86}, OB))
    monkeypatch.setattr(o, "_model_rows", lambda *a, **k: {c: {"p_fair": 0.9, "p_lgbm": 0.9, "p_gru": None, "p_chronos": None} for c in cfg.COINS})
    monkeypatch.setattr(live_plan, "decide", lambda *a, **k: {"action": "trade", "slot": "top1", "mult": 1.0, "scenario": "S", "bin": "T-8.25"})
    now = time.time(); close = int((now // 900 + 1) * 900); tk = live_plan.close_ts_from_ticker  # noqa
    from crypto_trading.crypto_strategies.w7_scenarios import inputs
    tkr = inputs.ticker("BTC", close); day = time.strftime("%Y-%m-%d", time.gmtime(now)); p = tmp_path / "w7" / f"log_{day}.jsonl"
    p.write_text(json.dumps({"ts": now, "strategy": "w7_noisefade", "action": "paper_entry", "leg": "band", "ticker": "KXBTC15M-OLD", "side": "yes", "cost": 0.85}) + "\n")
    o._t8_leg(close, now)                     # first poll starts at the end of the file: the old row is never replayed
    assert o.st["fills"] == []
    with open(p, "a") as fh:
        fh.write(json.dumps({"ts": now, "strategy": "w7_noisefade", "action": "paper_entry", "leg": "band", "ticker": tkr, "side": "yes", "cost": 0.85}) + "\n")
        fh.write(json.dumps({"ts": now, "strategy": "w7_noisefade", "action": "paper_entry", "leg": "band", "ticker": inputs.ticker("ETH", close), "side": "yes", "cost": 0.70}) + "\n")
    o._t8_leg(close, now)
    assert len(o.st["fills"]) == 1 and o.st["fills"][0]["ticker"] == tkr and o.st["fills"][0]["entry_source"] == "table_top1_T-8.25"
    o._t8_leg(close, now); assert len(o.st["fills"]) == 1          # no double handling


def test_paper_write_paths_are_w11s_own():
    assert str(cfg.OUT).endswith("w11_prod_mirror") and cfg.SLOTS.parent == cfg.OUT and cfg.MODELS.parent == cfg.OUT and cfg.DATA.parent == cfg.OUT


def test_model_worker_protocol(tmp_path):
    """The torch worker answers a predict request on synthetic data (GRU absent -> None, Chronos if cached)."""
    import subprocess, sys, os, json
    from crypto_trading.crypto_strategies.w11_prod_mirror import observer as ob
    X, M = _synthetic(5); M = M.iloc[:5]
    ctx = np.stack([100 + np.cumsum(np.random.default_rng(i).normal(0, 0.01, 400)) for i in range(5)])
    np.savez(tmp_path / "req.npz", X=X, ctx=ctx, **{c: M[c].values for c in ("coin", "close_ts", "strike", "rv60_prior", "rv15_prior", "hour", "dow", "dev8h")})
    env = dict(os.environ, PYTHONPATH=str(cfg.OUT.parents[2]), HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", OMP_NUM_THREADS="2")
    w = subprocess.Popen([sys.executable, "-m", "crypto_trading.crypto_strategies.w11_prod_mirror.model_worker"], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    w.stdin.write(json.dumps({"cmd": "predict", "id": 7, "npz": str(tmp_path / "req.npz"), "k": 63, "horizon": 59}) + "\n"); w.stdin.flush()
    msg = None
    for _ in range(50):
        line = w.stdout.readline()
        if not line: break
        m = json.loads(line)
        if m.get("id") == 7: msg = m; break
    w.stdin.write(json.dumps({"cmd": "quit"}) + "\n"); w.stdin.flush(); w.wait(timeout=30)
    assert msg and msg["ok"] and msg["p_gru"] == [None] * 5 and len(msg["p_chronos"]) == 5 and len(msg["p_chronos_base"]) == 5 and len(msg["chronos_dir_bp"]) == 5
    assert all(v is None or 0.0 <= v <= 1.0 for v in msg["p_chronos"] + msg["p_chronos_base"])


def test_scanner_waits_for_productions_read_then_fires(monkeypatch, tmp_path):
    """Inside a bin W11 fires only after the live scanner's decision rows appear (1-2 s later), or 2 s after
    production's latest possible trigger when it logged nothing."""
    from crypto_trading.crypto_strategies.w11_prod_mirror import observer as ob
    from crypto_trading.crypto_strategies.w7_scenarios import inputs
    monkeypatch.setattr(ob, "OUT", tmp_path); monkeypatch.setattr(ob, "STATE", tmp_path / "state.json"); monkeypatch.setattr(ob, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(ob, "SLOTS", tmp_path / "slots.json"); monkeypatch.setattr(ob, "W7_LOG_DIR", tmp_path / "w7"); (tmp_path / "w7").mkdir()
    monkeypatch.setattr(ob, "latest_stamp", lambda: None)
    monkeypatch.setattr(EventExecutionRouter, "TABLE_LIVE", LIVE_CFG); monkeypatch.setattr(EventExecutionRouter, "PROD_BAND", PROD_BAND)
    o = ob.Observer(chronos=False, use_worker=False)
    monkeypatch.setattr(o, "_settle", lambda now: None); monkeypatch.setattr(o, "_t8_leg", lambda close, now: None); monkeypatch.setattr(o, "_maybe_train", lambda now, rem: None)
    reads = []
    monkeypatch.setattr(o, "_book", lambda tk: (reads.append(tk), None)[1:] + (None,) if False else (reads.append(tk) or None, None))   # book unavailable -> no decisions, but we see the read
    monkeypatch.setattr(o, "_model_rows", lambda *a, **k: {})
    now0 = time.time(); close = int((now0 // 900 + 1) * 900); centre = close - 9.75 * 60
    clock = {"t": centre - 5}                                                             # inside the bin (prod's earliest trigger is centre - 6)
    monkeypatch.setattr(ob.time, "time", lambda: clock["t"])
    assert o.tick()["status"] == "WAIT" and reads == []                                   # production has not read yet
    day = time.strftime("%Y-%m-%d", time.gmtime(clock["t"])); p = tmp_path / "w7" / f"log_{day}.jsonl"
    p.write_text(json.dumps({"ts": clock["t"] - 0.5, "strategy": "w7_table", "action": "table_decision", "ticker": inputs.ticker("ETH", close), "bin": "T-9.75", "decision": "skip"}) + "\n")
    assert o.tick()["status"] == "WAIT" and reads == []                                   # < 1 s after production's row
    clock["t"] = centre - 3.5
    rep = o.tick(); assert rep["status"] == "SCANNED" and rep["sync_gap_s"] == 2.0 and len(reads) == 5   # 2 s after production's read
    # a bin where production logged nothing: fire at centre + 6 s
    o.st["done"] = {}; reads.clear(); p.write_text(""); clock["t"] = centre + 3
    assert o.tick()["status"] == "WAIT" and reads == []
    clock["t"] = centre + 6.5
    rep = o.tick(); assert rep["status"] == "SCANNED" and rep["sync_gap_s"] is None and len(reads) == 5


def test_macro_guard_clamps_every_w11_variant(monkeypatch, tmp_path):
    from crypto_trading.crypto_common import macro_calendar as mc
    from crypto_trading.crypto_strategies.w11_prod_mirror import observer as ob
    monkeypatch.setattr(ob, "OUT", tmp_path); monkeypatch.setattr(ob, "STATE", tmp_path / "state.json"); monkeypatch.setattr(ob, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(ob, "SLOTS", tmp_path / "slots.json"); monkeypatch.setattr(ob.cfg, "MODELS", tmp_path / "models"); monkeypatch.setattr(ob, "latest_stamp", lambda: None)
    monkeypatch.setattr(EventExecutionRouter, "TABLE_LIVE", LIVE_CFG); monkeypatch.setattr(EventExecutionRouter, "PROD_BAND", PROD_BAND)
    o = ob.Observer(chronos=False, use_worker=False)
    monkeypatch.setattr(o.router, "_utc_hour", lambda: 15); monkeypatch.setattr(o.router, "_trend_bp", lambda c, lb: 0.0); monkeypatch.setattr(o.router, "_flow_gate_decision", lambda t, s: {"decision": "accept"})
    monkeypatch.setattr(mc, "active", lambda now=None, after_s=2700, before_s=0: {"key": "nfp", "name": "NFP", "t_utc": time.time() - 60, "until": time.time() + 2640, "source": "fred"})
    from crypto_trading.crypto_strategies.w7_scenarios import inputs
    close = 1_790_000_000; tk = inputs.ticker("BTC", close); o.models = {"meta": {"z_terciles": [1.0, 1.5]}}
    probs = {"p_fair": 0.90, "p_lgbm": 0.80, "p_lgbm_np": 0.88, "p_gru": 0.90, "p_gru_np": None, "p_chronos": None, "p_chronos_base": 0.86, "p_blend1": 0.86, "p_blend2": 0.84, "z": 0.5}
    out = o._order(tk, "yes", 0.85, OB, "T-9.75", {"action": "trade", "slot": "top1", "mult": 1.5, "scenario": "S"}, probs, close, close - 585, entry_source="table_top1_T-9.75", size_mult=1.5)
    assert out["contracts"] == 5 and out["macro"]["event"] == "nfp"
    v = o.st["fills"][-1]["var"]; assert v["base"]["n"] == 5 and all(x["n"] <= 5 for x in v.values()) and v["eq_size"]["n"] == 5 and v["night_full"]["n"] == 5
