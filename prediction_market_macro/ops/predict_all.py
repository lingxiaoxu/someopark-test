"""ops/predict_all.py — §8.0 step 2: re-predict EVERY open (series, period), daily.

Model dispatch grows as models land (M1: claims; M2+: cpi/pce/payrolls/u3/fed).
Each Pred is written through pred_to_row (online PIT assertion) and coverage advances.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from prediction_market_macro.config.registry import REGISTRY
from prediction_market_macro.model.common import grid_pmf, pred_to_row
from prediction_market_macro.util.periods import kalshi_period_to_key

# series → (module, function). Grows as models land.
SERIES_DISPATCH: dict[str, tuple[str, str]] = {
    "KXJOBLESSCLAIMS": ("prediction_market_macro.model.claims", "predict"),
    "KXCPI": ("prediction_market_macro.model.cpi", "predict"),
    "KXCPICORE": ("prediction_market_macro.model.cpi", "predict"),
    "KXCPIYOY": ("prediction_market_macro.model.cpi", "predict"),
    "KXCPICOREYOY": ("prediction_market_macro.model.cpi", "predict"),
    "KXPCECORE": ("prediction_market_macro.model.pce", "predict"),
    "KXPAYROLLS": ("prediction_market_macro.model.payrolls", "predict"),
    "KXU3": ("prediction_market_macro.model.u3", "predict"),
    "KXFEDDECISION": ("prediction_market_macro.model.fed", "predict"),
    "KXFED": ("prediction_market_macro.model.fed", "predict_kxfed"),
    "KXWTIW": ("prediction_market_macro.model.energy", "predict"),
    "KXNATGASW": ("prediction_market_macro.model.energy", "predict"),
    "KXAAAGASW": ("prediction_market_macro.model.energy", "predict"),
    "KXGDP": ("prediction_market_macro.model.gdp", "predict"),
}


def _open_periods(conn, series: str) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT DISTINCT period FROM contracts WHERE series=? AND status='active'",
        (series,)).fetchall()
    out = []
    for r in rows:
        key = kalshi_period_to_key(r["period"])
        if key:
            out.append((r["period"], key))
    return out


def _predicted_coverage(conn, series: str, period: str) -> None:
    # Freeze runs are one-shot tasks. Once marked done, a later prediction cannot
    # rely on another executor pass to restore their state (deciding can fail or
    # the book can close meanwhile). Keep the condition in the write itself so a
    # concurrent freeze cannot be lost between a read and an unconditional update.
    conn.execute(
        "INSERT INTO coverage(series,period,state,updated_ts) VALUES(?,?,'predicted',?)"
        " ON CONFLICT(series,period) DO UPDATE SET state=excluded.state,"
        " updated_ts=excluded.updated_ts WHERE coverage.state!='frozen'",
        (series, period, datetime.now(timezone.utc).isoformat()))
    conn.commit()


class PredictionError(RuntimeError):
    """One or more requested predictions failed; successes and alerts are committed."""

    def __init__(self, failures, succeeded):
        self.failures = tuple(failures)
        self.succeeded = succeeded
        super().__init__(f"prediction failed ({len(self.failures)} errors, {succeeded} succeeded): "
                         + "; ".join(self.failures))


def run(conn, settings, *, only_series: set[str] | None = None,
        update_coverage: bool = True, fail_on_error: bool = False) -> int:
    import importlib
    from prediction_market_macro.model.common import Categorical
    from prediction_market_macro.research import param_select
    now = datetime.now(timezone.utc)
    # cpi/0.3.0 anchors YoY on the Cleveland nowcast; this is the single door every
    # live prediction passes through, so the PIT tail-guard lives here — a tick that
    # predicts at 19:00 UTC must be able to see the nowcast published that morning,
    # not yesterday's. Best-effort: the guard's own throttle bounds it to ~one fetch
    # attempt per hour, and a dead feed degrades the model to its internal chain.
    try:
        from prediction_market_macro.ingest import cleveland_nowcast
        cleveland_nowcast.refresh_if_stale(conn, now)
    except Exception as e:                                       # noqa: BLE001
        conn.execute(
            "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
            (now.isoformat(), "warn", "predict_all",
             f"cleveland_nowcast.refresh_if_stale: {e}"))
    n = 0
    failures = []

    def failed(label, error):
        message = f"{label}: {error}"
        failures.append(message)
        conn.execute("INSERT INTO alerts(ts,level,source,message) VALUES(?,?,?,?)",
                     (now.isoformat(), "error", "predict_all", message))

    for spec in REGISTRY.values():
        if only_series is not None and spec.ticker not in only_series:
            continue
        disp = SERIES_DISPATCH.get(spec.ticker)
        if disp is None:
            continue
        # #119: today's DSR-gated parameter choice. A single SELECT — the scoring runs in
        # `param_select.refresh` earlier in the pipeline. `{}` means the gate held and the
        # registered defaults are used, which is the common case; `params=None` is passed
        # in that case so the model takes exactly the path it took before this landed.
        try:
            mod = importlib.import_module(disp[0])
            fn = getattr(mod, disp[1])
            params = param_select.current(conn, spec.ticker) or None
        except Exception as exc:
            if not fail_on_error:
                raise
            failed(spec.ticker, exc)
            continue
        for kalshi_tok, key in _open_periods(conn, spec.ticker):
            try:
                pred = fn(conn, now, key, series=spec.ticker, params=params)
                if isinstance(pred.dist, Categorical):
                    ladder = None
                else:
                    pmf = grid_pmf(pred.dist, spec.round_rule)
                    ladder = {str(k): round(v, 6) for k, v in pmf.items()}
                conn.execute(
                    "INSERT OR REPLACE INTO preds(series, period, asof, model_version,"
                    " dist_json, ladder_json, inputs_json, data_horizon, created_ts)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    pred_to_row(pred, ladder))
                if update_coverage:
                    _predicted_coverage(conn, spec.ticker, key)
                n += 1
            except Exception as e:                               # noqa: BLE001
                failed(f"{spec.ticker}/{key}", e)
    conn.commit()
    if fail_on_error and failures:
        raise PredictionError(failures, n)
    return n
