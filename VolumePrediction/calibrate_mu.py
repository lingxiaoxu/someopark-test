"""
calibrate_mu — P2⑤ runner: 四策略真实历史成交/信号 → μ/λ 校准工件
=================================================================
Plan §9-P2 弱点⑤ / §7.7 / §7.12-2 的落地执行器。纯读生产文件,只写
outputs/registry/mu_calibration.json(经 service._Econ 的原子写路径)。

数据源(全部只读):
  pairs (mrpt+mtfs): trading_signals/{mrpt,mtfs}_signals_*.json 的 OPEN_* 事件
      (z_score/s1_shares/s2_shares) × 我方原始 grouped bar 前向收盘价
      (price_data/volume_prediction/raw,按 §7.10 splits 一致复权)
      → (z_score, delay_days, realized_pnl_frac) 回归 → μ=收敛差斜率
  aiss: qlib-main/semiconductor_strategy/trading_signals/aiss_daily_report_*.json
      子板块 composite_score + 指数价 → 横截面动量组合 alpha-延迟衰减曲线
  ssrs: qlib-main/sector_rotation/trading_signals/sr_daily_report_*.json
      行业 ETF composite_score + 价 → 同上
  λ: trade_ledger_{aiss,ssrs}.jsonl 真实成交 × 当日美元量
      → participation + VWAP 口径冲击代理(无到达价 → 明示代理口径)

样本不足/曲线无衰减 → econ/calibration.py 内建论文先验降级并显式标注
calibration_source(paper_prior=冷启动),绝不静默。
μ 量纲: 名义比例/日(与 losscon 的 λz² 同量纲,见 econ/calibration.py 头注)。

运行(仓库根): conda run -n someopark_run python -m VolumePrediction.calibrate_mu
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import pandas as pd

from VolumePrediction.common import REPO, load_config, get_logger
from VolumePrediction.service import VolumeService

log = get_logger("calibrate_mu")

MAX_DELAY = 5          # 假想执行延迟 0..5 交易日
HOLD_DAYS = 10         # 收敛/持有观察窗(交易日)


# ═══════════════════════════════ pairs(mrpt+mtfs) ═══════════════════════════

def load_pairs_events(signals_dir: Optional[Path] = None) -> pd.DataFrame:
    """OPEN_* 信号事件表: strategy/signal_date/pair/s1/s2/shares/z。"""
    sdir = signals_dir or (REPO / "trading_signals")
    rows = []
    for strat in ("mrpt", "mtfs"):
        for f in sorted(sdir.glob(f"{strat}_signals_*.json")):
            try:
                d = json.loads(f.read_text())
            except Exception as e:  # noqa: BLE001
                log.warning(f"unreadable signal file {f.name}: {e}")
                continue
            sd = d.get("signal_date")
            if not sd:
                continue
            for s in d.get("signals") or []:
                if not str(s.get("action", "")).startswith("OPEN"):
                    continue
                need = ("s1", "s2", "s1_shares", "s2_shares",
                        "s1_price", "s2_price")
                if any(s.get(k) is None for k in need):
                    continue
                # 信号强度: mrpt=z_score;mtfs=momentum_spread(可为 null → 0,
                # 仅作回归控制项,延迟斜率按事件内平衡设计不受影响)
                if s.get("z_score") is not None:
                    z, zsrc = abs(float(s["z_score"])), "z_score"
                elif s.get("momentum_spread") is not None:
                    z, zsrc = abs(float(s["momentum_spread"])), "momentum_spread"
                else:
                    z, zsrc = 0.0, "null→0"
                rows.append({
                    "strategy": strat, "signal_date": sd,
                    "pair": s.get("pair") or f"{s['s1']}/{s['s2']}",
                    "s1": s["s1"], "s2": s["s2"],
                    "s1_shares": float(s["s1_shares"]),
                    "s2_shares": float(s["s2_shares"]),
                    "z": z, "z_source": zsrc,
                })
    df = pd.DataFrame(rows)
    if not df.empty:
        # 同 signal_date 多份文件(重跑)→ 同 (strategy,date,pair) 去重取末份
        df = df.drop_duplicates(subset=["strategy", "signal_date", "pair"],
                                keep="last").reset_index(drop=True)
    return df


def build_close_matrix(tickers, start: str, end: str,
                       warnings: list) -> pd.DataFrame:
    """前向收盘价矩阵(index=DatetimeIndex, col=ticker),按 §7.10 复权到当前口径。"""
    from VolumePrediction.data import polygon_loader as pl
    px = pl.load_range(start, end, tickers=set(tickers))
    if px.empty:
        return pd.DataFrame()
    closes = px.pivot_table(index="date", columns="ticker", values="c",
                            aggfunc="first")
    closes.index = pd.to_datetime(closes.index)
    closes = closes.sort_index()
    try:
        import VolumePrediction.data.splits_loader as sl   # sys.path → 根目录
        from CorporateActions import adjust_price_df       # 集中复用,一行不改
        by_t: dict[str, list] = {}
        for sp in sl.refresh():
            by_t.setdefault(sp.get("ticker"), []).append(sp)
        n_adj = 0
        for t in closes.columns:
            splits = by_t.get(t)
            if not splits:
                continue
            frame = closes[[t]].rename(columns={t: "c"}).copy()
            frame, n = adjust_price_df(frame, t, splits=splits,
                                       price_cols=["c"], volume_col=None)
            if n:
                closes[t] = frame["c"]
                n_adj += n
        log.info(f"split adjustment applied: {n_adj} events across window")
    except Exception as e:  # noqa: BLE001 — 不静默: 记入 warnings + 日志
        msg = f"splits adjustment unavailable ({type(e).__name__}) — using raw closes"
        log.warning(msg)
        warnings.append(msg)
    return closes


def pairs_signal_history(events: pd.DataFrame, closes: pd.DataFrame,
                         max_delay: int = MAX_DELAY,
                         hold: int = HOLD_DAYS) -> pd.DataFrame:
    """(z_score, delay_days, realized_pnl_frac) 样本(calibrate_mu_pairs 契约)。

    延迟 d 入场(signal_date 后第 1+d 个交易日收盘)、固定在 t0+max_delay+hold
    出场 → 晚入场少捕获的收敛差就是延迟机会成本;pnl 按双腿总名义归一。
    """
    if events.empty or closes.empty:
        return pd.DataFrame()
    dates = closes.index
    rows = []
    for ev in events.itertuples():
        pos = int(dates.searchsorted(pd.Timestamp(ev.signal_date), side="right"))
        h_pos = pos + max_delay + hold
        if h_pos >= len(dates):
            continue                                   # 前向窗未走完 → 事件出样
        if ev.s1 not in closes.columns or ev.s2 not in closes.columns:
            continue
        p1 = closes[ev.s1].to_numpy()
        p2 = closes[ev.s2].to_numpy()
        p1_0, p2_0, p1_H, p2_H = p1[pos], p2[pos], p1[h_pos], p2[h_pos]
        if not all(np.isfinite(x) and x > 0 for x in (p1_0, p2_0, p1_H, p2_H)):
            continue
        notional = abs(ev.s1_shares) * p1_0 + abs(ev.s2_shares) * p2_0
        if notional <= 0:
            continue
        for d in range(max_delay + 1):
            p1_d, p2_d = p1[pos + d], p2[pos + d]
            if not (np.isfinite(p1_d) and np.isfinite(p2_d)):
                continue
            pnl = (ev.s1_shares * (p1_H - p1_d)
                   + ev.s2_shares * (p2_H - p2_d))
            rows.append({"strategy": ev.strategy, "pair": ev.pair,
                         "signal_date": ev.signal_date, "z_score": ev.z,
                         "delay_days": d,
                         "realized_pnl_frac": pnl / notional})
    return pd.DataFrame(rows)


# ═══════════════════════════ momentum(aiss / ssrs) ══════════════════════════

BREAK_THRESHOLD = 0.35     # 相邻报告价变 >35% 视为指数重基/拆股断裂(实测: AISS
                           # equipment 2026-06 有 2.49× 断点;真实日移 ≤0.14)


def momentum_decay_curve(report_dir: Path, pattern: str,
                         max_delay: int = MAX_DELAY,
                         hold: int = HOLD_DAYS,
                         break_threshold: float = BREAK_THRESHOLD
                         ) -> Tuple[Optional[pd.Series], dict]:
    """日报 composite_score 横截面动量组合的 alpha-延迟衰减曲线。

    每个报告日 t: w_i ∝ score_i − mean(score)(Σ|w|=1,多空);
    延迟 d 执行 → 持有 [t+d, t+d+hold](报告日历)组合收益 alpha(d);
    曲线 = alpha(d) 对事件求均值(名义比例口径,单位=组合总名义的比例)。
    断裂防护: 事件全跨度内相邻报告价变超 break_threshold 的名字判为
    重基/拆股断裂 → 整事件剔除该名字(篮子跨延迟一致),计入 diagnostics。
    """
    files = sorted(report_dir.glob(pattern))
    panel: dict[str, dict[str, tuple]] = {}          # date → {ticker: (score, price)}
    for f in files:                                   # 文件名含时间戳升序 → 末份覆盖
        try:
            d = json.loads(f.read_text())
        except Exception as e:  # noqa: BLE001
            log.warning(f"unreadable report {f.name}: {e}")
            continue
        sd = d.get("signal_date")
        if not sd:
            continue
        day = {}
        for s in d.get("signals") or []:
            tk, sc, pr = s.get("ticker"), s.get("composite_score"), s.get("price")
            if tk and sc is not None and pr and pr > 0:
                day[tk] = (float(sc), float(pr))
        if len(day) >= 3:                             # 横截面至少 3 名字
            panel[sd] = day
    diag = {"n_report_files": len(files), "n_report_days": len(panel)}
    if not panel:
        return None, diag
    curve, core_diag = decay_curve_from_panel(panel, max_delay, hold,
                                              break_threshold)
    diag.update(core_diag)
    return curve, diag


def decay_curve_from_panel(panel: dict,
                           max_delay: int = MAX_DELAY,
                           hold: int = HOLD_DAYS,
                           break_threshold: float = BREAK_THRESHOLD
                           ) -> Tuple[Optional[pd.Series], dict]:
    """momentum_decay_curve 的面板核心(E11-T2 抽取,行为与原函数逐位一致):
    panel = {date: {ticker: (score, price)}} → (curve, diagnostics)。
    供合成信号源(ssrs 价格合成动量)复用同一套事件/断裂/加权逻辑。"""
    diag: dict = {}
    dates = sorted(panel)
    acc: dict[int, list] = {d: [] for d in range(max_delay + 1)}
    n_events = 0
    n_break_excluded = 0
    for i in range(len(dates) - max_delay - hold):
        span = dates[i: i + max_delay + hold + 1]
        day0 = panel[span[0]]
        valid = []
        for t in sorted(day0):
            ps = [panel[d].get(t) for d in span]
            if any(p is None for p in ps):
                continue
            px = [p[1] for p in ps]
            if any(abs(px[k + 1] / px[k] - 1.0) > break_threshold
                   for k in range(len(px) - 1)):
                n_break_excluded += 1              # 重基/拆股断裂 → 整事件剔除
                continue
            valid.append(t)
        if len(valid) < 3:
            continue
        scores = np.array([day0[t][0] for t in valid])
        w = scores - scores.mean()
        if np.abs(w).sum() <= 0:
            continue
        w = w / np.abs(w).sum()
        for d in range(max_delay + 1):
            e_day, x_day = panel[span[d]], panel[span[d + hold]]
            alpha = float(sum(wt * (x_day[t][1] / e_day[t][1] - 1.0)
                              for t, wt in zip(valid, w)))
            acc[d].append(alpha)
        n_events += 1
    curve = pd.Series({d: float(np.mean(v)) for d, v in acc.items() if v},
                      name="alpha").sort_index()
    diag.update({"n_events": n_events,
                 "n_break_excluded": n_break_excluded,
                 "break_threshold": break_threshold,
                 "alpha_by_delay": {int(k): round(float(v), 6)
                                    for k, v in curve.items()}})
    return (curve if len(curve) >= 3 else None), diag


# ═══════════════════════════════ λ fills(账本) ═══════════════════════════════

def build_fills(svc: VolumeService) -> Tuple[pd.DataFrame, dict]:
    """trade_ledger 真实成交 → participation + VWAP 口径冲击代理。

    无到达价 → impact 用 (成交价−当日VWAP)/VWAP×side 代理(含时点漂移噪声,
    diagnostics 明示口径);pairs 侧无逐笔成交价记录 → 不入 λ 样本(留档)。
    """
    cfg = load_config()
    ledgers = cfg.get("consume_paths", {}).get("ledgers", {})
    rows, n_raw = [], 0
    for strat, rel in ledgers.items():
        p = REPO / rel
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            n_raw += 1
            date, tk = r.get("date"), r.get("ticker")
            gross, price = r.get("gross"), r.get("price")
            side = str(r.get("side", "")).upper()
            if not (date and tk and gross and price):
                continue
            day = svc._load_day(date)
            if day.empty:
                continue
            m = day[day["ticker"] == tk]
            if m.empty:
                continue
            vw = float(m.iloc[0]["vw"])
            dv = float(m.iloc[0]["dollar_volume"])
            if vw <= 0 or dv <= 0:
                continue
            sign = 1.0 if side.startswith("BUY") else -1.0
            rows.append({"strategy": strat, "date": date, "ticker": tk,
                         "participation": float(gross) / dv,
                         "impact": sign * (float(price) - vw) / vw})
    fills = pd.DataFrame(rows)
    diag = {"n_ledger_records": n_raw, "n_with_volume": int(len(fills)),
            "n_impact_positive": int((fills["impact"] > 0).sum()) if len(fills) else 0,
            "impact_proxy": "side*(fill_price-day_vwap)/day_vwap (无到达价的 TCA 代理)",
            "note": "pairs 无逐笔成交价记录 → 未入 λ 样本(§7.7 留档)"}
    return fills, diag


# ═══════════════════════════════════ runner ═════════════════════════════════

def run(artifacts_dir: Optional[Path] = None,
        max_delay: int = MAX_DELAY, hold: int = HOLD_DAYS) -> dict:
    svc = VolumeService(artifacts_dir=artifacts_dir)
    warnings: list[str] = []

    # 1) pairs μ(mrpt+mtfs 合样 → 注册键 pairs_decay)
    events = load_pairs_events()
    sig_hist = pd.DataFrame()
    if events.empty:
        warnings.append("no pairs OPEN events found in trading_signals/")
    else:
        tickers = set(events["s1"]) | set(events["s2"])
        start = min(events["signal_date"])
        end = svc._last_raw_date() or pd.Timestamp.now().strftime("%Y-%m-%d")
        closes = build_close_matrix(tickers, start, end, warnings)
        sig_hist = pairs_signal_history(events, closes, max_delay, hold)
    res_pairs = svc.econ.calibrate_mu(
        "pairs", signal_history_df=sig_hist if len(sig_hist) else None)
    log.info(f"pairs μ: {res_pairs}")
    # 分策略参考回归(只入 diagnostics,注册键以合样为准——不挑子样本)
    from VolumePrediction.econ import calibration as cal
    per_strat = {}
    if len(sig_hist):
        for st, sub in sig_hist.groupby("strategy"):
            r = cal.calibrate_mu_pairs(sub, strategy=st)
            per_strat[st] = {k: r.get(k) for k in
                             ("mu", "slope", "r2", "n", "calibration_source")}

    # 2) aiss μ(子板块动量衰减 → aiss_mom_decay)
    curve_a, diag_a = momentum_decay_curve(
        REPO / "qlib-main/semiconductor_strategy/trading_signals",
        "aiss_daily_report_*.json", max_delay, hold)
    if curve_a is None:
        warnings.append("aiss decay curve unavailable → cold_start prior")
    res_aiss = svc.econ.calibrate_mu("aiss", alpha_decay_curve=curve_a)
    log.info(f"aiss μ: {res_aiss}")

    # 3) ssrs μ(行业 ETF 动量衰减 → ssrs_mom_decay)
    curve_s, diag_s = momentum_decay_curve(
        REPO / "qlib-main/sector_rotation/trading_signals",
        "sr_daily_report_*.json", max_delay, hold)
    if curve_s is None:
        warnings.append("ssrs decay curve unavailable → cold_start prior")
    res_ssrs = svc.econ.calibrate_mu("ssrs", alpha_decay_curve=curve_s)
    log.info(f"ssrs μ: {res_ssrs}")

    # 4) λ(真实账本成交;n<MIN_SAMPLES → 论文先验,显式标注)
    fills, diag_f = build_fills(svc)
    res_lambda = svc.econ.calibrate_lambda_from_fills(
        "all", fills_df=fills if len(fills) else None)
    log.info(f"lambda: {res_lambda}")

    # 5) 六 profile 逐个解析 μ + 来源(§5.7/§7.12;urgent 剖面 μ=∞ 按定义)
    from VolumePrediction.econ import objective as obj
    profiles = {}
    for name, prof in sorted(obj.registry().items()):
        mu, src = obj.resolve_mu(prof, artifacts_dir=svc.art)
        profiles[name] = {
            "mode": prof.mode, "mu_source": prof.mu_source,
            "mu_key": prof.mu_key,
            "mu": ("inf" if math.isinf(mu) else mu),
            "calibration_source": src,
        }

    diagnostics = {
        "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "params": {"max_delay": max_delay, "hold_days": hold},
        "mu_units": "名义比例/日(losscon 的 λz² 同量纲)",
        "pairs": {
            "n_open_events": int(len(events)),
            "n_regression_rows": int(len(sig_hist)),
            "by_strategy": (events["strategy"].value_counts().to_dict()
                            if len(events) else {}),
            "strength_source": (events["z_source"].value_counts().to_dict()
                                if len(events) else {}),
            "per_strategy_regression": per_strat,
            "note": ("注册键 pairs_decay 以 mrpt+mtfs 合样为准;分策略斜率仅供"
                     "参考(mrpt 呈衰减/mtfs 反向 → 合样无稳健衰减时按冷启动"
                     "先验标注,不挑子样本)"),
            "signal_date_range": ([str(events["signal_date"].min()),
                                   str(events["signal_date"].max())]
                                  if len(events) else None),
        },
        "aiss": diag_a, "ssrs": diag_s, "lambda": diag_f,
        "warnings": warnings,
    }
    svc.econ._write_mu("profiles", profiles)
    svc.econ._write_mu("diagnostics", diagnostics)

    out_path = svc.art / "registry" / "mu_calibration.json"
    summary = {"artifact": str(out_path),
               "pairs_decay": res_pairs, "aiss_mom_decay": res_aiss,
               "ssrs_mom_decay": res_ssrs, "lambda_all": res_lambda,
               "profiles": profiles, "warnings": warnings}
    log.info(f"mu calibration artifact → {out_path}")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="μ/λ calibration runner (P2⑤)")
    ap.add_argument("--artifacts-dir", default=None,
                    help="覆盖输出目录(测试用;默认 VolumePrediction/outputs)")
    ap.add_argument("--max-delay", type=int, default=MAX_DELAY)
    ap.add_argument("--hold", type=int, default=HOLD_DAYS)
    a = ap.parse_args()
    res = run(artifacts_dir=Path(a.artifacts_dir) if a.artifacts_dir else None,
              max_delay=a.max_delay, hold=a.hold)
    print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
