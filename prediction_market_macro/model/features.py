"""model/features.py — the ONLY data door for models (PLAN §9.1, §5-bis.4-1).

`feature_frame(series, asof)` returns {name: value} computed strictly from rows with
knowledge_time <= asof, plus the max knowledge_time actually used (data_horizon) so the
caller can stamp the Pred. Models never SELECT raw tables (code-review reject).
Every frame is persisted to the features table (evidence chain).
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd


class FeatureStore:
    def __init__(self, conn):
        self._conn = conn

    # ── low-level PIT reads ───────────────────────────────────────────────
    def fred_series(self, sid: str, asof: datetime) -> tuple[pd.Series, str | None]:
        rows = self._conn.execute(
            "SELECT event_time, value, MAX(vintage_date), knowledge_time FROM fred_obs "
            "WHERE sid=? AND knowledge_time<=? GROUP BY event_time ORDER BY event_time",
            (sid, asof.isoformat())).fetchall()
        s = pd.Series({pd.Timestamp(r["event_time"]): r["value"] for r in rows}, dtype=float)
        s.name = sid
        horizon = max((r["knowledge_time"] for r in rows), default=None)
        return s, horizon

    def fred_first_prints(self, sid: str, asof: datetime) -> tuple[pd.Series, str | None]:
        """FIRST-vintage value per event_time (PIT ≤ asof) — the label-side read used
        by print-anchored models (claims/u3/payrolls). Single-door twin of
        fred_series (which returns the LATEST visible vintage)."""
        # exactly ONE min/max aggregate — SQLite's bare-column-from-that-row rule
        # only holds then; a second aggregate silently breaks first-print selection
        rows = self._conn.execute(
            "SELECT event_time, value, MIN(vintage_date) FROM fred_obs"
            " WHERE sid=? AND knowledge_time<=?"
            " GROUP BY event_time ORDER BY event_time",
            (sid, asof.isoformat())).fetchall()
        s = pd.Series({pd.Timestamp(r["event_time"]): r["value"] for r in rows},
                      dtype=float)
        s.name = sid
        h = self._conn.execute(
            "SELECT MAX(knowledge_time) m FROM fred_obs WHERE sid=? AND"
            " knowledge_time<=?", (sid, asof.isoformat())).fetchone()
        return s, (h["m"] if h else None)

    def fred_scalar_latest(self, sid: str, asof: datetime) -> float | None:
        """Latest visible value of a scalar series (e.g. DFEDTARU target bound).
        Query text mirrors the historical in-model read exactly — replay canaries
        (health §9.6-3) demand byte-identical recomputation of stored preds."""
        r = self._conn.execute(
            "SELECT value FROM fred_obs WHERE sid=? AND knowledge_time<=?"
            " ORDER BY event_time DESC LIMIT 1", (sid, asof.isoformat())).fetchone()
        if r is None or r["value"] is None:
            return None
        return float(r["value"])

    def fred_vintages(self, sid: str, asof: datetime) -> list[dict]:
        """All PIT-visible vintage rows ordered (event_time, vintage_date) — for
        models that need per-vintage reconstruction (payrolls printed_changes)."""
        rows = self._conn.execute(
            "SELECT event_time, value, vintage_date, knowledge_time FROM fred_obs"
            " WHERE sid=? AND knowledge_time<=? ORDER BY event_time, vintage_date",
            (sid, asof.isoformat())).fetchall()
        return [dict(r) for r in rows]

    def kalshi_ladder_at(self, series: str, tok: str, asof: datetime) -> list[dict]:
        """Contract legs + the latest quote at-or-before asof for a Kalshi period
        token (the fed market-prior read, behind the single door)."""
        rows = self._conn.execute(
            "SELECT c.ticker, c.floor_strike strike, q.yes_bid, q.yes_ask, q.ts"
            " FROM contracts c JOIN quotes q ON q.ticker=c.ticker AND q.ts="
            "  (SELECT MAX(ts) FROM quotes WHERE ticker=c.ticker AND ts<=?)"
            " WHERE c.series=? AND c.period=?",
            (asof.isoformat(), series, tok)).fetchall()
        return [dict(r) for r in rows]

    def kalshi_periods(self, series: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT period FROM contracts WHERE series=?", (series,)).fetchall()
        return [r["period"] for r in rows]

    def fut_closes(self, root: str, asof: datetime, n: int = 260) -> tuple[pd.Series, str | None]:
        rows = self._conn.execute(
            "SELECT event_time, close, knowledge_time FROM fut_daily WHERE root=? AND"
            " knowledge_time<=? ORDER BY event_time DESC LIMIT ?",
            (root, asof.isoformat(), n)).fetchall()
        s = pd.Series({pd.Timestamp(r["event_time"]): r["close"] for r in rows}).sort_index()
        horizon = max((r["knowledge_time"] for r in rows), default=None)
        return s, horizon

    # ── frame assembly ────────────────────────────────────────────────────
    def frame(self, series: str, asof: datetime, defs: dict[str, tuple]) -> "Frame":
        """defs: {name: ("fred", sid, transform) | ("fut", root, transform)};
        transform(pd.Series) -> float."""
        vals: dict[str, float] = {}
        horizons: list[str] = []
        for name, spec in defs.items():
            kind, key, fn = spec
            if kind == "fred":
                s, h = self.fred_series(key, asof)
            elif kind == "fut":
                s, h = self.fut_closes(key, asof)
            else:
                raise ValueError(kind)
            if h:
                horizons.append(h)
            v = fn(s)
            if v is not None and np.isfinite(v):
                vals[name] = float(v)
        horizon = max(horizons) if horizons else asof.isoformat()
        f = Frame(series=series, asof=asof, values=vals,
                  data_horizon=datetime.fromisoformat(horizon))
        self._persist(f)
        return f

    def _persist(self, f: "Frame") -> None:
        for name, v in f.values.items():
            self._conn.execute(
                "INSERT OR REPLACE INTO features(series, asof, name, value, source,"
                " knowledge_time) VALUES(?,?,?,?,?,?)",
                (f.series, f.asof.isoformat(), name, v, "feature_frame",
                 f.data_horizon.isoformat()))
        self._conn.commit()


class Frame:
    def __init__(self, series: str, asof: datetime, values: dict[str, float],
                 data_horizon: datetime):
        assert data_horizon <= asof, \
            f"PIT violation in frame: {data_horizon} > {asof}"
        self.series, self.asof, self.values, self.data_horizon = series, asof, values, data_horizon

    def __getitem__(self, k: str) -> float:
        return self.values[k]

    def get(self, k: str, default: float | None = None) -> float | None:
        return self.values.get(k, default)
