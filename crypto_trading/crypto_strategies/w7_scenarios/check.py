"""Independent validation of one W7 scenario run directory.

Deliberately re-implements labelling, aggregation and formatting instead of
importing scenarios.py / tables.py, so a bug in the producer cannot hide itself.

    python -m crypto_trading.crypto_strategies.w7_scenarios.check RUN_DIR [--no-freshness]

Exit 0 = all checks passed; 1 = at least one error (details in RUN_DIR/check.json).
"""
from __future__ import annotations

import argparse
import bisect
import gzip
import json
import math
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .config import (BAND, CENTRES, COINS, EDGES, FEE_MULT, FUND_BASE, FUNDING_CACHE, HL, LABELS,
                     MIN_N, ORDER, POOLED, SNAPSHOT, STAR_N, STRIPS, W7_STATE)

ROWS = ORDER + ["全部"]
COLS = LABELS[::-1]


def _relabel(r) -> str:
    """Plain if/elif statement of the documented decision list."""
    s = 1.0 if r.fav == "yes" else -1.0
    vol = "高波动" if r.mkt_vr2 >= 1.2 else "低波动"
    if r.flow_valid:
        af, am = r.flow_1m * s, r.mom_1m_bp * s
        if af <= -0.5 and am < 0:
            return "大单反向·" + vol
        if af >= 0.5 and am > 0:
            return "大单同向·" + vol
    if (r.fav == "yes" and r.dev8h >= 0.25) or (r.fav == "no" and r.dev8h <= -0.25):
        return "funding热门方拥挤"
    if (r.fav == "yes" and r.dev8h <= -0.25) or (r.fav == "no" and r.dev8h >= 0.25):
        return "funding对手方拥挤"
    d = "上涨" if r.mkt_trend60 >= 25 else ("下跌" if r.mkt_trend60 <= -25 else "横盘")
    return d + "·" + vol


def _cell(g: pd.DataFrame, ref: pd.DataFrame):
    if len(g) < MIN_N:
        return None
    x = g.pnl.to_numpy(); mu = x.mean()
    wid = g.close_ts.to_numpy()
    uniq, inv = np.unique(wid, return_inverse=True)
    G = len(uniq)
    resid = np.bincount(inv, weights=x - mu)
    se_own = math.sqrt(G / (G - 1) * (resid ** 2).sum()) / len(x) if G > 1 else math.inf
    rm = ref.groupby("close_ts").pnl.mean().to_numpy()
    se_ref = rm.std(ddof=1) / math.sqrt(G)
    return mu, mu / max(se_own, se_ref), len(x)


def _fmt(c) -> str:
    if c is None:
        return "—"
    mu, t, n = c
    return "{:+.2f}c ({:+.1f}){}".format(mu, t, "*" if n < STAR_N else "")


def _official() -> dict:
    out = {}
    try:
        for t in json.loads(W7_STATE.read_text()).get("trades", []):
            if t.get("win") is not None:
                out[t["ticker"]] = (t["side"] == "yes") == bool(t["win"])
    except (OSError, ValueError):
        pass
    try:
        for x in json.loads(SNAPSHOT.read_text())["strategies"]["fave"]["settlements"]:
            if x.get("result") in ("yes", "no"):
                out[x["ticker"]] = x["result"] == "yes"
    except (OSError, ValueError, KeyError):
        pass
    return out


def _ticker(coin, close_ts) -> str:
    from zoneinfo import ZoneInfo
    t = datetime.fromtimestamp(int(close_ts), ZoneInfo("America/New_York"))
    return f"KX{coin}15M-{t.strftime('%y%b%d%H%M').upper()}-{t.strftime('%M')}"


def _hl(kind, coin, day, since):
    rows = []
    for name in (f"{day}.jsonl.gz", f"{day}.jsonl"):
        p = HL / kind / coin / name
        if p.exists():
            with (gzip.open(p, "rt") if name.endswith(".gz") else open(p)) as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(r.get("recv_ts"), (int, float)) and r["recv_ts"] >= since:
                        rows.append(r)
            break
    return rows


def run_checks(run: Path, freshness: bool = True, flow_samples: int = 12, raw_samples: int = 10,
               coverage: bool = True) -> dict:
    errors, warnings, done = [], [], []
    meta = json.loads((run / "meta.json").read_text())
    tj = json.loads((run / "tables.json").read_text())
    md = (run / "tables.md").read_text()
    lab = pd.read_parquet(run / "labelled.parquet")

    # 1 structure
    if set(tj) != set(COINS) | {POOLED}:
        errors.append(f"tables present: {sorted(tj)}")
    for co, rows in tj.items():
        if list(rows) != ROWS:
            errors.append(f"{co}: rows {list(rows)}")
        for sc, r in rows.items():
            if list(r["cells"]) != COLS:
                errors.append(f"{co}/{sc}: columns {list(r['cells'])}")
    done.append("structure: 6 tables x 13 rows x 6 columns")

    # 2 mutually exclusive labels, re-derived independently
    bad = (~lab.scen.isin(ORDER)).sum()
    if bad:
        errors.append(f"{bad} rows with a label outside the 12 scenarios")
    rel = [_relabel(r) for r in lab.itertuples()]
    mism = int((pd.Series(rel, index=lab.index) != lab.scen).sum())
    if mism:
        errors.append(f"{mism} rows whose label differs from the documented decision list")
    for co in COINS:
        s = sum(tj[co][sc]["share"] for sc in ORDER)
        if abs(s - 1) > 1e-9:
            errors.append(f"{co}: scenario shares sum to {s:.6f}")
    done.append(f"labels: {len(lab)} rows, each exactly one of 12, if/elif relabel mismatches={mism}")

    # 3 PnL / band / outcome
    col = np.where(lab.coin == "BTC", lab.fill60, lab.fill40)
    cost = pd.Series(col, index=lab.index).fillna(lab.top_cost)
    if (cost - lab.cost).abs().max() > 1e-12:
        errors.append("cost column does not equal fill60(BTC)/fill40 with top_cost fallback")
    won = ((lab.fav == "yes") == (lab.outcome_yes == 1)).astype(float)
    pnl = 100 * (won - cost - FEE_MULT * cost * (1 - cost))
    if (pnl - lab.pnl).abs().max() > 1e-9:
        errors.append("pnl does not match 100*(won - c - 0.07c(1-c))")
    inb = (cost >= BAND[0]) & (cost <= BAND[1])
    if (inb != lab.inband).any():
        errors.append("in-band flag mismatch")
    off = _official()
    rec_tk = lab["ticker"] if "ticker" in lab else pd.Series([None] * len(lab), index=lab.index)
    if rec_tk.isna().mean() > 0.01:
        errors.append(f"{rec_tk.isna().mean():.1%} of rows lack the recorder's ticker")
    per_window = lab.groupby(["coin", "close_ts"]).ticker.nunique() if "ticker" in lab else pd.Series(dtype=int)
    if (per_window > 1).any():
        errors.append(f"{int((per_window > 1).sum())} coin-windows carry more than one ticker")
    dup = lab.drop_duplicates(["coin", "close_ts"]).ticker.dropna().duplicated().sum() if "ticker" in lab else 0
    if dup:
        errors.append(f"{dup} tickers are shared by different windows")
    rebuilt = pd.Series([_ticker(c, t) for c, t in zip(lab.coin, lab.close_ts)], index=lab.index)
    remap = int((rebuilt != rec_tk)[rec_tk.notna()].sum())
    if remap:
        warnings.append(f"{remap} rows: ticker rebuilt from close time differs from the recorded one (DST fall-back hour?)")
    tk = [r if isinstance(r, str) else b_ for r, b_ in zip(rec_tk, rebuilt)]
    o = pd.Series([off.get(k) for k in tk], index=lab.index)
    known = o.notna()
    omis = int(((lab.outcome_yes == 1) != o.astype("boolean"))[known].sum())
    if omis:
        errors.append(f"{omis} rows disagree with the official settlement")
    if known.mean() < 0.5:
        warnings.append(f"only {known.mean():.0%} of rows matched an official settlement (W7 paused?); strike rule used for the rest")
    done.append(f"pnl/band/cost recomputed; official outcome agreement on {int(known.sum())} rows (mismatches {omis})")

    # 4 timing: entries inside their bin and before close; funding settled by open; window
    rem = (lab.close_ts - lab.recv_ts) / 60
    lo = lab.bin.map({b: c - 0.75 for b, c in CENTRES.items()})
    hi = lab.bin.map({b: c + 0.75 for b, c in CENTRES.items()})
    if ((rem < lo - 1e-9) | (rem >= hi + 1e-9)).any():
        errors.append("entry snapshot outside its time bin")
    if ((lab.close_ts <= meta["start_ts"]) | (lab.close_ts > meta["end_ts"])).any():
        errors.append("rows outside the stated window")
    fu = pd.read_csv(FUNDING_CACHE)
    fu["t"] = fu.time_ms / 1000.0
    fu["dev"] = (fu.funding - FUND_BASE) * 8 * 1e4
    fu = fu.sort_values(["coin", "t"])
    fu["dev8h"] = fu.groupby("coin")["dev"].transform(lambda x: x.rolling(8, min_periods=6).mean())
    rng = random.Random(int(meta["built_ts"]))
    sample = lab.sample(min(300, len(lab)), random_state=rng.randrange(10**9))
    fmis = 0
    for r in sample.itertuples():
        h = fu[(fu.coin == r.coin) & (fu.t <= r.close_ts - 900)]
        exp = h.dev8h.iloc[-1] if len(h) else np.nan
        if not (abs(exp - r.dev8h) < 1e-9):
            fmis += 1
    if fmis:
        errors.append(f"funding dev8h mismatch on {fmis}/{len(sample)} sampled rows (cutoff must be <= window open)")
    done.append(f"timing: bins/window ok; funding as-of-open recheck on {len(sample)} rows (mismatches {fmis})")

    # 5 order-flow spot check from raw Hyperliquid files with the live gate function
    from crypto_trading.crypto_strategies.downside_paper.features import compute_features
    fs = lab.sample(min(flow_samples, len(lab)), random_state=rng.randrange(10**9))
    flmis = 0
    for r in fs.itertuples():
        day = datetime.fromtimestamp(r.recv_ts, timezone.utc).strftime("%Y-%m-%d")
        prev = (datetime.fromtimestamp(r.recv_ts, timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        rows = {k: [x for x in _hl(k, r.coin, prev, r.recv_ts - 330) + _hl(k, r.coin, day, r.recv_ts - 330)
                    if x["recv_ts"] <= r.recv_ts] for k in ("context", "book", "trades")}
        f = compute_features(rows["context"], rows["book"], rows["trades"], r.recv_ts)
        same = bool(f.get("flow_valid")) == bool(r.flow_valid)
        if same and f.get("flow_valid"):
            same = abs(f["observed_flow_imbalance_1m"] - r.flow_1m) < 1e-9 and abs(f["momentum_1m_bp"] - r.mom_1m_bp) < 1e-9
        flmis += not same
    if flmis:
        errors.append(f"order-flow features differ from a raw recompute on {flmis}/{len(fs)} samples")
    done.append(f"order-flow raw recompute on {len(fs)} random entries (mismatches {flmis})")

    # 5b raw Kalshi re-parse of random entries with the CURRENT code (catches stale caches)
    from crypto_trading.crypto_strategies.live_watch.w7_noisefade import walk_ladder
    rs = lab.sample(min(raw_samples, len(lab)), random_state=rng.randrange(10**9))
    rmis = 0
    for r in rs.itertuples():
        day = datetime.fromtimestamp(r.recv_ts, timezone.utc).strftime("%Y-%m-%d")
        found = None
        for name in (f"{day}.jsonl.gz", f"{day}.jsonl"):
            p = STRIPS / f"KX{r.coin}15M" / "orderbook" / name
            if p.exists():
                with (gzip.open(p, "rt") if name.endswith(".gz") else open(p)) as fh:
                    for line in fh:
                        if f'"recv_ts":{r.recv_ts!r}' in line[:40] or str(r.recv_ts)[:14] in line[:40]:
                            x = json.loads(line)
                            if abs(x["recv_ts"] - r.recv_ts) < 1e-6:
                                found = x; break
                break
        if found is None:
            rmis += 1; continue
        ob = found["ob"]["orderbook_fp"]
        y, n = walk_ladder(ob, "yes", 25), walk_ladder(ob, "no", 25)
        mid = (y["top_cost"] + 1 - n["top_cost"]) / 2
        fav = "yes" if mid > 0.5 else "no"
        size = 60 if r.coin == "BTC" else 40
        w = walk_ladder(ob, fav, size)
        c = w.get("fill_cost") if w and not w.get("shortfall") else None
        c = c if c is not None else walk_ladder(ob, fav, 25)["top_cost"]
        if fav != r.fav or abs(c - r.cost) > 1e-9:
            rmis += 1
    if rmis:
        errors.append(f"raw Kalshi re-parse disagrees on {rmis}/{len(rs)} sampled entries (stale cache or parser change)")
    done.append(f"raw Kalshi re-parse of {len(rs)} random entries with current code (mismatches {rmis})")

    # 5c coverage per coin per UTC day against what the raw recorder actually holds
    lo_ts, hi_ts = meta["start_ts"], meta["end_ts"]
    raw = {}
    for day in [] if not coverage else sorted({datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d") for t in (lo_ts, hi_ts)} |
                      {datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d") for t in range(int(lo_ts), int(hi_ts), 86400)}):
        for co in COINS:
            keys = set()
            for name in (f"{day}.jsonl.gz", f"{day}.jsonl"):
                p = STRIPS / f"KX{co}15M" / "orderbook" / name
                if not p.exists():
                    continue
                with (gzip.open(p, "rt") if name.endswith(".gz") else open(p)) as fh:
                    for line in fh:
                        try:
                            x = json.loads(line)
                            ct = datetime.fromisoformat(x["close_time"].replace("Z", "+00:00")).timestamp()
                            ob = x["ob"]["orderbook_fp"]
                        except (ValueError, KeyError, TypeError):
                            continue
                        rem_ = (ct - x["recv_ts"]) / 60
                        if not (lo_ts < ct <= hi_ts) or not (3 <= rem_ < 12) or not ob.get("yes_dollars") or not ob.get("no_dollars"):
                            continue
                        keys.add((int(round(ct)), int((rem_ - 3) // 1.5)))
                break
            raw[(co, day)] = len(keys)
    lab_day = lab.assign(day=[datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d") for t in lab.recv_ts]) \
        .groupby(["coin", "day"]).size()
    thin = []
    for (co, day), n_raw in raw.items():
        got = int(lab_day.get((co, day), 0))
        if n_raw >= 100 and got < 0.8 * n_raw:
            errors.append(f"{co} {day}: only {got} of {n_raw} recorded entries were labelled")
        if n_raw < 0.5 * 576 and day not in (min(d for _, d in raw), max(d for _, d in raw)):
            thin.append(f"{co} {day} ({n_raw})")
    if thin:
        warnings.append("recorder held < 50% of a day's entries (outage, not a pipeline error): " + ", ".join(thin[:10]))
    newest = lab.groupby("coin").close_ts.max()
    for co in COINS:
        if co not in newest or hi_ts - newest[co] > 3600:
            errors.append(f"STALE: {co} newest labelled window is more than 1h older than the window end")
    done.append(f"coverage: labelled vs raw recorder entries for {len(raw)} coin-days")

    # 6 every cell re-aggregated and re-formatted independently
    trades = lab[lab.inband]
    mdcells, sec = {}, None
    for line in md.splitlines():
        if line.startswith("## "):
            sec = line[3:].strip()
        elif sec and line.startswith("| ") and not line.startswith("| 情形"):
            parts = [x.strip() for x in line.strip().strip("|").split("|")]
            if len(parts) == 2 + len(COLS):
                for b_, v in zip(COLS, parts[2:]):
                    mdcells[(sec, parts[0], b_)] = v
    if len(mdcells) != (len(COINS) + 1) * len(ROWS) * len(COLS):
        errors.append(f"markdown has {len(mdcells)} cells, expected {(len(COINS) + 1) * len(ROWS) * len(COLS)}")
    cmis = 0
    for co in list(COINS) + [POOLED]:
        t0 = trades if co == POOLED else trades[trades.coin == co]
        snaps = lab if co == POOLED else lab[lab.coin == co]
        for sc in ROWS:
            g = t0 if sc == "全部" else t0[t0.scen == sc]
            want_share = 1.0 if sc == "全部" else float((snaps.scen == sc).mean())
            if abs(tj[co][sc]["share"] - want_share) > 1e-12:
                cmis += 1
            for b in COLS:
                c = _cell(g[g.bin == b], t0[t0.bin == b])
                got = tj[co][sc]["cells"][b]
                if (c is None) != (got is None) or (c is not None and (
                        abs(c[0] - got["mean"]) > 1e-9 or abs(c[1] - got["t"]) > 1e-9 or c[2] != got["n"])):
                    cmis += 1
                if mdcells.get((co, sc, b)) != _fmt(c):
                    cmis += 1
    if cmis:
        errors.append(f"{cmis} table cells/shares/markdown strings differ from an independent recompute")
    tot = sum(int(tj[co]["全部"]["cells"][b]["n"]) for co in COINS for b in COLS if tj[co]["全部"]["cells"][b])
    if tot != int(trades.shape[0]):
        errors.append(f"sum of per-coin 全部 n ({tot}) != trades ({len(trades)})")
    done.append("all cells re-aggregated and re-formatted independently")

    # 6b derived best-time table, re-derived from the verified tables.json with separate code
    dmis = 0
    try:
        dj = json.loads((run / "derived.json").read_text())
        dmd = (run / "derived.md").read_text()
    except OSError:
        dj, dmd = None, ""
        errors.append("derived.json / derived.md missing")
    if dj is not None:
        def best_of(cells):
            cand = [(cells[b]["mean"], -abs(float(b[2:]) - 8.25), b) for b in COLS if cells.get(b)]
            return max(cand)[2] if cand else None

        matrix = {}
        for line in dmd.split("## 明细")[0].splitlines():
            if line.startswith("| ") and not line.startswith("| 情形"):
                parts = [x.strip() for x in line.strip().strip("|").split("|")]
                matrix[parts[0]] = parts[1:]
        heads = list(COINS) + [POOLED]

        def pctl(xs, q):                       # linear interpolation between order statistics
            if not xs:
                return None
            xs = sorted(xs); k = (len(xs) - 1) * q / 100.0; f = int(k)
            return xs[f] + (xs[min(f + 1, len(xs) - 1)] - xs[f]) * (k - f)

        want = {}
        for co in heads:
            for sc in ROWS:
                tab = co
                b = best_of(tj[co][sc]["cells"]); src = "own" if b else None
                if b is None and co != POOLED:
                    tab = POOLED
                    b = best_of(tj[POOLED][sc]["cells"]); src = "pooled" if b else None
                pk, psrc = b, src
                pc = tj[tab][sc]["cells"][b] if b else None
                clamp = {"T-11.25": "T-9.75", "T-3.75": "T-5.25"}   # entries only in [T-9.75, T-5.25]
                if b in clamp:
                    pk = clamp[b]; pc = tj[tab][sc]["cells"].get(pk)
                    if pc is None and tab != POOLED:
                        pc = tj[POOLED][sc]["cells"].get(pk); psrc = "pooled" if pc else psrc
                act = None if b is None else ("trade" if pc is not None and pc["mean"] >= 0 else "skip")
                want[co, sc] = dict(best=b, source=src, pick=pk, pick_source=psrc, action=act,
                                    v=pc["mean"] if pc else None)
        every = [c["mean"] for co in COINS for sc in ROWS if sc != "全部" for c in tj[co][sc]["cells"].values() if c]
        traded = [w["v"] for (co, sc), w in want.items() if co != POOLED and sc != "全部" and w["action"] == "trade"]
        hi, lo = pctl(every, 90), pctl(traded, 10)
        th = dj.get("_thresholds") or {}
        for k, v in (("hi_c", hi), ("lo_c", lo)):
            g = th.get(k)
            if (g is None) != (v is None) or (v is not None and abs(g - v) > 1e-9):
                dmis += 1
        allowed = ("T-9.75", "T-8.25", "T-6.75", "T-5.25")
        for (co, sc), w in want.items():
            mult = 0.0 if w["action"] != "trade" else (1.5 if hi is not None and w["v"] >= hi else
                                                       0.5 if lo is not None and w["v"] <= lo else 1.0)
            got = dj[co][sc]
            if any(got.get(k) != w[k] for k in ("best", "source", "pick", "pick_source", "action")) or got.get("mult") != mult:
                dmis += 1
            # top2: best allowed bin other than top1's, same table, > 0; x0.5 at or below the low threshold
            t2 = None
            if w["best"] is not None:
                tab2 = co if w["source"] == "own" else POOLED
                cells2 = tj[tab2][sc]["cells"]
                cand = [(cells2[x]["mean"], -abs(float(x[2:]) - 8.25), x) for x in allowed if x != w["pick"] and cells2.get(x)]
                if cand and max(cand)[0] > 0:
                    m2 = max(cand); t2 = (m2[2], 0.5 if (lo is not None and m2[0] <= lo) else 1.0, w["source"])
            g2 = got.get("top2")
            if (t2 is None) != (g2 is None) or (t2 and (g2.get("pick"), g2.get("mult"), g2.get("source")) != t2):
                dmis += 1
            if w["best"] is None:
                want1 = "无数据"
            elif w["action"] == "skip":
                want1 = "跳过"
            else:
                want1 = w["pick"] + ("†" if w["pick_source"] == "pooled" else "") + {1.5: " ×1.5", 0.5: " ×0.5"}.get(mult, "")
            want2 = "—" if t2 is None else t2[0] + ("†" if t2[2] == "pooled" else "") + (" ×0.5" if t2[1] == 0.5 else "")
            if matrix.get(sc, [None] * len(heads))[heads.index(co)] != f"{want1} / {want2}":
                dmis += 1
        if dmis:
            errors.append(f"derived best-time table differs from an independent re-derivation in {dmis} places")
        done.append("derived time/size table re-derived independently (fallback, [T-9.75,T-5.25] clamp, skip, size tiers, top2, markdown)")

    # 7 sample size and input freshness (BAU data)
    days = (meta["end_ts"] - meta["start_ts"]) / 86400
    if len(trades) < 300 * days:
        errors.append(f"only {len(trades)} trades for {days:.1f} days — data hole?")
    for co in COINS:
        if (trades.coin == co).sum() < 60 * days:
            errors.append(f"{co}: only {(trades.coin == co).sum()} trades — data hole?")
    if freshness:
        from zoneinfo import ZoneInfo
        built = meta["built_ts"]
        fr = meta["freshness"]
        et = datetime.fromtimestamp(built, ZoneInfo("America/New_York"))
        maint = et.weekday() == 3 and (3, 0) <= (et.hour, et.minute) <= (5, 15)   # Kalshi weekly maintenance
        for co in COINS:
            v = (fr.get("strips_newest_recv") or {}).get(co)
            if not v or built - v > 900:
                (warnings if maint else errors).append(
                    f"STALE: Kalshi 15M recorder for {co} silent {int(built - (v or 0))}s" + (" (Thursday maintenance)" if maint else ""))
            v = (fr.get("hl_trades_newest_recv") or {}).get(co)
            if not v or built - v > 900:
                errors.append(f"STALE: Hyperliquid recorder for {co} silent {int(built - (v or 0))}s")
        from .funding import health
        probs = health(fu.drop(columns=["t", "dev", "dev8h"]), now_ms=int(built * 1000))
        errors += ["STALE: " + p for p in probs]
        try:
            if built - W7_STATE.stat().st_mtime > 3600:
                warnings.append("W7 state not updated for > 1h (official outcomes may lag; strike rule covers them)")
        except OSError:
            warnings.append("W7 state missing")
        rec = int((fu.source == "recorder").sum())
        if rec:
            warnings.append(f"{rec} funding hours are recorder fills (API was unreachable); replaced on the next API success")
        done.append("freshness: strips15m, hyperliquid recorder, funding continuity, W7 state")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "checks": done, "checked_at": time.time()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--no-freshness", action="store_true")
    a = ap.parse_args(argv)
    run = Path(a.run_dir)
    res = run_checks(run, freshness=not a.no_freshness)
    (run / "check.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
