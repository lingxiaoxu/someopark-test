"""ingest/ercot.py — ERCOT grid fundamentals, PIT, SHADOW (§7-bis).

Why this source (2026-08-30, user-directed): ERCOT is the deepest freely-readable
window into US energy fundamentals at daily cadence. Texas power-sector natural-gas
burn is the single largest weather-driven demand block in the US gas market; the
EIA storage prints that move NG (and with it KXNATGASW) are, to first order,
production minus LNG minus exactly this kind of burn. The market's storage
expectations are built from grid data like this — it is "information the market
has" in the same sense the Cleveland nowcast was for CPI.

Source: the PUBLIC dashboard JSON endpoints behind ercot.com's charts — no
authentication, distinct from the Public API (api.ercot.com), which needs the
B2C password and is the later backfill path for years of history:

    /api/1/services/read/dashboards/fuel-mix.json        gen by fuel, 5-min
    /api/1/services/read/dashboards/supply-demand.json   demand + capacity, 5-min
    /api/1/services/read/dashboards/system-wide-prices.json  RT/DAM hub SPP, 15-min
    /api/1/services/read/dashboards/combine-wind-solar.json  wind/solar actuals, hourly

Each carries roughly the current and previous day, so this ingest ACCRUES history
forward from its landing day; the Public API backfill (blocked on
ERCOT_API_PASSWORD, see .env) will extend the same table backward when it lands.

Stored: DAILY aggregates in `ercot_daily(date, metric, value)`, long format.
knowledge_time = pull time (we know a day's value when we fetched it, and a
partial day's row is overwritten as the day completes — the last write before
midnight carries the completed day). `first_seen_ts` survives overwrites, so the
PIT question "when did we first see any value for this day" stays answerable.

Consumption status (2026-08-30): SHADOW. No model reads this table, and none may
until a preregistered gate clears (the cleveland_nowcast §7-bis route: measure
the candidate anchor against settled events first, adopt through PREREGISTER.md).
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone

_BASE = "https://www.ercot.com/api/1/services/read/dashboards"
_UA = {"User-Agent": "someopark-macro/1.0"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ercot_daily(
    date TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    knowledge_time TEXT NOT NULL,
    first_seen_ts TEXT NOT NULL,
    PRIMARY KEY(date, metric)
);
"""


def _get(name: str) -> dict:
    req = urllib.request.Request(f"{_BASE}/{name}.json", headers=_UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _fuel_mix_days(payload: dict) -> dict[str, dict[str, float]]:
    """date -> {gas_gen_avg_mw, total_gen_avg_mw, gas_share} from 5-min gen by fuel."""
    out = {}
    for day, intervals in (payload.get("data") or {}).items():
        gas, total, n = 0.0, 0.0, 0
        for _ts, fuels in intervals.items():
            g = float(fuels.get("Natural Gas", {}).get("gen", 0.0))
            t = sum(float(v.get("gen", 0.0)) for v in fuels.values())
            gas += g
            total += t
            n += 1
        if n:
            out[day] = {"gas_gen_avg_mw": gas / n, "total_gen_avg_mw": total / n,
                        "gas_share": (gas / total) if total else 0.0}
    return out


def _supply_demand_days(payload: dict) -> dict[str, dict[str, float]]:
    days: dict[str, list] = {}
    for row in payload.get("data") or []:
        d = str(row.get("timestamp", ""))[:10]
        if d and row.get("demand"):
            days.setdefault(d, []).append((float(row["demand"]),
                                           float(row.get("capacity") or 0.0)))
    # >= 12 five-minute intervals (one hour): a day represented by a single boundary
    # row is a timezone artifact of the dashboard window, not a day.
    return {d: {"demand_avg_mw": sum(x[0] for x in v) / len(v),
                "demand_max_mw": max(x[0] for x in v),
                "capacity_avg_mw": sum(x[1] for x in v) / len(v)}
            for d, v in days.items() if len(v) >= 12}


def _prices_today(payload: dict, today: str) -> dict[str, dict[str, float]]:
    """The prices dashboard carries only the current day, unlabeled — stamp it."""
    out = {}
    rt = [float(r["hbHubAvg"]) for r in payload.get("rtSppData") or []
          if r.get("hbHubAvg") is not None]
    dam = [float(r["hbHubAvg"]) for r in payload.get("damSppData") or []
           if r.get("hbHubAvg") is not None]
    m = {}
    if rt:
        m["rt_spp_hubavg"] = sum(rt) / len(rt)
        m["rt_spp_hubmax"] = max(rt)
    if dam:
        m["dam_spp_hubavg"] = sum(dam) / len(dam)
    if m:
        out[today] = m
    return out


def _wind_solar_days(payload: dict) -> dict[str, dict[str, float]]:
    out = {}
    for key in ("currentDay",):
        blk = payload.get(key) or {}
        d = str(blk.get("date", ""))[:10]
        rows = list((blk.get("data") or {}).values())
        wind = [float(r["actualWind"]) for r in rows if r.get("actualWind") is not None]
        sol = [float(r["actualSolar"]) for r in rows if r.get("actualSolar") is not None]
        m = {}
        if wind:
            m["wind_avg_mw"] = sum(wind) / len(wind)
        if sol:
            m["solar_avg_mw"] = sum(sol) / len(sol)
        if d and m:
            out[d] = m
    return out


def refresh(conn) -> dict:
    """Pull all four public dashboards, upsert daily aggregates. Returns {metric_rows}."""
    conn.executescript(_SCHEMA)
    now = datetime.now(timezone.utc).isoformat()
    days: dict[str, dict[str, float]] = {}
    for d, m in _fuel_mix_days(_get("fuel-mix")).items():
        days.setdefault(d, {}).update(m)
    for d, m in _supply_demand_days(_get("supply-demand")).items():
        days.setdefault(d, {}).update(m)
    px = _get("system-wide-prices")
    # the prices dashboard is single-day and unlabeled row-wise; its own lastUpdated
    # names the day — max(days) once mislabeled it onto a boundary-artifact date
    today = str(px.get("lastUpdated", now))[:10]
    for d, m in _prices_today(px, today).items():
        days.setdefault(d, {}).update(m)
    for d, m in _wind_solar_days(_get("combine-wind-solar")).items():
        days.setdefault(d, {}).update(m)
    n = 0
    for d, metrics in days.items():
        for metric, value in metrics.items():
            if value != value:                       # NaN is an absent observation
                continue
            conn.execute(
                "INSERT OR REPLACE INTO ercot_daily(date, metric, value,"
                " knowledge_time, first_seen_ts) VALUES(?,?,?,?,"
                " COALESCE((SELECT first_seen_ts FROM ercot_daily WHERE date=? AND"
                " metric=?), ?))",
                (d, metric, float(value), now, d, metric, now))
            n += 1
    conn.commit()
    return {"days": len(days), "rows": n}


# ── the authenticated Public API: rolling ~5-month row archive, deep zips later ──
_B2C = ("https://ercotb2c.b2clogin.com/ercotb2c.onmicrosoft.com/"
        "B2C_1_PUBAPI-ROPC-FLOW/oauth2/v2.0/token")
_CID = "fec253ea-0d06-4272-a5e6-b478baeecd70"
_API = "https://api.ercot.com/api/public-reports"


def _api_token() -> tuple[str, str]:
    """(bearer id_token, subscription key) from the environment. Raises with the
    variable name when a credential is absent — a silent None here would read as an
    ERCOT outage."""
    import os
    import urllib.parse
    sub = os.environ.get("ERCOT_API_SUBSCRIPTION_KEY")
    user = os.environ.get("ERCOT_API_USERNAME")
    pw = os.environ.get("ERCOT_API_PASSWORD")
    missing = [k for k, v in (("ERCOT_API_SUBSCRIPTION_KEY", sub),
                              ("ERCOT_API_USERNAME", user),
                              ("ERCOT_API_PASSWORD", pw)) if not v or v == "__FILL_ME__"]
    if missing:
        raise RuntimeError(f"ERCOT API credentials missing from env: {missing}")
    body = urllib.parse.urlencode({
        "grant_type": "password", "username": user, "password": pw,
        "scope": f"openid {_CID} offline_access", "client_id": _CID,
        "response_type": "id_token"}).encode()
    req = urllib.request.Request(_B2C, data=body)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["id_token"], sub


def _api_pages(path: str, params: dict, token: str, sub: str, max_pages: int = 40):
    """Yield data rows across pages. size=50000 per page; the API caps as it pleases."""
    import urllib.parse
    page = 1
    while page <= max_pages:
        q = dict(params, size=50000, page=page)
        url = f"{_API}/{path}?{urllib.parse.urlencode(q)}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}", "Ocp-Apim-Subscription-Key": sub})
        with urllib.request.urlopen(req, timeout=120) as r:
            j = json.loads(r.read())
        yield from j.get("data") or []
        meta = j.get("_meta") or {}
        if page >= int(meta.get("totalPages") or 1):
            return
        page += 1


def backfill(conn, days: int = 150, log=None) -> dict:
    """Fill `ercot_daily` backward from the Public API's rolling row archive.

    INSERT OR IGNORE on purpose: the forward dashboard accrual (5-min resolution,
    fuel split included) owns any day it has touched; the backfill only fills days
    the dashboards never saw. Wind/solar rows are vintage-stamped (postedDatetime),
    so actuals are deduped to the LATEST posting per (day, hour).
    """
    from datetime import date, timedelta
    say = log or (lambda *_a, **_k: None)
    conn.executescript(_SCHEMA)
    token, sub = _api_token()
    now = datetime.now(timezone.utc).isoformat()
    d0 = (date.today() - timedelta(days=days)).isoformat()
    d1 = date.today().isoformat()
    rng = {"deliveryDateFrom": d0, "deliveryDateTo": d1}
    out: dict[str, dict[str, float]] = {}

    dem: dict[str, list[float]] = {}
    for row in _api_pages("np6-235-cd/system_wide_demand", rng, token, sub):
        d, _t, v, _f = row
        if v is not None:
            dem.setdefault(d, []).append(float(v))
    for d, v in dem.items():
        if len(v) >= 48:
            out.setdefault(d, {}).update(
                {"demand_avg_mw": sum(v) / len(v), "demand_max_mw": max(v)})
    say(f"  demand: {len(dem)} days")

    for path, col, metric in (("np4-732-cd/wpp_hrly_avrg_actl_fcast", 3, "wind_avg_mw"),
                              ("np4-737-cd/spp_hrly_avrg_actl_fcast", 3, "solar_avg_mw")):
        latest: dict[tuple, tuple] = {}
        for row in _api_pages(path, rng, token, sub):
            posted, d, hr = row[0], row[1], row[2]
            gen = row[col]
            if gen is None:
                continue
            k = (d, hr)
            if k not in latest or posted > latest[k][0]:
                latest[k] = (posted, float(gen))
        per_day: dict[str, list[float]] = {}
        for (d, _hr), (_p, g) in latest.items():
            per_day.setdefault(d, []).append(g)
        for d, v in per_day.items():
            if len(v) >= 20:
                out.setdefault(d, {})[metric] = sum(v) / len(v)
        say(f"  {metric}: {len(per_day)} days")

    hub: dict[str, list[float]] = {}
    for row in _api_pages("np4-190-cd/dam_stlmnt_pnt_prices",
                          dict(rng, settlementPoint="HB_HUBAVG"), token, sub):
        d, _hr, _pt, px, _f = row
        if px is not None:
            hub.setdefault(d, []).append(float(px))
    for d, v in hub.items():
        if len(v) >= 20:
            out.setdefault(d, {})["dam_spp_hubavg"] = sum(v) / len(v)
    say(f"  dam prices: {len(hub)} days")

    n = 0
    for d, metrics in out.items():
        for metric, value in metrics.items():
            if value != value:
                continue
            cur = conn.execute("INSERT OR IGNORE INTO ercot_daily(date, metric, value,"
                               " knowledge_time, first_seen_ts) VALUES(?,?,?,?,?)",
                               (d, metric, float(value), now, now))
            n += cur.rowcount
    conn.commit()
    return {"days": len(out), "rows_inserted": n}


def backfill_eia930(conn, start: str = "2019-01-01", log=None) -> dict:
    """Deep history for the SAME quantities from EIA-930 (hourly grid monitor, daily
    rollup): ERCO demand and net generation by fuel back to 2018, via the EIA key the
    storage ingest already uses. This is the sample the ERCOT dashboards cannot give
    (they accrue forward only) and the zip archives price at thousands of requests.

    Metrics are source-tagged (`eia_*`) rather than merged into the dashboard names:
    EIA-930 is net generation in MWh/day, the dashboards are average MW — mixing them
    silently would manufacture a unit break at the seam.
    """
    import os
    import urllib.parse
    say = log or (lambda *_a, **_k: None)
    conn.executescript(_SCHEMA)
    key = os.environ.get("EIA_API_KEY")
    if not key:
        raise RuntimeError("EIA_API_KEY missing from env")
    now = datetime.now(timezone.utc).isoformat()

    def pull(route, extra):
        rows, offset = [], 0
        while True:
            q = [("api_key", key), ("frequency", "daily"), ("data[0]", "value"),
                 ("facets[respondent][]", "ERCO"), ("facets[timezone][]", "Central"),
                 ("start", start), ("length", 5000), ("offset", offset)] + extra
            url = (f"https://api.eia.gov/v2/electricity/rto/{route}/data/?"
                   + urllib.parse.urlencode(q))
            with urllib.request.urlopen(url, timeout=90) as r:
                j = json.loads(r.read())["response"]
            rows += j["data"]
            offset += 5000
            if offset >= int(j.get("total") or 0):
                return rows

    n = 0
    fuels = {"NG": "eia_gas_gen_mwh", "WND": "eia_wind_gen_mwh",
             "SUN": "eia_solar_gen_mwh", "COL": "eia_coal_gen_mwh",
             "NUC": "eia_nuclear_gen_mwh"}
    for ft, metric in fuels.items():
        rows = pull("daily-fuel-type-data", [("facets[fueltype][]", ft)])
        say(f"  {metric}: {len(rows)} rows")
        for r in rows:
            if r.get("value") is None:
                continue
            n += conn.execute(
                "INSERT OR REPLACE INTO ercot_daily(date, metric, value, knowledge_time,"
                " first_seen_ts) VALUES(?,?,?,?, COALESCE((SELECT first_seen_ts FROM"
                " ercot_daily WHERE date=? AND metric=?), ?))",
                (r["period"], metric, float(r["value"]), now,
                 r["period"], metric, now)).rowcount
    rows = pull("daily-region-data", [("facets[type][]", "D")])
    say(f"  eia_demand_mwh: {len(rows)} rows")
    for r in rows:
        if r.get("value") is None:
            continue
        n += conn.execute(
            "INSERT OR REPLACE INTO ercot_daily(date, metric, value, knowledge_time,"
            " first_seen_ts) VALUES(?,?,?,?, COALESCE((SELECT first_seen_ts FROM"
            " ercot_daily WHERE date=? AND metric=?), ?))",
            (r["period"], "eia_demand_mwh", float(r["value"]), now,
             r["period"], "eia_demand_mwh", now)).rowcount
    conn.commit()
    return {"rows": n}
