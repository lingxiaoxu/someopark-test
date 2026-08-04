"""ingest/weather.py — population-weighted US degree days (PLAN §28; the gap energy.py
flags at its own NG branch).

`model/energy.py` annotates weather as a known failure mode of the natural-gas model and
then does nothing about it, because there was no weather source at all. This is that
source: daily HDD/CDD for 14 metros plus a population-weighted national aggregate, from
Open-Meteo's ERA5 reanalysis. Measured 2026-08-04: zero nulls, one HTTP call per city
(~1.2s each) even for a 36-year span. The default start is 2005 rather than the 1940
archive floor because the thing this has to explain — EIA weekly gas storage — itself
only starts 2010 (865 rows); five years of run-up is enough to fit a seasonal normal,
and a longer pull just burns Open-Meteo's cells x days quota for data nothing consumes.

Degree days are the standard heating/cooling demand proxy:
    HDD = max(65°F - Tmean, 0)      CDD = max(Tmean - 65°F, 0)

WEIGHTS are metro population, not gas load. That is a real simplification and it is
stated rather than hidden: gas heating load per capita is much higher in Chicago than in
Phoenix, so a load-weighted index would tilt further north. Population weighting is the
conventional first cut (it is what "population-weighted HDD" means in the EIA/NOAA
releases) and refining it is a model question, not an ingest one.

PIT — the one judgement call in this file, stated plainly:
  knowledge_time = event_time + 1 day at 12:00 UTC.
  ERA5 itself publishes on a ~5-day lag, so if the *reanalysis* were the fact we needed,
  D+1 would be a leak. It is not: the fact is what the temperature was in Chicago
  yesterday, which is public from NWS observations by the next morning, and the
  reanalysis is only a tidy way to fetch it. The residual exposure is that ERA5T revises
  into ERA5 — those revisions are small at daily-mean resolution, but we cannot measure
  them from a single vintage, so the caveat stands and this source is `shadow` until it
  clears a §9.5 gate like any other (§7-bis).

Nothing here writes a feature or touches a model. Wiring is §29 and must pass the gate.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from time import sleep
from datetime import date, datetime, time, timedelta, timezone

_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
_CITY_DELAY_S = 3.0            # spacing between city pulls; 0 in tests

# metro, (lat, lon), population weight in millions (2020 CSA/MSA, rounded).
# Chosen to span the climate zones that drive US gas load: the northern heating belt
# carries the winter draw, the southern cooling belt the summer power burn.
CITIES: dict[str, tuple[float, float, float]] = {
    "NYC": (40.71, -74.01, 20.1),
    "LA": (34.05, -118.24, 13.2),
    "CHI": (41.88, -87.63, 9.5),
    "DAL": (32.78, -96.80, 8.1),
    "HOU": (29.76, -95.37, 7.5),
    "WDC": (38.91, -77.04, 6.4),
    "ATL": (33.75, -84.39, 6.3),
    "PHL": (39.95, -75.17, 6.2),
    "PHX": (33.45, -112.07, 5.1),
    "BOS": (42.36, -71.06, 4.9),
    "DET": (42.33, -83.05, 4.4),
    "SEA": (47.61, -122.33, 4.0),
    "MSP": (44.98, -93.27, 3.7),
    "DEN": (39.74, -104.99, 3.0),
}

BASE_F = 65.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS weather_daily(
  region TEXT NOT NULL,                  -- city code above, or 'US' for the weighted mean
  event_time TEXT NOT NULL,              -- observation date
  tmean_f REAL, hdd REAL, cdd REAL,
  knowledge_time TEXT NOT NULL,          -- event_time + 1d 12:00 UTC (see module docstring)
  first_seen_ts TEXT NOT NULL,
  PRIMARY KEY(region, event_time));
"""


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _knowledge_time(day: str) -> str:
    d = date.fromisoformat(day) + timedelta(days=1)
    return datetime.combine(d, time(12, 0), tzinfo=timezone.utc).isoformat()


def _degree_days(tmean_f: float) -> tuple[float, float]:
    return max(BASE_F - tmean_f, 0.0), max(tmean_f - BASE_F, 0.0)


def _fetch_city(lat: float, lon: float, start: str, end: str,
                timeout: int = 180, tries: int = 5) -> dict[str, float]:
    """One city, whole range. Open-Meteo's free quota is weighted by cells x days, so a
    multi-decade daily pull is worth many nominal 'calls' and 429s on a tight loop — hence
    the backoff. Chunking the range would only trade one big weight for several."""
    url = (f"{_ARCHIVE}?latitude={lat}&longitude={lon}&start_date={start}&end_date={end}"
           "&daily=temperature_2m_mean&temperature_unit=fahrenheit&timezone=UTC")
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                doc = json.load(r)
            break
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == tries - 1:
                raise
            sleep(20 * (attempt + 1))
    daily = doc.get("daily") or {}
    return {d: v for d, v in zip(daily.get("time") or [], daily.get("temperature_2m_mean") or [])
            if v is not None}


def pull(conn, start: str = "2005-01-01", end: str | None = None) -> dict:
    """Fetch every city over [start, end] and write the cities plus the 'US' aggregate.

    `end` defaults to yesterday: today's row would be a partial day, and a partial daily
    mean is not the same statistic as a whole one.

    INSERT OR REPLACE on (region, event_time) — a re-run of an overlapping window updates
    values in place, which is what we want while the tail of ERA5 is still ERA5T. It does
    mean this table is last-vintage-only; if a revision study is ever needed the table
    needs a vintage column first, exactly like fred_obs has.
    """
    ensure_schema(conn)
    end = end or (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    series: dict[str, dict[str, float]] = {}
    for i, (code, (lat, lon, _w)) in enumerate(CITIES.items()):
        if i and _CITY_DELAY_S:
            sleep(_CITY_DELAY_S)           # stay inside the free per-minute quota
        series[code] = _fetch_city(lat, lon, start, end)

    rows = 0
    for code, obs in series.items():
        for day, t in obs.items():
            hdd, cdd = _degree_days(t)
            conn.execute(
                "INSERT OR REPLACE INTO weather_daily(region, event_time, tmean_f, hdd, cdd,"
                " knowledge_time, first_seen_ts) VALUES(?,?,?,?,?,?,"
                " COALESCE((SELECT first_seen_ts FROM weather_daily WHERE region=?"
                "           AND event_time=?), ?))",
                (code, day, t, hdd, cdd, _knowledge_time(day), code, day, now))
            rows += 1

    # national aggregate: weight the DEGREE DAYS, not the temperatures. Averaging Tmean
    # first and then applying the 65F kink would net a freezing Minneapolis against a
    # mild Phoenix and report neither heating nor cooling demand on a day with plenty of
    # both. Only days where every city reported are aggregated, so the weight set is
    # constant through the series and a gap cannot masquerade as a mild day.
    wsum = sum(w for _, _, w in CITIES.values())
    all_days = sorted(set.intersection(*(set(o) for o in series.values())))
    for day in all_days:
        hdd = cdd = tw = 0.0
        for code, (_la, _lo, w) in CITIES.items():
            t = series[code][day]
            h, c = _degree_days(t)
            hdd += w * h
            cdd += w * c
            tw += w * t
        conn.execute(
            "INSERT OR REPLACE INTO weather_daily(region, event_time, tmean_f, hdd, cdd,"
            " knowledge_time, first_seen_ts) VALUES('US',?,?,?,?,?,"
            " COALESCE((SELECT first_seen_ts FROM weather_daily WHERE region='US'"
            "           AND event_time=?), ?))",
            (day, tw / wsum, hdd / wsum, cdd / wsum, _knowledge_time(day), day, now))
        rows += 1
    conn.commit()
    return {"cities": len(series), "days": len(all_days), "rows": rows,
            "start": all_days[0] if all_days else None,
            "end": all_days[-1] if all_days else None}


def weekly_us(conn, asof: datetime, region: str = "US", weeks: int = 260):
    """PIT accessor: weekly HDD/CDD sums known at `asof`, oldest first.

    Weekly because that is the frequency of the thing it has to explain — the EIA gas
    storage change (NG_STORAGE_WEEKLY, Thursday, Friday-to-Friday week). Rows land in
    week-ending-Friday buckets to match.
    """
    rows = conn.execute(
        "SELECT event_time, hdd, cdd FROM weather_daily WHERE region=? AND knowledge_time<=?"
        " ORDER BY event_time", (region, asof.isoformat())).fetchall()
    buckets: dict[str, list[float]] = {}
    for r in rows:
        d = date.fromisoformat(r["event_time"])
        friday = d + timedelta(days=(4 - d.weekday()) % 7)
        b = buckets.setdefault(friday.isoformat(), [0.0, 0.0, 0.0])
        b[0] += r["hdd"]
        b[1] += r["cdd"]
        b[2] += 1
    out = [(k, v[0], v[1]) for k, v in sorted(buckets.items()) if v[2] == 7]
    return out[-weeks:]
