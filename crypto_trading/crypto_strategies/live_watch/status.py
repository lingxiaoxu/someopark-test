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


def _wstat(vals: list[float]) -> tuple[int, float, float]:
    import math
    n = len(vals)
    if n < 2:
        return n, (vals[0] if vals else 0.0), 0.0
    mu = sum(vals) / n
    var = sum((x - mu) ** 2 for x in vals) / (n - 1)
    se = math.sqrt(var / n) if var > 0 else float("inf")
    return n, mu, (mu / se if se > 0 else 0.0)


def _w7_v3_matrix(st: dict) -> None:
    """v3 scoreboard: the pre-registered PRIMARY verdict line, then the
    bucket x side x execution MAP (display only — judging any non-primary
    bucket requires a new pre-registration, docstring rule)."""
    from .w7_noisefade import always_valid_bound, pooled_stats
    tr = st.get("trades") or []
    wp = [v["sum_c"] / v["n"] for v in (st.get("windows_primary") or {}).values()
          if v.get("n")]
    n, mu_eq, t_eq = _wstat(wp)
    ntr, nw, mu, t = pooled_stats(st.get("windows_primary") or {})
    na, mua, _ = _wstat([v["sum_c"] / v["n"] for v in (st.get("windows") or {}).values()
                         if v.get("n")])
    # The gate runs on the MONEY (pooled per-contract, window-cluster-robust);
    # the equal-weight number is shown only because the v3 comparand was first
    # registered on it and it reads several times better (2026-09-02 audit).
    print(f"      └ ★主格[0.85,0.98] 窗口 {nw}/300  池化{mu:+.2f}c/张  t_CR={t:+.2f}"
          f"  ← 判据 | 等权{mu_eq:+.2f}c t={t_eq:+.2f}(仅对照) | 宽带{na}窗{mua:+.2f}c")
    v = st.get("verdict")
    if v:
        print(f"      └ ⚖️ 判决已闩锁: {'通过' if v['passed'] else '未通过'} "
              f"@{v['decided_at_windows']}窗 池化{v['pooled_mean_c']:+.2f}c "
              f"t={v['pooled_t_clustered']:+.2f}")
    elif nw >= 2:
        print(f"      └ kill 边界(连续监控有效): t ≤ -{always_valid_bound(nw):.2f}"
              f"(n={nw});判决在 300 窗一次性闩锁")
    buckets = ((0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 0.981))
    for side in ("no", "yes"):
        cells = []
        for lo, hi in buckets:
            g = [x for x in tr if x.get("side", "no") == side
                 and lo <= x["cost"] < hi]
            if not g:
                cells.append(f"{lo:.2f}+: --")
                continue
            mt = sum(x["pnl_c"] for x in g) / len(g)
            mk = [x["maker_pnl_c"] for x in g if x.get("maker_pnl_c") is not None]
            mks = f"/mk{sum(mk)/len(mk):+.1f}" if mk else ""
            cells.append(f"{lo:.2f}+: {len(g)}笔 {mt:+.1f}c{mks}")
        print(f"      └ {side:3}侧  " + "  ".join(cells))
    obs = st.get("obs_trades") or []
    if obs:
        mo = sum(x["pnl_c"] for x in obs) / len(obs)
        _, _, mo_t = _wstat([x["pnl_c"] for x in obs])
        print(f"      └ 观察腿[0.50,0.60) {len(obs)}笔 均值{mo:+.2f}c t={mo_t:+.2f}"
              f"  ← FLB 负面印证;**显著**转正(t≥2)才算结构变了,不是符号翻面")
    mk_all = [x for x in tr if x.get("maker_fill") is not None]
    if mk_all:
        fr = sum(1 for x in mk_all if x["maker_fill"]) / len(mk_all)
        mkp = [x["maker_pnl_c"] for x in mk_all if x.get("maker_pnl_c") is not None]
        s = (f" 成交均值{sum(mkp)/len(mkp):+.2f}c" if mkp else "")
        print(f"      └ maker平行簿: fill {fr:.0%}{s}"
              f"  ← 测量非模式(回测判死 −10.6c t=−5.3,此为前向确认)")
    sl = [x["depth"]["slippage_c"] for x in tr
          if isinstance(x.get("depth"), dict)
          and x["depth"].get("slippage_c") is not None]
    if sl:
        sl2 = sorted(sl)
        print(f"      └ taker走簿滑点中位 {sl2[len(sl2)//2]:+.2f}c"
              f" · 最差 {sl2[-1]:+.2f}c  ← 只读记录,不交易")
    for sr, b in (st.get("by_series") or {}).items():
        if b.get("n"):
            print(f"      └ {sr:12} n={b['n']:4} 胜{b['wins']/b['n']:.0%} "
                  f"均值{b['sum_c']/b['n']:+.2f}c")


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
        if name == "w7_noisefade":
            _w7_v3_matrix(st)
            continue
        wins = st.get("windows") or {}
        if wins:
            wm = [v["sum_c"] / v["n"] for v in wins.values() if v["n"]]
            pos = sum(1 for x in wm if x > 0)
            avg = sum(wm) / len(wm) if wm else 0.0
            print(f"      └ 独立窗口 {len(wm)}  均值{avg:+.2f}c  正{pos}/{len(wm)}"
                  f"  ← 有效样本(五币=一次宏观下注)")

    print("-" * 88)
    print("FOCUS W7 v3.1: verdict = PRIMARY [0.85,0.98] ONLY, latched ONCE at 300 "
          "windows on POOLED per-contract P&L with a window-cluster-robust t>=2.5; "
          "other buckets are a MAP (a new pre-registration to judge one); kill uses "
          "a continuous-monitoring boundary, not a fixed t=-2")
    print("FOCUS W4: funding trail30 is the income leg — see the daily "
          "heartbeat; verdict needs the external spot account (user).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
