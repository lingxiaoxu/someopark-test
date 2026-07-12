"""Plan 06 §2 risk metrics, tiers 0–5 — PURE functions over pandas/np inputs.

Computational patterns ported from /RiskManager.py (read-only template; copy-
first principle — same formulas, crypto inputs):
  * parametric VaR (z·σ), historical VaR + CVaR tail-mean  ← RiskManager.var()
  * Cornish-Fisher/Zangari VaR incl. the reliability-domain floor
                                                    ← RiskManager.diag_distribution()
  * CDaR + time-under-water                          ← RiskManager.diag_cdar()
  * Euler risk contribution (mctr/trc/prc)           ← RiskManager.diag_risk_contribution()
  * PSR / min track record                           ← RiskManager.diag_psr()
  * Almgren-Chriss time-to-flatten (participation)   ← RiskManager.liquidity()
  * stress-table shape (scenario, kind, shock)       ← RiskManager.stress()

Crypto adaptations: 365-day annualization (backtest.metrics.TRADING_DAYS);
net-BTC-delta replaces SPY-beta as the tier-0 factor; funding/basis exposure
(tier 4); crypto-sized stress scenarios (plan §2 tier 2). No I/O here.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.backtest.metrics import TRADING_DAYS

Z95 = 1.645
Z99 = 2.326
ADV_PARTICIPATION = 0.20      # Almgren-Chriss participation cap (template value)
RISK_FREE_ANNUAL = 0.05


# ── tier 0: exposure / delta / leverage / liquidation ──────────────────────

def asset_of(ticker: str) -> str:
    """KXBTCPERP → BTC (asset roll-up key)."""
    t = ticker.upper()
    if t.startswith("KX"):
        t = t[2:]
    return t.removesuffix("PERP")


def exposures(positions: dict[str, float], marks: dict[str, float]) -> dict:
    """Signed per-ticker notionals + gross/net (dollars)."""
    per = {t: c * float(marks.get(t, 0.0)) for t, c in positions.items()}
    gross = sum(abs(v) for v in per.values())
    net = sum(per.values())
    return {"per_ticker": per, "gross": gross, "net": net}


def per_asset_delta(positions: dict[str, float], marks: dict[str, float]) -> dict[str, float]:
    out: dict[str, float] = {}
    for t, c in positions.items():
        out[asset_of(t)] = out.get(asset_of(t), 0.0) + c * float(marks.get(t, 0.0))
    return out


def net_btc_delta(asset_deltas: dict[str, float],
                  beta_map: dict[str, float] | None = None) -> float:
    """Dollar BTC-equivalent delta: Σ delta_asset × beta_asset (default β=1)."""
    beta_map = beta_map or {}
    return sum(d * float(beta_map.get(a, 1.0)) for a, d in asset_deltas.items())


def leverage(gross: float, net: float, equity: float) -> dict:
    if equity <= 0:
        return {"gross_leverage": None, "net_leverage": None}
    return {"gross_leverage": gross / equity, "net_leverage": net / equity}


def liquidation_distance_pct(mark: float, liq_price: float, contracts: float) -> float | None:
    """% adverse move from mark to liquidation (long: down; short: up)."""
    if mark <= 0 or liq_price is None or contracts == 0:
        return None
    return (mark - liq_price) / mark if contracts > 0 else (liq_price - mark) / mark


# ── tier 2: VaR / CVaR / Cornish-Fisher / stress ────────────────────────────

def var_historical(returns: pd.Series, level: float = 0.95) -> float | None:
    """Historical VaR (positive number = loss). Template: -percentile(P, 5)."""
    r = returns.dropna()
    if len(r) < 20:
        return None
    return float(-np.percentile(r, (1 - level) * 100))


def cvar_historical(returns: pd.Series, level: float = 0.95) -> float | None:
    """Rockafellar-Uryasev expected shortfall — template tail-mean pattern."""
    r = returns.dropna()
    if len(r) < 20:
        return None
    var = -np.percentile(r, (1 - level) * 100)
    tail = r[r <= -var]
    return float(-tail.mean()) if len(tail) else float(var)


def var_parametric(returns: pd.Series, level: float = 0.95) -> float | None:
    """Gaussian VaR z·σ (template uses z95=1.645/z99=2.326 — same via ppf)."""
    r = returns.dropna()
    if len(r) < 20:
        return None
    from scipy import stats as sstats
    z = float(sstats.norm.ppf(level))
    return float(z * r.std(ddof=1) - r.mean())


def var_cornish_fisher(returns: pd.Series, level: float = 0.95) -> dict | None:
    """Zangari CF VaR — ported VERBATIM from RiskManager.diag_distribution(),
    including the domain-reliability check and the Gaussian floor (a fat-tailed
    series must never report less tail risk than normal)."""
    r = returns.dropna()
    if len(r) < 20:
        return None
    from scipy import stats as sstats
    g1 = float(sstats.skew(r))
    g2_excess = float(sstats.kurtosis(r))
    mu, sd = float(r.mean()), float(r.std(ddof=1))
    z = float(sstats.norm.ppf(1 - level))          # negative tail quantile
    zcf = (z + (z ** 2 - 1) / 6 * g1 + (z ** 3 - 3 * z) / 24 * g2_excess
           - (2 * z ** 3 - 5 * z) / 36 * g1 ** 2)
    var_param = -(mu + z * sd)
    var_cf_raw = -(mu + zcf * sd)
    cf_reliable = (abs(g1) <= 2.0 and g2_excess <= 7.0 and zcf <= z)
    return {"var_cf": max(var_cf_raw, var_param), "var_cf_raw": var_cf_raw,
            "var_param": var_param, "skew": g1, "excess_kurtosis": g2_excess,
            "cf_reliable": bool(cf_reliable)}


# Crypto stress scenarios (plan §2 tier 2) — template (name, kind, shock) shape.
STRESS_SCENARIOS: list[tuple[str, str, float]] = [
    ("BTC -10%", "btc", -0.10),
    ("BTC -20%", "btc", -0.20),
    ("BTC -30%", "btc", -0.30),
    ("Funding spike +10bps/cycle", "funding", 1e-3),
    ("Liq-cascade gap -5%", "gap", -0.05),
    ("Index/oracle gap 2%", "index_gap", 0.02),
    ("Vol jump 2x", "vol", 2.0),
    ("Venue halt 24h", "halt", 24.0),
]


def stress_table(*, net_btc_delta_usd: float, gross_usd: float, net_usd: float,
                 var95_usd: float | None, equity: float) -> list[dict]:
    """Beta-implied scenario P&L — template pattern with crypto kinds.

    Approximations (report-only diagnostics, like the template's):
      btc:       pnl = net BTC delta × shock
      funding:   one cycle's extra funding on NET notional (longs pay +rate)
      gap:       forced mark gap on GROSS (both sides gap against you in a
                 cascade — conservative)
      index_gap: net exposure marked against a mispriced index
      vol:       stressed VaR = VaR95 × multiple (loss ≈ additional VaR)
      halt:      cannot exit for N hours → VaR scaled by √(N/24) extra day(s)
    """
    out = []
    for name, kind, shock in STRESS_SCENARIOS:
        if kind == "btc":
            pnl = net_btc_delta_usd * shock
        elif kind == "funding":
            pnl = -abs(net_usd) * shock
        elif kind == "gap":
            pnl = gross_usd * shock
        elif kind == "index_gap":
            pnl = -abs(net_usd) * shock
        elif kind == "vol":
            pnl = -(var95_usd or 0.0) * (shock - 1.0)
        elif kind == "halt":
            pnl = -(var95_usd or 0.0) * math.sqrt(shock / 24.0)
        else:
            pnl = 0.0
        out.append({"scenario": name, "est_pnl": pnl,
                    "est_pnl_pct_equity": (pnl / equity * 100) if equity else None})
    return out


# ── tier 3: correlation / risk contribution ────────────────────────────────

def correlation_matrix(returns_by: pd.DataFrame) -> pd.DataFrame:
    return returns_by.dropna(how="all").corr()


def risk_contribution(returns_by: pd.DataFrame,
                      weights: np.ndarray | None = None) -> list[dict]:
    """Euler decomposition — template mctr/trc/prc pattern verbatim.

    prc sums to 1 across components; component_var_95 = Z95 × trc (dollars if
    the input series are dollar P&L)."""
    df = returns_by.dropna(how="all").fillna(0.0)
    if df.shape[1] < 1 or len(df) < 10:
        return []
    cov = df.cov().values
    w = np.ones(df.shape[1]) if weights is None else np.asarray(weights, dtype=float)
    port_var = float(w @ cov @ w)
    sd = math.sqrt(port_var) if port_var > 0 else 0.0
    if sd <= 0:
        return []
    mctr = (cov @ w) / sd
    trc = w * mctr
    prc = trc / sd
    rows = [{"component": c, "risk_contribution_pct": float(prc[i] * 100),
             "component_var_95": float(Z95 * trc[i])}
            for i, c in enumerate(df.columns)]
    rows.sort(key=lambda r: -(r["risk_contribution_pct"] or 0))
    return rows


def stress_correlation_var(returns_by: pd.DataFrame, level: float = 0.95) -> float | None:
    """Khandani-Lo deleveraging stress: all pairwise correlations → 1.

    Perfectly-correlated portfolio σ = Σ|σ_i| (dollar vols add) → VaR = z·Σσ.
    Always ≥ the normal-correlation parametric VaR."""
    df = returns_by.dropna(how="all").fillna(0.0)
    if df.shape[1] < 1 or len(df) < 10:
        return None
    from scipy import stats as sstats
    z = float(sstats.norm.ppf(level))
    return float(z * sum(df[c].std(ddof=1) for c in df.columns))


# ── tier 4: funding / basis / liquidity ─────────────────────────────────────

def funding_exposure(positions: dict[str, float], marks: dict[str, float],
                     funding_est: dict[str, float]) -> dict:
    """Expected NEXT-cycle funding P&L (uses costs.funding_payment convention)."""
    from crypto_trading.crypto_common.costs import funding_payment
    per = {t: funding_payment(c, float(marks.get(t, 0.0)),
                              float(funding_est.get(t, 0.0)))
           for t, c in positions.items()}
    return {"per_ticker": per, "total_next_cycle": sum(per.values())}


def basis_exposure(positions: dict[str, float], marks: dict[str, float]) -> dict:
    """$ P&L per 1bp mark-vs-index basis move, per ticker + aggregate."""
    per = {t: c * float(marks.get(t, 0.0)) * 1e-4 for t, c in positions.items()}
    return {"per_ticker_per_bp": per, "net_per_bp": sum(per.values()),
            "gross_per_bp": sum(abs(v) for v in per.values())}


def time_to_flatten_days(contracts: float, adv_contracts: float,
                         participation: float = ADV_PARTICIPATION) -> float | None:
    """Almgren-Chriss horizon — template: |shares| / (ADV × participation)."""
    if adv_contracts is None or adv_contracts <= 0:
        return None
    return abs(contracts) / (adv_contracts * participation)


# ── tier 5: health (PSR, CDaR) ──────────────────────────────────────────────

def psr(returns: pd.Series, *, sr_benchmark: float = 0.0) -> dict | None:
    """Probabilistic Sharpe Ratio — ported from RiskManager.diag_psr()."""
    r = returns.dropna()
    n = len(r)
    if n < 20 or r.std(ddof=1) == 0:
        return None
    from scipy import stats as sstats
    sr = float(r.mean() / r.std(ddof=1))
    g1 = float(sstats.skew(r))
    g2 = float(sstats.kurtosis(r, fisher=False))
    denom = math.sqrt(max(1e-9, 1 - g1 * sr + (g2 - 1) / 4 * sr ** 2))
    psr0 = float(sstats.norm.cdf((sr - sr_benchmark) * math.sqrt(n - 1) / denom))
    mintrl = (1 + (1 - g1 * sr + (g2 - 1) / 4 * sr ** 2) * (Z95 / sr) ** 2) if sr != 0 else None
    return {"sharpe_daily": sr, "sharpe_annual": sr * math.sqrt(TRADING_DAYS),
            "psr": psr0, "psr_pass_95": bool(psr0 >= 0.95),
            "min_track_record_days": mintrl, "n_obs": n}


def cdar_block(nav: pd.Series, alpha: float = 0.95) -> dict | None:
    """CDaR + max-DD + time-under-water — ported from RiskManager.diag_cdar()."""
    nav = nav.dropna()
    if len(nav) < 20:
        return None
    peak = nav.cummax()
    dd = (peak - nav) / peak
    dar = float(np.percentile(dd, alpha * 100))
    tail = dd[dd >= dar]
    cdar = float(tail.mean()) if len(tail) else dar
    return {"max_drawdown_pct": float(dd.max() * 100),
            "dar_pct": dar * 100, "cdar_pct": cdar * 100,
            "avg_drawdown_pct": float(dd.mean() * 100),
            "time_under_water_pct": float((dd > 1e-6).mean() * 100)}
