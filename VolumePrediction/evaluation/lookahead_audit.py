"""
evaluation/lookahead_audit — 弱点⑨: 前视偏差显式审计
=====================================================
三层审计(全部可证明断言,非抽象口号):

1) 结构断言(逐票抽样): A14 之后
   - tech_v_ma1(t)  == v(t−1)
   - tech_ret_ma1(t) == ret(t−1)
   - tech_v_ma5(t)  == ma5_v(t)   ← 目标基线(前5日均)与移位后特征的交叉一致性
   - eta(t) == v(t) − ma5_v(t),且 ma5_v(t) == mean(v_{t-5..t-1})(不含当日)
2) 合成序列证明 prove_shift_correctness(): 构造已知递增序列走完整管道,
   逐元素断言 (X,y) 配对与"目标上移一行并 drop last"的旧 notebook 布局完全相同
3) 基本面 PIT 核(接口注入 availability_dates): fund1_/fund2_ 列的每个"值变化日"
   之前必须存在一个 acceptedDate(≤ 变化日−1);无任何 acceptedDate 先于变化日 → 违规

输出: report dict {passed, checks:[{name,passed,detail}], violations:[...]}
      + to_markdown(report)。测试临时文件仅 /tmp/vp_tests/。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

_TOL = 1e-9


def _sample_tickers(panel: pd.DataFrame, n: int, seed: int) -> List[str]:
    tks = sorted(panel.index.get_level_values("ticker").unique())
    rng = np.random.default_rng(seed)
    if len(tks) <= n:
        return list(tks)
    return list(rng.choice(tks, size=n, replace=False))


def _check(name: str, cond: bool, detail: str = "") -> dict:
    return {"name": name, "passed": bool(cond), "detail": detail}


def audit(
    panel: pd.DataFrame,
    features_meta: Optional[Dict[str, dict]] = None,
    n_tickers: int = 10,
    seed: int = 7,
    availability_dates: Optional[Dict[str, Sequence]] = None,
) -> dict:
    """对 A14 之后的面板做前视审计。panel 需含 v/ma5_v/eta 与 tech_ 列。"""
    checks: List[dict] = []
    violations: List[str] = []
    tickers = _sample_tickers(panel, n_tickers, seed)

    for tk in tickers:
        sub = panel.xs(tk, level="ticker")
        v = sub["v"]

        # 1a) tech_v_ma1(t) == v(t−1)
        if "tech_v_ma1" in sub.columns:
            expect = v.shift(1)
            got = sub["tech_v_ma1"]
            mask = expect.notna() & got.notna()
            ok = bool(np.allclose(got[mask], expect[mask], atol=_TOL, equal_nan=True))
            checks.append(_check(f"{tk}:tech_v_ma1==v(t-1)", ok))
            if not ok:
                gm = got[mask]
                bad = gm.index[~np.isclose(gm.values, expect[mask].values, atol=_TOL)][:3]
                violations.append(f"{tk} tech_v_ma1 前视: 例 {list(map(str, bad))}")

        # 1b) tech_ret_ma1(t) == ret(t−1)
        if "tech_ret_ma1" in sub.columns and "ret" in sub.columns:
            expect = sub["ret"].shift(1)
            got = sub["tech_ret_ma1"]
            mask = expect.notna() & got.notna()
            ok = bool(np.allclose(got[mask], expect[mask], atol=_TOL))
            checks.append(_check(f"{tk}:tech_ret_ma1==ret(t-1)", ok))
            if not ok:
                violations.append(f"{tk} tech_ret_ma1 前视")

        # 1c) tech_v_ma5(t) == ma5_v(t)(移位后特征 == 目标基线,双定义交叉)
        if "tech_v_ma5" in sub.columns and "ma5_v" in sub.columns:
            got, expect = sub["tech_v_ma5"], sub["ma5_v"]
            mask = expect.notna() & got.notna()
            ok = bool(np.allclose(got[mask], expect[mask], atol=_TOL))
            checks.append(_check(f"{tk}:tech_v_ma5==ma5_v", ok))
            if not ok:
                violations.append(f"{tk} tech_v_ma5 与 ma5_v 不一致(移位或窗口定义错)")

        # 1d) eta 与 ma5_v 定义(前5日均,不含当日)
        if {"eta", "ma5_v"}.issubset(sub.columns):
            manual_ma5 = v.shift(1).rolling(5, min_periods=5).mean()
            m1 = manual_ma5.notna() & sub["ma5_v"].notna()
            ok1 = bool(np.allclose(sub["ma5_v"][m1], manual_ma5[m1], atol=_TOL))
            m2 = sub["eta"].notna()
            ok2 = bool(np.allclose(sub["eta"][m2], (v - sub["ma5_v"])[m2], atol=_TOL))
            checks.append(_check(f"{tk}:ma5_v 前5日定义", ok1))
            checks.append(_check(f"{tk}:eta==v−ma5_v", ok2))
            if not ok1:
                violations.append(f"{tk} ma5_v 含当日(目标定义错)")
            if not ok2:
                violations.append(f"{tk} eta 定义不一致")

        # 3) 基本面 PIT: 值变化日前必须有 acceptedDate
        if availability_dates and tk in availability_dates:
            acc = pd.to_datetime(pd.Index(list(availability_dates[tk]))).normalize()
            fund_cols = [c for c in sub.columns
                         if c.startswith("fund1_") or c.startswith("fund2_")]
            for c in fund_cols:
                s = sub[c].dropna()
                if len(s) < 2:
                    continue
                changed = s.index[1:][~np.isclose(s.values[1:], s.values[:-1], atol=_TOL)]
                for d in changed:
                    if not (acc < d).any():
                        violations.append(
                            f"{tk} {c} 在 {d.date()} 变化但无更早 acceptedDate(PIT 违规)")
                ok = all((acc < d).any() for d in changed)
                checks.append(_check(f"{tk}:{c} PIT", ok, f"{len(changed)} 次变化"))

    # 2) 合成证明(全局一次)
    proof = prove_shift_correctness()
    checks.append(_check("prove_shift_correctness", proof["passed"], proof["detail"]))
    if not proof["passed"]:
        violations.append("合成序列移位证明失败: " + proof["detail"])

    passed = all(c["passed"] for c in checks) and not violations
    return {"passed": passed, "n_tickers_sampled": len(tickers),
            "checks": checks, "violations": violations}


def prove_shift_correctness() -> dict:
    """合成已知序列,证明本管道布局与旧 notebook"目标上移 drop last"逐元素等价。"""
    from VolumePrediction.features import pipeline as fp

    n = 40
    dates = pd.date_range("2024-01-02", periods=n, freq="B")
    v_seq = np.arange(n, dtype=float)          # v_t = t → 一切均值/移位可手算
    df = pd.DataFrame({
        "V": np.exp(v_seq),
        "close": 100 * np.cumprod(1 + 0.01 * np.ones(n)),
    }, index=pd.MultiIndex.from_product([dates, ["SYN"]], names=["date", "ticker"]))

    p = fp.add_volume_features(df)
    p = fp.add_return_rollups(p)
    shifted = fp.shift_volume_columns_and_drop_last(p)

    # 本布局: 行 t 的 (tech_v_ma1, y=eta_t)
    ours = shifted[["tech_v_ma1", "eta"]].xs("SYN", level="ticker").dropna()

    # 旧布局: 特征不移,目标上移一行,drop last
    legacy = p.xs("SYN", level="ticker").copy()
    legacy["y"] = legacy["eta"].shift(-1)
    legacy = legacy.iloc[:-1]
    legacy_pairs = legacy[["tech_v_ma1", "y"]].dropna()
    # 对齐比较: 我方行 t ↔ 旧方行 t−1(同一 (X,y) 对)
    ours_vals = list(zip(ours["tech_v_ma1"].round(9), ours["eta"].round(9)))
    legacy_vals = list(zip(legacy_pairs["tech_v_ma1"].round(9), legacy_pairs["y"].round(9)))
    same = ours_vals == legacy_vals
    # 附加手算锚点: v_t=t → ma5_v(t)=t−3 → eta=3(t≥6);tech_v_ma1(t)=t−1
    anchor = bool(np.allclose(ours["eta"].iloc[-1], 3.0) and
                  np.allclose(ours["tech_v_ma1"].iloc[-1], v_seq[-2]))
    return {"passed": bool(same and anchor),
            "detail": f"pairs_equal={same}, hand_anchor={anchor}, n={len(ours_vals)}"}


def to_markdown(report: dict) -> str:
    lines = ["# Lookahead Audit Report",
             f"- passed: **{report['passed']}**",
             f"- tickers sampled: {report['n_tickers_sampled']}",
             f"- checks: {sum(c['passed'] for c in report['checks'])}/{len(report['checks'])} 通过", ""]
    if report["violations"]:
        lines.append("## Violations")
        lines += [f"- {v}" for v in report["violations"]]
    else:
        lines.append("## Violations\n- 无")
    failed = [c for c in report["checks"] if not c["passed"]]
    if failed:
        lines.append("\n## Failed checks")
        lines += [f"- {c['name']}: {c['detail']}" for c in failed]
    return "\n".join(lines)
