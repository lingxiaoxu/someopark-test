"""
pairs_adapter — MRPT/MTFS 薄封装(Plan §5.6/§5.8/附录 D;只读输入,只写 outputs/)
================================================================================
输入(只读): pair_universe_{strategy}.json + inventory_{strategy}.json
输出: outputs/adapters/pairs_{strategy}_advice_{date}.json (schema_version=v1)
内容: 每持仓对 → 双腿前瞻 ADV/清仓天数/退出联合排程预览;每候选对 → 双腿 ADV。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pandas as pd

from VolumePrediction.common import REPO, OUT, get_logger, sanitize_for_json
from VolumePrediction.service import VolumeService

log = get_logger("pairs_adapter")


def _read_json(p: Path):
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except Exception:  # noqa: BLE001
        return None


def run(strategy: str = "mrpt", date: Optional[str] = None,
        service: Optional[VolumeService] = None,
        out_dir: Optional[Path] = None) -> dict:
    svc = service or VolumeService()
    date = date or pd.Timestamp.now().strftime("%Y-%m-%d")
    warnings: list[str] = []

    uni = _read_json(REPO / f"pair_universe_{strategy}.json")
    inv = _read_json(REPO / f"inventory_{strategy}.json")
    if uni is None:
        warnings.append(f"pair_universe_{strategy}.json missing/unreadable")
    if inv is None:
        warnings.append(f"inventory_{strategy}.json missing/unreadable")

    positions = []
    for key, pos in ((inv or {}).get("positions") or (inv or {}).get("pairs")
                     or {}).items() if isinstance(inv, dict) else []:
        if not isinstance(pos, dict):
            continue
        s1, s2 = pos.get("s1"), pos.get("s2")
        if not s1 or not s2:
            parts = key.split("/")
            s1, s2 = (parts + [None, None])[:2]
        if not s1 or not s2:
            continue
        d1 = svc.execute.days_to_liquidate(s1, pos.get("s1_shares", 0) or 0, date=date)
        d2 = svc.execute.days_to_liquidate(s2, pos.get("s2_shares", 0) or 0, date=date)
        positions.append({
            "pair": key, "s1": s1, "s2": s2,
            "s1_dtl": d1.get("days"), "s2_dtl": d2.get("days"),
            "bottleneck": s1 if (d1.get("days") or 0) >= (d2.get("days") or 0) else s2,
            "adv_forecast": {s1: svc.adv.get_adv_forecast(s1, date=date),
                             s2: svc.adv.get_adv_forecast(s2, date=date)},
            "objective": "pairs_exit"})

    candidates = []
    # pair_universe_mrpt/mtfs.json 顶层是 list(实际生产格式,同 universe.py
    # 2026-07-24 修复);dict 格式 {"pairs":[...]} 兼容保留
    if isinstance(uni, list):
        pair_list = uni
    else:
        pair_list = (uni or {}).get("pairs") or (uni or {}).get("selected_pairs") or []
    for p in pair_list[:100]:
        s1 = p.get("s1") if isinstance(p, dict) else None
        s2 = p.get("s2") if isinstance(p, dict) else None
        if isinstance(p, str) and "/" in p:
            s1, s2 = (p.split("/") + [None])[:2]
        if s1 and s2:
            candidates.append({
                "s1": s1, "s2": s2,
                "adv_forecast": {s1: svc.adv.get_adv_forecast(s1, date=date),
                                 s2: svc.adv.get_adv_forecast(s2, date=date)},
                "objective": "pairs_entry"})

    advice = {"schema_version": "v1", "strategy": strategy, "date": date,
              "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
              "positions": positions, "candidates": candidates,
              "warnings": warnings}
    # 非有限值 → null + warning(裸 NaN 会让 jq/JS 解析炸;BK 改名票实证)
    advice, _nan = sanitize_for_json(advice)
    if _nan:
        advice["warnings"].append(
            f"no VP data (null-ed, likely rename/delist/new-listing): {_nan}")
        log.warning(f"pairs_adapter[{strategy}] 无数据字段 → null: {_nan}")
    od = Path(out_dir) if out_dir else (OUT / "adapters")
    od.mkdir(parents=True, exist_ok=True)
    p = od / f"pairs_{strategy}_advice_{date}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(advice, indent=2, ensure_ascii=False, default=str,
                              allow_nan=False))
    tmp.replace(p)
    log.info(f"pairs_adapter[{strategy}] → {p.name} "
             f"({len(positions)} pos, {len(candidates)} cand)")
    return advice
