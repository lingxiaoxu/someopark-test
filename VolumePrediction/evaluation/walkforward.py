"""
walkforward — P2⑩: 滚动训练/验证引擎 + 行业/市值十分位分层 OOS 报告
==================================================================
论文协议(G1)是前 3 年训练/后 2 年测试的**单一切分**——复现表只走 replication_g;
本引擎是计划 §九 P2⑩ 的扩展轨(替代旧作单切分,"两不混"),另行报告。

协议:
- 滚动窗: train_window/test_window/step 以**月**为单位,自面板首日锚定;
  每窗 [train_start, train_end) 训练 → [train_end, test_end) OOS 预测;
  末窗测试期允许不满(partial 标注),测试期为空即停
- 模型注入: 与 replication_g.make_model 同契约的工厂
  factory(n_pred, seed) -> (BaseModel, fit_kwargs)——BaseModel 见 models/__init__;
  内置注册表默认只含**轻模型**(ols/lassocv/pcr5/pls5/fwdstep/adaboost,
  零 torch/零 lightgbm import);深模型(nn/rnn/nn2/lgbm)须 allow_deep/--deep 显式开启
- MPS/libomp 纪律(tests/conftest 实测三轮定案): pip-lightgbm 与 torch 的 libomp
  **同进程共存不可靠**——默认轻模型路径二者都不 import;开 --deep 后
  LightGBMModel 自带 torch 检测→干净子进程隔离(models/ml.py),与 nn/rnn 顺序不敏感
- 多 seed: 深(随机)模型每窗 seeds 次训练取预测均值(论文 A.1 的 5 种子协议,
  DEV_CONTRACTS: 平均由本调用侧负责);轻模型确定性,单 seed;--quick 全部单 seed+减 epochs
- 分层报告(靶点⑩核心): 各模型 OOS 预测跨窗合并后按
  ① 行业——fund2_ind* 哑变量 argmax 还原(哑变量在 A13 不做 z 化,恒 0/1;
    全 0 行=UNK) ② 市值十分位——fund1_size_ln_mcap 逐日 pct-rank → 1..10
    (面板该列已逐日横截面 z 化,z 是逐日严格单调变换 → 十分位不受影响;
    缺失/零填充行照常参与排名,报告口径与生产面板一致)
  分组算 η 口径 OOS R²(分母 Σy²,与 replication_g._r2_eta / metrics.oos_r2_eta
  完全一致——ma5 恒为 0% 的论文归一);全局 vs 分层对比进 summary(min/max/加权均值)
- 产物(默认 outputs/walkforward/;测试经 out_dir 重定向 /tmp,生产零污染):
  wf_windows_{tag}.csv   每窗×模型一行,**逐窗 append=增量落盘**(中断不失已算窗)
  wf_stratified_{tag}.csv 每模型完成即 append(global/industry/size_decile 三类行)
  summary.json           每模型完成即原子重写(tmp+rename)

用法:
  python -m VolumePrediction.evaluation.walkforward --panel paper_full_v4 --models ols
      [--train-months 24 --test-months 6 --step-months 6] [--seeds 5] [--deep] [--quick]
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

from VolumePrediction.common import OUT, get_logger
from VolumePrediction.evaluation.metrics import oos_r2_eta, r2_v_total
from VolumePrediction.models import BaseModel, feature_cols

log = get_logger("walkforward")
WF_DIR = OUT / "walkforward"

# 工厂契约: (n_pred, seed) -> (model, fit_kwargs)
ModelFactory = Callable[[int, int], Tuple[BaseModel, dict]]

# 轻模型(零 torch/零 lightgbm)——walkforward 默认全集;确定性 → 单 seed
LIGHT_MODELS = ("ols", "lassocv", "pcr5", "pls5", "fwdstep", "adaboost")
# 深/重模型——须 allow_deep;nn/rnn/nn2 引 torch,lgbm 自带子进程隔离(见模块头)
DEEP_MODELS = ("nn", "rnn", "nn2", "lgbm")
# 需要多 seed 平均的随机训练模型(论文 A.1;lgbm/adaboost 固定 random_state 确定性)
STOCHASTIC_MODELS = frozenset({"nn", "rnn", "nn2"})


def registry_factory(kind: str, quick: bool = False) -> ModelFactory:
    """内置注册表 → 工厂(与 replication_g.make_model 同契约,覆盖全模型库)。"""
    def _light(cls):
        def f(n_pred: int, seed: int):
            return cls(), {}
        return f

    if kind == "ols":
        from VolumePrediction.models.linear import OLSModel
        return _light(OLSModel)
    if kind == "lassocv":
        from VolumePrediction.models.linear import LassoCVModel
        return _light(LassoCVModel)
    if kind == "pcr5":
        from VolumePrediction.models.linear import PCRModel
        return _light(PCRModel)
    if kind == "pls5":
        from VolumePrediction.models.linear import PLSModel
        return _light(PLSModel)
    if kind == "fwdstep":
        from VolumePrediction.models.linear import ForwardStepwiseModel
        return _light(ForwardStepwiseModel)
    if kind == "adaboost":
        from VolumePrediction.models.ml import AdaBoostModel
        return _light(AdaBoostModel)
    if kind == "nn":
        from VolumePrediction.models.deep import PaperNN
        return lambda n, s: (PaperNN(n, seed=s), {"epochs": 5} if quick else {})
    if kind == "rnn":
        from VolumePrediction.models.deep import PaperRNN
        return lambda n, s: (PaperRNN(n, seed=s), {"epochs": 3} if quick else {})
    if kind == "nn2":
        from VolumePrediction.models.ml import NN2Model
        return lambda n, s: (NN2Model(seed=s, epochs=10 if quick else 50), {})
    if kind == "lgbm":
        from VolumePrediction.models.ml import LightGBMModel
        return lambda n, s: (LightGBMModel(random_state=s), {})
    raise ValueError(f"unknown model kind {kind!r} "
                     f"(registry: {LIGHT_MODELS + DEEP_MODELS})")


# ── 窗口生成 ─────────────────────────────────────────────────────────────────

def make_windows(dates: pd.DatetimeIndex, train_months: int,
                 test_months: int, step_months: int) -> List[dict]:
    """自首日锚定的滚动窗清单。

    每窗: train=[t0+i·step, +train_months), test=[train_end, +test_months);
    测试期为空即停;末窗测试不满标 partial。返回 [{window_id, train_start,
    train_end, test_start, test_end, partial}](边界均为名义日期,半开区间)。
    """
    if step_months <= 0 or train_months <= 0 or test_months <= 0:
        raise ValueError("train/test/step months must all be positive")
    dates = pd.DatetimeIndex(dates).sort_values().unique()
    if len(dates) == 0:
        return []
    t0, t_last = dates[0], dates[-1]
    out: List[dict] = []
    i = 0
    while True:
        tr_start = t0 + pd.DateOffset(months=i * step_months)
        tr_end = tr_start + pd.DateOffset(months=train_months)   # 半开
        te_end = tr_end + pd.DateOffset(months=test_months)
        n_train = int(((dates >= tr_start) & (dates < tr_end)).sum())
        n_test = int(((dates >= tr_end) & (dates < te_end)).sum())
        if n_test == 0 or n_train == 0:
            break
        out.append({"window_id": i, "train_start": tr_start,
                    "train_end": tr_end, "test_start": tr_end,
                    "test_end": te_end,
                    "partial": bool(te_end - pd.Timedelta(days=1) > t_last)})
        i += 1
    return out


# ── 分层键还原 ────────────────────────────────────────────────────────────────

def industry_strata(panel: pd.DataFrame) -> pd.Series:
    """行业分层键: fund2_ind* 哑变量 argmax 还原;全 0/无哑变量列 → 'UNK'。"""
    ind_cols = [c for c in panel.columns if c.startswith("fund2_ind")]
    if not ind_cols:
        log.warning("no fund2_ind* columns — industry strata all 'UNK'")
        return pd.Series("UNK", index=panel.index, name="industry")
    vals = panel[ind_cols].to_numpy(dtype=float)
    labels = np.array([c.replace("fund2_ind_", "") for c in ind_cols])
    strata = np.where(np.nanmax(vals, axis=1) > 0.5,
                      labels[np.nanargmax(vals, axis=1)], "UNK")
    return pd.Series(strata, index=panel.index, name="industry")


def size_decile_strata(panel: pd.DataFrame, n: int = 10) -> pd.Series:
    """市值十分位分层键: fund1_size_ln_mcap 逐日 pct-rank → 'D1'..'D10'。

    面板该列已逐日横截面 z 化——z 为逐日严格单调变换,十分位与原始 ln(mcap)
    完全一致;缺列时返回全 'NA' 并 warning(不抛,行业分层照常出)。
    """
    col = "fund1_size_ln_mcap"
    if col not in panel.columns:
        log.warning(f"no {col} column — size deciles all 'NA'")
        return pd.Series("NA", index=panel.index, name="size_decile")
    pct = (panel[col].groupby(level="date").rank(pct=True, method="first"))
    dec = np.ceil(pct * n).clip(1, n)
    out = pd.Series([f"D{int(d)}" if pd.notna(d) else "NA" for d in dec],
                    index=panel.index, name="size_decile")
    return out


def stratified_r2(eta_true: pd.Series, eta_hat: pd.Series,
                  strata: pd.Series) -> pd.DataFrame:
    """按分层键分组的 η 口径 OOS R²(分母 Σy²,同 oos_r2_eta)。"""
    rows = []
    st = strata.reindex(eta_true.index)
    for key, idx in eta_true.groupby(st).groups.items():
        rows.append({"stratum": str(key), "n_obs": int(len(idx)),
                     "r2_eta": oos_r2_eta(eta_true.loc[idx], eta_hat.loc[idx])})
    return (pd.DataFrame(rows).sort_values("n_obs", ascending=False)
            .reset_index(drop=True))


# ── 增量落盘小工具 ────────────────────────────────────────────────────────────

def _append_rows(path: Path, rows: List[dict]) -> None:
    """CSV append(首写带 header)——单窗完成即持久化,中断不失已算窗。"""
    pd.DataFrame(rows).to_csv(path, mode="a", header=not path.exists(),
                              index=False)


def _atomic_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str, ensure_ascii=False))
    tmp.rename(path)


# ── 主引擎 ───────────────────────────────────────────────────────────────────

def run(panel: Union[pd.DataFrame, str],
        models: Optional[List[str]] = None,
        factories: Optional[Dict[str, ModelFactory]] = None,
        train_months: int = 24, test_months: int = 6, step_months: int = 6,
        seeds: int = 5, quick: bool = False, allow_deep: bool = False,
        out_dir: Optional[Path] = None,
        run_tag: Optional[str] = None) -> dict:
    """滚动训练/验证 + 分层报告。返回 summary dict(同 summary.json)。

    panel: DataFrame(DEV_CONTRACTS schema)或 tag 字符串(经 replication_g.load_panel);
    models: 注册表名清单(默认 ["ols"]);factories: 注入式模型工厂 name→factory
      (与注册表同契约;注入名可任意,视为确定性单 seed——随机性模型请经
      STOCHASTIC_MODELS 内置名走注册表,或自行在工厂内做种子平均);
    out_dir: 产物目录(默认 outputs/walkforward/;测试传 /tmp 路径)。
    """
    if isinstance(panel, str):
        from VolumePrediction.replication_g import load_panel
        panel_tag, panel = panel, load_panel(panel)
    else:
        panel_tag = "<in-memory>"
    out_dir = Path(out_dir) if out_dir is not None else WF_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    run_tag = run_tag or datetime.now().strftime("%Y%m%d_%H%M%S")
    win_csv = out_dir / f"wf_windows_{run_tag}.csv"
    strat_csv = out_dir / f"wf_stratified_{run_tag}.csv"
    sum_json = out_dir / "summary.json"

    models = models or ["ols"]
    factories = dict(factories or {})
    for mk in models:
        if mk in factories:
            continue
        if mk in DEEP_MODELS and not allow_deep:
            raise ValueError(
                f"model {mk!r} is deep (torch/lightgbm) — pass allow_deep=True/"
                f"--deep explicitly (walkforward 默认只跑轻模型,见模块头纪律)")
        factories[mk] = registry_factory(mk, quick=quick)

    cols = feature_cols(panel)
    if not cols:
        raise ValueError("panel has no feature columns (tech_/fund1_/... prefixes)")
    dates = panel.index.get_level_values("date").unique().sort_values()
    windows = make_windows(dates, train_months, test_months, step_months)
    if not windows:
        raise ValueError(f"no walk-forward windows: {len(dates)} panel dates vs "
                         f"train={train_months}m test={test_months}m")
    log.info(f"walkforward[{run_tag}] panel {panel.shape} | {len(windows)} windows "
             f"| models {models} | train/test/step={train_months}/{test_months}/"
             f"{step_months}m quick={quick}")

    # 分层键在全面板上一次算好(仅由当行特征列派生,无跨期信息 → 无前视)
    strata_ind = industry_strata(panel)
    strata_size = size_decile_strata(panel)
    d_lv = panel.index.get_level_values("date")

    summary: dict = {
        "run_tag": run_tag, "panel_tag": panel_tag,
        "panel_shape": list(panel.shape),
        "params": {"train_months": train_months, "test_months": test_months,
                   "step_months": step_months, "seeds": seeds, "quick": quick},
        "n_windows": len(windows), "models": models,
        "files": {"windows": str(win_csv), "stratified": str(strat_csv)},
        "global_r2": {}, "stratified": {}, "comparison": {},
    }

    for mk in models:
        fac = factories[mk]
        pooled: List[pd.Series] = []      # 该模型全部窗的 OOS 预测(分层用)
        for w in windows:
            t0 = time.time()
            tr_m = (d_lv >= w["train_start"]) & (d_lv < w["train_end"])
            te_m = (d_lv >= w["test_start"]) & (d_lv < w["test_end"])
            tr = panel[tr_m]
            tr = tr[tr["eta"].notna()]
            te = panel[te_m]
            te = te[te["eta"].notna()]
            if tr.empty or te.empty:
                log.warning(f"{mk} w{w['window_id']}: empty after eta mask — skip")
                continue
            n_seeds = 1 if (quick or mk not in STOCHASTIC_MODELS) else seeds
            preds, pc = [], None
            for sd in range(n_seeds):
                m, fkw = fac(len(cols), sd)
                m.fit(tr, tr["eta"], **fkw)
                preds.append(m.predict(te))
                pc = m.param_count()
            eta_hat = pd.concat(preds, axis=1).mean(axis=1)
            pooled.append(eta_hat)
            row = {
                "window_id": w["window_id"], "model": mk,
                "train_start": w["train_start"].date(),
                "train_end": w["train_end"].date(),
                "test_start": w["test_start"].date(),
                "test_end": w["test_end"].date(),
                "partial": w["partial"],
                "n_train": len(tr), "n_test": len(te),
                "r2_eta": round(oos_r2_eta(te["eta"], eta_hat), 6),
                "r2_v_total": round(
                    r2_v_total(te["v"], te["ma5_v"], eta_hat), 6),
                "param_count": pc, "n_seeds": n_seeds,
                "fit_seconds": round(time.time() - t0, 1),
            }
            _append_rows(win_csv, [row])            # 增量落盘(逐窗)
            log.info(f"{mk} w{w['window_id']} "
                     f"[{row['test_start']}→{row['test_end']}]: "
                     f"r2_eta={row['r2_eta']:.4f} n_test={row['n_test']:,} "
                     f"({row['fit_seconds']}s)")
        if not pooled:
            log.warning(f"{mk}: no windows produced predictions — skipped in report")
            continue

        # 跨窗合并 → 全局 + 分层(靶点⑩)
        eta_hat_all = pd.concat(pooled)
        eta_true_all = panel.loc[eta_hat_all.index, "eta"]
        g_r2 = oos_r2_eta(eta_true_all, eta_hat_all)
        strat_rows = [{"model": mk, "stratum_type": "global", "stratum": "ALL",
                       "n_obs": int(eta_true_all.notna().sum()),
                       "r2_eta": round(g_r2, 6)}]
        strat_result: Dict[str, dict] = {}
        for stype, skey in (("industry", strata_ind),
                            ("size_decile", strata_size)):
            tbl = stratified_r2(eta_true_all, eta_hat_all, skey)
            # np.float64 → Python float(json default=str 会把 numpy 标量串化,
            # 破坏 summary 数值语义)
            strat_result[stype] = {
                r["stratum"]: (None if pd.isna(r["r2_eta"])
                               else round(float(r["r2_eta"]), 6))
                for r in tbl.to_dict("records")}
            strat_rows += [{"model": mk, "stratum_type": stype, **r}
                           for r in tbl.round({"r2_eta": 6}).to_dict("records")]
        _append_rows(strat_csv, strat_rows)

        # 全局 vs 分层对比(spread=分层异质性;加权均值应≈全局)
        summary["global_r2"][mk] = round(g_r2, 6)
        summary["stratified"][mk] = strat_result
        comp = {}
        for stype, d in strat_result.items():
            vals = [v for v in d.values() if v is not None]
            comp[stype] = {"min": min(vals), "max": max(vals),
                           "spread": round(max(vals) - min(vals), 6),
                           "n_strata": len(vals)} if vals else None
        summary["comparison"][mk] = {"global": round(g_r2, 6), **comp}
        _atomic_json(sum_json, summary)             # 每模型完成即刷新
        log.info(f"{mk} pooled OOS r2_eta={g_r2:.4f} | "
                 f"strata: { {k: (v['spread'] if v else None) for k, v in comp.items()} }")

    _atomic_json(sum_json, summary)
    log.info(f"walkforward artifacts → {out_dir}")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="P2⑩ walk-forward + 分层报告")
    ap.add_argument("--panel", default="latest", help="面板 tag(如 paper_full_v4)")
    ap.add_argument("--models", default="ols",
                    help=f"逗号分隔;轻: {','.join(LIGHT_MODELS)};"
                         f"深(须 --deep): {','.join(DEEP_MODELS)}")
    ap.add_argument("--train-months", type=int, default=24)
    ap.add_argument("--test-months", type=int, default=6)
    ap.add_argument("--step-months", type=int, default=6)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--deep", action="store_true",
                    help="允许 nn/rnn/nn2/lgbm(torch/lightgbm 重依赖)")
    ap.add_argument("--quick", action="store_true", help="单 seed+减 epochs 冒烟")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--tag", default=None, help="run_tag(默认时间戳)")
    a = ap.parse_args()
    res = run(a.panel, models=a.models.split(","),
              train_months=a.train_months, test_months=a.test_months,
              step_months=a.step_months, seeds=a.seeds, quick=a.quick,
              allow_deep=a.deep,
              out_dir=Path(a.out_dir) if a.out_dir else None, run_tag=a.tag)
    print(json.dumps({"n_windows": res["n_windows"],
                      "global_r2": res["global_r2"],
                      "comparison": res["comparison"]}, ensure_ascii=False))
