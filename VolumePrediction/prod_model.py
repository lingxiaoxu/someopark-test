"""prod_model.py — 学习模型(lgbm)的生产 freeze/serve 路径(②晋升,2026-08-01)。

设计(plan §5.3/§.ops 对应):
  freeze(panel_tag, asof)  — retrain 侧: 全量面板训练 + 工件落盘(模型/冻结统计/
                             fund末值/meta)。不触碰 production 指针(promote 另走)。
  serve(artifact_dir, target_date) — 读取端: 组装 target_date 的特征 X → η̂ →
                             pred_v = ma5_v + η̂。返回 forecast 帧(schema 与
                             refresh 的 ma5 工件一致,model_version 行级标注)。

特征时序契约(与 pipeline_build 逐位一致,2026-08-01 三步取证):
  - 面板行 T 的 tech = causal-z(raw_tech(T-1)),统计窗 ≤T-2(A13 expanding
    shift1 + A14 下移);存档面板被 A14 丢弃了每票首行,复现 expanding 必须
    回补首日(freeze 侧已处理,parity 逐位验证)。
  - fund1/fund2: 逐日横截面 z(clip±5);季度 ffill 语义 → serve 用冻结日末值
    延续(工件 meta 记 staleness 起点;月度 refreeze 刷新)。
  - cal/earn: 行日 T 当日可知(前瞻日历),serve 时对 T 直接计算。
  - 目标 η_T = v_T − ma5_v(T), ma5_v(T)=mean(v_{T-5..T-1});pred_v=ma5_v+η̂。

通用路径 vs 快路径:
  - target = asof 后首个交易日: X 的 tech/ma5_v 已在 freeze 时精确预计算(z_next),
    零重算零漂移。
  - target 更晚(日更第2天起): tech raw 从 raw grouped 尾窗重算,z 用冻结 mu/sd
    (漂移 = 冻结统计 vs 真 expanding 的差,O(1/n) 每日;refreeze 周期内可忽略,
    meta 记录 drift_days 供 health 检查)。
"""
from __future__ import annotations

import json
import logging
import pickle
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from VolumePrediction.common import REPO

log = logging.getLogger("VolumePrediction.prod_model")

OUT = REPO / "VolumePrediction" / "outputs"
ART_ROOT = OUT / "registry" / "artifacts"
TECH_SPECS = [(s, w) for s in ("ret", "v") for w in (1, 5, 22, 252)]
TECH_COLS = [f"tech_{s}_ma{w}" for s, w in TECH_SPECS]


# ─────────────────────────────────────────────────────────────────────────────
# freeze(retrain 侧;重依赖允许)
# ─────────────────────────────────────────────────────────────────────────────

def _prepend_first_day(base: pd.DataFrame) -> dict:
    """A14 丢弃的每票首行回补(见模块docstring)。返回 {ticker: (date, v)}。"""
    dates_all = sorted(base.index.get_level_values("date").unique())
    first_global = dates_all[0]
    # 稠密网格下所有票首行=全局首日;被丢行=全局首日的前一交易日,从 raw 读
    from VolumePrediction.data import polygon_loader as pl
    days = pl.trading_days("2018-12-20", str(pd.Timestamp(first_global).date()))
    prev_day = days[-2] if len(days) >= 2 and days[-1] == str(pd.Timestamp(first_global).date()) else None
    out = {}
    if prev_day is None:
        return out
    p = REPO / "price_data" / "volume_prediction" / "raw" / f"grouped_{prev_day}.parquet"
    if not p.exists():
        return out
    raw0 = pd.read_parquet(p)
    if "dollar_volume" not in raw0.columns and {"v", "vw"}.issubset(raw0.columns):
        raw0["dollar_volume"] = raw0["v"].astype(float) * raw0["vw"].astype(float)
    r0 = raw0.set_index("ticker")["dollar_volume"]
    ts = pd.Timestamp(prev_day)
    for tk in base.index.get_level_values("ticker").unique():
        if tk in r0.index and r0.loc[tk] > 0:
            out[tk] = (ts, float(np.log(r0.loc[tk])))
    return out


def freeze(panel_path: str | Path, asof: str,
           art_dir: Optional[str | Path] = None,
           version: Optional[str] = None) -> dict:
    """全量训练 + 工件落盘。输出目录默认 outputs/registry/artifacts/<version>/。

    工件内容:
      model.pkl            — lgbm(目标 η,特征 cols_upto('earn'))
      per_ticker.parquet   — 每票: tech 冻结 mu/sd + z_next + ma5v_next + fund末值
      meta.json            — asof/trained_through/first_serve_date/特征清单/指标
    """
    from VolumePrediction.replication_legacy import cols_upto, make_model
    t0 = pd.Timestamp.now()
    panel_path = Path(panel_path)
    panel = pd.read_parquet(panel_path)
    dates = panel.index.get_level_values("date")
    asof_ts = pd.Timestamp(asof)
    assert dates.max() == asof_ts, f"panel end {dates.max()} != asof {asof}"

    cols = cols_upto(panel, "earn")
    tr = panel[panel["eta"].notna()]
    model = make_model("lgbm", len(cols))
    model.fit(tr[cols], tr["eta"])
    log.info(f"freeze: lgbm fit on {len(tr):,} rows × {len(cols)} feats")

    # ── 每票预计算 ──
    base = panel[["ret", "v"]]
    prev = _prepend_first_day(base)
    fund_cols = [c for c in cols if c.startswith(("fund1_", "fund2_"))]
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
        # active: 冻结日仍在交易(末个非NaN v 的日期==asof)。面板宇宙是多年并集,
        # 含退市票——serve 必须只出活跃票,否则会用多年前的尾巴造"预测"。
        vv = g["v"].dropna()
        rec["active"] = bool(len(vv) and vv.index[-1] == asof_ts)
        rows.append(rec)
    per_ticker = pd.DataFrame(rows).set_index("ticker").join(fund_last, how="left")
    log.info(f"freeze: active tickers {int(per_ticker['active'].sum())}/{len(per_ticker)}")

    from VolumePrediction.data import polygon_loader as pl
    fut = pl.trading_days(asof, str((asof_ts + pd.Timedelta(days=10)).date()))
    first_serve = next(d for d in fut if d > asof)

    version = version or f"lgbm_{panel_path.stem.split('_')[-1]}_{asof.replace('-','')}"
    art = Path(art_dir) if art_dir else ART_ROOT / version
    art.mkdir(parents=True, exist_ok=True)
    with open(art / "model.pkl", "wb") as f:
        pickle.dump(model, f)
    per_ticker.to_parquet(art / "per_ticker.parquet")
    meta = {
        "version": version, "kind": "learned.lgbm",
        "panel": panel_path.name, "trained_through": asof,
        "first_serve_date": first_serve,
        "n_train_rows": int(len(tr)), "n_tickers": int(len(per_ticker)),
        "feature_cols": cols, "fund_cols": fund_cols,
        "target": "eta", "pred_rule": "pred_v = ma5_v + eta_hat",
        "built_at": datetime.now().isoformat(timespec="seconds"),
    }
    with open(art / "meta.json", "w") as f:
        json.dump(meta, f, indent=1)
    log.info(f"freeze done → {art} ({(pd.Timestamp.now()-t0).total_seconds():.0f}s)")
    return meta


# ─────────────────────────────────────────────────────────────────────────────
# serve(读取端;禁网络,只读 raw cache + 工件)
# ─────────────────────────────────────────────────────────────────────────────

def _load_artifact(art_dir: str | Path):
    art = Path(art_dir)
    with open(art / "meta.json") as f:
        meta = json.load(f)
    with open(art / "model.pkl", "rb") as f:
        model = pickle.load(f)
    per_ticker = pd.read_parquet(art / "per_ticker.parquet")
    return model, per_ticker, meta


def _tech_and_ma5_from_raw(tickers, asof_freeze: str, target: str,
                           per_ticker: pd.DataFrame) -> pd.DataFrame:
    """通用路径: raw 尾窗重算 tech raw + 冻结统计 z 化 + ma5_v(target)。
    尾窗 = target 前 ≥260 个原始日(ma252 需要)。"""
    from VolumePrediction.data import polygon_loader as pl
    from VolumePrediction.data import splits_loader as sl
    tgt = pd.Timestamp(target)
    days = [d for d in pl.trading_days("2024-06-01", str(tgt.date())) if d < target]
    days = days[-330:]           # ma252 需 253 个有效日,留 IPO/停牌余量
    frames = []
    raw_dir = REPO / "price_data" / "volume_prediction" / "raw"
    for d in days:
        p = raw_dir / f"grouped_{d}.parquet"
        if not p.exists():
            continue
        df = pd.read_parquet(p, columns=None)
        if "dollar_volume" not in df.columns and {"v", "vw"}.issubset(df.columns):
            df["dollar_volume"] = df["v"].astype(float) * df["vw"].astype(float)
        sub = df[df["ticker"].isin(tickers)][["ticker", "c", "dollar_volume"]].copy()
        sub["date"] = pd.Timestamp(d)
        frames.append(sub)
    long = pd.concat(frames, ignore_index=True)
    # 复权(与面板 _adjusted_wide 同款: 逐票 sl.adjust;ret 用复权 close)
    adj_frames = []
    for tk, g in long.groupby("ticker"):
        gg = g.set_index("date").sort_index()
        try:
            adj, _ = sl.adjust(gg, tk)
            gg["c"] = adj["c"].astype(float)
        except Exception:  # noqa: BLE001 — 无拆股记录票原样
            pass
        adj_frames.append(gg.reset_index())
    long = pd.concat(adj_frames, ignore_index=True)
    wide_c = long.pivot_table(index="date", columns="ticker", values="c", aggfunc="last")
    wide_V = long.pivot_table(index="date", columns="ticker", values="dollar_volume", aggfunc="last")
    ret = wide_c.pct_change()
    v = np.log(wide_V.where(wide_V > 0))
    out = {}
    for s, wname in (("ret", ret), ("v", v)):
        for w in (1, 5, 22, 252):
            raw = wname.rolling(w, min_periods=w).mean().iloc[-1]
            col = f"tech_{s}_ma{w}"
            mu = per_ticker[f"{col}__mu"]
            sd = per_ticker[f"{col}__sd"].replace(0.0, np.nan)
            z = ((raw - mu) / sd).clip(-5, 5)
            out[col] = z.fillna(0.0)
    vt = v.tail(5)
    ma5 = vt.mean()
    ma5[vt.notna().sum() < 5] = np.nan
    out["ma5v_next"] = ma5
    return pd.DataFrame(out)


def serve(art_dir: str | Path, target_date: str) -> pd.DataFrame:
    """组装 target_date 的 X → η̂ → forecast 帧。

    返回列: date(=target 前一交易日,与 ma5 工件的 asof 语义一致), ticker,
            pred_v, pred_V, pred_eta, model_version, trained_through, generated_at
    覆盖: 工件宇宙 ∩ 特征完备票;调用方(refresh)对未覆盖票补 ma5 行。
    """
    model, per_ticker, meta = _load_artifact(art_dir)
    import VolumePrediction.features.pipeline as fpipe
    from VolumePrediction.data import earnings_loader as el

    if "active" in per_ticker.columns:
        n0 = len(per_ticker)
        per_ticker = per_ticker[per_ticker["active"]]
        log.info(f"serve: active 过滤 {len(per_ticker)}/{n0}")
    fast = (target_date == meta["first_serve_date"])
    if fast:
        tech = per_ticker[[f"{c}__z_next" for c in TECH_COLS]].copy()
        tech.columns = TECH_COLS
        tech["ma5v_next"] = per_ticker["ma5v_next"]
    else:
        tech = _tech_and_ma5_from_raw(set(per_ticker.index), meta["trained_through"],
                                      target_date, per_ticker)
        log.warning(f"serve general path: target={target_date} (freeze={meta['trained_through']},"
                    f" 冻结统计随距离产生 O(1/n) 漂移;建议月度 refreeze)")

    X = tech.join(per_ticker[meta["fund_cols"]], how="left")

    # cal/earn: 对 target 当日计算(前瞻日历,当日可知)
    tgt = pd.Timestamp(target_date)
    idx = pd.MultiIndex.from_product([[tgt], X.index], names=["date", "ticker"])
    shell = pd.DataFrame(index=idx)
    shell = fpipe.add_calendar_flags(shell)
    syms = sorted(X.index)
    try:
        earn_fut = el.future_dates(syms)
    except Exception as e:  # noqa: BLE001
        log.warning(f"future earnings calendar unavailable at serve: {e}")
        earn_fut = None
    shell = fpipe.create_earnings_dummies(shell, el.historical_dates(syms),
                                          future_dates=earn_fut)
    caearn = shell.droplevel("date")
    X = X.join(caearn, how="left")

    cols = meta["feature_cols"]
    missing = [c for c in cols if c not in X.columns]
    if missing:
        raise RuntimeError(f"serve 特征缺列: {missing[:8]}")
    ok = X["ma5v_next"].notna()
    Xf = X.loc[ok, cols].fillna(0.0)
    eta_hat = pd.Series(model.predict(Xf), index=Xf.index)

    pred_v = X.loc[ok, "ma5v_next"] + eta_hat
    asof_prev = meta["trained_through"] if fast else None
    if asof_prev is None:
        from VolumePrediction.data import polygon_loader as pl
        ds = [d for d in pl.trading_days("2024-06-01", target_date) if d < target_date]
        asof_prev = ds[-1]
    out = pd.DataFrame({
        "date": asof_prev, "ticker": pred_v.index,
        "pred_v": pred_v.values, "pred_V": np.exp(pred_v.values),
        "pred_eta": eta_hat.values,
        "model_version": meta["version"],
        "trained_through": meta["trained_through"],
        "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
    }).dropna(subset=["pred_v"]).reset_index(drop=True)
    return out
