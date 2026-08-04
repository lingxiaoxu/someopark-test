"""prod_model_rnn.py — PaperRNN(窄集 tech+fund1)的生产 freeze/serve(E4 实施)。

为什么是 RNN 且是窄集(2026-08-03 复赛实证,12 窗 walk-forward):
    窄集 RNN  0.294  ← 本模块服务的对象
    全集 lgbm 0.182  (现役 production)
    窄集 lgbm 0.144
    全集 RNN  0.139  (RNN 被宽特征淹没 — 论文 Table 1 同结论)
窄集只需 tech_(8) + fund1_(6) = 14 特征,不需要 fund2/cal/earn → 服务层比
lgbm 路径更简单(无日历/财报 shell)。

红线遵守:
  - serve 路径零 torch: 训练侧导出 weights.npz,服务侧 rnn_export.RNNWeights
    纯 numpy 前向(tests/test_rnn_export.py 有"封死 import torch"的功能性红线)。
  - 有状态服务: RNN 是 many-to-one(seq_len=10),serve 需要每票末 9 日特征窗
    (seq_tail)。工件冻结 seq_tail + 日戳;日更后滚动更新并原子回写。
  - 幂等: 同日重复 serve 不二次入窗(seq_tail_date 戳判定)。
  - 断档: 日期不连续 → 拒绝 fast path,大声 log 并要求 refreeze/重建,
    绝不用错位的窗静默出预测。

工件(art_dir):
  weights.npz         — LSTM+3 dense 权重(numpy 前向用)
  per_ticker.parquet  — 冻结 mu/sd + z_next + ma5v_next + active + fund1 末值
  seq_tail.npz        — tickers / dates(9) / feats (n_tk, 9, n_feat)
  meta.json           — kind=learned.rnn / seq_len / feature_cols / first_serve_date
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from VolumePrediction.common import REPO
from VolumePrediction.prod_model import (ART_ROOT, TECH_COLS, TECH_SPECS,
                                         _prepend_first_day)

log = logging.getLogger("VolumePrediction.prod_model_rnn")

SEQ_LEN = 10
NARROW_PREFIXES = ("tech_", "fund1_")


def narrow_cols(panel: pd.DataFrame) -> list:
    return [c for c in panel.columns if c.startswith(NARROW_PREFIXES)]


# ─────────────────────────────────────────────────────────────────────────────
# freeze(训练侧;允许 torch)
# ─────────────────────────────────────────────────────────────────────────────

def freeze(panel_path: str | Path, asof: str,
           art_dir: Optional[str | Path] = None,
           version: Optional[str] = None,
           seeds: int = 3, epochs: Optional[int] = None) -> dict:
    """窄集 RNN 全量训练 + 工件落盘(不动 production 指针;promote 另走)。

    seeds: 多 seed 训练 → 导出每个 seed 的权重,serve 时预测取均值
    (与 walk-forward 的 seed 平均同口径;单 seed 幸运票不可信,见设计文档 §5)。
    """
    from VolumePrediction.models.deep import PaperRNN
    from VolumePrediction.rnn_export import export_weights

    t0 = pd.Timestamp.now()
    panel_path = Path(panel_path)
    panel = pd.read_parquet(panel_path)
    dates = panel.index.get_level_values("date")
    asof_ts = pd.Timestamp(asof)
    assert dates.max() == asof_ts, f"panel end {dates.max()} != asof {asof}"

    cols = narrow_cols(panel)
    assert cols, "面板无 tech_/fund1_ 列"
    tr = panel[panel["eta"].notna()]
    log.info(f"freeze(rnn): {len(tr):,} rows × {len(cols)} narrow feats, seeds={seeds}")

    art = Path(art_dir) if art_dir else ART_ROOT / (
        version or f"rnn_{panel_path.stem.split('_')[-1]}_{asof.replace('-', '')}")
    art.mkdir(parents=True, exist_ok=True)

    w_files = []
    for sd in range(seeds):
        m = PaperRNN(len(cols), seed=sd)
        m.fit(tr[cols], tr["eta"], epochs=epochs)
        f = art / f"weights_s{sd}.npz"
        export_weights(m, f)
        w_files.append(f.name)
        log.info(f"  seed {sd}: trained, final loss {m.train_history[-1]:.5f} → {f.name}")

    # ── 每票冻结统计(与 lgbm 路径同口径,复用同一 TECH_SPECS 语义) ──
    base = panel[["ret", "v"]]
    prev = _prepend_first_day(base)
    fund_cols = [c for c in cols if c.startswith("fund1_")]
    fund_last = panel[fund_cols].groupby(level="ticker").tail(1).droplevel("date")

    rows = []
    for tk, grp in base.groupby(level="ticker"):
        g = grp.droplevel("ticker")
        if tk in prev:
            d0, v0 = prev[tk]
            g = pd.concat([pd.DataFrame({"ret": [np.nan], "v": [v0]},
                                        index=pd.DatetimeIndex([d0], name="date")), g])
        rec = {"ticker": tk}
        for s, w in TECH_SPECS:
            col = f"tech_{s}_ma{w}"
            raw = g[s].rolling(w, min_periods=w).mean()
            mu_e = raw.expanding(min_periods=2).mean()
            sd_e = raw.expanding(min_periods=2).std()
            rec[f"{col}__mu"] = float(mu_e.iloc[-1])
            rec[f"{col}__sd"] = float(sd_e.iloc[-1])
            mu1 = mu_e.iloc[-2] if len(mu_e) > 1 else np.nan
            sd1 = sd_e.iloc[-2] if len(sd_e) > 1 else np.nan
            r1 = raw.iloc[-1]
            z = (r1 - mu1) / sd1 if (pd.notna(sd1) and sd1 > 0 and pd.notna(r1)) else 0.0
            rec[f"{col}__z_next"] = float(np.clip(z if np.isfinite(z) else 0.0, -5, 5))
        vt = g["v"].dropna().tail(5)
        rec["ma5v_next"] = float(vt.mean()) if len(vt) == 5 else np.nan
        vv = g["v"].dropna()
        rec["active"] = bool(len(vv) and vv.index[-1] == asof_ts)
        rows.append(rec)
    per_ticker = pd.DataFrame(rows).set_index("ticker").join(fund_last, how="left")
    n_active = int(per_ticker["active"].sum())
    log.info(f"freeze(rnn): active {n_active}/{len(per_ticker)}")

    # ── seq_tail: 每票末 SEQ_LEN-1 行的面板特征(已是面板口径 z 值) ──
    tail = panel[cols].groupby(level="ticker").tail(SEQ_LEN - 1)
    tk_order = sorted(per_ticker.index)
    n_feat, L = len(cols), SEQ_LEN - 1
    feats = np.zeros((len(tk_order), L, n_feat), dtype=np.float32)
    tail_dates = {}
    pos = {t: i for i, t in enumerate(tk_order)}
    for tk, grp in tail.groupby(level="ticker"):
        i = pos.get(tk)
        if i is None:
            continue
        v = grp.values.astype(np.float32)
        feats[i, L - len(v):, :] = v          # 不足 L 的新票左侧零填充(同训练语义)
        tail_dates[tk] = str(grp.index.get_level_values("date")[-1].date())
    np.savez(art / "seq_tail.npz",
             tickers=np.array(tk_order), feats=feats,
             last_date=np.array([tail_dates.get(t, "") for t in tk_order]))

    per_ticker.to_parquet(art / "per_ticker.parquet")

    from VolumePrediction.data import polygon_loader as pl
    fut = pl.trading_days(asof, str((asof_ts + pd.Timedelta(days=10)).date()))
    first_serve = next(d for d in fut if d > asof)

    meta = {
        "version": art.name, "kind": "learned.rnn",
        "panel": panel_path.name, "trained_through": asof,
        "first_serve_date": first_serve,
        "seq_len": SEQ_LEN, "seeds": seeds, "weight_files": w_files,
        "n_train_rows": int(len(tr)), "n_tickers": int(len(per_ticker)),
        "n_active": n_active,
        "feature_cols": cols, "fund_cols": fund_cols,
        "target": "eta", "pred_rule": "pred_v = ma5_v + mean_seeds(eta_hat)",
        "seq_tail_date": asof,
        "built_at": datetime.now().isoformat(timespec="seconds"),
    }
    with open(art / "meta.json", "w") as f:
        json.dump(meta, f, indent=1)
    log.info(f"freeze(rnn) done → {art} "
             f"({(pd.Timestamp.now() - t0).total_seconds():.0f}s)")
    return meta


# ─────────────────────────────────────────────────────────────────────────────
# serve(读取端;零 torch,零网络)
# ─────────────────────────────────────────────────────────────────────────────

def _load(art_dir: str | Path):
    art = Path(art_dir)
    with open(art / "meta.json") as f:
        meta = json.load(f)
    per_ticker = pd.read_parquet(art / "per_ticker.parquet")
    z = np.load(art / "seq_tail.npz", allow_pickle=False)
    seq = {"tickers": z["tickers"].astype(str), "feats": z["feats"],
           "last_date": z["last_date"].astype(str)}
    return art, meta, per_ticker, seq


def serve(art_dir: str | Path, target_date: str,
          update_state: bool = False) -> pd.DataFrame:
    """target_date 的 η̂ → forecast 帧(schema 与 lgbm serve/ma5 工件一致)。

    update_state=True 时把当日特征行滚入 seq_tail 并原子回写(日更调用方用);
    幂等: seq_tail_date >= target 前一交易日则跳过滚动。
    """
    from VolumePrediction.rnn_export import RNNWeights

    art, meta, per_ticker, seq = _load(art_dir)
    cols = meta["feature_cols"]
    L = meta["seq_len"] - 1

    if "active" in per_ticker.columns:
        n0 = len(per_ticker)
        per_ticker = per_ticker[per_ticker["active"]]
        log.info(f"serve(rnn): active 过滤 {len(per_ticker)}/{n0}")

    if target_date != meta["first_serve_date"]:
        # 有状态服务的断档纪律: 只在"冻结日之后首个交易日"给精确窗;更晚需要
        # 逐日滚动过来的 seq_tail。此处要求调用方按日连续调用(update_state=True),
        # 否则窗与目标日错位 → 拒绝出数,不静默降级。
        if meta.get("seq_tail_date") != _prev_trading_day(target_date):
            raise RuntimeError(
                f"serve(rnn): seq_tail 停在 {meta.get('seq_tail_date')},"
                f" 目标 {target_date} 的前一交易日是 {_prev_trading_day(target_date)}"
                f" — 窗断档。请按日连续 serve(update_state=True) 或 refreeze。")

    # 当日特征行(与 lgbm fast path 同口径: 冻结 z_next + fund1 末值)
    tech = per_ticker[[f"{c}__z_next" for c in TECH_COLS]].copy()
    tech.columns = TECH_COLS
    X = tech.join(per_ticker[meta["fund_cols"]], how="left")
    missing = [c for c in cols if c not in X.columns]
    if missing:
        raise RuntimeError(f"serve(rnn) 特征缺列: {missing[:8]}")
    X = X[cols].fillna(0.0)

    # 窗 = seq_tail(9) + 当日行 → (n, 10, F)
    pos = {t: i for i, t in enumerate(seq["tickers"])}
    keep = [t for t in X.index if t in pos]
    if not keep:
        raise RuntimeError("serve(rnn): 工件宇宙与 active 票无交集")
    idx = np.array([pos[t] for t in keep])
    W = np.concatenate([seq["feats"][idx],
                        X.loc[keep].values.astype(np.float32)[:, None, :]], axis=1)

    preds = []
    for wf_name in meta["weight_files"]:
        w = RNNWeights.load(art / wf_name)
        preds.append(w.predict_windows(W))
    eta_hat = np.mean(preds, axis=0)
    log.info(f"serve(rnn): {len(keep)} tickers × {len(preds)} seeds")

    ma5 = per_ticker.loc[keep, "ma5v_next"].values
    ok = np.isfinite(ma5)
    pred_v = ma5 + eta_hat
    out = pd.DataFrame({
        "date": _prev_trading_day(target_date),
        "ticker": np.array(keep)[ok],
        "pred_v": pred_v[ok], "pred_V": np.exp(pred_v[ok]),
        "pred_eta": eta_hat[ok],
        "model_version": meta["version"],
        "trained_through": meta["trained_through"],
        "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
    }).reset_index(drop=True)

    if update_state:
        _roll_seq_tail(art, meta, seq, X, keep, target_date, L)
    return out


def _roll_seq_tail(art: Path, meta: dict, seq: dict, X: pd.DataFrame,
                   keep: list, target_date: str, L: int) -> None:
    """当日特征行入窗、最老行出窗,原子回写(幂等: 同日重复调用不二次滚动)。

    seq_tail_date 语义 = **窗口内最后一行的日期**。serve(T) 用的窗是
    seq_tail(末行 = T 的前一交易日) ⊕ T 的特征行;滚动后末行变成 T,
    故戳记 target_date。(2026-08-03 首次影子运行抓到: 原先误记 prev_td,
    窗口永不前进,次日必撞"断档"。)
    """
    if meta.get("seq_tail_date") == target_date:
        log.info(f"seq_tail 已含 {target_date} — 幂等跳过滚动")
        return
    pos = {t: i for i, t in enumerate(seq["tickers"])}
    feats = seq["feats"].copy()
    last = seq["last_date"].copy()
    for t in keep:
        i = pos[t]
        feats[i, :-1, :] = feats[i, 1:, :]
        feats[i, -1, :] = X.loc[t].values.astype(np.float32)
        last[i] = target_date
    # 临时名必须以 .npz 结尾: np.savez 会给非 .npz 名自动追加后缀,
    # 写成 seq_tail.npz.tmp.npz 后原子替换必然 FileNotFoundError
    # (2026-08-03 被 seq_tail_date 语义 bug 掩盖,修完前者才暴露)
    tmp = art / "seq_tail.tmp.npz"
    np.savez(tmp, tickers=seq["tickers"], feats=feats, last_date=last)
    tmp.replace(art / "seq_tail.npz")
    meta["seq_tail_date"] = target_date
    mtmp = art / "meta.json.tmp"
    with open(mtmp, "w") as f:
        json.dump(meta, f, indent=1)
    mtmp.replace(art / "meta.json")
    log.info(f"seq_tail 滚动至 {target_date}({len(keep)} 票)")


def _prev_trading_day(target_date: str) -> str:
    from VolumePrediction.data import polygon_loader as pl
    start = str((pd.Timestamp(target_date) - pd.Timedelta(days=15)).date())
    ds = [d for d in pl.trading_days(start, target_date) if d < target_date]
    if not ds:
        raise RuntimeError(f"无法确定 {target_date} 的前一交易日")
    return ds[-1]
