"""
pipeline_build — 面板构建总装(数据层×特征层集成;A1-A16 的执行入口)
=================================================================
build_panel(start, end): 服务宇宙 → 原始 bar → 拆股现算复权(§7.10) → 基础长面板
(V=美元量/close=复权价/ret) → A7 量特征 → A8-A9 tech → A10 日历 → A6 财报分桶
→ fund1/fund2(附录 B) → A16 FRED → A13 zscore → A14 移位 → A11-A12/双协议 fill
→ A15 落盘。

重算力入口——调度纪律: pairs 管道运行期不得启动全量构建
(guard_heavy_compute();--force-heavy 显式越过)。
"""
from __future__ import annotations

import socket
import time

# urllib 系(fredapi 等)默认无超时——死连接永久阻塞;全局兜底(requests/pymongo
# 各自显式设超时,不受此影响)。2026-07-24 排障: CPU 冻结型挂死的最后防线。
socket.setdefaulttimeout(120)
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from VolumePrediction.common import REPO, OUT, load_config, get_logger
from VolumePrediction.data import polygon_loader as pl
from VolumePrediction.data import splits_loader as sl
from VolumePrediction.data import universe as uni
from VolumePrediction.data import earnings_loader as el
from VolumePrediction.data import inhouse_loader as ih
from VolumePrediction.data import factor_proxy as fp
from VolumePrediction.features import pipeline as fpipe
from VolumePrediction.data import export as dexp

log = get_logger("pipeline_build")


def guard_heavy_compute() -> bool:
    """pairs 管道进程存在则 False。"""
    import subprocess
    try:
        r = subprocess.run(["pgrep", "-f", "pipeline_runner.sh"],
                           capture_output=True, text=True)
        if r.stdout.strip():
            return False
    except Exception:  # noqa: BLE001
        pass
    return True


def _adjusted_wide(start: str, end: str, tickers: set) -> Dict[str, pd.DataFrame]:
    """原始长表 → 逐票复权(显式 splits) → 宽表 {field: date×ticker}。"""
    px = pl.load_range(start, end, tickers)
    if px.empty:
        raise RuntimeError("raw cache empty for range")
    frames: Dict[str, list] = {k: [] for k in
                               ("c", "dollar_volume", "raw_c")}
    for tk, g in px.groupby("ticker"):
        df = g.set_index(pd.to_datetime(g["date"])).sort_index()
        df = df[~df.index.duplicated(keep="last")]
        raw_c = df["c"].astype(float).rename(tk)
        adj, _ = sl.adjust(df, tk)
        frames["c"].append(adj["c"].astype(float).rename(tk))
        frames["dollar_volume"].append(
            adj["dollar_volume"].astype(float).rename(tk))
        frames["raw_c"].append(raw_c)
    return {k: pd.concat(v, axis=1).sort_index() for k, v in frames.items()}


def _base_panel(dollar: pd.DataFrame, close: pd.DataFrame) -> pd.DataFrame:
    """宽表 → MultiIndex(date,ticker) 基础面板: V/close/ret。"""
    V = dollar.stack(future_stack=True)
    C = close.stack(future_stack=True)
    panel = pd.DataFrame({"V": V, "close": C})
    panel.index.names = ["date", "ticker"]
    panel = panel.sort_index()
    panel["ret"] = panel.groupby(level="ticker")["close"].pct_change()
    return panel


def build_panel(start: str, end: str,
                include_fred: bool = True,
                include_fund: bool = True,
                include_intraday: bool = True,
                tickers: Optional[set] = None,
                fill_policy: Optional[str] = None,
                zscore_train_end: Optional[str] = None,
                out_tag: Optional[str] = None,
                out_dir: Optional[Path] = None) -> pd.DataFrame:
    """全流程面板构建;落盘(A15)并返回。"""
    cfg = load_config()
    fill_policy = fill_policy or cfg["models"]["train"]["fill_policy"]
    t0 = time.time()

    # A1-A4 宇宙
    if tickers is None:
        days = pl.trading_days(start, end)
        members_by_day = {d: uni.membership(d) for d in days}
        universe_union: set = set().union(*members_by_day.values())
        extra = uni.strategy_symbols()
        etf_set = extra["etf"]
        universe_union |= etf_set | extra["pairs"] | extra["aiss"]
    else:
        universe_union = set(tickers)
        members_by_day = None
        etf_set = uni.strategy_symbols()["etf"] & universe_union
    log.info(f"universe union: {len(universe_union)} tickers")

    # A5 价量(复权) → 基础面板
    wide = _adjusted_wide(start, end, universe_union)
    panel = _base_panel(wide["dollar_volume"], wide["c"])

    # A7 量特征(v/ma5_v/eta) → A8-A9 tech → A10 日历
    panel = fpipe.add_volume_features(panel)
    panel = fpipe.add_return_rollups(panel)
    panel = fpipe.add_calendar_flags(panel)

    # 日内量形态(E5,2026-08-04): 只用 4:00-13:00 ET 窗 —— 采集任务 14:05 盘中跑,
    # 尾盘两根条永远缺,训练必须与服务同窗(详见 intraday_features 模块头)。
    # 无小时线/Mongo 不可达时跳过,不阻断面板构建。
    if include_intraday:
        try:
            from VolumePrediction.features import intraday_features as ifeat
            panel = ifeat.add_intraday_features(panel)
        except Exception as e:  # noqa: BLE001 — 增强特征缺失不应阻断主线
            log.warning(f"intraday features 跳过(非致命): {e}")

    # A6 财报 10 桶(历史=复用 MFE 含退市;未来=FMP 前瞻)
    syms = sorted(universe_union)
    earn_hist = el.historical_dates(syms)
    try:
        earn_fut = el.future_dates(syms)
    except Exception as e:  # noqa: BLE001
        log.warning(f"future calendar unavailable: {e}")
        earn_fut = None
    panel = fpipe.create_earnings_dummies(panel, earn_hist, future_dates=earn_fut)

    # fund1/fund2(附录 B;G3)
    if include_fund:
        panel = _add_fundamentals(panel, wide["c"], wide["raw_c"],
                                  wide["dollar_volume"], syms)

    # A16 FRED 72
    if include_fred:
        try:
            fred = ih.fred_macro(start, end)
            fred.columns = [f"fund2_fred_{c}" for c in fred.columns]
            fred = fred.reindex(
                panel.index.get_level_values("date").unique().sort_values()).ffill()
            panel = panel.join(fred, on="date")
        except Exception as e:  # noqa: BLE001
            log.warning(f"FRED merge skipped: {e}")

    # 服务宇宙旗标
    tick_lv = panel.index.get_level_values("ticker")
    panel["is_etf"] = tick_lv.isin(etf_set).astype(float)
    if members_by_day is not None:
        date_lv = panel.index.get_level_values("date")
        panel["in_r3k"] = np.fromiter(
            ((t in members_by_day.get(d.strftime("%Y-%m-%d"), ()))
             for d, t in zip(date_lv, tick_lv)),
            dtype=bool, count=len(panel)).astype(float)

    # A13 标准化(哑变量与旗标不动):
    # - tech_: 按票时序 z(论文口径,近平稳序列适用)
    # - fund1_/fund2_ 连续特征: 逐日横截面 z(附录 B 设计)。2026-07-25 诊断:
    #   时序 z + 训练窗统计对含时间趋势的水平量(firm_age/size/be_me)在测试期
    #   被 clip 轨道钉死 → NN/RNN 分布漂移崩溃(train R2 18% 健康,test -200%);
    #   横截面 z 逐日居中,天然无此病
    tech_cols = [c for c in panel.columns if c.startswith("tech_")]
    panel = fpipe.zscore_normalize(panel, tech_cols, train_end=zscore_train_end)
    # intraday_ 与 fund 同走逐日横截面 z(占比/比值类,截面可比性强于时序 z)
    fund_cols = [c for c in panel.columns
                 if (c.startswith(("fund1_", "fund2_", "intraday_"))
                     and not c.startswith("fund2_ind"))]
    if fund_cols:
        from VolumePrediction.data import factor_proxy as _fp
        panel[fund_cols] = (_fp.cross_sectional_zscore(panel[fund_cols])
                            .clip(-5.0, 5.0))

    # A14 移位(tech_ 下移 1;cal_/earn_ 当日可知不移)
    panel = fpipe.shift_volume_columns_and_drop_last(panel)

    # A11-A12/双协议填充(特征列;目标 eta 的 NaN 保留由训练侧滤除)
    fcols = [c for c in panel.columns
             if c.startswith(("tech_", "fund1_", "fund2_", "cal_", "earn_",
                              "intraday_"))]
    panel = fpipe.fill(panel, fcols, policy=fill_policy)

    # A15 落盘
    tag = out_tag or f"{start}_{end}"
    dexp.save_panel(panel, fill_policy=fill_policy, tag=tag, out_dir=out_dir)
    log.info(f"panel built: {panel.shape} in {time.time()-t0:.0f}s tag={tag}")
    return panel


def _add_fundamentals(panel: pd.DataFrame, close: pd.DataFrame,
                      raw_close: pd.DataFrame,
                      dollar: pd.DataFrame, syms: List[str]) -> pd.DataFrame:
    """fund-1 6 + fund-2 风格/行业/SUE(附录 B;§6 源;PIT 纪律)。"""
    ret = close.pct_change()
    spy_ret = ret["SPY"] if "SPY" in ret.columns else ret.mean(axis=1)
    wide_feats: Dict[str, pd.DataFrame] = {}
    wide_feats["fund1_dimson_beta"] = pd.DataFrame(
        {tk: fp.beta_252(ret[tk], spy_ret) for tk in close.columns})
    wide_feats["fund2_mom_12_1"] = pd.DataFrame(
        {tk: fp.momentum_12_1(close[tk]) for tk in close.columns})
    wide_feats["fund2_resvol_60"] = pd.DataFrame(
        {tk: fp.resvol_60(ret[tk], spy_ret) for tk in close.columns})
    wide_feats["fund2_amihud_20"] = pd.DataFrame(
        {tk: fp.amihud_20(ret[tk], dollar[tk]) for tk in close.columns})

    mcap = {}
    for tk in close.columns:
        try:
            mcap[tk] = fp.market_cap_series(tk, raw_close[tk].dropna())
        except Exception:  # noqa: BLE001
            mcap[tk] = pd.Series(dtype=float)
    mcap_df = pd.DataFrame(mcap).reindex(close.index)
    ln_mcap = np.log(mcap_df.where(mcap_df > 0))
    # SIZE 第四级 fallback(2026-07-26): ADR/外国发行人(BIDU/BMO 等 ~19% 票)
    # share_float 与 SEC financials 双缺 → 真市值不可得。附录 B 原授权
    # "size 用美元成交额代理" —— 在 **逐日 z 空间** 融合: 有市值票用 ln_mcap
    # 的截面 z,缺失票回填 ln(252d ADV) 的截面 z(两者同为规模代理,z 后可比;
    # 原始单位不可直接混拼)。融合后列已是 z 口径 → 加入 fund 组截面 z 化时
    # 近似幂等(再 z 一次不破坏排序)。
    ln_adv = np.log(dollar.rolling(252, min_periods=60).mean()
                    .where(lambda x: x > 0))
    z_mcap = ln_mcap.sub(ln_mcap.mean(axis=1), axis=0).div(
        ln_mcap.std(axis=1).replace(0, np.nan), axis=0)
    z_adv = ln_adv.sub(ln_adv.mean(axis=1), axis=0).div(
        ln_adv.std(axis=1).replace(0, np.nan), axis=0)
    n_fb = int((z_mcap.isna() & z_adv.notna()).sum().sum())
    log.info(f"SIZE fallback: {n_fb:,} 格用 ADV 代理 z 回填(ADR/外国发行人)")
    wide_feats["fund1_size_ln_mcap"] = z_mcap.fillna(z_adv)

    try:
        ref = uni.fetch_reference_snapshot(uni.rebalance_day(2025))
        ld = pd.to_datetime(ref.set_index("ticker")["list_date"], errors="coerce")
        wide_feats["fund1_firm_age"] = pd.DataFrame(
            {tk: (close.index - ld.get(tk, pd.NaT)).days / 365.25
             for tk in close.columns}, index=close.index)
    except Exception as e:  # noqa: BLE001
        log.warning(f"firm age skipped: {e}")

    bal = ih.annual_statement("fmp_balance_sheet_statement", syms,
                              ["totalStockholdersEquity", "totalDebt"])
    if not bal.empty:
        be, lev = {}, {}
        for tk, g in bal.groupby("symbol"):
            # 同一 acceptedDate 可含多份报表(补正/多财年同日受理)→ 索引重复会使
            # reindex 抛错;按(受理日,财年日)排序后每受理日留最新一份(ffill 语义)
            g = (g.dropna(subset=["acceptedDate"])
                  .sort_values(["acceptedDate", "date"])
                  .drop_duplicates("acceptedDate", keep="last"))
            idx = pd.to_datetime(g["acceptedDate"])
            be[tk] = pd.Series(g["totalStockholdersEquity"].values,
                               index=idx).reindex(close.index, method="ffill")
            lev[tk] = pd.Series(
                (g["totalDebt"] / g["totalStockholdersEquity"]
                 .replace(0, np.nan)).values,
                index=idx).reindex(close.index, method="ffill")
        be_df = pd.DataFrame(be).reindex(columns=close.columns)
        wide_feats["fund1_be_me"] = be_df / mcap_df
        wide_feats["fund1_book_leverage"] = pd.DataFrame(lev).reindex(
            columns=close.columns)

    ep = {}
    for tk in close.columns:
        try:
            ep[tk] = fp.earnyld(tk, raw_close[tk].dropna())
        except Exception:  # noqa: BLE001
            ep[tk] = pd.Series(dtype=float)
    wide_feats["fund2_earnyld"] = pd.DataFrame(ep).reindex(close.index)

    try:
        sur = ih.earnings_surprises(syms, close.index.min().date(),
                                    close.index.max().date())
        sue = {}
        for tk, g in sur.dropna().groupby("symbol"):
            g = g.sort_values("date").drop_duplicates("date", keep="last")
            idx = pd.to_datetime(g["date"])
            if tk not in raw_close.columns:
                continue
            px_at = raw_close[tk].reindex(idx, method="ffill")
            vals = (g["actual"].values - g["estimated"].values) / px_at.values
            sue[tk] = (pd.Series(vals, index=idx)
                       .reindex(close.index).ffill(limit=90))
        wide_feats["fund1_sue"] = pd.DataFrame(sue).reindex(
            columns=close.columns)
    except Exception as e:  # noqa: BLE001
        log.warning(f"SUE skipped: {e}")

    for name, df in wide_feats.items():
        s = df.reindex(close.index).stack(future_stack=True)
        s.index.names = ["date", "ticker"]
        panel[name] = s.reindex(panel.index)

    dummies = fp.industry_dummies(list(close.columns))
    for col in dummies.columns:
        panel[col] = panel.index.get_level_values("ticker").map(
            dummies[col]).astype(float)
    return panel


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--no-fund", action="store_true")
    ap.add_argument("--no-fred", action="store_true")
    ap.add_argument("--tickers", nargs="*", default=None)
    ap.add_argument("--tag", default=None)
    ap.add_argument("--zscore-train-end", default=None)
    ap.add_argument("--force-heavy", action="store_true")
    a = ap.parse_args()
    if not a.force_heavy and not guard_heavy_compute():
        raise SystemExit("pairs pipeline running — heavy build blocked")
    build_panel(a.start, a.end, include_fred=not a.no_fred,
                include_fund=not a.no_fund,
                tickers=set(a.tickers) if a.tickers else None,
                zscore_train_end=a.zscore_train_end, out_tag=a.tag)
