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

    fast = (target_date == meta["first_serve_date"])
    if not fast:
        # 有状态服务的断档纪律: 只在"冻结日之后首个交易日"给精确窗;更晚需要
        # 逐日滚动过来的 seq_tail。此处要求调用方按日连续调用(update_state=True),
        # 否则窗与目标日错位 → 拒绝出数,不静默降级。
        if meta.get("seq_tail_date") != _prev_trading_day(target_date):
            raise RuntimeError(
                f"serve(rnn): seq_tail 停在 {meta.get('seq_tail_date')},"
                f" 目标 {target_date} 的前一交易日是 {_prev_trading_day(target_date)}"
                f" — 窗断档。请按日连续 serve(update_state=True),或用"
                f" rebuild_seq_tail(art, '{target_date}') 从 raw 重建窗,或 refreeze。")

    # ── 当日特征行 ──────────────────────────────────────────────────────────
    # fast(target == first_serve_date): 用冻结时预算好的 z_next / ma5v_next。
    # 否则**必须从 raw 重算**(与 lgbm serve 的通用路径同一函数)。
    #
    # 2026-08-26 事故根因: 此处原先只有 fast 分支 —— per_ticker 是冻结工件,
    # z_next/ma5v_next 是为 first_serve_date **单日**预算的常量,X 完全不含
    # target_date 依赖。于是 _roll_seq_tail 每天把同一行滚进窗,滚满 L=9 次后
    # (2026-08-14)整个 10 行窗变成同一行的 10 份拷贝,η̂ 落到不动点,输出
    # 逐位冻结:8/14→8/26 连续 9 个交易日 3,869 票预测值一个 bit 都没变,
    # 而同期全市场成交额跌了 17.8%,持仓票 MAPE 25.7→56.4。
    # lgbm 的 prod_model.serve 一直有这条通用分支,只有 RNN 漏写了。
    if fast:
        tech = per_ticker[[f"{c}__z_next" for c in TECH_COLS]].copy()
        tech.columns = list(TECH_COLS)
        ma5_src = per_ticker["ma5v_next"]
    else:
        from VolumePrediction.prod_model import _tech_and_ma5_from_raw
        gen = _tech_and_ma5_from_raw(set(per_ticker.index),
                                     meta["trained_through"], target_date,
                                     per_ticker)
        tech = gen[list(TECH_COLS)]
        ma5_src = gen["ma5v_next"]
        log.warning(
            f"serve(rnn) 通用路径: target={target_date}"
            f"(freeze={meta['trained_through']};冻结 mu/sd 随距离产生 O(1/n)"
            f" 漂移,建议月度 refreeze)")
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

    # 水平锚必须与 tech 同源: fast=冻结值,否则=通用路径当日重算值。
    # (原先无条件读 per_ticker["ma5v_next"] —— 那是 7/31 的常量,pred_v 的
    #  整个水平永久钉死在冻结日,正是 8/14 起输出逐位冻结的直接成因。)
    ma5 = ma5_src.reindex(keep).values.astype(float)
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


def next_trading_day(asof: str) -> str:
    """asof 之后的首个 NYSE 交易日 = serve() 的正确 target。

    serve(T) 预测的是 **T 日**(锚 ma5v_next = T 之前 5 个交易日的 log 量均值,
    与 features/pipeline.py 的 `ma5_v = v.shift(1).rolling(5).mean()` 同一规则)。
    所以日更在 asof 收盘后要的是 serve(next_trading_day(asof)),**不是**
    serve(asof) —— 后者预测的是今天,已经发生过了。
    """
    from VolumePrediction.data import polygon_loader as pl
    end = str((pd.Timestamp(asof) + pd.Timedelta(days=15)).date())
    ds = [d for d in pl.trading_days(asof, end) if d > asof]
    if not ds:
        raise RuntimeError(f"无法确定 {asof} 的下一交易日")
    return ds[0]


def rebuild_seq_tail(art_dir: str | Path, target_date: str) -> dict:
    """从 raw 重建 seq_tail 的 L 行序列窗(通用路径口径),修复污染/断档。

    为什么需要它: seq_tail 是**有状态**的,一旦滚进过错误的行,单靠"以后滚对"
    要等 L 个交易日才能把脏行挤出窗。2026-08-26 事故里窗内 10 行全是同一行
    冻结值,只修 serve() 的话还要再烂 9 天 —— 必须能一次性重建。

    窗语义(与 _roll_seq_tail 一致): 第 j 行 = 第 j 个交易日的**特征行**,
    "d 日的特征行"用 < d 的数据算(features/pipeline.py 的特征是 shift(1) 滞后的)。
    窗末行 = _prev_trading_day(target_date),重建后 serve(target_date) 恰好接上。

    代价: L 次 _tech_and_ma5_from_raw,每次约 26s(读 330 个 raw 日 + 逐票复权),
    L=9 时约 4 分钟。因此**只做显式修复,不挂日更自动跑** —— 自动重建会把
    "窗坏了"这件事悄悄掩盖掉。
    """
    from VolumePrediction.data import polygon_loader as pl
    from VolumePrediction.prod_model import _tech_and_ma5_from_raw

    art, meta, per_ticker, seq = _load(art_dir)
    cols, L = meta["feature_cols"], meta["seq_len"] - 1
    if "active" in per_ticker.columns:
        per_ticker = per_ticker[per_ticker["active"]]

    start = str((pd.Timestamp(target_date) - pd.Timedelta(days=40)).date())
    days = [d for d in pl.trading_days(start, target_date) if d < target_date][-L:]
    if len(days) < L:
        raise RuntimeError(f"rebuild_seq_tail: {target_date} 前不足 {L} 个交易日")

    tickers = seq["tickers"]
    feats = np.zeros((len(tickers), L, len(cols)), dtype=np.float32)
    for j, d in enumerate(days):
        gen = _tech_and_ma5_from_raw(set(per_ticker.index),
                                     meta["trained_through"], d, per_ticker)
        Xd = (gen[list(TECH_COLS)]
              .join(per_ticker[meta["fund_cols"]], how="left"))[cols].fillna(0.0)
        # 工件宇宙里当日无数据的票 → 零填充(与 freeze 对新票的左填充同语义)
        feats[:, j, :] = Xd.reindex(tickers).fillna(0.0).values.astype(np.float32)
        log.info(f"rebuild_seq_tail: {d} 特征行就位 ({j + 1}/{L})")

    # 自检: 重建后窗内各行必须**不全相同** —— 全相同正是本次事故的病征。
    # 逐票沿时间轴求标准差,任一特征 >0 即该票的窗在动。
    varying = int((feats.std(axis=1) > 1e-9).any(axis=1).sum())
    if varying == 0:
        raise RuntimeError("rebuild_seq_tail: 重建后窗内所有行仍完全相同 — "
                           "特征源有问题,拒绝落盘")
    log.info(f"rebuild_seq_tail: {varying}/{len(tickers)} 票窗内存在逐日变化")

    tmp = art / "seq_tail.tmp.npz"
    np.savez(tmp, tickers=tickers, feats=feats,
             last_date=np.array([days[-1]] * len(tickers)))
    tmp.replace(art / "seq_tail.npz")
    meta["seq_tail_date"] = days[-1]
    meta["seq_tail_rebuilt_at"] = datetime.now().isoformat(timespec="seconds")
    mtmp = art / "meta.json.tmp"
    with open(mtmp, "w") as f:
        json.dump(meta, f, indent=1)
    mtmp.replace(art / "meta.json")
    log.info(f"rebuild_seq_tail: 窗重建完成 {days[0]}..{days[-1]} → seq_tail_date={days[-1]}")
    return {"days": days, "seq_tail_date": days[-1], "n_tickers": len(tickers),
            "n_varying": varying}
