"""Daily ledger: the table-driven W7 prod entries vs what the old T-8-only rule would have done.

For every coin-window since the table went live (2026-10-02 19:40 UTC):
  live   = the real prod fills (all entry sources: table top1/top2 and T-8 fallback),
           priced at the real fill and fee, settled on the real outcome;
  t8     = the W7 paper entry at T-8 (the same live book W7 always priced from) if it
           was in the prod band (config.BAND, 0.80-0.97 since 2026-10-04), at the prod base size in force at that
           moment (day/night), cost + 0.5c for the +1c limit, Kalshi taker fee.
The T-8 counterfactual does not re-run the day flow gate or the NO-after-dump cut,
so it slightly overstates the old rule's trade count. Outcomes: official result when
known, else next window's strike >= this strike (the settlement identity).

    python -m crypto_trading.crypto_strategies.w7_scenarios.live_vs_t8
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import inputs
from .config import BAND, CT, COINS, OUT
from .live_plan import _strikes_live, close_ts_from_ticker

GO_LIVE = pd.Timestamp("2026-10-02T19:40:00Z")
LW = CT / "trading_signals" / "live_watch"
DEST = OUT / "table_vs_t8"
NIGHT_HOURS = range(0, 6)
# prod base sizes in force over time (execution_events.PROD_SIZING history)
SIZES = [(pd.Timestamp("2026-09-30T00:00:00Z"), {"day": (60, 40), "night": (15, 10)}),
         (pd.Timestamp("2026-10-02T20:25:36Z"), {"day": (40, 30), "night": (15, 10)}),
         (pd.Timestamp("2026-10-04T20:23:27Z"), {"day": (40, 30), "night": (20, 20)})]   # night 20/20 (user 2026-10-04)
FEE = lambda c: 0.07 * c * (1 - c)


def _base_size(coin: str, at: pd.Timestamp) -> int:
    table = [s for t, s in SIZES if t <= at][-1]
    btc, other = table["night" if at.hour in NIGHT_HOURS else "day"]
    return btc if coin == "BTC" else other


def _days(since: pd.Timestamp, until: pd.Timestamp) -> list[str]:
    return [d.strftime("%Y-%m-%d") for d in pd.date_range(since.normalize(), until.normalize())]


def build(until: pd.Timestamp | None = None) -> pd.DataFrame:
    until = until or pd.Timestamp.now(tz="UTC")
    days = _days(GO_LIVE, until)
    strikes = {c: {} for c in COINS}
    for c in COINS:
        for d in days + [(until + pd.Timedelta(days=1)).strftime("%Y-%m-%d")]:
            for ct, k in _strikes_live(c, d):
                strikes[c].setdefault(ct, k)
    official = inputs.official_outcomes()

    def yes_won(ticker: str):
        if ticker in official:
            return float(official[ticker])
        coin = ticker[2:].split("15M")[0]
        ct = close_ts_from_ticker(ticker)
        k, kn = strikes[coin].get(ct), strikes[coin].get(ct + 900)
        if k is None or kn is None or kn == k:
            return None
        return float(kn > k)

    rows = []
    for d in days:
        p = LW / f"log_{d}.jsonl"
        if not p.exists():
            continue
        for line in open(p):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ts = r.get("ts")
            if r.get("strategy") != "w7_noisefade" or not isinstance(ts, str) or pd.Timestamp(ts) < GO_LIVE:
                continue
            tk = r.get("ticker")
            if r.get("action") == "paper_entry" and r.get("leg") == "band" and BAND[0] <= r["cost"] <= BAND[1]:
                coin = tk[2:].split("15M")[0]
                n = _base_size(coin, pd.Timestamp(ts))
                rows.append(dict(kind="t8", ticker=tk, coin=coin, side=r["side"], n=n, cost=r["cost"] + 0.005,
                                 fee=FEE(r["cost"] + 0.005), src="t8"))
            elif r.get("action") == "live_order_result" and r.get("env") == "prod" and r.get("status") == "live_sent":
                resp = json.loads(r.get("response") or "{}")
                if "fill_count" not in resp or float(resp["fill_count"]) <= 0:
                    continue
                px = float(resp["average_fill_price"])
                rows.append(dict(kind="live", ticker=tk, coin=tk[2:].split("15M")[0], side=r["side"],
                                 n=float(resp["fill_count"]), cost=px if r["body_sent"]["side"] == "bid" else 1 - px,
                                 fee=float(resp["average_fee_paid"]), src=r.get("entry_source") or "t8_fallback"))
    df = pd.DataFrame(rows, columns=["kind", "ticker", "coin", "side", "n", "cost", "fee", "src"])
    if df.empty:
        return df
    df["close_ts"] = df.ticker.map(close_ts_from_ticker)
    y = df.ticker.map(yes_won)
    df["won"] = [None if pd.isna(v) else float((s == "yes") == (v == 1)) for v, s in zip(y, df.side)]
    df["pnl"] = df.n * (df.won - df.cost - df.fee)
    return df


def render(df: pd.DataFrame) -> str:
    built = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    lines = [f"# W7 按表下单 vs 只按 T-8(实盘对照账本)", "",
             f"- 生成 {built} UTC;起点 {GO_LIVE:%Y-%m-%d %H:%M} UTC(按表下单上线)。",
             f"- 实盘 = 真实成交价、手续费、结算;T-8 = 同一窗口 W7 纸面在 T-8 的入场价(在 {BAND[0]:.2f}–{BAND[1]:.2f} 才算)× 当时实盘基础仓位,"
             "价格加 0.5c 近似 +1c 限价;未重算白天大单闸门和 NO 急跌缩仓。", ""]
    if df.empty:
        return "\n".join(lines + ["暂无数据。"]) + "\n"
    s = df[df.won.notna()].copy()
    pending = int(df.won.isna().sum())
    s["day"] = pd.to_datetime(s.close_ts, unit="s", utc=True).dt.tz_convert("America/New_York").dt.date
    g = s.groupby(["day", "kind"]).agg(n=("n", "sum"), pnl=("pnl", "sum"), trades=("pnl", "size")).unstack("kind").fillna(0)
    lines += ["## 按天(美东)", "", "| 日期 | 按表实盘 | 笔数 | 张数 | 只按 T-8 | 笔数 | 张数 | 差额 |", "|---|---|---|---|---|---|---|---|"]
    for day, r in g.iterrows():
        lv, t8 = r.get(("pnl", "live"), 0), r.get(("pnl", "t8"), 0)
        lines.append(f"| {day} | {lv:+.2f} | {int(r.get(('trades', 'live'), 0))} | {int(r.get(('n', 'live'), 0))} "
                     f"| {t8:+.2f} | {int(r.get(('trades', 't8'), 0))} | {int(r.get(('n', 't8'), 0))} | {lv - t8:+.2f} |")
    lv, t8 = s[s.kind == "live"].pnl.sum(), s[s.kind == "t8"].pnl.sum()
    w = s.groupby(["close_ts", "kind"]).pnl.sum().unstack().fillna(0)
    diff = w.get("live", 0) - w.get("t8", 0)
    t = diff.mean() / diff.std(ddof=1) * len(diff) ** 0.5 if len(diff) > 2 and diff.std() > 0 else float("nan")
    lines += [f"| **合计** | **{lv:+.2f}** | | | **{t8:+.2f}** | | | **{lv - t8:+.2f}** |", "",
              f"- 窗口数 {len(w)};按窗口的差额 t = {t:.2f}(|t| < 2 视为无法区分);未结算 {pending} 笔。",
              "- 按入场来源(实盘):" + ", ".join(f"{k} {v:+.2f}" for k, v in s[s.kind == 'live'].groupby('src').pnl.sum().round(2).items()), ""]
    last = s[s.day == s.day.max()]
    ww = last.groupby(["close_ts", "kind"]).agg(pnl=("pnl", "sum"), n=("n", "sum")).unstack("kind").fillna(0)
    lines += [f"## {s.day.max()} 逐窗口", "", "| 收盘(美东) | 按表实盘 | 张数 | 只按 T-8 | 张数 |", "|---|---|---|---|---|"]
    for ct, r in ww.iterrows():
        et = pd.Timestamp(ct, unit="s", tz="UTC").tz_convert("America/New_York")
        lines.append(f"| {et:%H:%M} | {r.get(('pnl', 'live'), 0):+.2f} | {int(r.get(('n', 'live'), 0))} "
                     f"| {r.get(('pnl', 't8'), 0):+.2f} | {int(r.get(('n', 't8'), 0))} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    df = build()
    DEST.mkdir(parents=True, exist_ok=True)
    df.to_csv(DEST / "ledger.csv", index=False)
    (DEST / "latest.md").write_text(render(df))
    print(DEST / "latest.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
