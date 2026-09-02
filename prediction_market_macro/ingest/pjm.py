"""ingest/pjm.py — PJM grid fundamentals, PIT, SHADOW (§7-bis).

Why this source (2026-09-02, user-directed): PJM is the largest US power market —
roughly 65 GW of gas burn against ERCOT's ~30 GW on a comparable day — and the same
mechanism ERCOT_NOTES.md documents applies with more national weight: power-sector gas
burn is the demand side of the EIA storage prints that move NG (and with it KXNATGASW).
This module is the deliberate parallel of `ingest/ercot.py`: same table shape, same PIT
declarations, same shadow discipline, same failure semantics. It shares the ERCOT lane's
EIA key and reuses the AEUS strategy's PJM Data Miner 2 credential (`PJM_API_KEY` in the
repo-root .env), so nothing new is provisioned.

Sources, in the same two tiers ERCOT uses:

  * PJM Data Miner 2 (`https://api.pjm.com/api/v1/gen_by_fuel`, header
    `Ocp-Apim-Subscription-Key`): HOURLY generation by fuel, accrued forward each daily
    refresh. This is the fine-grained fuel split that EIA-930's daily rollup cannot give.
    Non-member rate limit is ~6 requests/min, so this module issues ONE request per
    refresh (a 3-day window) and never loops — the AEUS module owns bulk PJM pulls.
  * EIA-930 (`api.eia.gov/v2/electricity/rto/...`, `EIA_API_KEY`): daily demand and net
    generation by fuel back to 2019-01-01, the deep history the Data Miner archive wall
    (~731 days for non-members) cannot reach.

Stored: DAILY aggregates in `pjm_daily(date, metric, value)`, long format, exactly like
`ercot_daily`. Metric names are SOURCE-TAGGED: `eia_*` metrics are net generation in
MWh/day; the un-prefixed metrics are average MW from Data Miner. The two are never
merged — mixing them would manufacture a unit break at the seam (the mistake ERCOT's
notes call out explicitly).

knowledge_time = pull time. The PIT lag lives where it is consumed: D+2 for EIA-930
(published next-day with small revisions, so D+2 admits knowing it LATER than the world
did) and week-end+2d in the fred_obs mirror — the same conservative direction ERCOT and
cleveland_nowcast both registered.

Consumption status (2026-09-02): SHADOW. No model reads this table, and none may until a
preregistered gate clears. `tests/test_ingest_pjm.py` greps the model package for the
table name and fails if any consumer appears without that gate.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

_DM_BASE = "https://api.pjm.com/api/v1"
_UA = {"User-Agent": "someopark-macro/1.0", "Accept": "application/json"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pjm_daily(
    date TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    knowledge_time TEXT NOT NULL,
    first_seen_ts TEXT NOT NULL,
    PRIMARY KEY(date, metric)
);
"""

# EIA-930 fuel ids -> our metric names. `NG` is Natural Gas on the FUEL route (on the
# region route the same string means Net generation — the collision is real and is why
# the two routes are pulled by separate functions).
_EIA_FUELS = {"NG": "eia_gas_gen_mwh", "WND": "eia_wind_gen_mwh",
              "SUN": "eia_solar_gen_mwh", "COL": "eia_coal_gen_mwh",
              "NUC": "eia_nuclear_gen_mwh", "OIL": "eia_oil_gen_mwh",
              "WAT": "eia_hydro_gen_mwh"}


def _dm_get(feed: str, params: dict) -> list[dict]:
    """One Data Miner 2 request. `startRow` is mandatory — omitting it returns HTTP 400
    with a field error, not an empty page."""
    key = os.environ.get("PJM_API_KEY")
    if not key or key == "__FILL_ME__":
        raise RuntimeError("PJM_API_KEY missing from env")
    url = f"{_DM_BASE}/{feed}?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={**_UA, "Ocp-Apim-Subscription-Key": key})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read()).get("items") or []


def _fuel_mix_days(items: list[dict]) -> dict[str, dict[str, float]]:
    """Hourly gen_by_fuel rows -> {date: {gas_gen_avg_mw, total_gen_avg_mw, gas_share}}.

    Averaged over the hours present, like ERCOT's 5-min dashboard parser. A day is kept
    only with >= 12 hourly observations: a partial boundary day is a window artifact of
    the request range, not a day.
    """
    per: dict[str, dict[str, float]] = {}
    hours: dict[str, set] = {}
    for row in items:
        ts = str(row.get("datetime_beginning_ept") or "")
        if len(ts) < 10 or row.get("mw") is None:
            continue
        d, mw = ts[:10], float(row["mw"])
        acc = per.setdefault(d, {"gas": 0.0, "total": 0.0})
        acc["total"] += mw
        if str(row.get("fuel_type") or "").strip().lower() == "gas":
            acc["gas"] += mw
        hours.setdefault(d, set()).add(ts[11:13])
    out = {}
    for d, acc in per.items():
        n = len(hours.get(d, ()))
        if n < 12:
            continue
        out[d] = {"gas_gen_avg_mw": acc["gas"] / n,
                  "total_gen_avg_mw": acc["total"] / n,
                  "gas_share": (acc["gas"] / acc["total"]) if acc["total"] else 0.0}
    return out


def _upsert(conn, days: dict, now: str, replace: bool = True) -> int:
    verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    n = 0
    for d, metrics in days.items():
        for metric, value in metrics.items():
            if value != value:                       # NaN is an absent observation
                continue
            cur = conn.execute(
                f"{verb} INTO pjm_daily(date, metric, value, knowledge_time,"
                " first_seen_ts) VALUES(?,?,?,?, COALESCE((SELECT first_seen_ts FROM"
                " pjm_daily WHERE date=? AND metric=?), ?))",
                (d, metric, float(value), now, d, metric, now))
            n += cur.rowcount if not replace else 1
    return n


def refresh(conn, days_back: int = 3) -> dict:
    """Forward accrual from PJM Data Miner 2: hourly fuel mix -> daily averages.

    ONE request per call (the non-member limit is ~6/min and the AEUS strategy owns the
    bulk pulls). Partial days are overwritten as they complete; `first_seen_ts` survives
    the overwrite, so "when did we first see this day" stays answerable.
    """
    conn.executescript(_SCHEMA)
    today = datetime.now(timezone.utc).date()
    lo = (today - timedelta(days=days_back)).isoformat()
    hi = today.isoformat()
    items = _dm_get("gen_by_fuel", {
        "startRow": 1, "rowCount": 50000,
        "datetime_beginning_ept": f"{lo} 00:00 to {hi} 23:59",
        "fields": "datetime_beginning_ept,fuel_type,mw,fuel_percentage_of_total"})
    now = datetime.now(timezone.utc).isoformat()
    days = _fuel_mix_days(items)
    n = _upsert(conn, days, now)
    conn.commit()
    return {"days": len(days), "rows": n}


def _eia_pull(route: str, extra: list[tuple], start: str) -> list[dict]:
    key = os.environ.get("EIA_API_KEY")
    if not key:
        raise RuntimeError("EIA_API_KEY missing from env")
    rows, offset = [], 0
    while True:
        q = [("api_key", key), ("frequency", "daily"), ("data[0]", "value"),
             ("facets[respondent][]", "PJM"),
             # Eastern is REQUIRED: with no timezone facet the API returns one row per
             # US timezone per day, with DIFFERENT values, and a naive sum inflates 5x.
             ("facets[timezone][]", "Eastern"),
             ("start", start), ("length", 5000), ("offset", offset)] + extra
        url = (f"https://api.eia.gov/v2/electricity/rto/{route}/data/?"
               + urllib.parse.urlencode(q))
        with urllib.request.urlopen(url, timeout=90) as r:
            j = json.loads(r.read())["response"]
        rows += j["data"]
        offset += 5000
        if offset >= int(j.get("total") or 0):
            return rows


def backfill_eia930(conn, start: str = "2019-01-01", log=None) -> dict:
    """Deep history from EIA-930: PJM demand and net generation by fuel, 2019 onward.

    Source-tagged `eia_*` metrics in MWh/day — never merged with the Data Miner average-MW
    names. INSERT OR REPLACE + COALESCE keeps the first-seen anchor while letting EIA's
    small next-day revisions land.
    """
    say = log or (lambda *_a, **_k: None)
    conn.executescript(_SCHEMA)
    now = datetime.now(timezone.utc).isoformat()
    days: dict[str, dict[str, float]] = {}
    for fuel, metric in _EIA_FUELS.items():
        rows = _eia_pull("daily-fuel-type-data", [("facets[fueltype][]", fuel)], start)
        say(f"  {metric}: {len(rows)} rows")
        for r in rows:
            if r.get("value") is None:
                continue
            days.setdefault(r["period"], {})[metric] = float(r["value"])
    rows = _eia_pull("daily-region-data", [("facets[type][]", "D")], start)
    say(f"  eia_demand_mwh: {len(rows)} rows")
    for r in rows:
        if r.get("value") is not None:
            days.setdefault(r["period"], {})["eia_demand_mwh"] = float(r["value"])
    n = _upsert(conn, days, now)
    conn.commit()
    return {"days": len(days), "rows": n}


def mirror_weekly_burn(conn) -> int:
    """Mirror weekly PJM gas burn into fred_obs as `PJM_GASBURN_W` — the panel entry.

    The established pattern (EIA feeds, ERCOT_GASBURN_W): synthetic sids in fred_obs are
    how non-FRED series reach the DFM panels. W-SAT weekly mean of eia_gas_gen_mwh,
    knowledge_time = week end + 2 days 00:00 UTC (the same D+2 conservatism the ERCOT
    lane registered). INSERT OR IGNORE: vintages append, never rewrite.
    """
    import pandas as pd
    conn.executescript(_SCHEMA)
    rows = conn.execute("SELECT date, value FROM pjm_daily WHERE"
                        " metric='eia_gas_gen_mwh' ORDER BY date").fetchall()
    if not rows:
        return 0
    s = pd.Series({pd.Timestamp(r[0]): float(r[1]) for r in rows})
    wk = s.resample("W-SAT").mean().dropna()
    now = datetime.now(timezone.utc)
    n = 0
    for ts, v in wk.items():
        kt = (ts + pd.Timedelta(days=2)).strftime("%Y-%m-%dT00:00:00+00:00")
        if pd.Timestamp(kt[:10]) > pd.Timestamp(now.date()):
            continue
        n += conn.execute(
            "INSERT OR IGNORE INTO fred_obs(sid, event_time, value, vintage_date,"
            " knowledge_time, first_seen_ts) VALUES(?,?,?,?,?,?)",
            ("PJM_GASBURN_W", ts.date().isoformat(), float(v),
             ts.date().isoformat(), kt, now.isoformat())).rowcount
    conn.commit()
    return n
