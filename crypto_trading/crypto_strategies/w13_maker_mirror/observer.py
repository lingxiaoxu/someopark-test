"""W13 observer = W11 with MAKER execution. Identical decisions (prod rules, table, variants, models), but every
order is a simulated RESTING order: posted at touch - 1c, re-posted at the new touch - 1c on every strips book refresh
(~10 s) until 45 s before the close, filled when the book crosses our price (conservative proxy), maker fee 0.
Unfilled orders are kept and settled hypothetically (adverse-selection check), and the taker counterfactual
(what W11 would have got from the same book) is settled alongside. Paper only - never sends.

W11 observer docstring follows: the W7 PROD rules as a paper mirror + the research overlays, every window.

Per 15-minute window and coin it does exactly what production does, with its own ledger:
  * T-9.75 / T-6.75 / T-5.25: the scanner rule (live book, favourite, prod band, scenario
    table top1/top2, sizes, cap, slots) -> a simulated IOC fill against that book;
  * T-8: triggered by W7's own paper entry (read from the live_watch log tail, the same
    cost W7 priced) -> the production T-8 table decision inside submit();
and, at every bin for every coin, it records the market price with the fair value and the
LightGBM / GRU / Chronos probabilities, then settles everything on the official result.
Variants (filters / sizing) are evaluated on the same fills. Paper only - never sends.

    python -m crypto_trading.crypto_strategies.w13_maker_mirror.observer [--once] [--loop 10]
"""
from __future__ import annotations
import argparse, json, logging, os, subprocess, sys, time, traceback
from datetime import datetime, timezone
from pathlib import Path
import numpy as np, pandas as pd
from . import config as cfg
from .config import (NAME, OUT, STATE, SLOTS, LOG_DIR, REPORT, MODELS, COINS, BINS, STEP, CONTRACTS_PAPER, LOOP_S, T8_POLL_S,
                     RETRAIN_EVERY_S, VARIANTS, HOUR_SKIP, FLOOR82, CAP_TOTAL, CHASE, TRAIN_ENABLED, STRIPS)
from .features import feats, live_arrays, index_series_10s, ALL
from .models import load_models, latest_stamp, lgbm_predict, logistic_predict
from .features import NOPRICE
import select
from .paper_router import MakerPaperRouter, walk_with_limit, top_cost
from crypto_trading.crypto_strategies.w7_scenarios import live_plan, inputs
from crypto_trading.crypto_strategies.live_watch.w7_noisefade import fetch_orderbook, walk_ladder, favorite_side, MAIN_LO, MAIN_HI
from crypto_trading.crypto_common.execution_events import EventExecutionRouter
from crypto_trading.crypto_common.config import SIGNALS_DIR

logger = logging.getLogger(NAME)
W7_LOG_DIR = SIGNALS_DIR / "live_watch"
VERSION = "w13_maker_mirror_v1"


# ───────────── state / log ─────────────
def _load_state() -> dict:
    try: st = json.loads(STATE.read_text())
    except (OSError, ValueError): st = {}
    st.setdefault("version", VERSION); st.setdefault("registered_at", datetime.now(timezone.utc).isoformat())
    for k, v in (("done", {}), ("t8_done", {}), ("t8_cursor", {}), ("fills", []), ("model_rows", []), ("var_filled", {}), ("win_total", {}), ("errors", []), ("cycles", 0),
                 ("ledgers", {}), ("resting", []), ("unfilled", []), ("cap_fills", []),
                 ("maker_capped", {"attempted": 0, "filled": 0, "unfilled": 0}),
                 ("maker", {"attempted": 0, "filled": 0, "unfilled": 0, "improve_c_sum": 0.0, "ttf_sum": 0.0, "reposts_sum": 0, "taker_equiv_pnl": 0.0, "taker_equiv_n": 0,
                            "unfilled_settled": 0, "unfilled_would_won": 0, "unfilled_taker_pnl": 0.0, "filled_won": 0, "filled_settled": 0})):
        st.setdefault(k, v)
    for v_ in VARIANTS:                                   # a variant added later starts its own ledger from zero
        st["ledgers"].setdefault(v_, {"trades": 0, "contracts": 0.0, "pnl": 0.0, "won": 0, "lost": 0, "fees": 0.0})
    return st


def _save_state(st: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True); tmp = STATE.with_suffix(".tmp"); tmp.write_text(json.dumps(st, default=str)); os.replace(tmp, STATE)


def log_row(row: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    p = LOG_DIR / f"log_{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"
    with open(p, "a") as fh: fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **row}, default=str) + "\n")


def _trim(d: dict, n=400) -> dict:
    return {k: (v if len(json.dumps(v, default=str)) <= n else str(v)[:n]) for k, v in d.items()}


# ───────────── the observer ─────────────
class Observer:
    def __init__(self, chronos: bool = True, use_worker: bool | None = None):
        OUT.mkdir(parents=True, exist_ok=True)
        live_plan.SLOTS_FILE = SLOTS; live_plan.reset_ledger()           # W11's OWN slot/cap ledger, same code as prod
        self.router = MakerPaperRouter(strategy="w7_noisefade"); self.router.improve_c = float(CHASE["improve_c"]); self._strip_cache = {}
        self.st = _load_state(); self.models = None; self.model_stamp = None
        self.training: subprocess.Popen | None = None; self.cache = {}
        # torch models (GRU, Chronos) run in model_worker, a separate process: torch's libomp deadlocks
        # with LightGBM's in one process on macOS, and a slow forecast must never delay the observer.
        self.use_worker = chronos if use_worker is None else use_worker; self.worker = None; self.worker_started = 0.0; self.req_id = 0
        self._reload_models()

    # ---- torch worker
    def _worker_start(self) -> None:
        now = time.time()
        if self.worker is not None and self.worker.poll() is None: return
        if now - self.worker_started < 60: return
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[3]), OMP_NUM_THREADS="2", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", PYTHONUNBUFFERED="1")
        self.worker = subprocess.Popen([sys.executable, "-m", "crypto_trading.crypto_strategies.w13_maker_mirror.model_worker"], env=env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=open(OUT / "worker.log", "a"), text=True, bufsize=1)
        self.worker_started = now; logger.info("[%s] model worker started (pid %s)", NAME, self.worker.pid)
        if self.model_stamp: self._worker_call({"cmd": "reload", "stamp": self.model_stamp}, 60)

    def _worker_call(self, req: dict, timeout: float) -> dict | None:
        if self.worker is None or self.worker.poll() is not None: return None
        try:
            self.worker.stdin.write(json.dumps(req) + "\n"); self.worker.stdin.flush()
            deadline = time.time() + timeout
            while time.time() < deadline:
                r, _, _ = select.select([self.worker.stdout], [], [], max(0.05, deadline - time.time()))
                if not r: break
                line = self.worker.stdout.readline()
                if not line: return None
                try: msg = json.loads(line)
                except ValueError: continue
                if "event" in msg: logger.info("[%s] worker: %s", NAME, msg); continue
                if req.get("cmd") == "predict" and msg.get("id") != req.get("id"): continue      # a stale reply from an earlier timed-out request
                return msg
            self._err(f"worker timeout ({req.get('cmd')}, {timeout}s)"); return None
        except (OSError, ValueError, BrokenPipeError) as e:
            self._err(f"worker call failed: {e}"); return None

    def _err(self, msg: str) -> None:
        logger.warning("[%s] %s", NAME, msg); self.st["errors"] = (self.st["errors"] + [{"at": time.time(), "msg": msg[:300]}])[-50:]

    # ---- models
    def _reload_models(self) -> None:
        if self.use_worker: self._worker_start()
        stamp = latest_stamp()
        if stamp and stamp != self.model_stamp:
            try:
                self.models = load_models(stamp); self.model_stamp = stamp; self.st["model_stamp"] = stamp; logger.info("[%s] models %s loaded", NAME, stamp)
                if self.use_worker: self._worker_call({"cmd": "reload", "stamp": stamp}, 60)
            except Exception as e: self._err(f"model load failed {stamp}: {e}")      # noqa: BLE001

    def _maybe_train(self, now: float, rem: float) -> None:
        if not TRAIN_ENABLED: return                                     # W13 scores W11's models, never trains
        if self.training is not None:
            if self.training.poll() is None: return
            self.training = None; self._reload_models()
        meta_at = None
        try: meta_at = datetime.fromisoformat(json.loads((MODELS / "latest.json").read_text())["at"]).timestamp()
        except Exception: pass                                            # noqa: BLE001
        if meta_at is not None and now - meta_at < RETRAIN_EVERY_S: return
        if not (11.0 <= rem <= 14.5): return                                # a quiet part of the window
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[3]))
        self.training = subprocess.Popen([sys.executable, "-m", "crypto_trading.crypto_strategies.w13_maker_mirror.train"], env=env,
                                         stdout=open(OUT / "train.log", "a"), stderr=subprocess.STDOUT, preexec_fn=lambda: os.nice(15))
        logger.info("[%s] retraining started (pid %s)", NAME, self.training.pid)

    # ---- features + model probabilities for all coins at this moment
    def _probs(self, close: int, now: float, k: int) -> pd.DataFrame | None:
        key = (close, k)
        if key in self.cache and now - self.cache[key][0] < 90: return self.cache[key][1]
        try:
            dev = {}
            try:
                for c in COINS: dev[c] = live_plan.dev8h_at_open(c, close)
            except Exception: pass                                        # noqa: BLE001
            X, M = live_arrays(close, now, COINS, dev); F = feats(X, M, k)
            for c_ in ("p_lgbm", "p_lgbm_np", "p_dir", "mag_bp", "p_blend1", "p_blend2", "p_gru", "p_gru_np", "p_chronos", "p_chronos_base", "chronos_dir_bp"): F[c_] = np.nan
            if self.models:
                try:
                    F["p_lgbm"] = lgbm_predict(self.models["lgbm"], F)
                    if self.models.get("lgbm_np") is not None: F["p_lgbm_np"] = lgbm_predict(self.models["lgbm_np"], F, NOPRICE)
                    if self.models.get("lgbm_dir") is not None: F["p_dir"] = lgbm_predict(self.models["lgbm_dir"], F, NOPRICE)
                    if self.models.get("lgbm_mag") is not None: F["mag_bp"] = lgbm_predict(self.models["lgbm_mag"], F, NOPRICE)
                    for nm in ("blend1", "blend2"):
                        if nm in (self.models["meta"].get("blends") or {}): F["p_" + nm] = logistic_predict(self.models["meta"]["blends"][nm], F)
                except Exception as e: self._err(f"lgbm/blend predict: {e}")       # noqa: BLE001
            if self.use_worker and self.worker is not None:
                try:
                    t_entry = close - 900 + STEP * k; ctx = np.stack([index_series_10s(c, t_entry - 3600, t_entry, now) for c in COINS])
                    tmp = OUT / "tmp"; tmp.mkdir(exist_ok=True); self.req_id += 1; path = tmp / f"req_{self.req_id}.npz"
                    np.savez(path, X=X, ctx=ctx, **{c: M[c].values for c in ("coin", "close_ts", "strike", "rv60_prior", "rv15_prior", "hour", "dow", "dev8h")})
                    msg = self._worker_call({"cmd": "predict", "id": self.req_id, "npz": str(path), "k": int(k), "horizon": int(round((900 - STEP * k) / 10))}, 8.0)
                    try: path.unlink()
                    except OSError: pass
                    if msg and msg.get("ok"):
                        for key_, col_ in (("p_gru", "p_gru"), ("p_gru_np", "p_gru_np"), ("p_chronos", "p_chronos"), ("p_chronos_base", "p_chronos_base"), ("chronos_dir_bp", "chronos_dir_bp")):
                            if key_ in msg: F[col_] = [np.nan if v is None else v for v in msg[key_]]
                    elif msg: self._err(f"worker error: {msg.get('err')}")
                except Exception as e: self._err(f"worker predict: {e}")      # noqa: BLE001
            self.cache = {kk: v for kk, v in self.cache.items() if now - v[0] < 900}; self.cache[key] = (now, F)
            return F
        except Exception as e:                                            # noqa: BLE001
            self._err(f"features failed: {e}"); return None

    def _model_rows(self, close: int, now: float, b: str, k: int, books: dict) -> dict:
        """Record market vs model probabilities for every coin at this bin; returns {coin: probs}."""
        F = self._probs(close, now, k); out = {}
        for c in COINS:
            bk = books.get(c); kyes_live = np.nan
            if bk:
                side = favorite_side((bk["yes_bid"] + bk["yes_ask"]) / 2.0)
                if side:
                    cost = bk[side].get("fill_cost") or bk[side].get("top_cost"); kyes_live = cost if side == "yes" else 1 - cost
            r = F[F.coin == c].iloc[0] if F is not None and (F.coin == c).any() else None
            g = lambda col, nd=4: None if r is None or pd.isna(r[col]) else round(float(r[col]), nd)
            probs = {"p_mkt_yes": None if np.isnan(kyes_live) else round(float(kyes_live), 4), "kyes_snap": g("kyes"),
                     # binary predictors (yes axis)
                     "p_fair": g("p_fair_prior"), "p_fair_in": g("p_fair_in"), "p_fair_dv": g("p_fair_dv"), "p_blend1": g("p_blend1"), "p_blend2": g("p_blend2"),
                     "p_lgbm": g("p_lgbm"), "p_lgbm_np": g("p_lgbm_np"), "p_gru": g("p_gru"), "p_gru_np": g("p_gru_np"), "p_chronos": g("p_chronos"), "p_chronos_base": g("p_chronos_base"),
                     # remaining-move predictors and single factors (direction / magnitude)
                     "p_dir": g("p_dir"), "mag_bp": g("mag_bp", 2), "chronos_dir_bp": g("chronos_dir_bp", 2),
                     "dist_bp": g("dist", 2), "z": g("z", 3), "f3": g("f3", 3), "imb5": g("imb5", 3), "dk": g("dk", 4), "mom1": g("mom1", 2), "mom_all": g("mom_all", 2),
                     "idx_e": g("idx_e", 6), "model": self.model_stamp}
            out[c] = probs
            row = {"action": "w13_model", "coin": c, "ticker": inputs.ticker(c, close), "close_ts": close, "bin": b, "k": k, "at": now, **probs, "y_yes": None, "y_rem_bp": None}
            self.st["model_rows"].append(row); log_row(row)
        return out

    # ---- books
    def _book(self, ticker: str):
        ob = fetch_orderbook(ticker)
        if ob is None: return None, None
        out = {}
        for side in ("no", "yes"):
            w = walk_ladder(ob, side, CONTRACTS_PAPER)
            if w is None: return None, ob
            out[side] = w
        yb, ya = round(1.0 - out["no"]["top_cost"], 4), round(out["yes"]["top_cost"], 4)
        if not (0.0 < yb < 1.0 and 0.0 < ya < 1.0): return None, ob
        out["yes_bid"], out["yes_ask"] = yb, ya
        return out, ob

    # ---- variant plan (take / size) from the decision-time context; the fill (maker) comes later
    def _variant_plan(self, out: dict, probs: dict, side: str, ticker: str, close: int) -> tuple[dict, dict]:
        sizing = EventExecutionRouter.PROD_SIZING["w7_noisefade"]; band = "night" if self.router._utc_hour() in sizing["night_hours_utc"] else "day"
        base_n = int(sizing[band].get(ticker.split("-", 1)[0], sizing[band]["default"])); day_n = int(sizing["day"].get(ticker.split("-", 1)[0], sizing["day"]["default"]))
        paper_price = float(out["paper_price_dollars"]); hour = datetime.fromtimestamp(close, timezone.utc).hour
        MODELS_ = ("p_fair", "p_blend1", "p_blend2", "p_lgbm", "p_lgbm_np", "p_gru", "p_gru_np", "p_chronos", "p_chronos_base")
        p_fav = {m: (None if probs.get(m) is None else (probs[m] if side == "yes" else 1 - probs[m])) for m in MODELS_}
        ge = lambda m, thr=0.0: p_fav.get(m) is not None and p_fav[m] - paper_price >= thr
        zq = (self.models or {}).get("meta", {}).get("z_terciles") if self.models else None; z = probs.get("z")
        zrule_bad = (zq is not None and z is not None and ((paper_price >= 0.90 and z <= zq[0]) or (paper_price < 0.84 and z > zq[1])))
        wkey = str(close); wt = self.st["win_total"].setdefault(wkey, 0.0); plan = {}
        for v in VARIANTS:
            take, n = True, int(out["contracts"])
            if v == "f_lgbm": take = ge("p_lgbm")
            elif v == "f_lgbm_01": take = ge("p_lgbm", 0.01)
            elif v == "f_lgbm_02": take = ge("p_lgbm", 0.02)
            elif v == "f_lgbm_np": take = ge("p_lgbm_np")
            elif v == "f_gru": take = ge("p_gru")
            elif v == "f_gru_np": take = ge("p_gru_np")
            elif v == "f_chronos": take = ge("p_chronos")
            elif v == "f_chronos_base": take = ge("p_chronos_base")
            elif v == "f_fair": take = ge("p_fair")
            elif v == "f_blend1": take = ge("p_blend1")
            elif v == "f_blend2": take = ge("p_blend2")
            elif v == "f_hour": take = hour not in HOUR_SKIP
            elif v == "floor82": take = paper_price >= FLOOR82
            elif v == "f_zrule": take = not zrule_bad
            elif v == "eq_size": n = base_n
            elif v == "size_kelly_mkt": n = int(base_n * (1.5 if paper_price >= 0.90 else 1.0 if paper_price >= 0.85 else 0.5) + 0.5)
            elif v == "size_edge": n = int(base_n * float(np.clip(1 + ((p_fav["p_lgbm"] - paper_price) * 20 if p_fav.get("p_lgbm") is not None else 0.0), 0.5, 1.5)) + 0.5)
            elif v == "cap_total": n = int(min(n, max(0, CAP_TOTAL[band] - wt)))
            elif v == "night_full":
                n = int(round(day_n * float(out.get("size_mult") or 1.0) * (0.25 if (out.get("no_dump") or {}).get("mult") == 0.25 else 1.0))) if band == "night" else int(out["contracts"])
            elif v == "combo": take = ge("p_lgbm") and hour not in HOUR_SKIP; n = base_n
            if out.get("macro") and n > int(out["macro"].get("contracts") or 5): n = int(out["macro"]["contracts"])   # the prod macro-release guard applies to every variant
            if v != "base":
                cap = int((day_n if v == "night_full" else base_n) * (2.0 if v in ("eq_size", "combo", "size_kelly_mkt", "size_edge") else 1.5) + 1e-9)
                already = self.st["var_filled"].setdefault(v, {}).get(ticker, 0.0); n = int(min(n, max(0, cap - already)))
            plan[v] = {"take": bool(take and n >= 1), "n": int(n)}
        return plan, {"base_n": base_n, "day_n": day_n, "band": band, "hour": hour, "paper_price": paper_price}

    # ---- one paper order through the production rules -> a RESTING order chased by _chase()
    def _order(self, ticker: str, side: str, cost: float, ob: dict, b: str, d: dict | None, probs: dict, close: int, now: float, entry_source=None, size_mult=None) -> dict:
        self.router.book = ob
        out = self.router.submit(ticker=ticker, side=side, entry_price=cost, contracts=CONTRACTS_PAPER, armed=True, size_mult=size_mult,
                                 entry_source=entry_source, table_decision=d)
        row = {"action": "w13_order", "ticker": ticker, "close_ts": close, "bin": b, **_trim({k: v for k, v in out.items() if k not in ("body_sent",)})}
        rs = out.get("resting") or {}
        if out.get("status") == "live_sent" and rs.get("price") is not None:
            plan, ctx = self._variant_plan(out, probs, side, ticker, close); coin = ticker[2:].split("15M")[0]
            pend = {"ticker": ticker, "coin": coin, "close_ts": close, "side": side, "bin": b, "slot": (d or {}).get("slot") or ("t8" if entry_source is None else None),
                    "entry_source": out.get("entry_source"), "scenario": (d or {}).get("scenario") or ((out.get("table") or {}).get("scenario")),
                    "paper_price": ctx["paper_price"], "limit": float(out["price_dollars"]), "base_n": ctx["base_n"], "mult": out.get("size_mult"), "at": now, "probs": probs,
                    "contracts": int(out["contracts"]), "price": float(rs["price"]), "touch0": rs.get("touch"), "reposts": 0, "last_strip_ts": now,
                    "price_cap": float(min(rs["price"], float(out["price_dollars"]))), "main_done": False, "cap_done": False,
                    "expires": close - float(CHASE["stop_before_close_s"]), "plan": plan, "macro": out.get("macro"),
                    "taker": {"filled": float(rs.get("taker_filled") or 0.0), "cost": float(rs.get("taker_cost") or 0.0), "fee": float(rs.get("taker_fee") or 0.0)}}
            self.st["resting"].append(pend); self.st["maker"]["attempted"] += 1; self.st["maker_capped"]["attempted"] += 1
            try: live_plan.record_exposure(ticker, int(out["contracts"]))          # a resting order holds the window cap like a fill until it expires
            except Exception as e: self._err(f"exposure record: {e}")              # noqa: BLE001
            row["resting"] = {k: pend[k] for k in ("price", "touch0", "contracts", "expires", "taker")}
        log_row(row); return out

    # ---- the latest recorded book for a coin (strips recorder, ~10 s cadence; read-only file tail)
    def _latest_strip(self, coin: str, now: float):
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        p = STRIPS / f"KX{coin}15M" / "orderbook" / f"{day}.jsonl"
        try: size = os.path.getsize(p)
        except OSError: return None
        c = self._strip_cache.get(coin)
        if c and c[0] == str(p) and c[1] == size: return c[2]
        try:
            with open(p, "rb") as fh:
                fh.seek(max(0, size - int(CHASE["tail_bytes"]))); lines = fh.read().split(b"\n")
        except OSError: return None
        last = None
        for line in reversed(lines):
            if not line.strip(): continue
            try: r = json.loads(line)
            except ValueError: continue
            if r.get("ticker") and r.get("recv_ts"): last = r; break
        res = None if last is None else (float(last["recv_ts"]), last["ticker"], (last.get("ob") or {}).get("orderbook_fp") or {})
        self._strip_cache[coin] = (str(p), size, res); return res

    # ---- chase every resting order: re-post at touch - 1c on each new book, fill when the book crosses our price, expire at close - 45 s
    def _chase(self, now: float, books: dict | None = None) -> None:
        """Two legs per resting order: MAIN re-posts at touch - 1c without limit (the 10/02 study's rule); CAPPED never
        rests above the decision limit (paper price + 1c). Both fill when the book crosses their price; both expire
        at close - 45 s. The window-cap exposure follows the MAIN leg."""
        if not self.st["resting"]: return
        keep = []
        for p in self.st["resting"]:
            if now >= p["expires"]:
                if not p.get("main_done"):
                    self.st["maker"]["unfilled"] += 1; p["unfilled_at"] = now; p["settled"] = False; self.st["unfilled"].append(p)
                    try: live_plan.settle(p["ticker"], p["contracts"], 0.0)
                    except Exception as e: self._err(f"exposure release: {e}")         # noqa: BLE001
                    log_row({"action": "w13_unfilled", "ticker": p["ticker"], "bin": p["bin"], "side": p["side"], "price": p["price"], "touch0": p["touch0"], "reposts": p["reposts"],
                             "contracts": p["contracts"], "taker": p["taker"], "waited_s": round(now - p["at"], 1)})
                if not p.get("cap_done"):
                    self.st["maker_capped"]["unfilled"] += 1
                    log_row({"action": "w13_cap_unfilled", "ticker": p["ticker"], "bin": p["bin"], "side": p["side"], "price_cap": p["price_cap"], "limit": p["limit"], "touch0": p["touch0"]})
                continue
            snap = self._latest_strip(p["coin"], now)
            live = (books or {}).get(p["coin"])                                        # (ts, ticker, ob) fetched by this tick's bin scan
            if live and live[1] == p["ticker"] and (snap is None or live[0] > snap[0]): snap = live
            if snap is None or snap[1] != p["ticker"] or snap[0] <= p["last_strip_ts"]:
                keep.append(p); continue
            ts_, _, ob = snap; p["last_strip_ts"] = ts_
            touch = top_cost(ob, p["side"])
            if touch is None: keep.append(p); continue
            if not p.get("cap_done") and touch <= p["price_cap"] + 1e-9:                 # the capped leg fills at ITS resting price
                p["cap_done"] = True; self.st["maker_capped"]["filled"] += 1
                self.st["cap_fills"].append({"ticker": p["ticker"], "coin": p["coin"], "close_ts": p["close_ts"], "side": p["side"], "bin": p["bin"], "contracts": p["contracts"],
                                             "price": float(p["price_cap"]), "at": now, "ttf_s": round(now - p["at"], 1), "settled": False})
                log_row({"action": "w13_cap_fill", "ticker": p["ticker"], "bin": p["bin"], "side": p["side"], "price": p["price_cap"], "contracts": p["contracts"], "ttf_s": round(now - p["at"], 1)})
            if not p.get("main_done"):
                if touch <= p["price"] + 1e-9:                                           # the market came to us: filled at OUR resting price
                    self._fill(p, float(p["price"]), now, touch); p["main_done"] = True
                else:
                    new = round(min(max(touch - float(CHASE["improve_c"]), 0.01), 0.99), 2)
                    if abs(new - p["price"]) >= 0.005: p["price"] = new; p["reposts"] += 1
            if not p.get("cap_done"):
                p["price_cap"] = round(min(max(touch - float(CHASE["improve_c"]), 0.01), float(p["limit"])), 2)
            if not (p.get("main_done") and p.get("cap_done")): keep.append(p)
        self.st["resting"] = keep

    def _poll_books(self, now: float) -> dict:
        """Live books for the coins with resting orders every CHASE['poll_s'] seconds - the strips recorder only writes a
        snapshot every 90 s - but never within 8 s of a scanner bin centre, so these reads never sit next to production's own."""
        if not self.st["resting"] or now - getattr(self, "_last_poll", 0.0) < float(CHASE.get("poll_s", 15)): return {}
        rem_s = (now // 900 + 1) * 900 - now
        if any(abs(rem_s - float(b_[2:]) * 60) < 8 for b_ in BINS): return {}
        self._last_poll = now; out = {}
        for tk in sorted({p["ticker"] for p in self.st["resting"]}):
            try:
                ob_ = fetch_orderbook(tk)
                if ob_: out[tk[2:].split("15M")[0]] = (time.time(), tk, ob_)
            except Exception as e: self._err(f"poll book {tk}: {e}")                  # noqa: BLE001
        return out

    def _fill(self, p: dict, price: float, now: float, touch: float) -> None:
        got = float(p["contracts"]); fee = 0.0; var = {}
        for v, pl in p["plan"].items():
            n = min(pl["n"], int(got)) if pl["take"] else 0
            var[v] = {"take": bool(pl["take"] and n >= 1), "n": float(n), "cost": round(price, 4), "fee": 0.0}
            if n >= 1: self.st["var_filled"].setdefault(v, {})[p["ticker"]] = self.st["var_filled"].get(v, {}).get(p["ticker"], 0.0) + n
        if var["cap_total"]["take"]: self.st["win_total"][str(p["close_ts"])] = self.st["win_total"].get(str(p["close_ts"]), 0.0) + var["cap_total"]["n"]
        fill = {k: p[k] for k in ("ticker", "coin", "close_ts", "side", "bin", "slot", "entry_source", "scenario", "paper_price", "limit", "base_n", "mult", "at", "probs", "macro")}
        fill.update(var=var, settled=False, maker={"price": price, "touch0": p["touch0"], "touch_at_fill": touch, "reposts": p["reposts"], "ttf_s": round(now - p["at"], 1),
                                                   "improve_c": round((p["taker"]["cost"] - price) * 100, 2) if p["taker"]["filled"] > 0 else None, "taker": p["taker"]})
        self.st["fills"].append(fill); m = self.st["maker"]; m["filled"] += 1; m["ttf_sum"] += now - p["at"]; m["reposts_sum"] += p["reposts"]
        if fill["maker"]["improve_c"] is not None: m["improve_c_sum"] += fill["maker"]["improve_c"]
        log_row({"action": "w13_fill", "ticker": p["ticker"], "bin": p["bin"], "side": p["side"], "price": price, "contracts": got, "maker": fill["maker"]})

    # ---- settlement (fills on the official result; unfilled orders and the taker counterfactual settled alongside)
    def _settle(self, now: float) -> None:
        due = [f for f in self.st["fills"] if not f["settled"] and f["close_ts"] + 60 < now]
        due_u = [u for u in self.st["unfilled"] if not u.get("settled") and u["close_ts"] + 60 < now]
        rows = [r for r in self.st["model_rows"] if r.get("y_yes") is None and r["close_ts"] + 60 < now]
        if not due and not due_u and not rows: return
        off = inputs.official_outcomes(); strikes = {}
        def yes_won(coin, close):
            tk = inputs.ticker(coin, close)
            if tk in off: return float(off[tk]), "official"
            if coin not in strikes:
                strikes[coin] = {}
                for dd in {datetime.fromtimestamp(close + s, timezone.utc).strftime("%Y-%m-%d") for s in (0, 900)}:
                    for c_, k_ in live_plan._strikes_live(coin, dd): strikes[coin].setdefault(c_, k_)
            k, kn = strikes[coin].get(close), strikes[coin].get(close + 900)
            return (None, None) if (k is None or kn is None or kn == k) else (float(kn > k), "strike_identity")
        m = self.st["maker"]
        for f in due:
            y, src = yes_won(f["coin"], f["close_ts"])
            if y is None:
                if now - f["close_ts"] > 3 * 3600: f["settled"] = True; f["unresolved"] = True
                continue
            won = float((f["side"] == "yes") == (y == 1)); f["settled"] = True; f["won"] = won; f["settle_src"] = src; f["pnl"] = {}
            for v, x in f["var"].items():
                pnl = x["n"] * (won - x["cost"] - x["fee"]) if x["take"] else 0.0; f["pnl"][v] = round(pnl, 4)
                if x["take"]:
                    L = self.st["ledgers"].setdefault(v, {"trades": 0, "contracts": 0.0, "pnl": 0.0, "won": 0, "lost": 0, "fees": 0.0}); L["trades"] += 1; L["contracts"] += x["n"]; L["pnl"] += pnl; L["fees"] += x["n"] * x["fee"]; L["won" if won else "lost"] += 1
            tk_ = f["maker"]["taker"]; tp = tk_["filled"] * (won - tk_["cost"] - tk_["fee"]); f["taker_equiv_pnl"] = round(tp, 4)
            m["taker_equiv_pnl"] += tp; m["taker_equiv_n"] += 1; m["filled_settled"] += 1; m["filled_won"] += int(won)
            log_row({"action": "w13_settle", "ticker": f["ticker"], "won": won, "src": src, "pnl": f["pnl"], "taker_equiv_pnl": f["taker_equiv_pnl"]})
        for cf in [c for c in self.st["cap_fills"] if not c["settled"] and c["close_ts"] + 60 < now]:
            y, src = yes_won(cf["coin"], cf["close_ts"])
            if y is None:
                if now - cf["close_ts"] > 3 * 3600: cf["settled"] = True; cf["unresolved"] = True
                continue
            won = float((cf["side"] == "yes") == (y == 1)); cf["settled"] = True; cf["won"] = won; pnl = cf["contracts"] * (won - cf["price"])
            Lc = self.st["ledgers"].setdefault("maker_capped", {"trades": 0, "contracts": 0.0, "pnl": 0.0, "won": 0, "lost": 0, "fees": 0.0})
            Lc["trades"] += 1; Lc["contracts"] += cf["contracts"]; Lc["pnl"] += pnl; Lc["won" if won else "lost"] += 1; cf["pnl"] = round(pnl, 4)
            log_row({"action": "w13_cap_settle", "ticker": cf["ticker"], "won": won, "src": src, "pnl": cf["pnl"]})
        for u in due_u:
            y, src = yes_won(u["coin"], u["close_ts"])
            if y is None:
                if now - u["close_ts"] > 3 * 3600: u["settled"] = True; u["unresolved"] = True
                continue
            won = float((u["side"] == "yes") == (y == 1)); u["settled"] = True; u["won"] = won
            tk_ = u["taker"]; tp = tk_["filled"] * (won - tk_["cost"] - tk_["fee"]); u["taker_equiv_pnl"] = round(tp, 4)
            m["taker_equiv_pnl"] += tp; m["taker_equiv_n"] += 1; m["unfilled_settled"] += 1; m["unfilled_would_won"] += int(won); m["unfilled_taker_pnl"] += tp
            log_row({"action": "w13_unfilled_settle", "ticker": u["ticker"], "won": won, "src": src, "taker_equiv_pnl": u["taker_equiv_pnl"]})
        for r in rows:
            y, src = yes_won(r["coin"], r["close_ts"])
            if y is not None:
                r["y_yes"] = y
                kn = strikes.get(r["coin"], {}).get(r["close_ts"] + 900) if r["coin"] in strikes else None
                if kn is None:
                    if r["coin"] not in strikes: yes_won(r["coin"], r["close_ts"])
                    kn = strikes.get(r["coin"], {}).get(r["close_ts"] + 900)
                if kn and r.get("idx_e"): r["y_rem_bp"] = round(float(np.log(kn / r["idx_e"]) * 1e4), 2)
                log_row({"action": "w13_model_settle", **{k_: r.get(k_) for k_ in r if k_ not in ("action",)}})
            elif now - r["close_ts"] > 3 * 3600: r["y_yes"] = -1
        self.st["win_total"] = {k_: v for k_, v in self.st["win_total"].items() if int(k_) > now - 3 * 3600}
        cut = now - 7 * 86400
        self.st["fills"] = [f for f in self.st["fills"] if not f["settled"] or f["close_ts"] > cut]
        self.st["unfilled"] = [u for u in self.st["unfilled"] if not u.get("settled") or u["close_ts"] > cut]
        self.st["cap_fills"] = [c for c in self.st["cap_fills"] if not c["settled"] or c["close_ts"] > cut]
        self.st["model_rows"] = [r for r in self.st["model_rows"] if r.get("y_yes") is None or r["close_ts"] > cut]
        for v in list(self.st["var_filled"]): self.st["var_filled"][v] = {t: n for t, n in self.st["var_filled"][v].items() if live_plan.close_ts_from_ticker(t, now) > now - 3 * 3600}

    # ---- the T-8 leg: mirror W7's own paper entry (the production call site)
    def _t8_leg(self, close: int, now: float) -> None:
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d"); p = W7_LOG_DIR / f"log_{day}.jsonl"
        if not p.exists(): return
        cur = self.st["t8_cursor"]; size = os.path.getsize(p)
        if cur.get("day") != day: cur.update(day=day, offset=size if cur.get("day") is None else 0)      # first run: start at the end
        if size <= cur["offset"]: return
        with open(p, "rb") as fh:
            fh.seek(cur["offset"]); chunk = fh.read()
        lines = chunk.split(b"\n"); complete = chunk.endswith(b"\n")
        cur["offset"] += len(chunk) if complete else len(chunk) - len(lines[-1])
        for line in (lines if complete else lines[:-1]):
            try: r = json.loads(line)
            except ValueError: continue
            if r.get("strategy") != "w7_noisefade" or r.get("action") != "paper_entry" or r.get("leg") != "band": continue
            tk, cost, side = r.get("ticker"), float(r.get("cost") or 0), r.get("side")
            if not tk or tk in self.st["t8_done"] or not (MAIN_LO <= cost <= MAIN_HI): continue      # the production call happens only in MAIN
            if live_plan.close_ts_from_ticker(tk, now) != close: continue
            self.st["t8_done"][tk] = now
            bk, ob = self._book(tk)
            if ob is None: self._err(f"T-8 book unavailable {tk}"); continue
            # model probabilities at this moment (cached per bin; the T-8.25 model ROWS are logged by the bin trigger itself)
            F = self._probs(close, now, BINS["T-8.25"]); coin = tk[2:].split("15M")[0]; pr = {}
            if F is not None and (F.coin == coin).any():
                r_ = F[F.coin == coin].iloc[0]
                pr = {m: (None if pd.isna(v) else float(v)) for m, v in (("p_fair", r_.p_fair_prior), ("p_lgbm", r_.p_lgbm), ("p_gru", r_.p_gru), ("p_chronos", r_.p_chronos))}
            self._order(tk, side, cost, ob, "T-8.25", None, pr, close, now, entry_source=None)   # entry_source=None -> the production T-8 path inside submit
        self.st["t8_done"] = {t: v for t, v in self.st["t8_done"].items() if now - v < 3 * 3600}

    # ---- production's per-coin decision rows for this window/bin: {coin: (ts, cost)}
    def _prod_rows(self, close: int, b: str, now: float) -> dict:
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d"); p = W7_LOG_DIR / f"log_{day}.jsonl"; out = {}
        try:
            with open(p, "rb") as fh:
                size = os.path.getsize(p); fh.seek(max(0, size - 65536)); lines = fh.read().split(b"\n")
                if size > 65536: lines = lines[1:]
        except OSError: return out
        for line in reversed(lines):
            if b"table_decision" not in line or b"w7_table" not in line: continue
            try: r = json.loads(line)
            except ValueError: continue
            try: t = float(r["ts"]) if isinstance(r["ts"], (int, float)) else datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).timestamp()
            except (KeyError, ValueError, TypeError): continue
            if now - t > 120: break
            if r.get("bin") == b and live_plan.close_ts_from_ticker(r.get("ticker", ""), now) == close:
                out.setdefault(r["ticker"][2:].split("15M")[0], (t, r.get("cost")))
        return out

    # ---- production's scanner just read its books? (its table_decision rows for this window/bin in the live log tail)
    def _prod_fired(self, close: int, b: str, now: float) -> float | None:
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d"); p = W7_LOG_DIR / f"log_{day}.jsonl"
        try:
            with open(p, "rb") as fh:
                size = os.path.getsize(p); fh.seek(max(0, size - 65536)); lines = fh.read().split(b"\n")
                if size > 65536: lines = lines[1:]                                     # drop the partial first line
        except OSError: return None
        for line in reversed(lines):
            if b"table_decision" not in line or b"w7_table" not in line: continue
            try: r = json.loads(line)
            except ValueError: continue
            if r.get("bin") != b: continue
            try: t = float(r["ts"]) if isinstance(r["ts"], (int, float)) else datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).timestamp()
            except (KeyError, ValueError, TypeError): continue
            if now - t > 120: break
            if live_plan.close_ts_from_ticker(r.get("ticker", ""), now) == close: return t
        return None

    # ---- one scanner tick
    def tick(self) -> dict:
        now = time.time(); close = int((now // 900 + 1) * 900); rem = (close - now) / 60.0
        self.st["cycles"] += 1; self.st["last_tick"] = now; rep = {"status": "IDLE", "rem": round(rem, 2)}
        in_bin = live_plan.bin_due(rem) is not None
        if not in_bin:                                                            # heavy housekeeping never runs inside a bin
            self._reload_models(); self._maybe_train(now, rem)
            try: self._settle(now)
            except Exception as e: self._err(f"settle: {e}")                     # noqa: BLE001
            if now - self.st.get("report_at", 0) >= 3600 and 11.0 <= rem <= 14.5:   # hourly report, in a quiet part of the window
                try:
                    from . import report as _report
                    REPORT.mkdir(parents=True, exist_ok=True); (REPORT / "latest.md").write_text(_report.render()); self.st["report_at"] = now
                except Exception as e: self._err(f"report: {e}")                 # noqa: BLE001
        try: self._chase(now, books=self._poll_books(now))
        except Exception as e: self._err(f"chase: {e}\n{traceback.format_exc()[-300:]}")   # noqa: BLE001
        if True:
            try: self._t8_leg(close, now)
            except Exception as e: self._err(f"t8 leg: {e}\n{traceback.format_exc()[-300:]}")   # noqa: BLE001
        # Sync with production's book read, never ahead of it: inside a bin, fire 1-2 s after the live
        # scanner's own decision rows appear in its log (same window/bin); if it logged nothing (no coin in
        # band), fire 2 s after its latest possible trigger (centre + 4 s). The T-8.25 bin (model rows
        # only; the T-8 order leg follows W7's paper entry above) fires 2 s after the earliest trigger.
        b = live_plan.bin_due(rem)
        if b is None: return rep
        k = BINS[b]; done = self.st["done"]; books = {}; rep = {"status": "SCANNED", "bin": b, "rem": round(rem, 2), "coins": {}}
        if all(f"{inputs.ticker(c, close)}|{b}" in done for c in COINS): rep["status"] = "DONE"; return rep
        centre = close - live_plan.BIN_CENTRES[b] * 60 if hasattr(live_plan, "BIN_CENTRES") else close - float(b[2:]) * 60
        if b == live_plan.LIVE_BIN:
            if now < centre - 6 + 2: return {"status": "WAIT", "bin": b, "rem": round(rem, 2)}
            prod_at = None
        else:
            prod_at = self._prod_fired(close, b, now)
            if prod_at is None and now < centre + 4 + 2: return {"status": "WAIT", "bin": b, "rem": round(rem, 2)}
            if prod_at is not None and now - prod_at < 1.0: return {"status": "WAIT", "bin": b, "rem": round(rem, 2)}
        rep["sync_gap_s"] = None if prod_at is None else round(now - prod_at, 1)
        todo = [c for c in COINS if f"{inputs.ticker(c, close)}|{b}" not in done]
        from concurrent.futures import ThreadPoolExecutor
        def fetch(c):
            t_ = time.time(); bk, ob = self._book(inputs.ticker(c, close)); return c, bk, ob, t_
        with ThreadPoolExecutor(max_workers=5) as ex:
            fetched = list(ex.map(fetch, todo))
        prod = self._prod_rows(close, b, time.time()) if b != live_plan.LIVE_BIN else {}
        try: self._chase(time.time(), books={c: (t_, inputs.ticker(c, close), ob) for c, bk, ob, t_ in fetched if ob})
        except Exception as e: self._err(f"chase(books): {e}")                          # noqa: BLE001
        for c, bk, ob, t_ in fetched:
            tk = inputs.ticker(c, close); books[c] = bk
            if bk is None: rep["coins"][c] = "book_unavailable"; continue
            done[f"{tk}|{b}"] = now
            bk["_ob"] = ob; bk["_at"] = t_; bk["_prod"] = prod.get(c)
        probs = self._model_rows(close, now, b, k, books) if any(bk for bk in books.values()) else {}
        if b == live_plan.LIVE_BIN: return rep                                      # T-8.25 orders come from W7's own call (above)
        lo, hi = EventExecutionRouter.PROD_BAND.get("w7_noisefade", (MAIN_LO, MAIN_HI))
        for c, bk in books.items():
            if not bk: continue
            tk = inputs.ticker(c, close)
            try:
                side = favorite_side((bk["yes_bid"] + bk["yes_ask"]) / 2.0)
                if side is None: rep["coins"][c] = "no_favourite"; continue
                cost = bk[side].get("fill_cost") or bk[side].get("top_cost")
                if cost is None or not (lo <= cost <= hi): rep["coins"][c] = f"out_of_band {side}@{cost}"; continue
                d = live_plan.decide(c, close, b, side, now, ticker=tk)
                log_row({"action": "w13_decision", "ticker": tk, "side": side, "cost": round(cost, 4), "bin": b, "decision": d["action"], "reason": d.get("reason"),
                         "scenario": d.get("scenario"), "slot": d.get("slot"), "mult": d.get("mult"), "slots": d.get("slots"), "inputs": d.get("inputs"), "probs": probs.get(c),
                         "book_at": round(bk["_at"], 3), "prod_at": None if not bk.get("_prod") else round(bk["_prod"][0], 3),
                         "gap_vs_prod_s": None if not bk.get("_prod") else round(bk["_at"] - bk["_prod"][0], 2),
                         "prod_cost": None if not bk.get("_prod") else bk["_prod"][1]})
                if d["action"] == "trade":
                    self._order(tk, side, cost, bk["_ob"], b, d, probs.get(c, {}), close, now, entry_source=f"table_{d['slot']}_{b}", size_mult=d["mult"])
                rep["coins"][c] = f"{d['action']} {d.get('scenario')} {d.get('slot') or ''}"
            except Exception as e:                                                # noqa: BLE001
                self._err(f"{tk} {b}: {e}\n{traceback.format_exc()[-400:]}"); rep["coins"][c] = f"error {type(e).__name__}"
        self.st["done"] = {k_: v for k_, v in done.items() if now - v < 3 * 3600}
        return rep

    def run(self, loop_s: float | None = LOOP_S, once: bool = False) -> None:
        while True:
            t0 = time.time()
            try: rep = self.tick()
            except Exception as e: self._err(f"tick: {e}\n{traceback.format_exc()[-400:]}"); rep = {"status": "ERROR"}   # noqa: BLE001
            try: _save_state(self.st)
            except OSError as e: logger.warning("[%s] state not saved: %s", NAME, e)
            if rep.get("status") in ("SCANNED", "ERROR"): logger.info("[%s] %s", NAME, json.dumps(rep, default=str)[:300])
            if once: break
            n_ = time.time(); rem_ = (((n_ // 900) + 1) * 900 - n_) / 60.0
            active = live_plan.bin_due(rem_) is not None and rep.get("status") in ("WAIT", "SCANNED", "IDLE")
            time.sleep(0.25 if active and rep.get("status") != "DONE" else max(0.5, min(loop_s, 2.0 if self.st["resting"] else loop_s) - (time.time() - t0)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__); ap.add_argument("--once", action="store_true"); ap.add_argument("--loop", type=float, default=LOOP_S)
    ap.add_argument("--no-chronos", action="store_true"); a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    Observer(chronos=not a.no_chronos).run(a.loop, a.once); return 0


if __name__ == "__main__":
    raise SystemExit(main())
