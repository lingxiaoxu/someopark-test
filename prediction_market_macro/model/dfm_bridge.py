"""model/dfm_bridge.py — DFM joint macro scenario engine (PLAN §7 dfm_bridge, §7-bis).

Read-only import of the repo-root `dfm/` package (Chen et al. 2025 diffusion factor
model — OU forward/reverse, factor score network, DSM training, sampling). All outputs
(checkpoints, scenario caches, metrics) live INSIDE prediction_market_macro/data/.

Usage (a) of the plan: a monthly macro panel (d~13 print-relevant series, n~300 months)
is the training slice; the model generates joint scenarios that PRESERVE the
cross-series covariance (same-month CPI+PCE+claims positions are correlated exposure).

§7-bis adoption protocol, hard-coded:
  gate_check() — holdout evaluation mirroring dfm/run_real_data.run_split (PCA warm
  start, marginal recalibration): DFM covariance must beat BOTH sample and Ledoit-Wolf
  (Frobenius error vs held-out realized covariance). Result → experiments('dfm_gate').
  scenario_var_dfm() only runs when the LATEST gate row passed AND the scenario cache
  is fresh; otherwise ops/risk.py stays on the independent ±stake-sum baseline.
  Training data of the assessed forecasting models NEVER includes DFM synthetics (铁律).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DFM_DIR = _REPO_ROOT / "dfm"

SCENARIO_CACHE = Path(__file__).resolve().parents[1] / "data" / "dfm_scenarios.npz"
GATE_NAME = "dfm_gate"
CACHE_MAX_AGE_DAYS = 14

# panel columns: (name, builder kind). All monthly, latest-vintage (risk usage — the
# panel never trains a forecasting model, so vintage PIT is not label-critical here).
_PANEL_SPEC = [
    ("cpi_mom", ("pct", "CPIAUCSL")), ("core_mom", ("pct", "CPILFESL")),
    ("pce_mom", ("pct", "PCEPILFE")), ("claims_chg", ("diff_k", "ICSA")),
    ("payrolls_chg", ("diff_k", "PAYEMS")), ("u3_chg", ("diff", "UNRATE")),
    ("wti_ret", ("logret_m", "DCOILWTICO")), ("gas_chg", ("diff", "GASREGW")),
    ("dgs2_chg", ("diff_m", "DGS2")), ("dgs10_chg", ("diff_m", "DGS10")),
    ("dgs30_chg", ("diff_m", "DGS30")), ("t5yie_chg", ("diff_m", "T5YIE")),
]


def _monthly(conn, sid: str) -> pd.Series:
    rows = conn.execute(
        "SELECT event_time, value, MAX(vintage_date) FROM fred_obs WHERE sid=?"
        " GROUP BY event_time ORDER BY event_time", (sid,)).fetchall()
    s = pd.Series({pd.Timestamp(r["event_time"]): r["value"] for r in rows}, dtype=float)
    return s.resample("ME").last() if len(s) else s


def build_macro_panel(conn) -> pd.DataFrame:
    cols = {}
    for name, (kind, sid) in _PANEL_SPEC:
        s = _monthly(conn, sid)
        if s.empty:
            continue
        if kind == "pct":
            cols[name] = s.pct_change() * 100.0
        elif kind == "diff":
            cols[name] = s.diff()
        elif kind == "diff_k":
            cols[name] = s.diff() / 1000.0
        elif kind == "diff_m":
            cols[name] = s.diff()
        elif kind == "logret_m":
            cols[name] = np.log(s).diff() * 100.0
    panel = pd.DataFrame(cols).dropna(how="any")
    return panel.tail(360)


def _import_dfm():
    if str(_DFM_DIR) not in sys.path:
        sys.path.insert(0, str(_DFM_DIR))
    import torch                                             # noqa: F401
    from diffusion import DiffusionProcess                   # noqa: F401
    from sampling import DiffusionSampler                    # noqa: F401
    from score_matching import train_diffusion_model         # noqa: F401
    from score_network import create_score_network           # noqa: F401
    from preprocessing import prepare_data_loader            # noqa: F401
    from config import DIFFUSION_CONFIG, SCORE_NETWORK_CONFIG, TRAINING_CONFIG  # noqa: F401
    import diffusion, sampling, score_matching, score_network, preprocessing, config
    return {"torch": torch, "DiffusionProcess": DiffusionProcess,
            "DiffusionSampler": DiffusionSampler,
            "train_diffusion_model": train_diffusion_model,
            "create_score_network": create_score_network,
            "prepare_data_loader": prepare_data_loader,
            "DIFFUSION_CONFIG": DIFFUSION_CONFIG,
            "SCORE_NETWORK_CONFIG": SCORE_NETWORK_CONFIG,
            "TRAINING_CONFIG": TRAINING_CONFIG}


def _cov_error(est: np.ndarray, real: np.ndarray) -> float:
    return float(np.linalg.norm(est - real, "fro") / np.linalg.norm(real, "fro"))


def gate_check(conn, settings, factors: int = 3, epochs: int = 600,
               gen_samples: int = 5000, seed: int = 0) -> dict:
    """§7-bis gate: train on the first 80% months, score DFM vs sample vs Ledoit-Wolf
    against the held-out 20% realized covariance (mirrors dfm/run_real_data.run_split:
    PCA warm start + marginal recalibration). Writes experiments('dfm_gate') and, on a
    PASS, refreshes the joint-scenario cache."""
    m = _import_dfm()
    torch = m["torch"]
    panel = build_macro_panel(conn)
    if len(panel) < 120:
        raise RuntimeError(f"macro panel too short (n={len(panel)})")
    n_hold = max(24, len(panel) // 5)
    train, test = panel.values[:-n_hold], panel.values[-n_hold:]
    n_train, d = train.shape
    mu, sd = train.mean(0), train.std(0) + 1e-12
    train_z = (train - mu) / sd

    corr_train = np.cov(train_z, rowvar=False)
    evals, evecs = np.linalg.eigh(corr_train)
    evals, evecs = evals[::-1], evecs[:, ::-1]
    beta_hat = evecs[:, :factors]
    lam_hat = evals[:factors]
    resid = train_z - (train_z @ beta_hat) @ beta_hat.T
    sigma2_hat = resid.var(0) + 1e-6

    torch.manual_seed(seed)
    device = torch.device("cpu")
    diffusion = m["DiffusionProcess"](
        data_dim=d, factor_dim=factors, T=m["DIFFUSION_CONFIG"]["T"],
        t0=m["DIFFUSION_CONFIG"]["t0"], eta=m["DIFFUSION_CONFIG"]["eta"],
        num_steps=m["DIFFUSION_CONFIG"]["num_steps"], device=device)
    net_cfg = m["SCORE_NETWORK_CONFIG"].copy()
    net_cfg["use_2d_unet"] = False
    net_cfg["init_beta"] = torch.tensor(beta_hat.copy())
    net_cfg["init_sigma_diag"] = torch.tensor(sigma2_hat.copy())
    model = m["create_score_network"](net_cfg, asset_dim=d, factor_dim=factors,
                                      device=device)
    tr_cfg = m["TRAINING_CONFIG"].copy()
    tr_cfg["num_epochs"] = epochs
    tr_cfg["batch_size"] = min(64, n_train)
    tr_cfg["early_stopping_patience"] = max(200, epochs // 5)
    tr_cfg["lr_scheduler_step"] = max(100, epochs // 4)
    tr_cfg["checkpoint_dir"] = str(SCENARIO_CACHE.parent / "dfm_ckpt")
    X = torch.tensor(train_z, dtype=torch.float32)
    loader = m["prepare_data_loader"](X, batch_size=tr_cfg["batch_size"], shuffle=True)
    vloader = m["prepare_data_loader"](X[: max(8, n_train // 5)],
                                       batch_size=tr_cfg["batch_size"], shuffle=False)
    model, _hist = m["train_diffusion_model"](model=model, diffusion_process=diffusion,
                                              train_loader=loader, val_loader=vloader,
                                              config=tr_cfg, device=device)

    sampler = m["DiffusionSampler"](model=model, diffusion_process=diffusion,
                                    device=device)
    gen_z = sampler.sample(num_samples=gen_samples, data_shape=(d,), noise_steps=180,
                           batch_size=256, final_denoise=True).cpu().numpy()
    gen = gen_z / (gen_z.std(0) + 1e-12) * sd + mu          # marginal recalibration

    real_cov = np.cov(test, rowvar=False)
    from sklearn.covariance import LedoitWolf
    metrics = {
        "n_train": n_train, "n_hold": int(n_hold), "d": d, "factors": factors,
        "epochs": epochs,
        "cov_error_sample": _cov_error(np.cov(train, rowvar=False), real_cov),
        "cov_error_ledoit_wolf": _cov_error(LedoitWolf().fit(train).covariance_, real_cov),
        "cov_error_diffusion": _cov_error(np.cov(gen, rowvar=False), real_cov),
    }
    metrics["pass"] = bool(
        metrics["cov_error_diffusion"] < metrics["cov_error_sample"]
        and metrics["cov_error_diffusion"] < metrics["cov_error_ledoit_wolf"])
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO experiments(name, config_hash, series, window,"
        " metrics_json, created_ts) VALUES(?,?,?,?,?,?)",
        (GATE_NAME, f"f{factors}e{epochs}s{seed}", "*",
         f"{panel.index[0].date()}..{panel.index[-1].date()}",
         json.dumps(metrics), now))
    conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                 (now, "info", "dfm_gate",
                  f"DFM gate {'PASS' if metrics['pass'] else 'FAIL'}: diffusion"
                  f" {metrics['cov_error_diffusion']:.3f} vs sample"
                  f" {metrics['cov_error_sample']:.3f} / LW"
                  f" {metrics['cov_error_ledoit_wolf']:.3f}"))
    conn.commit()
    if metrics["pass"]:
        SCENARIO_CACHE.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(SCENARIO_CACHE, scenarios=gen.astype(np.float32),
                            columns=np.array([c for c, _ in _PANEL_SPEC if c in panel.columns]),
                            created=np.array([now]))
    return metrics


def latest_gate(conn) -> dict | None:
    r = conn.execute(
        "SELECT metrics_json, created_ts FROM experiments WHERE name=?"
        " ORDER BY created_ts DESC LIMIT 1", (GATE_NAME,)).fetchone()
    return {**json.loads(r["metrics_json"]), "created_ts": r["created_ts"]} if r else None


def load_scenarios() -> tuple[np.ndarray, list[str]] | None:
    if not SCENARIO_CACHE.exists():
        return None
    z = np.load(SCENARIO_CACHE, allow_pickle=False)
    created = datetime.fromisoformat(str(z["created"][0]))
    if datetime.now(timezone.utc) - created > timedelta(days=CACHE_MAX_AGE_DAYS):
        return None
    return z["scenarios"], [str(c) for c in z["columns"]]


# series → panel column driving its print in a joint scenario
SERIES_COLUMN = {
    "KXCPI": "cpi_mom", "KXCPICORE": "core_mom", "KXCPIYOY": "cpi_mom",
    "KXCPICOREYOY": "core_mom", "KXPCECORE": "pce_mom",
    "KXJOBLESSCLAIMS": "claims_chg", "KXPAYROLLS": "payrolls_chg", "KXU3": "u3_chg",
    "KXWTIW": "wti_ret", "KXNATGASW": "wti_ret",       # NG proxied by the oil factor
    "KXAAAGASW": "gas_chg",
}


def scenario_var_dfm(conn) -> dict | None:
    """Joint scenario VaR over open paper positions: each scenario is one draw of the
    full macro month; every open position's series maps to its panel column and the
    position P&L is approximated as stake-linear in the shocked print's z-move against
    the position (sign from the held side). Fed/categorical positions get worst-case
    stake loss (event risk not in the panel). None → caller stays on the baseline."""
    gate = latest_gate(conn)
    if not gate or not gate.get("pass"):
        return None
    loaded = load_scenarios()
    if loaded is None:
        return None
    scen, cols = loaded
    col_ix = {c: i for i, c in enumerate(cols)}
    scen_z = (scen - scen.mean(0)) / (scen.std(0) + 1e-12)
    from prediction_market_macro.ops.risk import _open_exposure
    pos = _open_exposure(conn)
    if not pos:
        return {"mode": "dfm_joint", "n_scenarios": int(len(scen)), "var95_usd": 0.0,
                "var99_usd": 0.0, "worst_usd": 0.0}
    pnl = np.zeros(len(scen))
    for p in pos:
        stake = float(p["size_usd"] or 0.0)
        col = SERIES_COLUMN.get(p["series"])
        if col is None or col not in col_ix:
            pnl -= stake                                   # categorical: worst case
            continue
        z = scen_z[:, col_ix[col]]
        # stake-linear approximation, clipped at total loss / total win of the stake:
        # a +2z adverse print loses the full stake, favorable saves it
        pnl += np.clip(-z / 2.0, -1.0, 1.0) * stake
    losses = -pnl
    return {"mode": "dfm_joint", "n_scenarios": int(len(scen)),
            "var95_usd": round(float(np.quantile(losses, 0.95)), 2),
            "var99_usd": round(float(np.quantile(losses, 0.99)), 2),
            "worst_usd": round(float(losses.max()), 2),
            "gate_created": gate["created_ts"]}
