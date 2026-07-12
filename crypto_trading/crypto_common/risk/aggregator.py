"""Layer-2 portfolio risk aggregator (Plan 06 §1/§3/§5).

Nets EVERY strategy's live exposure into one book and governs the portfolio:
aggregate deltas/leverage, VaR/CVaR (historical + parametric + Cornish-Fisher),
cross-strategy correlation + Khandani-Lo stress-correlation VaR, Litterman risk
contribution, worst liquidation distance, funding/basis exposure, crypto stress
table, and the plan §3 amber/red limit sheet.

Governance only: read-only w.r.t. strategy state; on RED it trips the shared
Layer-1 kill files (RiskKill) + one portfolio halt file. It NEVER sends orders
(execution layer owns order flow, demo-gated).

Limit-evaluation pattern (value/amber/red/status add() rows) ported from
RiskManager.limits() / LIMITS_SPEC — thresholds re-based to the crypto table
in Plan 06 §3.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

import pandas as pd

from crypto_trading.crypto_common import config as _config
from crypto_trading.crypto_common import risk_kill as _rk
from crypto_trading.crypto_common.risk import metrics as m
from crypto_trading.crypto_common.risk_kill import Action, Breach, RiskKill
from crypto_trading.crypto_common.timeutils import utcnow

logger = logging.getLogger(__name__)

# Plan 06 §3 limit table (starting points — calibrate on demo/proxy data).
# direction: 'above' breaches when value >= threshold; 'below' when <=.
CRYPTO_LIMITS_SPEC: dict[str, dict] = {
    "gross_leverage":       {"amber": 1.5,  "red": 2.0,  "fmt": "x", "direction": "above"},
    "net_btc_delta_pct":    {"amber": 50.0, "red": 100.0, "fmt": "%", "direction": "above"},
    "var_95_1d_pct":        {"amber": 8.0,  "red": 12.0, "fmt": "%", "direction": "above"},
    "cvar_975_1d_pct":      {"amber": 12.0, "red": 18.0, "fmt": "%", "direction": "above"},
    "min_liq_distance_pct": {"amber": 25.0, "red": 15.0, "fmt": "%", "direction": "below"},
    "single_asset_conc_pct": {"amber": 50.0, "red": 70.0, "fmt": "%", "direction": "above"},
    "daily_loss_pct":       {"amber": 6.0,  "red": 10.0, "fmt": "%", "direction": "above"},
    "drawdown_pct":         {"amber": 10.0, "red": 20.0, "fmt": "%", "direction": "above"},
}
RED_ACTIONS = {           # plan §3 action column
    "gross_leverage": "block new / de-lever",
    "net_btc_delta_pct": "reduce",
    "var_95_1d_pct": "de-risk",
    "cvar_975_1d_pct": "de-risk",
    "min_liq_distance_pct": "reduce/flatten",
    "daily_loss_pct": "flatten-all + halt",
    "drawdown_pct": "halt",
}


@dataclass
class StrategyState:
    """Aggregator input contract — one per live strategy."""
    name: str
    equity: float
    positions: dict[str, float] = field(default_factory=dict)   # ticker -> signed contracts
    marks: dict[str, float] = field(default_factory=dict)       # ticker -> $/contract
    returns: pd.Series | None = None            # daily $ P&L (or fractional; consistent!)
    liq_prices: dict[str, float] = field(default_factory=dict)  # ticker -> liq mark
    funding_est: dict[str, float] = field(default_factory=dict) # ticker -> next rate
    equity_sod: float | None = None
    equity_peak: float | None = None


class PortfolioAggregator:
    def __init__(self, states: list[StrategyState],
                 beta_map: dict[str, float] | None = None):
        self.states = list(states)
        self.beta_map = beta_map or {}

    # ── netting (plan §5) ──────────────────────────────────────────────────
    def net_book(self) -> tuple[dict[str, float], dict[str, float]]:
        positions: dict[str, float] = {}
        marks: dict[str, float] = {}
        for s in self.states:
            for t, c in s.positions.items():
                positions[t] = positions.get(t, 0.0) + c
                if t in s.marks:
                    marks[t] = float(s.marks[t])
        return positions, marks

    def _returns_frame(self) -> pd.DataFrame:
        cols = {s.name: s.returns for s in self.states
                if s.returns is not None and len(s.returns.dropna()) >= 10}
        return pd.DataFrame(cols) if cols else pd.DataFrame()

    # ── main computation ───────────────────────────────────────────────────
    def compute(self) -> dict:
        equity = sum(s.equity for s in self.states)
        positions, marks = self.net_book()
        expo = m.exposures(positions, marks)
        # Gross counts STRATEGY legs (Σ per-strategy gross): two strategies
        # holding +100/−100 of one ticker net to zero delta at the venue but
        # still represent 2× the intent/wash-risk — the conservative gross the
        # leverage limit should see. The netted (physical) gross is kept too.
        expo["gross_netted"] = expo["gross"]
        expo["gross"] = sum(
            m.exposures(s.positions, {**marks, **s.marks})["gross"]
            for s in self.states)
        lev = m.leverage(expo["gross"], expo["net"], equity)
        deltas = m.per_asset_delta(positions, marks)
        btc_delta = m.net_btc_delta(deltas, self.beta_map)

        rf = self._returns_frame()
        port_ret = rf.sum(axis=1) if not rf.empty else pd.Series(dtype=float)
        var95_h = m.var_historical(port_ret)
        var95_p = m.var_parametric(port_ret)
        cvar975 = m.cvar_historical(port_ret, level=0.975)
        cf = m.var_cornish_fisher(port_ret)
        stress_corr = m.stress_correlation_var(rf) if not rf.empty else None

        liq_ds = [d for s in self.states for d in (
            m.liquidation_distance_pct(s.marks.get(t, 0.0), s.liq_prices.get(t),
                                       s.positions.get(t, 0.0))
            for t in s.positions) if d is not None]
        worst_liq = min(liq_ds) if liq_ds else None

        funding = m.funding_exposure(
            positions, marks,
            {t: r for s in self.states for t, r in s.funding_est.items()})
        basis = m.basis_exposure(positions, marks)

        conc = None
        if expo["gross"] > 0:
            by_asset = {a: abs(d) for a, d in deltas.items()}
            conc = max(by_asset.values()) / expo["gross"] * 100

        eq_sod = sum(s.equity_sod for s in self.states if s.equity_sod) or None
        daily_loss_pct = (max(0.0, (eq_sod - equity) / eq_sod * 100)
                          if eq_sod else None)
        eq_peak = sum(s.equity_peak for s in self.states if s.equity_peak) or None
        drawdown_pct = (max(0.0, (eq_peak - equity) / eq_peak * 100)
                        if eq_peak else None)

        var95_usd = var95_h if var95_h is not None else var95_p
        report = {
            "ts": time.time(),
            "equity": equity,
            "exposure": {**expo, **lev},
            "per_asset_delta": deltas,
            "net_btc_delta": btc_delta,
            "var": {"var_95_hist": var95_h, "var_95_param": var95_p,
                    "cvar_975_hist": cvar975, "cornish_fisher": cf,
                    "stress_correlation_var_95": stress_corr},
            "correlation": (m.correlation_matrix(rf).to_dict()
                            if rf.shape[1] >= 2 else None),
            "risk_contribution": m.risk_contribution(rf) if not rf.empty else [],
            "worst_liq_distance_pct": worst_liq,
            "funding_exposure": funding,
            "basis_exposure": basis,
            "stress": m.stress_table(net_btc_delta_usd=btc_delta,
                                     gross_usd=expo["gross"], net_usd=expo["net"],
                                     var95_usd=var95_usd, equity=equity),
        }
        report["limits"] = self.evaluate_limits(
            equity=equity, gross_leverage=lev["gross_leverage"],
            net_btc_delta=btc_delta, var95=var95_usd, cvar975=cvar975,
            worst_liq_pct=worst_liq, single_asset_conc_pct=conc,
            daily_loss_pct=daily_loss_pct, drawdown_pct=drawdown_pct)
        return report

    # ── limits (template add() pattern, crypto table) ──────────────────────
    def evaluate_limits(self, *, equity: float, gross_leverage: float | None,
                        net_btc_delta: float, var95: float | None,
                        cvar975: float | None, worst_liq_pct: float | None,
                        single_asset_conc_pct: float | None,
                        daily_loss_pct: float | None,
                        drawdown_pct: float | None) -> list[dict]:
        checks: list[dict] = []

        def add(name: str, value: float | None):
            if value is None:
                return
            spec = CRYPTO_LIMITS_SPEC[name]
            status = "green"
            if spec["direction"] == "above":
                if value >= spec["red"]:
                    status = "red"
                elif value >= spec["amber"]:
                    status = "amber"
            else:  # 'below' — e.g. liquidation distance shrinking
                if value <= spec["red"]:
                    status = "red"
                elif value <= spec["amber"]:
                    status = "amber"
            checks.append({"name": name, "value": value, "amber": spec["amber"],
                           "red": spec["red"], "fmt": spec["fmt"], "status": status,
                           "red_action": RED_ACTIONS.get(name)})

        add("gross_leverage", gross_leverage)
        if equity > 0:
            add("net_btc_delta_pct", abs(net_btc_delta) / equity * 100)
            if var95 is not None:
                add("var_95_1d_pct", var95 / equity * 100)
            if cvar975 is not None:
                add("cvar_975_1d_pct", cvar975 / equity * 100)
        if worst_liq_pct is not None:
            add("min_liq_distance_pct", worst_liq_pct * 100)
        add("single_asset_conc_pct", single_asset_conc_pct)
        add("daily_loss_pct", daily_loss_pct)
        add("drawdown_pct", drawdown_pct)
        return checks

    # ── kill-switch (plan §5: ONE switch, flatten any/all) ────────────────
    def kill_switch(self, limits: list[dict]) -> list[str]:
        """Trip Layer-1 halts for every strategy on portfolio-red breaches that
        demand halt/flatten. Returns tripped strategy names. Order flow stays
        with the execution layer (demo-gated) — this only sets halt state."""
        red = [c for c in limits if c["status"] == "red"
               and ("halt" in (c.get("red_action") or "")
                    or "flatten" in (c.get("red_action") or ""))]
        if not red:
            return []
        tripped = []
        detail = "; ".join(f"{c['name']}={c['value']:.3g} (red≥{c['red']})"
                           if c["fmt"] != "%" or c["value"] is None else
                           f"{c['name']}={c['value']:.2f}% (red {c['red']}%)"
                           for c in red)
        for s in self.states:
            RiskKill(s.name).trip(Breach("portfolio-aggregator",
                                         Action.FLATTEN_HALT, detail))
            tripped.append(s.name)
        _rk.STATE_DIR.mkdir(parents=True, exist_ok=True)
        (_rk.STATE_DIR / "halt_portfolio.json").write_text(json.dumps(
            {"ts": time.time(), "breaches": red, "tripped": tripped}, indent=1))
        logger.error("PORTFOLIO KILL: %s → tripped %s", detail, tripped)
        return tripped

    # ── persistence ────────────────────────────────────────────────────────
    def snapshot(self, report: dict | None = None) -> str:
        """Write the JSON snapshot; returns the path. Runs compute() if needed."""
        report = report or self.compute()
        out_dir = _config.SIGNALS_DIR / "risk"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"portfolio_risk_{utcnow().strftime('%Y%m%d_%H%M%S')}.json"
        path.write_text(json.dumps(report, indent=1, default=str))
        return str(path)


def run(states: list[StrategyState], *, beta_map: dict[str, float] | None = None,
        trip_on_red: bool = True) -> dict:
    """One aggregator pass: compute → snapshot → (optionally) kill-switch."""
    agg = PortfolioAggregator(states, beta_map=beta_map)
    report = agg.compute()
    agg.snapshot(report)
    if trip_on_red:
        report["tripped"] = agg.kill_switch(report["limits"])
    return report
