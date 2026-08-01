"""
replication_g — GKMSZ 论文复刻正式跑批器(Plan §三 G1-G10 的执行入口)
==================================================================
协议(G1/G4,论文精确):
- 样本: 2019-01→2023-12 五年;前 3 年训练/后 2 年测试单一切分;无 CV 无调参
- 目标: η = v − ma5(v);缺失滞后零填充(fill_policy=paper)
- 因子组累进(表 1 列结构): tech → +fund1 → +fund2 → +cal → +earn
- 模型: ols / nn(32-16-8) / rnn(lstm32,seq10);nn/rnn 5 种子平均
- 产物: outputs/replication/table1_{A,B,C}.csv + baselines_table.csv(G2)
  + fig1 数据 + markdown 汇总(recorder)

用法:
  python -m VolumePrediction.replication_g --panel <tag|latest> [--groups tech,cal]
    [--models ols,nn] [--seeds 5] [--quick]  # --quick: 单种子+减 epochs 冒烟
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from VolumePrediction.common import OUT, load_config, get_logger

log = get_logger("replication_g")
REP_DIR = OUT / "replication"

GROUP_ORDER = ["tech", "fund1", "fund2", "cal", "earn"]
GROUP_PREFIX = {"tech": "tech_", "fund1": "fund1_", "fund2": "fund2_",
                "cal": "cal_", "earn": "earn_"}


def load_panel(tag: str) -> pd.DataFrame:
    from VolumePrediction.data.export import load_panel as _lp, latest_meta
    if tag == "latest":
        return _lp()
    # 按 tag 找文件
    pdir = OUT / "panel"
    cands = sorted(pdir.glob(f"panel_*_{tag}.parquet")) or sorted(pdir.glob(f"panel_*{tag}*.parquet"))
    if not cands:
        raise FileNotFoundError(f"panel tag {tag} not found in {pdir}")
    return pd.read_parquet(cands[-1])


def paper_split(panel: pd.DataFrame):
    cfg = load_config()["split"]["paper"]
    d = panel.index.get_level_values("date")
    tr = panel[(d >= cfg["train"][0]) & (d <= cfg["train"][1])]
    te = panel[(d >= cfg["test"][0]) & (d <= cfg["test"][1])]
    return tr, te


def cols_upto(panel: pd.DataFrame, upto: str) -> List[str]:
    """累进因子组: GROUP_ORDER 中直到 upto(含)的全部列。"""
    idx = GROUP_ORDER.index(upto)
    prefixes = tuple(GROUP_PREFIX[g] for g in GROUP_ORDER[:idx + 1])
    return [c for c in panel.columns if c.startswith(prefixes)]


def _r2_eta(y: pd.Series, yhat: pd.Series) -> float:
    """面板 A: η 的 OOS R²(基准=0,即 ma5;论文口径)。"""
    m = y.notna() & yhat.notna()
    ss_res = float(((y[m] - yhat[m]) ** 2).sum())
    ss_tot = float((y[m] ** 2).sum())
    return 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")


def _r2_v_total(panel_te: pd.DataFrame, eta_hat: pd.Series) -> float:
    """面板 B: 对 v 总方差的 R̄²(v̂ = ma5_v + η̂)。"""
    v = panel_te["v"]
    vhat = panel_te["ma5_v"] + eta_hat.reindex(panel_te.index).fillna(0.0)
    m = v.notna() & panel_te["ma5_v"].notna()
    ss_res = float(((v[m] - vhat[m]) ** 2).sum())
    ss_tot = float(((v[m] - v[m].mean()) ** 2).sum())
    return 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")


def baselines_table(panel: pd.DataFrame) -> pd.DataFrame:
    """G2: ma5/lag1/ma22/ma252 对 v 的 R²(论文 93.68/92.53/92.60/86.12 对照)。"""
    v = panel["v"]
    g = v.groupby(level="ticker")
    rows = []
    preds = {
        "ma5": panel["ma5_v"],
        "lag1": g.shift(1),
        "ma22": g.apply(lambda s: s.shift(1).rolling(22, min_periods=22).mean()
                        ).reset_index(level=0, drop=True),
        "ma252": g.apply(lambda s: s.shift(1).rolling(252, min_periods=126).mean()
                         ).reset_index(level=0, drop=True),
    }
    paper_ref = {"ma5": 93.68, "lag1": 92.53, "ma22": 92.60, "ma252": 86.12}
    for name, p in preds.items():
        m = v.notna() & p.notna()
        ss_res = float(((v[m] - p[m]) ** 2).sum())
        ss_tot = float(((v[m] - v[m].mean()) ** 2).sum())
        rows.append({"baseline": name,
                     "r2_pct": round((1 - ss_res / ss_tot) * 100, 2),
                     "paper_pct": paper_ref[name],
                     "n_obs": int(m.sum())})
    return pd.DataFrame(rows)


def make_model(kind: str, n_pred: int, seed: int, quick: bool):
    """返回 (model, fit_kwargs)。deep 系构造要 n_pred;quick 的减 epochs 走 fit()。"""
    if kind == "ols":
        from VolumePrediction.models.linear import OLSModel
        return OLSModel(), {}
    if kind == "nn":
        from VolumePrediction.models.deep import PaperNN
        return PaperNN(n_pred, seed=seed), {"epochs": 5 if quick else None}
    if kind == "rnn":
        from VolumePrediction.models.deep import PaperRNN
        return PaperRNN(n_pred, seed=seed), {"epochs": 3 if quick else None}
    raise ValueError(kind)


def run(panel_tag: str, groups: Optional[List[str]] = None,
        models: Optional[List[str]] = None, seeds: int = 5,
        quick: bool = False, save_preds: bool = False) -> dict:
    REP_DIR.mkdir(parents=True, exist_ok=True)
    panel = load_panel(panel_tag)
    tr, te = paper_split(panel)
    tr = tr[tr["eta"].notna()]
    te_valid = te[te["eta"].notna()]
    log.info(f"panel {panel.shape} | train rows {len(tr):,} | test rows {len(te_valid):,}")

    groups = groups or GROUP_ORDER
    models = models or ["ols", "nn", "rnn"]
    t1A, t1B, t1C = {}, {}, {}
    for g in groups:
        cols = cols_upto(panel, g)
        if not cols:
            log.warning(f"group {g}: no columns — skipped (面板未含该组)")
            continue
        Xtr, ytr = tr[cols + ["v"]].drop(columns=["v"]), tr["eta"]
        Xte = te_valid[cols]
        for mk in models:
            t0 = time.time()
            n_seeds = 1 if (mk == "ols" or quick) else seeds
            preds = []
            pc = None
            for sd in range(n_seeds):
                m, fkw = make_model(mk, len(cols), sd, quick)
                fkw = {k: v for k, v in fkw.items() if v is not None}
                m.fit(Xtr, ytr, **fkw)
                preds.append(m.predict(Xte))
                pc = m.param_count()
            eta_hat = pd.concat(preds, axis=1).mean(axis=1)
            if save_preds:
                # G9 三改进源分解的接线口(replication_trading 读此约定):
                # eta_pred_{model}_{group}.parquet, index=(date,ticker), 列 eta_hat
                pf = REP_DIR / f"eta_pred_{mk}_{g}.parquet"
                eta_hat.rename("eta_hat").to_frame().to_parquet(pf)
                log.info(f"predictions archived → {pf.name}")
            key = (mk, g)
            t1A[key] = round(_r2_eta(te_valid["eta"], eta_hat) * 100, 2)
            t1B[key] = round(_r2_v_total(te_valid, eta_hat) * 100, 2)
            t1C[key] = pc
            log.info(f"table1 {mk}+{g}: A={t1A[key]}% B={t1B[key]}% "
                     f"params={pc} ({time.time()-t0:.0f}s, seeds={n_seeds})")

    def to_df(d: Dict) -> pd.DataFrame:
        return (pd.Series(d).unstack() if d else pd.DataFrame())

    A, B, C = to_df(t1A), to_df(t1B), to_df(t1C)
    A.to_csv(REP_DIR / "table1_A_r2_eta.csv")
    B.to_csv(REP_DIR / "table1_B_r2_v.csv")
    C.to_csv(REP_DIR / "table1_C_params.csv")

    bt = baselines_table(panel)
    bt.to_csv(REP_DIR / "baselines_table.csv", index=False)

    summary = {"panel_tag": panel_tag, "quick": quick,
               "groups": groups, "models": models,
               "table1_A": t1A and {f"{k[0]}+{k[1]}": v for k, v in t1A.items()},
               "baselines": bt.to_dict("records")}
    (REP_DIR / "summary.json").write_text(
        json.dumps(summary, indent=1, default=str, ensure_ascii=False))
    log.info(f"replication artifacts → {REP_DIR}")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default="latest")
    ap.add_argument("--groups", default=None)
    ap.add_argument("--models", default=None)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--save-preds", action="store_true",
                    help="存档 η̂ 到 eta_pred_*.parquet(G9 分解接线)")
    a = ap.parse_args()
    res = run(a.panel,
              groups=a.groups.split(",") if a.groups else None,
              models=a.models.split(",") if a.models else None,
              seeds=a.seeds, quick=a.quick, save_preds=a.save_preds)
    print(json.dumps({"baselines": res["baselines"]}, ensure_ascii=False))
