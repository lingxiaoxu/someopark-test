"""
aeus_adapter — AEUS 薄封装(Plan §5.6/§5.8/附录 D;只读输入,只写 outputs/)
=========================================================================
输入(只读): inventory_aeus.json(双层持仓) + account_aeus.json(AUM)
输出: outputs/adapters/aeus_advice_{date}.json (schema_version=v1)
内容: 持仓个股 ADV 前瞻/清仓天数 + 投降量信号(capitulation → recovery 维度
的二次确认特征,单向文件接口)。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pandas as pd

from VolumePrediction.common import REPO, OUT, load_config, get_logger, sanitize_for_json
from VolumePrediction.service import VolumeService

log = get_logger("aeus_adapter")


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
    inv = _read_json(REPO / paths["inventories"]["aeus"])
    acct = _read_json(REPO / paths["accounts"]["aeus"])
    if inv is None:
        warnings.append("inventory_aeus.json missing/unreadable")
    aum = (acct or {}).get("equity")

    tickers: set[str] = set()
    stock_holdings = (inv or {}).get("stock_holdings") or {}
    if isinstance(stock_holdings, dict):
        tickers |= {t for t in stock_holdings.keys() if isinstance(t, str)}
    extra = (cfg.get("universe", {}).get("service_extra", {}) or {}).get("etfs", [])
    watch = sorted(tickers | {"SOXX", "SMH"} & set(extra)) or sorted(set(extra) & {"SOXX", "SMH"})

    holdings_advice = []
    for tk in sorted(tickers):
        pos = stock_holdings.get(tk) or {}
        shares = pos.get("shares", 0) if isinstance(pos, dict) else 0
        holdings_advice.append({
            "ticker": tk, "shares": shares,
            "adv_forecast": svc.adv.get_adv_forecast(tk, date=date),
            "dtl": svc.execute.days_to_liquidate(tk, shares or 0, date=date).get("days"),
            "objective": "aeus_rebalance"})

    cap_universe = sorted(tickers) or watch
    cap = svc.signals.capitulation(cap_universe, date=date) if cap_universe else None

    # 公司行为体检(2026-08-26;与 pairs/ssrs adapter 同一登记处,纯本地零网络)
    from ticker_aliases import describe
    for t, d in sorted(describe([h["ticker"] for h in holdings_advice],
                                date).items()):
        warnings.append(
            f"CORPORATE ACTION [delisted] {t} ({d.get('name') or '?'}) "
            f"摘牌于 {d['delisted']} — 持仓票,该腿 ADV 已按 stale 拦截"
            if d["status"] == "delisted" else
            f"CORPORATE ACTION [renamed] {t} → {d['current']} — 取数已转发至现名")

    advice = {"schema_version": "v1", "strategy": "aeus", "date": date,
              "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
              "aum": aum,
              "holdings": holdings_advice,
              "capitulation": (cap.to_dict("records") if cap is not None else []),
              "warnings": warnings}
    od = Path(out_dir) if out_dir else (OUT / "adapters")
    od.mkdir(parents=True, exist_ok=True)
    p = od / f"aeus_advice_{date}.json"
    tmp = p.with_suffix(".tmp")
    # 非有限值 → null + warning(裸 NaN 让 jq/JS 解析炸;与 pairs_adapter 同款)
    advice, _nan = sanitize_for_json(advice)
    if _nan:
        advice["warnings"].append(
            f"no VP data (null-ed, likely rename/delist/new-listing): {_nan}")
        log.warning(f"aeus_adapter 无数据字段 → null: {_nan}")
    tmp.write_text(json.dumps(advice, indent=2, ensure_ascii=False, default=str,
                              allow_nan=False))
    tmp.replace(p)
    log.info(f"aeus_adapter → {p.name} ({len(holdings_advice)} holdings)")
    return advice
