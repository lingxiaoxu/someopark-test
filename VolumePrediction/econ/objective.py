"""
econ/objective — 策略效用剖面注册表(Plan §5.7 / §7.12-1)
=========================================================
论文的 μ=γσ²/A 只直接适配"向目标权重再平衡";四策略的交易问题不同,
以 Profile 抽象六种经济语义,API 侧统一 resolve:

  resolve(strategy='mrpt', trade_type='entry')  → pairs_entry
  resolve(objective='aiss_emergency')           → 显式指定

μ 解析(resolve_mu):
  - mu_source='calibrated' → 读 outputs/registry/mu_calibration.json 的 mu_key;
    缺失 → 冷启动 paper_prior(mu_from_aum 或网格中值),calibration_source 标注
  - mu_source='inf'        → float('inf')(紧急/止损退化,policy 走 solve_urgent)
  - mu_source='paper_grid' → 论文网格(G 复刻场景用,不与生产 profile 混)
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from VolumePrediction.common import load_config, OUT
from VolumePrediction.econ.policy import mu_from_aum

_PAPER_PRIOR_MU = 1.0e-6      # 网格中值;冷启动默认(显式标注,绝不静默)


@dataclass
class Profile:
    """§7.12-1 schema 全字段。"""
    name: str
    mode: str                              # tracking | event | urgent
    mu_source: str                         # calibrated | inf | paper_grid
    mu_key: Optional[str] = None
    lambda_form: str = "0.2/V"
    constraints: dict = field(default_factory=dict)   # adv_cap/dtl_max/leg_joint/basket
    degrade: str = "fallback_trailing"

    @property
    def adv_cap(self) -> float:
        return float(self.constraints.get("adv_cap", 0.20))

    @property
    def is_urgent(self) -> bool:
        return self.mode == "urgent" or self.mu_source == "inf"


_STRATEGY_MAP = {
    ("mrpt", "entry"): "pairs_entry",
    ("mrpt", "exit"): "pairs_exit",
    ("mrpt", "stop"): "pairs_stop",
    ("mtfs", "entry"): "pairs_entry",
    ("mtfs", "exit"): "pairs_exit",
    ("mtfs", "stop"): "pairs_stop",
    ("aiss", "rebalance"): "aiss_rebalance",
    ("aiss", "emergency"): "aiss_emergency",
    ("aiss", "stop"): "aiss_emergency",
    ("ssrs", "rebalance"): "ssrs_rebalance",
    ("ssrs", "emergency"): "pairs_stop",   # SSRS 无专属紧急剖面 → 通用 urgent
}


def registry(cfg: Optional[dict] = None) -> dict[str, Profile]:
    cfg = cfg or load_config()
    out: dict[str, Profile] = {}
    for name, spec in (cfg.get("strategy_profiles") or {}).items():
        lam = spec.get("lambda_form", cfg.get("econ", {}).get("lambda_form", "0.2/V"))
        out[name] = Profile(
            name=name,
            mode=spec.get("mode", "tracking"),
            mu_source=spec.get("mu_source", "calibrated"),
            mu_key=spec.get("mu_key"),
            lambda_form=lam,
            constraints=dict(spec.get("constraints") or {}),
            degrade=spec.get("degrade", "fallback_trailing"),
        )
    return out


def resolve(strategy: Optional[str] = None,
            trade_type: Optional[str] = None,
            objective: Optional[str] = None,
            cfg: Optional[dict] = None) -> Profile:
    reg = registry(cfg)
    if objective:
        if objective not in reg:
            raise KeyError(f"unknown objective profile: {objective!r}; "
                           f"available: {sorted(reg)}")
        return reg[objective]
    if strategy:
        key = (strategy.lower(), (trade_type or "rebalance").lower())
        name = _STRATEGY_MAP.get(key)
        if name and name in reg:
            return reg[name]
        raise KeyError(f"no profile mapping for {key}; available: {sorted(reg)}")
    raise ValueError("must pass objective= or strategy=(+trade_type)")


def resolve_mu(profile: Profile,
               aum: Optional[float] = None,
               artifacts_dir: Optional[Path] = None,
               cfg: Optional[dict] = None) -> tuple[float, str]:
    """→ (mu, calibration_source)。缺工件绝不抛异常——冷启动降级并标注。"""
    cfg = cfg or load_config()
    if profile.mu_source == "inf":
        return float("inf"), "inf"
    if profile.mu_source == "paper_grid":
        grid = cfg["econ"]["mu_grid"]
        return float(grid[len(grid) // 2]), "paper_grid"
    # calibrated
    art = Path(artifacts_dir) if artifacts_dir else OUT
    f = art / "registry" / "mu_calibration.json"
    if f.exists() and profile.mu_key:
        try:
            data = json.loads(f.read_text())
            entry = data.get(profile.mu_key)
            if entry and entry.get("mu") is not None and math.isfinite(float(entry["mu"])):
                return float(entry["mu"]), entry.get("calibration_source", "calibrated")
        except Exception:  # noqa: BLE001 — 破损工件按缺失处理(降级路径)
            pass
    if aum is not None:
        econ = cfg["econ"]
        return (mu_from_aum(aum, econ["aum_scenarios"], econ["mu_grid"]),
                "paper_prior_aum_map")
    return _PAPER_PRIOR_MU, "paper_prior"
