"""One-command scoreboard for the live_watch candidates.

    ./pipeline.sh watchstatus

Prints, per strategy: probe counters, settled paper trades, paper P&L — and
for the two FOCUS candidates (W4/W7, user 2026-08-26) their profit verdict
inputs. The paper records are the money scoreboard (official-result /
real-funding based); the demo account equity is NOT — demo validates order
mechanics only (crypto-dev/15 §0 principle 2).
"""
from __future__ import annotations

import json
import statistics as S

from . import common


def _load(name: str) -> dict:
    p = common.state_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> int:
    print("=" * 88)
    print("LIVE-WATCH SCOREBOARD   (paper = money truth; demo = mechanics only)")
    print("=" * 88)
    for name, label in (("w1_basis", "W1 basis"), ("w2_chronos", "W2 chronos"),
                        ("w3_mom24", "W3 mom24"), ("w4_carry", "W4 carry ★"),
                        ("w5_knockdown", "W5 knockdown [ARCHIVED]"),
                        ("w6_residual", "W6 residual"),
                        ("w7_noisefade", "W7 noisefade ★")):
        st = _load(name)
        if not st:
            print(f"{label:26} (no state yet)")
            continue
        pr = st.get("probe", {})
        tr = st.get("trades", [])
        parts = [f"{label:26}"]
        if pr:
            parts.append(" ".join(f"{k}={v}" for k, v in pr.items()))
        if tr:
            key = "pnl_c" if name in ("w5_knockdown", "w7_noisefade") else \
                  ("net_bps" if name == "w6_residual" else "net_usd")
            vals = [t.get(key) for t in tr if t.get(key) is not None]
            wins = sum(1 for t in tr if (t.get("win") if "win" in t
                                         else (t.get(key, 0) or 0) > 0))
            if vals:
                parts.append(f"| {len(tr)}笔 胜{wins/len(tr):.0%} "
                             f"均值{S.mean(vals):+.2f}({key})")
        parts.append(f"| 纸面${st.get('cum_net_usd', 0.0):+.2f}")
        if st.get("killed"):
            parts.append("| ⚠️ KILLED")
        print("  ".join(parts))
        wins = st.get("windows") or {}
        if wins:
            wm = [v["sum_c"] / v["n"] for v in wins.values() if v["n"]]
            pos = sum(1 for x in wm if x > 0)
            avg = sum(wm) / len(wm) if wm else 0.0
            print(f"      └ 独立窗口 {len(wm)}  均值{avg:+.2f}c  正{pos}/{len(wm)}"
                  f"  ← 有效样本(五币=一次宏观下注)")
        sl = [t["depth"]["slippage_c"] for t in (st.get("trades") or [])
              if isinstance(t.get("depth"), dict) and t["depth"].get("slippage_c") is not None]
        tp = [t["depth"]["top_size"] for t in (st.get("trades") or [])
              if isinstance(t.get("depth"), dict) and t["depth"].get("top_size")]
        if sl:
            sl2 = sorted(sl); tp2 = sorted(tp) if tp else [0]
            print(f"      └ prod 实盘成交(走簿 25 张): 滑点中位 {sl2[len(sl2)//2]:+.2f}c "
                  f"· 最差 {sl2[-1]:+.2f}c · 首档中位 {tp2[len(tp2)//2]:,.0f} 张"
                  f"  ← 只读记录,不交易")
        # W7's per-coin book: the only OOS-reproducing dimension
        for sr, b in (st.get("by_series") or {}).items():
            if b.get("n"):
                print(f"      └ {sr:12} n={b['n']:4} 胜{b['wins']/b['n']:.0%} "
                      f"均值{b['sum_c']/b['n']:+.2f}c")

    print("-" * 88)
    print("FOCUS W7: verdict at INDEPENDENT WINDOWS >= 200 (5 coins = 1 macro "
          "bet; raw trades overstate 3-5x) & mean>0 & window-clustered t>=2.5")
    print("FOCUS W4: funding trail30 is the income leg — see the daily "
          "heartbeat; verdict needs the external spot account (user).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
