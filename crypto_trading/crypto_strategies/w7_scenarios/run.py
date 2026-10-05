"""Build the rolling W7 scenario tables into a run directory (promotion is done by
ops/w7_scenarios.sh only after check.py passes).

    python -m crypto_trading.crypto_strategies.w7_scenarios.run build [--days 14]
        [--end ISO] [--start ISO] [--max-recv ISO] [--no-cache] [--workers 3] [--no-funding-refresh]
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from datetime import datetime, timezone
from multiprocessing import Pool
from pathlib import Path

import pandas as pd

from . import funding as fundmod
from . import inputs
from .config import (CACHE, COINS, HISTORY_DAYS, HL, OUT, SETTLE_LAG_S, STRIPS, VERSION,
                     WINDOW_DAYS)
from .scenarios import cost_and_pnl, label
from . import derived
from .tables import build_tables, render_md, to_json


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


def _ts(s: str) -> float:
    """ISO time; a naive value is UTC (as the CLI help says), never the machine's local zone."""
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def _strips_task(args):
    coin, day = args
    return inputs.strips_day(coin, day)


def _flow_task(args):
    coin, day, entries = args
    return inputs.flow_day(coin, day, entries)


def build(end_ts: float, start_ts: float, workers: int = 3, max_recv: float | None = None,
          refresh_funding: bool = True, out_root: Path = OUT) -> Path:
    t0 = time.time()
    built = time.time()
    funding_report = fundmod.refresh() if refresh_funding else {"refresh": "skipped"}
    fund = fundmod.load()

    hist_days = inputs.days_between(start_ts - HISTORY_DAYS * 86400, end_ts)
    with Pool(workers) as p:
        snaps = pd.concat(p.map(_strips_task, [(c, d) for c in COINS for d in hist_days]), ignore_index=True)
    if max_recv is not None:
        snaps = snaps[snaps.recv_ts <= max_recv]
    feats = inputs.strike_features(snaps)

    entry_days = inputs.days_between(start_ts, end_ts)
    snaps["day"] = [inputs.day_str(x) for x in snaps.recv_ts]
    ent_all = inputs.select_entries(snaps[snaps.day.isin(entry_days)])
    ent_all["day"] = [inputs.day_str(x) for x in ent_all.recv_ts]
    tasks = [(c, d, g.drop(columns="day")) for (c, d), g in ent_all.groupby(["coin", "day"])]
    with Pool(workers) as p:
        flow = pd.concat(p.map(_flow_task, tasks), ignore_index=True)

    ent = ent_all[(ent_all.close_ts > start_ts) & (ent_all.close_ts <= end_ts)].drop(columns=["day", "strike"])
    m = ent.merge(flow, on=["coin", "close_ts", "bin", "recv_ts"], how="inner", validate="one_to_one")
    assert len(m) == len(ent), "flow features missing for some entry snapshots"
    m = m.merge(feats[["coin", "close_ts", "strike", "strike_next", "mkt_trend60", "mkt_vr2"]],
                on=["coin", "close_ts"], how="left", validate="many_to_one")
    m["outcome_yes"] = inputs.resolve_outcomes(m, inputs.official_outcomes())
    m["dev8h"] = inputs.funding_at_open(m, fund)
    settled = m[m.outcome_yes.notna()].copy()
    complete = settled.mkt_trend60.notna() & settled.mkt_vr2.notna() & settled.dev8h.notna()
    pc_all = cost_and_pnl(settled)
    unclassified_inband = int((pc_all.inband & ~complete).sum())
    lab = settled[complete].copy()
    lab["scen"] = label(lab)
    lab = lab.join(pc_all.loc[lab.index])

    tables = build_tables(lab)
    stamp = datetime.fromtimestamp(built, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = out_root / "runs" / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    fresh = {   # per coin: one stalled series must not hide behind the other four
        "strips_newest_recv": {c: inputs.newest_recv(STRIPS / f"KX{c}15M" / "orderbook") for c in COINS},
        "hl_trades_newest_recv": {c: inputs.newest_recv(HL / "trades" / c) for c in COINS},
        "funding_latest_ms": int(fund.time_ms.max()) if len(fund) else None,
    }
    meta = {
        "version": VERSION, "built_ts": built, "built_utc": _iso(built), "window_days": round((end_ts - start_ts) / 86400, 3),
        "start_ts": start_ts, "end_ts": end_ts, "start_utc": _iso(start_ts), "end_utc": _iso(end_ts),
        "max_recv": max_recv, "entries": int(len(m)), "settled": int(len(settled)), "labelled": int(len(lab)),
        "trades": int(lab.inband.sum()), "unclassified_inband": unclassified_inband,
        "unsettled_or_tie": int(len(m) - len(settled)), "funding_refresh": funding_report,
        "freshness": fresh, "seconds": round(time.time() - t0, 1),
    }
    lab.to_parquet(run_dir / "labelled.parquet", index=False)
    (run_dir / "tables.json").write_text(json.dumps(to_json(tables), ensure_ascii=False))
    (run_dir / "tables.md").write_text(render_md(tables, meta))
    best = derived.best_times(to_json(tables))               # derived right after, from the same numbers
    (run_dir / "derived.json").write_text(json.dumps(best, ensure_ascii=False))
    (run_dir / "derived.md").write_text(derived.render_md(best, meta))
    (run_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    return run_dir


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build"])
    ap.add_argument("--days", type=float, default=WINDOW_DAYS)
    ap.add_argument("--end", help="window-close upper bound (ISO, UTC); default now - settle lag")
    ap.add_argument("--start", help="window-close lower bound (ISO, UTC); default end - days")
    ap.add_argument("--max-recv", help="ignore snapshots received after this (ISO, UTC)")
    ap.add_argument("--no-cache", action="store_true", help="rebuild every day from raw recorder files")
    ap.add_argument("--no-funding-refresh", action="store_true")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args(argv)
    if a.no_cache and CACHE.exists():
        shutil.rmtree(CACHE)
    end = _ts(a.end) if a.end else time.time() - SETTLE_LAG_S
    end = end - (end % 900)                                     # last fully closed quarter hour
    start = _ts(a.start) if a.start else end - a.days * 86400
    run_dir = build(end, start, workers=a.workers, max_recv=_ts(a.max_recv) if a.max_recv else None,
                    refresh_funding=not a.no_funding_refresh, out_root=Path(a.out))
    print(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
