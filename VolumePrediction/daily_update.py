"""
daily_update — P3 独立日更脚本(不进 conductor;手动/独立 cron 由用户后批)
==========================================================================
流程(Plan §9-P3 / §7.6 SLA):
  1. polygon_loader 增量拉当日 grouped bar(已缓存则幂等跳过)
  2. service.ops.refresh → 预测工件 outputs/volume_forecast_latest.parquet
     + outputs/history/volume_forecast_{date}.parquet(原子写,重跑覆盖=幂等)
  3. strategy_adapters 三建议文件(pairs×2 / aiss / ssrs → outputs/adapters/)

幂等: 原始 bar 不可变(存在即跳过);工件与建议文件按日期原子覆盖写。
--dry-run: 只读体检+计划报告,零写入。
失败语义: refresh 失败 → 退出码 1(工件保留昨日,health 标 stale,§7.6);
单个 adapter 失败不阻断其余,汇总入 exit code 2。

运行(仓库根,先 source .env 使 POLYGON_API_KEY 可见):
  set -a && source .env && set +a && \
  conda run -n someopark_run python -m VolumePrediction.daily_update

# ── cron 行(供用户后批,本脚本绝不自行安装;§7.6 SLA 16:15-17:00 ET)──
# 20 16 * * 1-5 cd /Users/xuling/code/someopark-test && set -a && . ./.env && set +a && conda run -n someopark_run python -m VolumePrediction.daily_update >> VolumePrediction/logs/daily_update_cron.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

from VolumePrediction.common import OUT, get_logger
from VolumePrediction.service import VolumeService

log = get_logger("daily_update")


def _target_date(date: Optional[str]) -> str:
    """目标交易日: 显式指定,否则 ET 今日(含)之前最近的 NYSE 交易日。"""
    if date:
        return date
    from VolumePrediction.data import polygon_loader as pl
    today = pd.Timestamp.now(tz="America/New_York").strftime("%Y-%m-%d")
    days = pl.trading_days("2026-01-01", today)
    if not days:
        raise RuntimeError("no trading days resolved — calendar unavailable")
    return days[-1]


def dry_run_report(svc: VolumeService, date: Optional[str]) -> dict:
    """零写入体检: 目标日/缓存/工件新鲜度/密钥可见性/计划写入清单。"""
    target = _target_date(date)
    raw_exists = (svc.raw / f"grouped_{target}.parquet").exists()
    health = svc.ops.health()
    planned = [str(OUT / "volume_forecast_latest.parquet"),
               str(OUT / "history" / f"volume_forecast_{target}.parquet")] + [
        str(OUT / "adapters" / f)
        for f in (f"pairs_mrpt_advice_{target}.json",
                  f"pairs_mtfs_advice_{target}.json",
                  f"aiss_advice_{target}.json",
                  f"ssrs_advice_{target}.json")]
    return {"mode": "dry_run", "target_date": target,
            "raw_cached": raw_exists,
            "would_fetch": not raw_exists,
            "polygon_key_visible": bool(os.environ.get("POLYGON_API_KEY")),
            "health": health, "planned_writes": planned}


def run(date: Optional[str] = None, fetch: bool = True,
        skip_adapters: bool = False) -> dict:
    svc = VolumeService()
    target = _target_date(date)
    log.info(f"daily_update start: target={target} fetch={fetch}")

    # 1+2) 增量拉取 + 预测工件(service.ops.refresh 内含 ensure_day 幂等拉取)
    res = svc.ops.refresh(date=target, fetch=fetch)
    if res.get("status") != "ok":
        log.error(f"refresh failed: {res}")
        return {"status": "error", "stage": "refresh", "detail": res}
    asof = res["asof"]

    # 2b) 改名发现(2026-08-15,BK→BNY 驱动;fail-open): 消费票(四策略持仓/
    # 槽位)从当日 raw 消失 → 查 Polygon events(旧名 404 再走 security master
    # CUSIP/CIK)确证改名并落 ticker_aliases.json —— 服务/历史归一自动生效。
    aliases = None
    try:
        import ticker_aliases as ta
        from VolumePrediction.common import REPO
        from VolumePrediction.shadow_rnn import _held_tickers
        day = svc._load_day(asof)
        rawset = set(day["ticker"]) if not day.empty else set()
        if rawset:
            consumed = set(_held_tickers(asof))
            for f in ("inventory_mrpt.json", "inventory_mtfs.json"):
                inv = json.loads((REPO / f).read_text())
                for name in (inv.get("pairs") or {}):
                    consumed.update(name.split("/"))
            gone = sorted(t for t in consumed
                          if t and t not in rawset and t not in ta.load_aliases())
            if gone:
                r = ta.refresh_aliases(gone)
                aliases = r
                if r["added"]:
                    log.warning(f"ticker renames detected: "
                                f"{ {k: v['current'] for k, v in r['added'].items()} }")
                if r["unresolved"]:
                    log.warning(f"tickers missing from raw, NOT a verified rename "
                                f"(delist/halt?): {r['unresolved']}")
            # 已入册条目的回收复查(≤每 7 天一次/条;旧名被新实体启用时
            # resolve 必须止步,否则 BK 型解析在回收后继续错给现名)
            rc = ta.recheck_recycled()
            if rc.get("recycled_found"):
                log.warning(f"aliased old names RECYCLED by new entities: "
                            f"{rc['recycled_found']} — resolve 已止步")
                aliases = {**(aliases or {}), "recycled_found":
                           rc["recycled_found"]}
    except Exception as e:  # noqa: BLE001 — 发现步失败绝不影响主流程
        log.error(f"alias discovery failed (non-fatal): {e}")
        aliases = {"status": "error", "error": str(e)}

    # 3) 三类 adapter 建议文件(pairs 两策略共 4 份;单个失败不阻断)
    adapters: dict[str, dict] = {}
    if not skip_adapters:
        from VolumePrediction.strategy_adapters import (aiss_adapter,
                                                        pairs_adapter,
                                                        ssrs_adapter)
        jobs = [("pairs_mrpt", lambda: pairs_adapter.run("mrpt", date=asof, service=svc)),
                ("pairs_mtfs", lambda: pairs_adapter.run("mtfs", date=asof, service=svc)),
                ("aiss", lambda: aiss_adapter.run(date=asof, service=svc)),
                ("ssrs", lambda: ssrs_adapter.run(date=asof, service=svc))]
        for name, job in jobs:
            try:
                adv = job()
                adapters[name] = {"status": "ok",
                                  "warnings": adv.get("warnings", [])}
            except Exception as e:  # noqa: BLE001 — 单 adapter 失败不阻断,汇总上报
                log.error(f"adapter {name} failed: {e}")
                adapters[name] = {"status": "error", "error": str(e)}

    # ── ③接线影子双算(W3/W4;2026-08-01,零接触策略代码;fail-open 附加步) ──
    wiring = None
    if not skip_adapters:
        try:
            from VolumePrediction import wiring_shadow
            ws = wiring_shadow.run(asof, service=svc)
            wiring = {"status": ws.get("status"),
                      "w3_flips": (ws.get("w3") or {}).get("flips"),
                      "w4_ratio_median": (ws.get("w4") or {}).get("ratio_median")}
        except Exception as e:  # noqa: BLE001 — 影子失败绝不影响主流程
            log.error(f"wiring_shadow failed (non-fatal): {e}")
            wiring = {"status": "error", "error": str(e)}

    # ── RNN 候选影子 AB（E4；2026-08-08 接线，防断更；fail-open 附加步）──
    # 每日 serve 候选 RNN（有状态滚动，只到 raw_last）+ 滞后评估补齐 AB 追踪表。
    # 此前未接线，serve 停在手动跑的 2026-08-05，AB 长期停 2 行。用 run_daily()
    # 纯函数入口（不走 argparse），避免 SystemExit 继承 BaseException 崩主流程。
    rnn_ab = None
    blend_ab = None
    if not skip_adapters:
        try:
            from VolumePrediction import shadow_rnn
            rc = shadow_rnn.run_daily()
            rnn_ab = {"status": "ok" if rc == 0 else "error", "rc": rc}
        except Exception as e:  # noqa: BLE001 — RNN 影子失败绝不影响主流程
            log.error(f"shadow_rnn failed (non-fatal): {e}")
            rnn_ab = {"status": "error", "error": str(e)}
        # 三层分层服务影子(选项 B,用户批准 2026-08-10;纯拼接零算力,8/15 拍板)
        try:
            from VolumePrediction import shadow_blend
            rcb = shadow_blend.run_daily()
            blend_ab = {"status": "ok" if rcb == 0 else "error", "rc": rcb}
        except Exception as e:  # noqa: BLE001 — blend 影子失败绝不影响主流程
            log.error(f"shadow_blend failed (non-fatal): {e}")
            blend_ab = {"status": "error", "error": str(e)}

    health = svc.ops.health()
    # §5.3 计划命名的健康工件落盘(plan L424): 供外部监控免起服务进程直接读。
    # 原子写(tmp+replace),失败绝不阻断日更主流程。
    try:
        hp = OUT / "service_health.json"
        tmp = hp.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {**health, "written_at": pd.Timestamp.now().isoformat()},
            indent=2, ensure_ascii=False, default=str))
        tmp.replace(hp)
    except Exception as e:  # noqa: BLE001
        log.warning(f"service_health.json write failed (non-fatal): {e}")
    out = {"status": "ok", "asof": asof, "refresh": res,
           "adapters": adapters, "health": health,
           "wiring_shadow": wiring, "rnn_ab_shadow": rnn_ab,
           "blend_ab_shadow": blend_ab, "ticker_aliases": aliases}
    if any(v.get("status") == "error" for v in adapters.values()):
        out["status"] = "partial"
    log.info(f"daily_update done: {out['status']} asof={asof} "
             f"n={res.get('n')} adapters={ {k: v['status'] for k, v in adapters.items()} }")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="VolumePrediction 日更(P3;独立脚本,幂等可重跑)")
    ap.add_argument("--date", default=None, help="目标交易日 YYYY-MM-DD(默认最近)")
    ap.add_argument("--dry-run", action="store_true", help="只读体检,零写入")
    ap.add_argument("--no-fetch", action="store_true",
                    help="跳过 Polygon 拉取,只用已缓存 bar 重算工件")
    ap.add_argument("--skip-adapters", action="store_true",
                    help="只刷预测工件,不产 adapter 建议文件")
    a = ap.parse_args()

    if a.dry_run:
        rep = dry_run_report(VolumeService(), a.date)
        print(json.dumps(rep, indent=2, ensure_ascii=False, default=str))
        return 0

    res = run(date=a.date, fetch=not a.no_fetch, skip_adapters=a.skip_adapters)
    print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
    return {"ok": 0, "partial": 2}.get(res.get("status"), 1)


if __name__ == "__main__":
    sys.exit(main())
