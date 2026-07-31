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
