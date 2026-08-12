"""
calibrate_lambda — E11-T1 runner: λ 市场代理单轨校准(Amihud)
==============================================================
EXTENSION_PLAN E11-T1 的落地执行器。自家 fills 轨物理不可测(发现②,
信噪 ~1:10⁴)→ λ 只走市场代理:全市场日线(自有 raw grouped bars,2,287 天)
按 PIT universe 取截面,Amihud ILLIQ 对美元量 log-log 回归:

    λ(V) = C · V^(−γ)      论文先验 C=0.2, γ=1(econ/policy FORM_MAIN)
    log(ILLIQ̄_i) = log C − γ·log($V̄_i)

产物(**8/15 前不接线**,生产 econ/policy 不读):
  outputs/registry/lambda_calibration.json   最新校准 + PIT 滚动序列 + 分层表
  outputs/econ_v2/lambda_calibration_report.md  验收报告(γ vs 1 偏离、
      新旧 λ 的 s*(v̄) 对照、分层 λ、滚动稳定性)

运行(仓库根):
  conda run -n someopark_run python -m VolumePrediction.calibrate_lambda
  可选: --years 3(面板年数) --window 252 --quarterly-since 2020-01-01
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import pandas as pd

from VolumePrediction.common import REPO, get_logger
from VolumePrediction.data import polygon_loader
from VolumePrediction.data.universe import membership
from VolumePrediction.econ.lambda_calibration import (
    amihud_panel, calibrate_lambda_amihud, rolling_calibration,
    s_curve_comparison, PAPER_C, PAPER_GAMMA, DEFAULT_WINDOW,
)

log = get_logger("calibrate_lambda")

OUT = Path(__file__).resolve().parent / "outputs"


def _available_days() -> list[str]:
    raw = REPO / "price_data" / "volume_prediction" / "raw"
    return sorted(p.stem.replace("grouped_", "") for p in raw.glob("grouped_*.parquet"))


def build_panel(days: list[str]) -> pd.DataFrame:
    """raw grouped bars → 长表(只留 PIT universe 成员;逐年取 membership)。"""
    frames = []
    memb_cache: dict[str, set] = {}
    for d in days:
        try:
            df = polygon_loader.load_day(d)
        except FileNotFoundError:
            continue
        y = d[:4]
        if y not in memb_cache:
            try:
                memb_cache[y] = set(membership(date.fromisoformat(f"{y}-06-30")))
            except Exception as e:  # noqa: BLE001 — 早年 vintage 缺失如实降级为全市场
                log.warning(f"membership({y}) unavailable ({e}) — full market cross-section")
                memb_cache[y] = set()
        m = memb_cache[y]
        sub = df[df["ticker"].isin(m)] if m else df
        frames.append(sub[["ticker", "c", "v", "vw"]].assign(date=d))
    panel_bars = pd.concat(frames, ignore_index=True)
    log.info(f"bars loaded: {len(panel_bars):,} rows / {len(days)} days")
    return amihud_panel(panel_bars)


def write_report(latest: dict, roll: pd.DataFrame, scurve: pd.DataFrame,
                 path: Path) -> None:
    dev = latest.get("deviation") or {}
    lines = [
        "# λ 校准报告 — 市场代理单轨(Amihud;E11-T1)",
        f"\n生成: {pd.Timestamp.now().isoformat(timespec='seconds')}  "
        f"asof: {latest['asof']}  source: {latest['calibration_source']}",
        "\n## 结果 vs 论文先验",
        f"- C = **{latest['C']:.4g}**(论文 {PAPER_C});"
        f"log 偏离 {dev.get('logC_minus_log_paper', float('nan')):+.2f}",
        f"- γ = **{latest['gamma']:.4f}**(论文 {PAPER_GAMMA});"
        f"偏离 γ−1 = {dev.get('gamma_minus_1', float('nan')):+.4f}",
        f"- 截面 R² = {latest['r2']:.3f},n_names = {latest['n_names']}"
        f",窗口 = {latest['window']}",
        "\nγ≈1 ⇒ λ∝1/V 形状成立;γ<1 ⇒ 大票冲击衰减慢于论文假设(λ 尾部更贵),"
        "γ>1 反之。C 定绝对刻度。",
        "\n## 分流动性层(实测中位 vs 拟合 vs 论文)",
        "| tier | $V 中位 | n | λ 实测中位 | λ 拟合 | λ 论文 0.2/V |",
        "|---|---|---|---|---|---|",
    ]
    for t, v in latest["tiers"].items():
        lines.append(f"| {t} | {v['dv_median']:.3g} | {v['n_names']} "
                     f"| {v['lambda_observed_median']:.3g} "
                     f"| {v['lambda_fitted']:.3g} | {v['lambda_paper']:.3g} |")
    lines += [
        "\n## PIT 滚动稳定性(逐季独立校准)",
        "| asof | C | γ | R² | n |", "|---|---|---|---|---|",
    ]
    for _, r in roll.iterrows():
        r2s = f"{r['r2']:.3f}" if pd.notna(r["r2"]) else "—"
        lines.append(f"| {r['asof']} | {r['C']:.4g} | {r['gamma']:.4f} "
                     f"| {r2s} | {int(r['n_names'])} |")
    lines += [
        "\n## s*(v̄) 新旧对照(z* = μ/(μ+λ);验收件②)",
        "| μ | $V | s* 论文 | s* 校准 | Δ |", "|---|---|---|---|---|",
    ]
    for _, r in scurve.iterrows():
        lines.append(f"| {r['mu']} | {r['dollar_volume']:.0e} "
                     f"| {r['s_paper']:.4f} | {r['s_calibrated']:.4f} "
                     f"| {r['delta']:+.4f} |")
    lines.append("\n**接线状态: 未接线(8/15 决策前冻结);生产 λ 仍为论文先验。**")
    path.write_text("\n".join(lines))
    log.info(f"report -> {path}")


def main(years: int, window: int, quarterly_since: str) -> dict:
    all_days = _available_days()
    if not all_days:
        raise SystemExit("no raw grouped bars — run polygon backfill first")
    latest_day = all_days[-1]
    start = (pd.Timestamp(quarterly_since) - pd.Timedelta(days=550)).strftime("%Y-%m-%d")
    days = [d for d in all_days if d >= min(start,
            (pd.Timestamp(latest_day) - pd.DateOffset(years=years)).strftime("%Y-%m-%d"))]
    panel = build_panel(days)

    latest = calibrate_lambda_amihud(panel, latest_day, window_days=window)
    asofs = [str(q.date()) for q in
             pd.date_range(quarterly_since, latest_day, freq="QS")] + [latest_day]
    roll = rolling_calibration(panel, asofs, window_days=window)
    scurve = s_curve_comparison(latest)

    artifact = {
        "lambda_amihud": latest,
        "rolling": roll.to_dict(orient="records"),
        "s_curve_comparison": scurve.to_dict(orient="records"),
        "wiring": "NOT WIRED (pre-8/15 freeze; production uses paper prior 0.2/V)",
        "method": "cross-sectional log(ILLIQ)~log($V) OLS on trailing "
                  f"{window}d per-name means; PIT universe membership",
    }
    reg = OUT / "registry" / "lambda_calibration.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    tmp = reg.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(artifact, indent=2, ensure_ascii=False, default=str))
    tmp.replace(reg)
    log.info(f"registry -> {reg}")

    rpt = OUT / "econ_v2" / "lambda_calibration_report.md"
    rpt.parent.mkdir(parents=True, exist_ok=True)
    write_report(latest, roll, scurve, rpt)
    return latest


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="λ market-proxy calibration (E11-T1)")
    ap.add_argument("--years", type=int, default=3, help="面板回溯年数(最新点)")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    ap.add_argument("--quarterly-since", default="2020-01-01")
    a = ap.parse_args()
    res = main(a.years, a.window, a.quarterly_since)
    print(json.dumps({k: res[k] for k in
                      ("C", "gamma", "r2", "n_names", "calibration_source")},
                     indent=2, default=str))
