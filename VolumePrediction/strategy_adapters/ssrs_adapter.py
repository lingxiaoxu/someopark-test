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

from VolumePrediction.common import REPO, OUT, load_config, get_logger
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
            "impact_per_100k": cost.get("cost_dollars"),
            "objective": "ssrs_rebalance"})

    outlook = svc.signals.market_liquidity_outlook(date=date, horizon=5)
    advice = {"schema_version": "v1", "strategy": "ssrs", "date": date,
              "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
              "aum": aum, "etfs": rows,
              "liquidity_trend_vs_ma5": outlook.get("trend_vs_ma5"),
              "upcoming_events": (outlook.get("upcoming_events").to_dict("records")
                                  if outlook.get("upcoming_events") is not None else []),
              "warnings": warnings}
    od = Path(out_dir) if out_dir else (OUT / "adapters")
    od.mkdir(parents=True, exist_ok=True)
    p = od / f"ssrs_advice_{date}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(advice, indent=2, ensure_ascii=False, default=str))
    tmp.replace(p)
    log.info(f"ssrs_adapter → {p.name} ({len(rows)} etfs)")
    return advice
