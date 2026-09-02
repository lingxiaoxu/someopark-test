"""
ssrs_adapter — SSRS 薄封装(Plan §5.6/§5.8/附录 D;只读输入,只写 outputs/)
=========================================================================
输入(只读): inventory_sector_rotation.json + account_ssrs.json
输出: outputs/adapters/ssrs_advice_{date}.json (schema_version=v1)
SPDR 超流动 → 我方规模 λ≈0 → 服务价值=执行时点/成本报告而非配给(§5.7)。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pandas as pd

from VolumePrediction.common import REPO, OUT, load_config, get_logger, sanitize_for_json
from VolumePrediction.service import VolumeService

log = get_logger("ssrs_adapter")


def _read_json(p: Path):
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except Exception:  # noqa: BLE001
        return None


def run(date: Optional[str] = None,
        service: Optional[VolumeService] = None,
        out_dir: Optional[Path] = None) -> dict:
    svc = service or VolumeService()
    cfg = load_config()
    date = date or pd.Timestamp.now().strftime("%Y-%m-%d")
    warnings: list[str] = []

    paths = cfg["consume_paths"]
    inv = _read_json(REPO / paths["inventories"]["ssrs"])
    acct = _read_json(REPO / paths["accounts"]["ssrs"])
    if inv is None:
        warnings.append("inventory_sector_rotation.json missing/unreadable")
    aum = (acct or {}).get("equity")

    holdings = (inv or {}).get("holdings") or {}
    etfs = sorted(holdings.keys()) if isinstance(holdings, dict) else []
    if not etfs:
        etfs = [e for e in cfg["universe"]["service_extra"]["etfs"]
                if e.startswith("XL")]

    rows = []
    for etf in etfs:
        pos = holdings.get(etf) if isinstance(holdings, dict) else None
        shares = (pos or {}).get("shares", 0) if isinstance(pos, dict) else 0
        cost = svc.econ.price_impact(etf, dollar_amount=1e5, date=date)
        rows.append({
            "etf": etf, "shares": shares,
            "adv_forecast": svc.adv.get_adv_forecast(etf, date=date),
            # 清仓天数(2026-09-01):与 pairs/aiss/aeus 同一 svc.execute 定义,
            # 面板三族 "清算力" 段才能用同一把尺
            "dtl": svc.execute.days_to_liquidate(etf, shares or 0, date=date).get("days"),
            "impact_per_100k": cost.get("cost_dollars"),
            "objective": "ssrs_rebalance"})

    # 公司行为体检(2026-08-26;与 pairs/aiss adapter 同一登记处,纯本地零网络)。
    # ETF 极少改名/清盘,但清盘(如 AAIT)与个股退市在数据面完全同形,同样要拦。
    from ticker_aliases import describe
    for t, d in sorted(describe([r["etf"] for r in rows], date).items()):
        warnings.append(
            f"CORPORATE ACTION [delisted] {t} ({d.get('name') or '?'}) "
            f"摘牌/清盘于 {d['delisted']} — 持仓 ETF,该腿 ADV 已按 stale 拦截"
            if d["status"] == "delisted" else
            f"CORPORATE ACTION [renamed] {t} → {d['current']} — 取数已转发至现名")

    outlook = svc.signals.market_liquidity_outlook(date=date, horizon=5)
    # 投降量监测(2026-09-01):与 pairs/aiss/aeus adapter 同一 svc.signals.capitulation,
    # 让五策略面板顶部的 "Capitulation 触发" 卡口径一致。范围 = 有持仓股数的 ETF。
    held_etfs = [r["etf"] for r in rows if (r.get("shares") or 0) > 0]
    cap = svc.signals.capitulation(held_etfs, date=date) if held_etfs else None
    advice = {"schema_version": "v1", "strategy": "ssrs", "date": date,
              "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
              "aum": aum, "etfs": rows,
              "capitulation": (cap.to_dict("records") if cap is not None else []),
              "liquidity_trend_vs_ma5": outlook.get("trend_vs_ma5"),
              "upcoming_events": (outlook.get("upcoming_events").to_dict("records")
                                  if outlook.get("upcoming_events") is not None else []),
              "warnings": warnings}
    od = Path(out_dir) if out_dir else (OUT / "adapters")
    od.mkdir(parents=True, exist_ok=True)
    p = od / f"ssrs_advice_{date}.json"
    tmp = p.with_suffix(".tmp")
    # 非有限值 → null + warning(裸 NaN 让 jq/JS 解析炸;与 pairs_adapter 同款)
    advice, _nan = sanitize_for_json(advice)
    if _nan:
        advice["warnings"].append(
            f"no VP data (null-ed, likely rename/delist/new-listing): {_nan}")
        log.warning(f"ssrs_adapter 无数据字段 → null: {_nan}")
    tmp.write_text(json.dumps(advice, indent=2, ensure_ascii=False, default=str,
                              allow_nan=False))
    tmp.replace(p)
    log.info(f"ssrs_adapter → {p.name} ({len(rows)} etfs)")
    return advice
