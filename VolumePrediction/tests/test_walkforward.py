"""单测: walkforward 引擎(P2⑩)+ VIF 共线诊断(P2④)。

合成小面板(两票×300 天,已知线性信号)驱动;输出**只写 /tmp/vp_tests/**
(生产目录 inventory*/trading_signals/price_data/historical_runs 零接触,
outputs/walkforward 生产件亦不写——经 out_dir 重定向,计划 §〇-6 纪律)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import shutil

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from VolumePrediction.evaluation.walkforward import (
    make_windows, industry_strata, size_decile_strata, stratified_r2, run)
from VolumePrediction.data.factor_proxy import vif_table, vif_prune
from VolumePrediction.tests.test_models_classic import make_panel

TMP = Path("/tmp/vp_tests/walkforward")
TMP.mkdir(parents=True, exist_ok=True)


def make_wf_panel(n_days=300, seed=0):
    """两票×300 天,eta 由前 3 个 tech 特征线性生成(可恢复信号);
    加行业哑变量(T0=SIC3/T1=SIC7)与市值列(T0 恒大于 T1 → 十分位 D10/D5)。"""
    X, y = make_panel(n_days=n_days, n_tickers=2, seed=seed)
    tick = X.index.get_level_values("ticker")
    X["fund2_ind_SIC3"] = (tick == "T0").astype(float)
    X["fund2_ind_SIC7"] = (tick == "T1").astype(float)
    rng = np.random.default_rng(seed + 1)
    X["fund1_size_ln_mcap"] = np.where(tick == "T0", 25.0, 20.0) \
        + rng.normal(0, 0.1, len(X))
    return X, y


PANEL, ETA = make_wf_panel()


# ── 窗口生成 ─────────────────────────────────────────────────────────────────

def test_make_windows_count_and_geometry():
    dates = PANEL.index.get_level_values("date").unique().sort_values()
    ws = make_windows(dates, train_months=6, test_months=2, step_months=2)
    # 300 bdays ≈ 2023-01-02→2024-02-23(~13.7 个月): 测试段起点 6/8/10/12 月处
    # 各有数据,第 5 窗(起点 14 月)测试段空 → 恰 4 窗
    assert len(ws) == 4
    for w in ws:
        assert w["test_start"] == w["train_end"]          # 无缝亦无重叠
        assert w["test_end"] > w["test_start"]
    # 相邻窗按 step 平移;末窗测试期溢出样本 → partial
    assert (ws[1]["train_start"] - ws[0]["train_start"]).days >= 56
    assert ws[-1]["partial"] and not ws[0]["partial"]


def test_make_windows_edge_cases():
    dates = PANEL.index.get_level_values("date").unique().sort_values()
    assert make_windows(dates[:30], 6, 2, 2) == []        # 数据不够一窗
    with pytest.raises(ValueError):
        make_windows(dates, 6, 0, 2)


# ── 分层键 ───────────────────────────────────────────────────────────────────

def test_industry_strata_from_dummies():
    s = industry_strata(PANEL)
    assert set(s.unique()) == {"SIC3", "SIC7"}
    assert (s[PANEL.index.get_level_values("ticker") == "T0"] == "SIC3").all()
    # 无哑变量列 → 全 UNK(不抛)
    assert (industry_strata(PANEL[["tech_f0", "eta"]]) == "UNK").all()


def test_size_decile_strata_and_monotone_invariance():
    s = size_decile_strata(PANEL)
    # 两票逐日 pct-rank = 0.5/1.0 → D5/D10;大市值票(T0)恒 D10
    assert set(s.unique()) == {"D5", "D10"}
    assert (s[PANEL.index.get_level_values("ticker") == "T0"] == "D10").all()
    # 逐日 z 化(逐日单调变换)不改变十分位——生产面板该列已 z 化的口径依据
    Z = PANEL.copy()
    Z["fund1_size_ln_mcap"] = (Z.groupby(level="date")["fund1_size_ln_mcap"]
                               .transform(lambda g: (g - g.mean()) / (g.std() or 1)))
    assert size_decile_strata(Z).equals(s)
    # 缺列 → 全 NA(不抛)
    assert (size_decile_strata(PANEL[["tech_f0", "eta"]]) == "NA").all()


def test_stratified_r2_matches_manual():
    yhat = ETA * 0.5                                       # 半幅预测
    st = industry_strata(PANEL)
    tbl = stratified_r2(ETA, yhat, st).set_index("stratum")
    for k in ("SIC3", "SIC7"):
        y = ETA[st == k]
        expect = 1 - ((y - 0.5 * y) ** 2).sum() / (y ** 2).sum()   # η 口径分母 Σy²
        assert abs(tbl.loc[k, "r2_eta"] - expect) < 1e-12
        assert tbl.loc[k, "n_obs"] == int((st == k).sum())


# ── 引擎端到端 ────────────────────────────────────────────────────────────────

def test_run_ols_end_to_end():
    out = TMP / "run_ols"
    shutil.rmtree(out, ignore_errors=True)   # 幂等: 增量 append 设计下残留会翻倍行数
    res = run(PANEL, models=["ols"], train_months=6, test_months=2,
              step_months=2, out_dir=out, run_tag="t1")
    assert res["n_windows"] == 4
    # per-window CSV: 4 窗×1 模型;线性信号可恢复 → 每窗 R² 高
    win = pd.read_csv(out / "wf_windows_t1.csv")
    assert len(win) == 4 and (win["model"] == "ols").all()
    assert (win["r2_eta"] > 0.5).all()
    assert (win["n_train"] > 0).all() and (win["n_test"] > 0).all()
    # 分层 CSV: global + 行业两层 + 十分位两层
    st = pd.read_csv(out / "wf_stratified_t1.csv")
    assert set(st["stratum_type"]) == {"global", "industry", "size_decile"}
    assert set(st.loc[st["stratum_type"] == "industry", "stratum"]) == {"SIC3", "SIC7"}
    assert set(st.loc[st["stratum_type"] == "size_decile", "stratum"]) == {"D5", "D10"}
    assert (st["r2_eta"] > 0.5).all()
    # summary.json: 全局 vs 分层对比结构
    js = json.loads((out / "summary.json").read_text())
    assert js["global_r2"]["ols"] > 0.5
    comp = js["comparison"]["ols"]
    assert set(comp) == {"global", "industry", "size_decile"}
    assert comp["industry"]["n_strata"] == 2
    assert comp["industry"]["min"] <= js["global_r2"]["ols"] <= comp["industry"]["max"] + 1e-9
    # 输出只落在传入 out_dir(/tmp)——生产 outputs/walkforward 零写入
    assert str(out).startswith("/tmp/")


def test_run_injected_factory_and_multi_model():
    shutil.rmtree(TMP / "run_inject", ignore_errors=True)
    """模型注入式: 任意 BaseModel 兼容工厂 + 注册表名混跑。"""
    from VolumePrediction.models.linear import PLSModel

    def custom_factory(n_pred: int, seed: int):
        return PLSModel(n_components=3), {}

    out = TMP / "run_inject"
    res = run(PANEL, models=["ols", "pls3_custom"],
              factories={"pls3_custom": custom_factory},
              train_months=6, test_months=2, step_months=2,
              out_dir=out, run_tag="t2")
    assert set(res["global_r2"]) == {"ols", "pls3_custom"}
    assert res["global_r2"]["pls3_custom"] > 0.5
    win = pd.read_csv(out / "wf_windows_t2.csv")
    assert len(win) == 8                                  # 4 窗 × 2 模型


def test_deep_models_gated():
    """MPS/libomp 纪律: 深模型(torch/lightgbm)默认拒绝,须显式 allow_deep。"""
    with pytest.raises(ValueError, match="deep"):
        run(PANEL, models=["nn"], train_months=6, test_months=2,
            step_months=2, out_dir=TMP / "gate")
    with pytest.raises(ValueError, match="unknown model"):
        run(PANEL, models=["nope"], train_months=6, test_months=2,
            step_months=2, out_dir=TMP / "gate")


# ── VIF(P2④) ───────────────────────────────────────────────────────────────

def _collinear_df(n=400, seed=0):
    rng = np.random.default_rng(seed)
    x1 = rng.normal(size=n)
    return pd.DataFrame({
        "x1": x1,
        "x2": x1 + rng.normal(0, 0.01, n),   # 与 x1 近乎共线 → VIF 巨大
        "x3": rng.normal(size=n),            # 独立 → VIF≈1
    })


def test_vif_table_flags_collinear():
    tbl = vif_table(_collinear_df()).set_index("feature")
    assert tbl.loc["x1", "vif"] > 100 and tbl.loc["x2", "vif"] > 100
    assert tbl.loc["x3", "vif"] < 2
    assert tbl.index[0] in ("x1", "x2")                   # 降序
    # 常量列被截距完全解释 → inf
    df = _collinear_df()
    df["const"] = 1.0
    assert np.isinf(vif_table(df).set_index("feature").loc["const", "vif"])


def test_vif_table_matches_statsmodels():
    """手写 R² 法 vs statsmodels variance_inflation_factor 数值对拍。"""
    sm_tools = pytest.importorskip("statsmodels.stats.outliers_influence")
    rng = np.random.default_rng(3)
    base = rng.normal(size=(300, 2))
    df = pd.DataFrame({
        "a": base[:, 0],
        "b": 0.6 * base[:, 0] + 0.8 * base[:, 1],         # 温和相关
        "c": rng.normal(size=300),
    })
    ours = vif_table(df).set_index("feature")["vif"]
    exog = np.hstack([np.ones((len(df), 1)), df.values])  # 标准用法: 加截距列
    for j, col in enumerate(df.columns):
        ref = sm_tools.variance_inflation_factor(exog, j + 1)
        assert abs(ours[col] - ref) < 1e-6, f"{col}: {ours[col]} vs {ref}"


def test_vif_prune_drops_exactly_one_of_pair():
    df = _collinear_df()
    kept = vif_prune(df, threshold=10.0)
    assert "x3" in kept
    assert len(kept) == 2                                 # x1/x2 只剔其一
    assert len({"x1", "x2"} & set(kept)) == 1
    assert kept == [c for c in df.columns if c in kept]   # 保持原列序
    # 全部低共线 → 原样保留
    clean = pd.DataFrame(np.random.default_rng(5).normal(size=(200, 3)),
                         columns=list("abc"))
    assert vif_prune(clean, threshold=10.0) == ["a", "b", "c"]


def test_vif_input_validation():
    df = _collinear_df(n=400)
    with pytest.raises(ValueError):
        vif_table(df[["x1"]])                             # <2 列
    with pytest.raises(ValueError):
        vif_table(df.head(3))                             # 行数不足
