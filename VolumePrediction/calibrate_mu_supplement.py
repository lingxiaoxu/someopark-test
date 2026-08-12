"""
calibrate_mu_supplement — E11-T2 runner: μ 补两腿(pairs OU / ssrs 合成动量)
==========================================================================
只写 outputs/registry/mu_calibration.json 的 pairs_decay / ssrs_mom_decay 键
(经 service._Econ 的原子写路径)+ profiles 重解析 + diagnostics 附录;
aiss(已实测)与 lambda_all 键不动。报告(含六 profiles 新旧对照)落
outputs/econ_v2/mu_supplement_report.md。

运行(仓库根):
  conda run -n someopark_run python -m VolumePrediction.calibrate_mu_supplement
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pandas as pd

from VolumePrediction.common import REPO, get_logger
from VolumePrediction.service import VolumeService
from VolumePrediction.calibrate_mu import build_close_matrix, decay_curve_from_panel
from VolumePrediction.econ import calibration as cal
from VolumePrediction.econ.mu_supplement import (
    pairs_ou_retention, synthetic_momentum_panel, SECTOR_ETFS,
)

log = get_logger("calibrate_mu_supplement")

OUT = Path(__file__).resolve().parent / "outputs"


def load_pair_universe() -> list[tuple[str, str]]:
    """当前 mrpt+mtfs pair universe(s1,s2)去重合并。"""
    pairs = []
    for st in ("mrpt", "mtfs"):
        p = REPO / f"pair_universe_{st}.json"
        if not p.exists():
            continue
        for row in json.loads(p.read_text()):
            s1, s2 = row.get("s1"), row.get("s2")
            if s1 and s2:
                pairs.append((s1, s2))
    return sorted(set(pairs))


def resolve_profiles(svc: VolumeService) -> dict:
    from VolumePrediction.econ import objective as obj
    out = {}
    for name, prof in sorted(obj.registry().items()):
        mu, src = obj.resolve_mu(prof, artifacts_dir=svc.art)
        out[name] = {"mode": prof.mode, "mu_source": prof.mu_source,
                     "mu_key": prof.mu_key,
                     "mu": ("inf" if isinstance(mu, float) and math.isinf(mu) else mu),
                     "calibration_source": src}
    return out


def main(lookback_days: int = 400) -> dict:
    svc = VolumeService()
    warnings: list[str] = []
    profiles_before = resolve_profiles(svc)
    end = svc._last_raw_date() or pd.Timestamp.now().strftime("%Y-%m-%d")
    start = (pd.Timestamp(end) - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    # ① pairs: OU 半衰期 → 保留曲线 → calibrate_mu_momentum(strategy='pairs')
    pairs = load_pair_universe()
    tickers = sorted({t for pr in pairs for t in pr})
    closes = build_close_matrix(tickers, start, end, warnings)
    curve_p, diag_p = pairs_ou_retention(closes, pairs)
    if curve_p is None:
        warnings.append("pairs OU retention unavailable → paper prior")
    res_pairs = cal.calibrate_mu_momentum(curve_p, strategy="pairs")
    if res_pairs["calibration_source"] == "alpha_decay_curve":
        res_pairs["calibration_source"] = "ou_half_life"   # 来源如实标注(验收①)
    svc.econ._write_mu("pairs_decay", res_pairs)
    log.info(f"pairs μ (OU): {res_pairs}")

    # ② ssrs: 自有 raw bars 的 ETF 价 → 合成动量面板 → 同一事件/断裂/加权逻辑
    ssrs_start = "2018-01-01"
    etf_closes = build_close_matrix(SECTOR_ETFS, ssrs_start, end, warnings)
    panel = synthetic_momentum_panel(etf_closes)
    curve_s, diag_s = decay_curve_from_panel(panel)
    diag_s["n_panel_days"] = len(panel)
    diag_s["signal"] = "synthetic 12-1 momentum from own raw ETF closes"
    if curve_s is None:
        warnings.append("ssrs synthetic momentum curve unavailable → paper prior")
    res_ssrs = cal.calibrate_mu_momentum(curve_s, strategy="ssrs")
    if res_ssrs["calibration_source"] == "alpha_decay_curve":
        res_ssrs["calibration_source"] = "synthetic_momentum_decay"
    svc.econ._write_mu("ssrs_mom_decay", res_ssrs)
    log.info(f"ssrs μ (synthetic momentum): {res_ssrs}")

    # ③ profiles 重解析 + diagnostics 附录(不动 aiss/lambda 键)
    profiles_after = resolve_profiles(svc)
    svc.econ._write_mu("profiles", profiles_after)
    reg_path = svc.art / "registry" / "mu_calibration.json"
    data = json.loads(reg_path.read_text())
    data.setdefault("diagnostics", {})["t2_supplement"] = {
        "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "pairs_ou": diag_p, "ssrs_synthetic": diag_s, "warnings": warnings,
    }
    tmp = reg_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    tmp.replace(reg_path)

    # ④ 报告: 六 profiles 新旧对照(验收②)
    lines = [
        "# μ 补两腿报告(E11-T2: pairs=OU half-life / ssrs=合成动量)",
        f"\n生成: {pd.Timestamp.now().isoformat(timespec='seconds')}",
        f"\n## pairs(OU): μ = **{res_pairs['mu']:.3e}**"
        f"(source={res_pairs['calibration_source']},"
        f" HL 中位 {diag_p.get('hl_median')},有效对 {diag_p.get('n_valid_hl')}"
        f"/{diag_p.get('n_pairs')})",
        f"\n## ssrs(合成动量): μ = **{res_ssrs['mu']:.3e}**"
        f"(source={res_ssrs['calibration_source']},"
        f" 事件 {diag_s.get('n_events')},面板天数 {diag_s.get('n_panel_days')},"
        f" alpha_by_delay={diag_s.get('alpha_by_delay')})",
        "\n## 六 profiles 新旧对照",
        "| profile | mode | μ 旧 | 来源旧 | μ 新 | 来源新 |",
        "|---|---|---|---|---|---|",
    ]
    for name in sorted(profiles_after):
        b, a = profiles_before[name], profiles_after[name]
        lines.append(f"| {name} | {a['mode']} | {b['mu']} | "
                     f"{b['calibration_source']} | {a['mu']} | "
                     f"{a['calibration_source']} |")
    lines.append("\n注: aiss_mom_decay 与 lambda_all 键本 runner 不动;"
                 "消费切换仍待 8/15 E1 决策(μ 值仅影响 shadow 工件)。")
    rpt = OUT / "econ_v2" / "mu_supplement_report.md"
    rpt.parent.mkdir(parents=True, exist_ok=True)
    rpt.write_text("\n".join(lines))
    log.info(f"report -> {rpt}")

    return {"pairs_decay": res_pairs, "ssrs_mom_decay": res_ssrs,
            "profiles": profiles_after, "warnings": warnings}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="mu supplement runner (E11-T2)")
    ap.add_argument("--lookback-days", type=int, default=400)
    a = ap.parse_args()
    res = main(a.lookback_days)
    print(json.dumps({k: res[k] for k in ("pairs_decay", "ssrs_mom_decay",
                                          "warnings")},
                     indent=2, ensure_ascii=False, default=str))
