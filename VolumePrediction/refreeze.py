"""refreeze.py — 学习模型月度再冻结编排(retrain 侧,②晋升配套,2026-08-01)。

用途: health.refreeze_due 亮起(冻结统计漂移>25交易日)或每月初手动运行。
流程(plan §.ops: retrain 落新版本,**不自动晋升**):
  1. 宇宙年表: 当年 vintage 缺失则补建(6月重构日后才需要新年份)
  2. 面板全量重建 prod_vN(2019-01-01 → 最近原始日;因果 expanding z)
  3. prod_model.freeze() → 新工件(全量重训 + 冻结统计/z_next/fund末值/active)
  4. OOS 对照(最近完整月 vs ma5) → registry 记 candidate + 指标
  5. **晋升需人工**: 审指标后 `svc.registry.promote(<version>, by='user')`
     (可随时 promote('baselines.ma5') 回退)

用法:
  conda run -n someopark_run python -m VolumePrediction.refreeze            # 全流程
  conda run -n someopark_run python -m VolumePrediction.refreeze --dry-run  # 只报告将做什么
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("VolumePrediction.refreeze")


def main(dry_run: bool = False) -> dict:
    from VolumePrediction.common import REPO
    from VolumePrediction.data import polygon_loader as pl
    from VolumePrediction.data import universe as uni

    today = date.today().strftime("%Y-%m-%d")
    days = pl.trading_days("2026-01-01", today)
    asof = days[-1] if days and days[-1] <= today else days[-2]
    year = int(asof[:4])
    tag = f"prod_{asof.replace('-', '')}"
    version = f"lgbm_{tag}"

    plan = {"asof": asof, "panel_tag": tag, "version": version,
            "vintage_needed": [],
            "steps": ["vintage", "panel(~4.5h)", "freeze(~1min)",
                      "oos_report", "registry candidate(不自动晋升)"]}
    for y in (year - 1, year):
        p = REPO / "VolumePrediction" / "outputs" / "universe" / f"vintage_{y}.parquet"
        if not p.exists():
            plan["vintage_needed"].append(y)
    log.info(f"refreeze plan: {plan}")
    if dry_run:
        return plan

    # 1) vintage
    for y in plan["vintage_needed"]:
        log.info(f"build_vintage({y}) ...")
        uni.build_vintage(y)

    # 2) 面板(重活)
    from VolumePrediction.pipeline_build import build_panel
    log.info(f"build_panel 2019-01-01 → {asof} tag={tag} (数小时)")
    build_panel("2019-01-01", asof, out_tag=tag)

    # 3) freeze
    import glob as _g
    from VolumePrediction import prod_model
    panel_path = sorted(_g.glob(str(
        REPO / "VolumePrediction" / "outputs" / "panel" / f"panel_*_{tag}.parquet")))[-1]
    meta = prod_model.freeze(panel_path, asof=asof, version=version)

    # 4) OOS 对照(最近完整月 vs ma5;诚实报告,不美化)
    from VolumePrediction.replication_legacy import cols_upto, make_model
    panel = pd.read_parquet(panel_path)
    d = panel.index.get_level_values("date")
    m_end = pd.Timestamp(asof).replace(day=1) - pd.Timedelta(days=1)
    m_start = m_end.replace(day=1)
    cols = cols_upto(panel, "earn")
    tr = panel[(d < m_start)]
    tr = tr[tr["eta"].notna()]
    te = panel[(d >= m_start) & (d <= m_end)]
    te = te[te["eta"].notna()]
    mdl = make_model("lgbm", len(cols))
    mdl.fit(tr[cols], tr["eta"])
    eh = mdl.predict(te[cols])
    pv = te["ma5_v"] + eh
    ss = lambda a, b: float(((a - b) ** 2).sum())
    r2 = 1 - ss(te["v"], pv) / ss(te["v"], te["v"].mean())
    r2_ma5 = 1 - ss(te["v"], te["ma5_v"]) / ss(te["v"], te["v"].mean())
    oos = {"oos_month": str(m_start.date()), "r2_logv": round(r2, 4),
           "r2_logv_ma5": round(r2_ma5, 4), "beats_ma5": bool(r2 > r2_ma5)}
    log.info(f"OOS 对照: {oos}")

    # 5) registry candidate(不晋升)
    from VolumePrediction.service import VolumeService
    svc = VolumeService()
    svc.registry.record_model(version, kind="learned.lgbm",
                              trained_through=asof,
                              metrics={**oos, "panel": tag}, status="candidate")
    log.info(f"记入 registry 为 candidate。晋升需人工: "
             f"svc.registry.promote('{version}', by='user')")
    return {**meta, "oos": oos}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    r = main(dry_run=a.dry_run)
    print(r)
    sys.exit(0)
