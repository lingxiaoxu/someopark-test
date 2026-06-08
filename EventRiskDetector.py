"""
EventRiskDetector.py — shared event-risk detector for the semiconductor de-risk overlay.

ONE module, imported by BOTH strategies (pure pandas/numpy + file reads, no
aiss_fetch_prices dependency):
  - MRPT/MTFS (someopark_run, root):  import EventRiskDetector
  - AISS      (qlib_run, qlib-main):  sys.path.insert(repo_root); import EventRiskDetector

It only *reads* the shared data (event_risk price store, NFP calendar, bellwether
calendar, semiconductor universe).  It does NOT execute trades or fetch prices —
each strategy calls these functions and applies its own de-risk action.

Triggers (§8.2):
  1. β+NFP        : SMH_beta (and, AISS-side, max with portfolio_beta) > 2.5
                    AND an NFP release within the next `nfp_days` trading days.
  2. bellwether   : NVDA/AVGO on its earnings reaction day D or D+1 closes < -4.5%.

β convention (§0): backtest = top-down SMH-vs-SPY 30d regression (PIT);
live = bottom-up from SMH holdings (opt-in; falls back to top-down on failure).

Default-off: nothing calls this yet; it is a library.
"""

import os
import sys
import json
import glob
from datetime import date, timedelta, datetime

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.abspath(__file__))

EVENT_STORE = os.path.join(_ROOT, "price_data", "event_risk", "prices")
NFP_DIR = os.path.join(_ROOT, "price_data", "macro", "nfp")
BELLWETHER_PATH = os.path.join(_ROOT, "price_data", "macro", "earnings_bellwether",
                                "bellwether_earnings.json")
UNIVERSE_PATH = os.path.join(_ROOT, "price_data", "semiconductor_universe.json")

BELLWETHERS = ["NVDA", "AVGO"]
DEFAULT_BETA_THRESHOLD = 2.5
DEFAULT_NFP_DAYS = 2
DEFAULT_BELLWETHER_DROP = -0.045
BIG_DROP_THRESHOLD = -0.03          # SMH single-day drop that lifts the veto early
BETA_WINDOW = 30


def _alert(msg: str) -> None:
    banner = "!" * 70
    for stream in (sys.stderr, sys.stdout):
        print(f"\n{banner}\n[EVENT_RISK ALERT] {msg}\n{banner}", file=stream)


# ── price access (read-only) ───────────────────────────────────────────────────
def _read_close(ticker: str, prices_dir: str = EVENT_STORE) -> pd.Series:
    f = os.path.join(prices_dir, f"{ticker}_prices.parquet")
    if not os.path.exists(f):
        return pd.Series(dtype=float)
    df = pd.read_parquet(f)
    col = "AdjClose" if "AdjClose" in df.columns else df.columns[0]
    s = df[col].copy()
    s.index = pd.to_datetime(df.index)
    return s.sort_index()


def _returns(ticker: str, prices_dir: str = EVENT_STORE) -> pd.Series:
    return _read_close(ticker, prices_dir).pct_change()


def trading_index(prices_dir: str = EVENT_STORE, ref: str = "SMH") -> pd.DatetimeIndex:
    """Trading calendar = the reference ticker's price index (PIT-safe)."""
    return pd.DatetimeIndex(_read_close(ref, prices_dir).index)


# ── D2: NFP calendar loader (mirrors VIXForecast._load_fomc_dates) ──────────────
def load_nfp_dates(nfp_dir: str = NFP_DIR) -> list:
    files = sorted(glob.glob(os.path.join(nfp_dir, "nfp_*.json")))
    dates = []
    for f in files:
        try:
            dates.extend(json.loads(open(f).read()))
        except Exception:
            continue
    return sorted({pd.Timestamp(d).normalize() for d in dates})


def nfp_within(asof, nfp_days: int = DEFAULT_NFP_DAYS, nfp_dir: str = NFP_DIR,
               prices_dir: str = EVENT_STORE) -> bool:
    """True if an NFP release falls within the next `nfp_days` trading days of asof."""
    asof = pd.Timestamp(asof).normalize()
    nfp = set(load_nfp_dates(nfp_dir))
    if not nfp:
        _alert("NFP calendar empty — trigger #1 cannot fire (refresh price_data/macro/nfp).")
        return False
    # forward-looking staleness guard (§11.2)
    if max(nfp) < asof + pd.Timedelta(days=60):
        _alert(f"NFP calendar only covers to {max(nfp).date()} (<60d ahead) — refresh FRED.")
    idx = trading_index(prices_dir)
    future = idx[idx > asof][:nfp_days]
    return any(d in nfp for d in future)


# ── β computation (§0) ──────────────────────────────────────────────────────────
def _rolling_beta(a: pd.Series, b: pd.Series, asof, window: int = BETA_WINDOW):
    asof = pd.Timestamp(asof).normalize()
    a = a[a.index <= asof].dropna().iloc[-window:]
    b = b[b.index <= asof].dropna()
    j = a.index.intersection(b.index)
    if len(j) < window // 2:
        return np.nan
    av, bv = a.loc[j], b.loc[j]
    var = bv.var()
    return float(av.cov(bv) / var) if var > 0 else np.nan


def smh_beta_topdown(asof, prices_dir: str = EVENT_STORE, window: int = BETA_WINDOW) -> float:
    """SMH 30d β vs SPY (PIT; the backtest convention)."""
    return _rolling_beta(_returns("SMH", prices_dir), _returns("SPY", prices_dir), asof, window)


def smh_beta_bottomup(asof, prices_dir: str = EVENT_STORE, window: int = BETA_WINDOW) -> float:
    """Live: Σ w_i·β_i over SMH's current holdings (yfinance). Falls back to top-down."""
    try:
        import yfinance as yf
        th = yf.Ticker("SMH").funds_data.top_holdings
        weights = {str(t): float(w) for t, w in th["Holding Percent"].items()}
        spy = _returns("SPY", prices_dir)
        num = den = 0.0
        for t, w in weights.items():
            bi = _rolling_beta(_returns(t, prices_dir), spy, asof, window)
            if not np.isnan(bi):
                num += w * bi
                den += w
        if den <= 0:
            raise ValueError("no constituent betas")
        return num / den           # renormalise over the (top-10) weight covered
    except Exception as e:  # noqa: BLE001
        _alert(f"SMH bottom-up β failed ({e!r}); falling back to top-down regression.")
        return smh_beta_topdown(asof, prices_dir, window)


def smh_beta(asof, prices_dir: str = EVENT_STORE, mode: str = "topdown",
             window: int = BETA_WINDOW) -> float:
    return (smh_beta_bottomup if mode == "bottomup" else smh_beta_topdown)(asof, prices_dir, window)


def ticker_beta(ticker: str, asof, ref: str = "SMH", prices_dir: str = EVENT_STORE,
                window: int = BETA_WINDOW) -> float:
    """β of a ticker vs `ref` (default SMH) over `window` days up to `asof`.
    Used for Tier-2 pair selection (long leg β to SMH, §8.4)."""
    return _rolling_beta(_returns(ticker, prices_dir), _returns(ref, prices_dir), asof, window)


# ── trigger #2: bellwether contagion ────────────────────────────────────────────
def _bellwether_dates(path: str = BELLWETHER_PATH) -> dict:
    try:
        d = json.load(open(path))
        return {t: set(pd.Timestamp(x).normalize()
                       for x in d.get(t, {}).get("reaction_dates", [])) for t in BELLWETHERS}
    except Exception as e:  # noqa: BLE001
        _alert(f"bellwether calendar unreadable ({e!r}) — trigger #2 skipped today.")
        return {t: set() for t in BELLWETHERS}


def bellwether_drop(asof, prices_dir: str = EVENT_STORE, path: str = BELLWETHER_PATH,
                    thresh: float = DEFAULT_BELLWETHER_DROP):
    """True if asof is D or D+1 of an NVDA/AVGO earnings date AND that name's
    close-to-close return on asof < thresh.  Returns (hit, reason)."""
    asof = pd.Timestamp(asof).normalize()
    cal = _bellwether_dates(path)
    idx = trading_index(prices_dir)
    for t in BELLWETHERS:
        dates = cal[t]
        if not dates:
            continue
        # reaction window = the earnings date D and the next trading day D+1
        window_dates = set()
        for D in dates:
            window_dates.add(D)
            nxt = idx[idx > D]
            if len(nxt):
                window_dates.add(nxt[0])
        if asof in window_dates:
            r = _returns(t, prices_dir)
            rv = r.get(asof, np.nan)
            if not np.isnan(rv) and rv < thresh:
                return True, f"{t} earnings reaction {rv*100:+.1f}% < {thresh*100:.1f}%"
    return False, ""


# ── combined trigger evaluation (§8.2) ──────────────────────────────────────────
def evaluate(asof, prices_dir: str = EVENT_STORE, aiss_beta: float = None,
             beta_threshold: float = DEFAULT_BETA_THRESHOLD, nfp_days: int = DEFAULT_NFP_DAYS,
             bellwether_thresh: float = DEFAULT_BELLWETHER_DROP, beta_mode: str = "topdown",
             nfp_dir: str = NFP_DIR, bellwether_path: str = BELLWETHER_PATH) -> dict:
    """Evaluate both triggers as of `asof` (a close date). Pure read; no side effects.
    `aiss_beta` (AISS only) is OR-ed with SMH_beta; MTFS passes None (SMH_beta only)."""
    asof = pd.Timestamp(asof).normalize()
    # Staleness guard: detector reads the parquet store directly (no auto-refresh).
    # If the store wasn't refreshed (RefreshEventRiskData failed / didn't run), β / NFP
    # window / trading-day distance would silently use stale data — alert loudly.
    _idx = trading_index(prices_dir)
    if len(_idx) and _idx.max() < asof:
        _alert(f"event_risk store STALE: latest {_idx.max().date()} < asof {asof.date()} "
               f"— run RefreshEventRiskData; triggers/veto-timing may be wrong.")
    sb = smh_beta(asof, prices_dir, beta_mode)
    beta_used = sb if (aiss_beta is None or np.isnan(aiss_beta)) else max(sb, aiss_beta)
    triggers = []
    # trigger 1: β + NFP
    if not np.isnan(beta_used) and beta_used > beta_threshold and nfp_within(asof, nfp_days, nfp_dir, prices_dir):
        triggers.append(f"beta+NFP (β={beta_used:.2f}>{beta_threshold}, NFP in {nfp_days}td)")
    # trigger 2: bellwether contagion
    bw_hit, bw_reason = bellwether_drop(asof, prices_dir, bellwether_path, bellwether_thresh)
    if bw_hit:
        triggers.append(f"bellwether ({bw_reason})")
    return {
        "asof": asof.date().isoformat(),
        "hit": bool(triggers),
        "triggers": triggers,
        "smh_beta": None if np.isnan(sb) else round(sb, 3),
        "beta_used": None if np.isnan(beta_used) else round(beta_used, 3),
        "aiss_beta": None if aiss_beta is None else round(aiss_beta, 3),
    }


# ── veto state machine (§8.3) ───────────────────────────────────────────────────
# Daily run (evening of D) decides actions for the NEXT open (D+1):
#   reduce  : one-time, when D is the signal day (D == signal_date)
#   veto    : D+1 == T+1 (always) ; D+1 == T+2 (only if T+1 had no SMH<-3% big drop) ; never T+3+
def load_veto_state(path: str) -> dict:
    try:
        return json.load(open(path))
    except Exception:
        return {}          # fail-safe: missing/corrupt -> no veto (§11.2)


def save_veto_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(state, open(path, "w"), indent=2)


def _td_distance(t_from, t_to, prices_dir: str = EVENT_STORE) -> int:
    """Number of trading days from t_from to t_to (>=0); -1 if t_to < t_from."""
    idx = trading_index(prices_dir)
    a, b = pd.Timestamp(t_from).normalize(), pd.Timestamp(t_to).normalize()
    sub = idx[(idx >= a) & (idx <= b)]
    return len(sub) - 1 if len(sub) else -1


def step_state(state: dict, asof, hit: bool, hit_reason: str, smh_ret_today: float,
               prices_dir: str = EVENT_STORE) -> dict:
    """Advance the veto state for the evening-of-`asof` run. Returns new state with
    `reduce_next_open` and `veto_next_open` decisions for D+1."""
    asof = pd.Timestamp(asof).normalize()
    state = dict(state or {})

    # (re)arm on a fresh hit
    if hit:
        active_dist = _td_distance(state.get("signal_date"), asof, prices_dir) if state.get("signal_date") else 99
        if not state.get("active") or active_dist >= 2:
            state = {"active": True, "signal_date": asof.date().isoformat(),
                     "reason": hit_reason, "reduce_done": False}

    out = {**state, "reduce_next_open": False, "veto_next_open": False}
    if not state.get("active"):
        return out

    dist = _td_distance(state["signal_date"], asof, prices_dir)   # 0 at signal day
    # reduce: one-time at signal day (for next open = T+1)
    if dist == 0 and not state.get("reduce_done"):
        out["reduce_next_open"] = True
        out["reduce_done"] = True
    # veto for next open (D+1):
    if dist == 0:                      # D+1 = T+1 -> always veto
        out["veto_next_open"] = True
    elif dist == 1:                    # D+1 = T+2 -> veto unless T+1 (=today) big-dropped
        out["veto_next_open"] = not (smh_ret_today is not None and smh_ret_today < BIG_DROP_THRESHOLD)
    else:                              # D+1 = T+3+ -> never; deactivate
        out["veto_next_open"] = False
        out["active"] = False
    return out


# ── universe + pair helpers (§8.1/§8.4) ─────────────────────────────────────────
def load_universe(path: str = UNIVERSE_PATH) -> dict:
    try:
        d = json.load(open(path))
        return {"tier1": set(d.get("tier1", [])), "tier2": set(d.get("tier2", []))}
    except Exception as e:  # noqa: BLE001
        _alert(f"semiconductor_universe.json unreadable ({e!r}).")
        return {"tier1": set(), "tier2": set()}


def long_leg(pair_key: str, direction: str):
    """Long leg ticker: long->s1, short->s2 (§8.4/decision 11)."""
    s1, s2 = pair_key.split("/")
    return s1 if str(direction).lower() == "long" else s2


def semi_tier(ticker: str, uni: dict):
    if ticker in uni.get("tier1", set()):
        return 1
    if ticker in uni.get("tier2", set()):
        return 2
    return None


def stress_pnl(pair_key: str, s1_shares: int, s2_shares: int, asof,
               prices_dir: str = EVENT_STORE, worst_n: int = 10, lookback: int = 252) -> float:
    """Empirical semi-stress P&L (§8.4): avg $ P&L of this pair (current shares) on
    the `worst_n` worst SMH days over the trailing `lookback`. Most negative = cut first.
    np.nan if insufficient history (caller falls back to long-leg notional × β)."""
    asof = pd.Timestamp(asof).normalize()
    s1, s2 = pair_key.split("/")
    smh_r = _returns("SMH", prices_dir)
    smh_r = smh_r[smh_r.index <= asof].dropna().iloc[-lookback:]
    if len(smh_r) < worst_n:
        return np.nan
    worst = smh_r.nsmallest(worst_n).index
    d1 = _read_close(s1, prices_dir).diff()
    d2 = _read_close(s2, prices_dir).diff()
    pnl = [s1_shares * d1.get(d, np.nan) + s2_shares * d2.get(d, np.nan) for d in worst]
    pnl = [p for p in pnl if not np.isnan(p)]
    return float(np.mean(pnl)) if pnl else np.nan


def smh_return(asof, prices_dir: str = EVENT_STORE):
    """SMH close-to-close return on `asof` (None if unavailable)."""
    r = _returns("SMH", prices_dir).get(pd.Timestamp(asof).normalize(), None)
    return None if (r is None or (isinstance(r, float) and np.isnan(r))) else float(r)


def log_evaluation(ev: dict, state: dict, tag: str, log_path: str, extra: str = "") -> None:
    """Append a one-line daily HEARTBEAT to `log_path` — written EVERY run (even when
    nothing triggers), so you can confirm the overlay ran and see what it decided.
    Tail it: `tail -n 20 <log_path>`."""
    line = (f"{datetime.now().isoformat(timespec='seconds')} [{tag}] asof={ev.get('asof')} "
            f"hit={ev.get('hit')} beta_used={ev.get('beta_used')} smh_beta={ev.get('smh_beta')} "
            f"triggers={ev.get('triggers')} active_next={state.get('veto_next_open')} "
            f"reduce_next={state.get('reduce_next_open')}" + (f" {extra}" if extra else ""))
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a") as f:
            f.write(line + "\n")
    except Exception as e:  # noqa: BLE001
        _alert(f"heartbeat log write failed ({e!r}): {log_path}")


def process(asof, prev_state: dict, prices_dir: str = EVENT_STORE, aiss_beta: float = None,
            beta_mode: str = "topdown", beta_threshold: float = DEFAULT_BETA_THRESHOLD,
            nfp_days: int = DEFAULT_NFP_DAYS, bellwether_thresh: float = DEFAULT_BELLWETHER_DROP,
            nfp_dir: str = NFP_DIR, bellwether_path: str = BELLWETHER_PATH):
    """One call per daily (evening-of-`asof`) run. Evaluates triggers + advances the
    veto state machine. Returns (new_state, evaluation). new_state carries
    `reduce_next_open` / `veto_next_open` / `active` / `signal_date` for D+1."""
    ev = evaluate(asof, prices_dir, aiss_beta, beta_threshold, nfp_days,
                  bellwether_thresh, beta_mode, nfp_dir, bellwether_path)
    new_state = step_state(prev_state, asof, ev["hit"], "; ".join(ev["triggers"]),
                           smh_return(asof, prices_dir), prices_dir)
    return new_state, ev


if __name__ == "__main__":
    asof = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    res = evaluate(asof)
    print(json.dumps(res, indent=2))
