"""wiring_shadow.py — ③接线的影子双算(W3/W4),零接触策略代码(2026-08-01)。

目的(consumption_wiring_proposal.md 第一批): 在**不改 SelectPairs/RiskManager 一行**
的前提下,外部忠实复刻两处 ADV 消费逻辑,每日双算"后视 vs 前瞻"并记录 diff。
两周数据后由用户决定 W3/W4 是否真切换。

忠实复刻的口径(2026-08-01 对码取证):
  W3 SelectPairs: `_MIN_AVG_DAILY_VOLUME=300_000`(股数);均值窗=其抓取序列全长
     (~120个交易日)。影子宇宙=当日 forecast 工件全部活跃票(选对宇宙的超集,
     flip 统计为真实过滤 flip 的上界,输出中如实标注)。
  W4 RiskManager: `adv=近20交易日股数均值`;`dtl=|shares|/(adv×ADV_PARTICIPATION=0.20)`;
     腿=当前 inventory(mrpt+mtfs)持仓。
  前瞻股数换算: forecast 预测的是美元量 pred_V → 股数 = pred_V / 最新close。

输出: outputs/wiring_shadow/wiring_shadow_{date}.json (明细)
      outputs/wiring_shadow/wiring_shadow_tracking.csv (逐日汇总累积)
失败语义: 任何异常记入返回值并大声 log,不抛出——daily_update 的附加步骤,
绝不影响 refresh/adapters 主流程。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from VolumePrediction.common import REPO, OUT

log = logging.getLogger("VolumePrediction.wiring_shadow")

W3_MIN_AVG_SHARES = 300_000     # == SelectPairs._MIN_AVG_DAILY_VOLUME
W3_TRAIL_DAYS = 120             # ≈其 lookback_days=180 抓到的交易日序列长
W4_ADV_WINDOW = 20              # == RiskManager.ADV_WINDOW
W4_PARTICIPATION = 0.20         # == RiskManager.ADV_PARTICIPATION(红线,只读引用)

RAW_DIR = REPO / "price_data" / "volume_prediction" / "raw"


def _trailing_shares(date: str, days: int) -> pd.DataFrame:
    """近 days 个原始日的股数成交量宽表(含 close 末值)。"""
    from VolumePrediction.data import polygon_loader as pl
    ds = [d for d in pl.trading_days("2025-06-01", date) if d <= date][-days:]
    frames = []
    for d in ds:
        p = RAW_DIR / f"grouped_{d}.parquet"
        if not p.exists():
            continue
        df = pd.read_parquet(p, columns=["ticker", "v", "c"])
        df["date"] = d
        frames.append(df)
    long = pd.concat(frames, ignore_index=True)
    shares = long.pivot_table(index="date", columns="ticker", values="v", aggfunc="last")
    close_last = long.sort_values("date").groupby("ticker")["c"].last()
    return shares, close_last


def run(date: str, service=None, out_dir: Optional[Path] = None) -> dict:
    from VolumePrediction.service import VolumeService
    svc = service or VolumeService()
    od = Path(out_dir) if out_dir else (OUT / "wiring_shadow")
    od.mkdir(parents=True, exist_ok=True)
    result: dict = {"date": date, "status": "ok", "errors": []}

    fc = svc._history_file(date)
    if fc is None or fc.empty:
        result["status"] = "error"
        result["errors"].append(f"no forecast artifact for {date}")
        log.error(result["errors"][-1])
        return result

    try:
        shares, close_last = _trailing_shares(date, W3_TRAIL_DAYS)
    except Exception as e:  # noqa: BLE001
        result["status"] = "error"
        result["errors"].append(f"raw load failed: {e}")
        log.error(result["errors"][-1])
        return result

    fc = fc.set_index("ticker")
    common = fc.index.intersection(shares.columns).intersection(close_last.index)
    pred_shares = (fc.loc[common, "pred_V"] / close_last.loc[common]).replace(
        [np.inf, -np.inf], np.nan)

    # ── W3: SelectPairs 过滤影子(超集宇宙,上界统计) ──
    try:
        trail_mean = shares[list(common)].mean()          # 全序列均值(口径同源)
        pass_old = trail_mean >= W3_MIN_AVG_SHARES
        # 前瞻口径: 用预测日量替换"明天的一天"没有意义——过滤是均值口径,
        # 影子采用"混合均值": (旧均值×(n-1) + 预测股数)/n ≈ 预测对过滤的边际影响;
        # 同时记录纯预测口径(pred_shares vs 阈值)作参考。
        n = shares[list(common)].notna().sum()
        blend = (trail_mean * (n - 1) + pred_shares) / n
        pass_new = blend >= W3_MIN_AVG_SHARES
        flips = pass_old != pass_new
        w3 = {
            "universe_n": int(len(common)),
            "pass_old": int(pass_old.sum()), "pass_new": int(pass_new.sum()),
            "flips": int(flips.sum()),
            "flip_tickers": sorted(common[flips].tolist())[:40],
            "note": "超集宇宙上界;blend=均值替换边际口径",
        }
    except Exception as e:  # noqa: BLE001
        w3 = {"error": str(e)}
        result["errors"].append(f"W3: {e}")

    # ── W4: RiskManager.dtl 影子(当前持仓腿) ──
    legs_out = []
    try:
        for inv_f, strat in ((REPO / "inventory_mrpt.json", "mrpt"),
                             (REPO / "inventory_mtfs.json", "mtfs")):
            inv = json.loads(inv_f.read_text())
            for pk, p in (inv.get("pairs") or {}).items():
                if not p.get("direction"):
                    continue
                for sym_key, sh_key in (("s1", "s1_shares"), ("s2", "s2_shares")):
                    tk = pk.split("/")[0] if sym_key == "s1" else pk.split("/")[1]
                    sh = p.get(sh_key)
                    if not sh or tk not in shares.columns:
                        continue
                    adv_old = float(shares[tk].dropna().tail(W4_ADV_WINDOW).mean())
                    adv_new = float(pred_shares.get(tk, np.nan))
                    dtl_old = abs(sh) / (adv_old * W4_PARTICIPATION) if adv_old > 0 else None
                    dtl_new = abs(sh) / (adv_new * W4_PARTICIPATION) if adv_new and adv_new > 0 else None
                    legs_out.append({
                        "strategy": strat, "pair": pk, "ticker": tk,
                        "shares": abs(sh),
                        "adv20_trailing": round(adv_old, 0),
                        "adv_forecast": round(adv_new, 0) if np.isfinite(adv_new) else None,
                        "ratio_new_over_old": round(adv_new / adv_old, 3)
                        if (adv_old > 0 and np.isfinite(adv_new)) else None,
                        "dtl_old": round(dtl_old, 3) if dtl_old is not None else None,
                        "dtl_new": round(dtl_new, 3) if dtl_new is not None else None,
                    })
        ratios = [l["ratio_new_over_old"] for l in legs_out
                  if l["ratio_new_over_old"] is not None]
        w4 = {
            "n_legs": len(legs_out),
            "ratio_median": round(float(np.median(ratios)), 3) if ratios else None,
            "ratio_min": round(float(min(ratios)), 3) if ratios else None,
            "ratio_max": round(float(max(ratios)), 3) if ratios else None,
            "dtl_breach_old": sum(1 for l in legs_out
                                  if (l["dtl_old"] or 0) > 5),
            "dtl_breach_new": sum(1 for l in legs_out
                                  if (l["dtl_new"] or 0) > 5),
            "legs": legs_out,
        }
    except Exception as e:  # noqa: BLE001
        w4 = {"error": str(e)}
        result["errors"].append(f"W4: {e}")

    result["w3"] = w3
    result["w4"] = {k: v for k, v in w4.items() if k != "legs"}

    # 明细 JSON(原子写) + 汇总 CSV 追加
    detail = {**result, "w4_legs": w4.get("legs", [])}
    pj = od / f"wiring_shadow_{date}.json"
    tmp = pj.with_suffix(".tmp")
    tmp.write_text(json.dumps(detail, indent=1, ensure_ascii=False, default=str))
    tmp.rename(pj)

    csv_p = od / "wiring_shadow_tracking.csv"
    row = pd.DataFrame([{
        "date": date,
        "w3_universe": w3.get("universe_n"), "w3_flips": w3.get("flips"),
        "w4_legs": w4.get("n_legs"), "w4_ratio_median": w4.get("ratio_median"),
        "w4_breach_old": w4.get("dtl_breach_old"),
        "w4_breach_new": w4.get("dtl_breach_new"),
        "status": result["status"],
    }])
    # 幂等写入(2026-08-26 修): 原先裸 mode="a" 追加 —— 同一日重跑就多一行,
    # 8/07、8/14、8/21 各留下一条重复行(周四重跑),下游按日读会取到旧那条。
    # 改成 "同日去重(留最新) + 按列对齐 + 整表原子重写",与 shadow_rnn.
    # append_track 同纪律(裸 append 不看已有 header,列序一变就静默错位)。
    if csv_p.exists():
        old = pd.read_csv(csv_p)
        old = old[old["date"].astype(str) != str(date)]
        if set(old.columns) == set(row.columns):
            row = row[old.columns]                    # 列序对齐,防错位
        n_dup = len(pd.read_csv(csv_p)) - len(old)
        if n_dup:
            log.info(f"wiring_shadow: {date} 已有 {n_dup} 行,覆盖为本次结果")
        row = pd.concat([old, row], ignore_index=True)
        row = row.sort_values("date", kind="stable").reset_index(drop=True)
    ctmp = csv_p.with_suffix(".tmp")
    row.to_csv(ctmp, index=False)
    ctmp.replace(csv_p)
    if result["errors"]:
        result["status"] = "partial" if (isinstance(w3, dict) and "error" not in w3) \
            or (isinstance(w4, dict) and "error" not in w4) else "error"
    log.info(f"wiring_shadow {date}: W3 flips={w3.get('flips')} "
             f"W4 ratio_med={w4.get('ratio_median')} → {pj.name}")
    return result
