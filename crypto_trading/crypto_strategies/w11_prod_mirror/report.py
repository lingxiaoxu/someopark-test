"""W11 report: variant ledgers, every predictor vs the market price, remaining-move predictability,
single factors, and the model-minus-market bucket table - all from the settled rows.
    python -m crypto_trading.crypto_strategies.w11_prod_mirror.report"""
from __future__ import annotations
import json
from datetime import datetime, timezone
import numpy as np, pandas as pd
from .config import STATE, REPORT, VARIANTS, VAR_DESC, LOG_DIR

BINARY = [("p_mkt_yes", "市场价(实时盘口)"), ("kyes_snap", "市场价(90s 快照)"), ("p_fair", "公允 Φ(d/σ) 前60分钟σ"), ("p_fair_in", "公允 本窗σ"), ("p_fair_dv", "公允 DVOL σ"),
          ("p_blend1", "逻辑回归 市场+公允"), ("p_blend2", "逻辑回归 +距离/动量"), ("p_lgbm", "LightGBM 全特征"), ("p_lgbm_np", "LightGBM 不用价格"),
          ("p_gru", "GRU 含价格"), ("p_gru_np", "GRU 不用价格"), ("p_chronos", "Chronos-bolt-small"), ("p_chronos_base", "Chronos-bolt-base")]
DIRN = [("p_dir", "LightGBM 方向(不用价格)"), ("chronos_dir_bp", "Chronos 中位数−当前"), ("dist_bp", "开盘以来涨跌 d(<0.5=回吐)"), ("f3", "3 分钟成交流"), ("imb5", "盘口不平衡"),
        ("dk", "Kalshi 价格漂移"), ("mom1", "1 分钟动量"), ("mom_all", "开盘以来动量")]


def _rows(action: str) -> list:
    out = []
    for p in sorted(LOG_DIR.glob("log_*.jsonl")):
        for line in open(p):
            try: r = json.loads(line)
            except ValueError: continue
            if r.get("action") == action: out.append(r)
    return out


def render() -> str:
    st = json.loads(STATE.read_text()) if STATE.exists() else {}
    L = st.get("ledgers", {}); lines = [f"# W11 = W7 实盘规则的纸面镜像 + 叠加规则(生成 {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC)", ""]
    lines += [f"- 登记 {st.get('registered_at', '?')[:19]};循环 {st.get('cycles', 0)} 次;模型 {st.get('model_stamp') or '—'};错误 {len(st.get('errors', []))} 条", "",
              "## 各变体(同一批模拟成交,只改'下不下'和'下多少')", "", "| 变体 | 笔 | 张 | 胜/负 | 胜率 | 净 | 每张 | 手续费 |", "|---|---|---|---|---|---|---|---|"]
    for v in VARIANTS:
        x = L.get(v, {}); n = x.get("trades", 0); c = x.get("contracts", 0.0)
        lines.append(f"| {VAR_DESC.get(v, v)} | {n} | {c:.0f} | {x.get('won', 0)}/{x.get('lost', 0)} | {x.get('won', 0) / n if n else 0:.1%} | {x.get('pnl', 0):+.2f} | {100 * x.get('pnl', 0) / c if c else 0:+.2f}c | {x.get('fees', 0):.2f} |")
    rows = [r for r in _rows("w11_model_settle") if r.get("y_yes") in (0, 1, 0.0, 1.0)]
    lines += ["", f"## 到期二元结果:每个预测器 vs 市场价(已结算 {len(rows)} 条模型行)", ""]
    if rows:
        from sklearn.metrics import roc_auc_score, log_loss
        from scipy.stats import spearmanr
        df = pd.DataFrame(rows); df["y"] = df.y_yes.astype(float)
        def sc(y, p):
            p = pd.to_numeric(p, errors="coerce"); m = p.notna() & y.notna(); p_ = np.clip(p[m].astype(float), 1e-4, 1 - 1e-4)
            if m.sum() < 20 or y[m].nunique() < 2: return "—"
            return f"{roc_auc_score(y[m], p_):.3f} / {log_loss(y[m], p_, labels=[0, 1]):.4f} (n={int(m.sum())})"
        lines += ["| 预测器 | " + " | ".join(sorted(df.bin.unique())) + " | 全部 |", "|---|" + "---|" * (df.bin.nunique() + 1)]
        for col, nm in BINARY:
            if col not in df: continue
            cells = [sc(g.y, g[col]) for _, g in sorted(df.groupby("bin"))] + [sc(df.y, df[col])]
            lines.append(f"| {nm} | " + " | ".join(cells) + " |")
        lines += ["", "AUC / logloss;logloss 越低越好。", "", "## 剩余走势(入场→结算)的方向与幅度", ""]
        d = df[pd.to_numeric(df.y_rem_bp, errors="coerce").notna()].copy(); d["y_rem_bp"] = d.y_rem_bp.astype(float); d = d[d.y_rem_bp != 0]; d["yd"] = (d.y_rem_bp > 0).astype(float)
        if len(d) >= 20:
            lines += [f"| 信号 | 方向 AUC(n={len(d)}) |", "|---|---|"]
            for col, nm in DIRN:
                if col not in d: continue
                p = pd.to_numeric(d[col], errors="coerce"); m = p.notna()
                if m.sum() < 20 or d.yd[m].nunique() < 2: continue
                lines.append(f"| {nm} | {roc_auc_score(d.yd[m], p[m]):.3f} |")
            mg = pd.to_numeric(d.get("mag_bp"), errors="coerce") if "mag_bp" in d else None
            if mg is not None and mg.notna().sum() >= 20:
                m = mg.notna(); lines.append(f"| LightGBM 幅度(bp)Spearman | {spearmanr(d.y_rem_bp[m], mg[m]).correlation:+.3f} |")
            q = pd.qcut(pd.to_numeric(d.dist_bp, errors="coerce"), 5, labels=["最不利", "Q2", "Q3", "Q4", "热门方已大幅领先"], duplicates="drop") if d.dist_bp.notna().sum() >= 50 else None
            if q is not None:
                lines += ["", "按'开盘以来涨跌(yes 轴)'分五档的剩余走势均值(bp):", ""]
                for k_, g in d.groupby(q, observed=True): lines.append(f"- {k_}: n={len(g)} 剩余 {g.y_rem_bp.mean():+.2f}bp")
        # model - market buckets (in-band favourites)
        b = df[pd.to_numeric(df.p_mkt_yes, errors="coerce").notna() & pd.to_numeric(df.p_lgbm, errors="coerce").notna()].copy()
        if len(b) >= 30:
            b["fav_yes"] = (b.p_mkt_yes.astype(float) >= 0.5); b["p_fav"] = np.where(b.fav_yes, b.p_mkt_yes.astype(float), 1 - b.p_mkt_yes.astype(float))
            b["pm_fav"] = np.where(b.fav_yes, b.p_lgbm.astype(float), 1 - b.p_lgbm.astype(float)); b["won"] = (b.y == b.fav_yes.astype(float)).astype(float)
            b = b[(b.p_fav >= 0.79) & (b.p_fav <= 0.98)]; b["edge_c"] = (b.won - b.p_fav - 0.07 * b.p_fav * (1 - b.p_fav)) * 100; b["gap"] = b.pm_fav - b.p_fav
            lines += ["", f"## LightGBM − 市场价 分桶(价带内 {len(b)} 条)", "", "| 桶 | n | 胜率 | 市场价均值 | 每张期望 |", "|---|---|---|---|---|"]
            for lo, hi in ((-1, -0.03), (-0.03, -0.01), (-0.01, 0.01), (0.01, 0.03), (0.03, 1)):
                g = b[(b.gap >= lo) & (b.gap < hi)]
                if len(g): lines.append(f"| [{lo:+.2f},{hi:+.2f}) | {len(g)} | {g.won.mean():.1%} | {g.p_fav.mean():.3f} | {g.edge_c.mean():+.2f}c |")
            k_ = b[b.gap >= 0]; s_ = b[b.gap < 0]
            lines.append(f"| 模型≥市场 合计 | {len(k_)} | {k_.won.mean() if len(k_) else 0:.1%} | | {k_.edge_c.mean() if len(k_) else 0:+.2f}c |")
            lines.append(f"| 模型<市场 合计 | {len(s_)} | {s_.won.mean() if len(s_) else 0:.1%} | | {s_.edge_c.mean() if len(s_) else 0:+.2f}c |")
    gaps = [r for r in _rows("w11_decision") if r.get("gap_vs_prod_s") is not None and r.get("prod_cost")]
    if gaps:
        g = np.array([r["gap_vs_prod_s"] for r in gaps]); pdiff = np.array([(r["cost"] - r["prod_cost"]) * 100 for r in gaps])
        lines += ["", f"## 与实盘读盘的时间差 / 价差(同币同时点 {len(g)} 次)", "", f"- 时间差中位 {np.median(g):.2f}s,最大 {g.max():.2f}s;价差中位 {np.median(pdiff):+.2f}c,|价差|>1c 占 {np.mean(np.abs(pdiff) > 1):.0%}"]
    lines += ["", "## 最近的错误", ""] + [f"- {datetime.fromtimestamp(e['at'], timezone.utc):%m-%d %H:%M} {e['msg'][:160]}" for e in st.get("errors", [])[-5:]]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    REPORT.mkdir(parents=True, exist_ok=True); md = render(); (REPORT / "latest.md").write_text(md); print(md); return 0


if __name__ == "__main__":
    raise SystemExit(main())
