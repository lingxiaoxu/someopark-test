"""ingest/weather_fcst.py — CPC 6-10 / 8-14 day temperature outlooks, per city, PIT.

Why this source exists when `weather_daily` (ERA5 reanalysis) already does: every
realized-weather feature died three rounds of walk-forward (PLAN_ALTDATA_EXEC §29.4 /
§29.7 / §29.8), and the structural reason was always the same — the market watches
weather and weather FORECASTS in real time, so realized reanalysis at knowledge_time
event+1.5d is stale by construction. The only weather information that can lead the
market is the forecast itself, with the vintage being the moment CPC published it.
This module ingests exactly that: the official 6-10 and 8-14 day temperature outlooks,
one row per (product, forecast date, city).

Source: https://ftp.cpc.ncep.noaa.gov/GIS/us_tempprcpfcst/{610,814}temp_YYYYMMDD.zip
  Daily ESRI shapefiles, verified available 2012-09-11 → present (~4,870 files each).
  Attributes per polygon: Fcst_Date, Start_Date, End_Date, Prob (33/40/50/60/70/80/90),
  Cat ('Above'|'Below'|'Normal'). Geographic NAD83 lon/lat (checked in the .prj), so
  point-in-polygon runs directly on coordinates.

City extraction: for each of the 14 CITIES (weather.py's exact list, one weight set)
we test which category polygons contain the city point and keep the HIGHEST-Prob
containing polygon (contours are nested: the 50% area sits inside the 40% inside the
33%). A city outside every polygon is the CPC "equal chances" region → cat 'EC',
prob NULL. Both are stored; deriving signed scores is a model question, not ingest
(the §29.7 lesson: pre-aggregating locks bad choices into the table).

Point-in-polygon is a ~15-line even-odd ray cast rather than a shapely dependency:
pyshp (pure python, already the parser) exposes rings but no containment at all, and
the even-odd rule over all rings of a record handles holes correctly by construction.

PIT — the one judgement call, stated plainly:
  knowledge_time = Fcst_Date at 21:00 UTC.
  CPC issues the 6-10/8-14 day outlooks daily around 3 PM Eastern; 3 PM EDT = 19Z and
  3 PM EST = 20Z, so 21Z is at least an hour after publication year-round. If the fact
  ever matters to the minute, 21Z is CONSERVATIVE (we admit knowing it later than the
  world did), which is the safe direction for a leak.

Nothing here writes a feature or touches a model. Wiring is gated like any other
source (§7-bis): this table is `shadow` until a preregistered test clears it.
"""
from __future__ import annotations

import io
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime, time, timedelta, timezone
from time import sleep

from prediction_market_macro.ingest.weather import CITIES

_BASE = "https://ftp.cpc.ncep.noaa.gov/GIS/us_tempprcpfcst"
PRODUCTS = ("610temp", "814temp")
ARCHIVE_FLOOR = "2012-09-11"          # earliest zip on the FTP listing, both products
_FETCH_DELAY_S = 0.4                  # politeness spacing between zip downloads

SCHEMA = """
CREATE TABLE IF NOT EXISTS weather_fcst(
  product TEXT NOT NULL,               -- '610temp' | '814temp'
  fcst_date TEXT NOT NULL,             -- issuance date (Fcst_Date attribute)
  region TEXT NOT NULL,                -- city code from ingest.weather.CITIES
  start_date TEXT NOT NULL,            -- valid-window start (lead ~6d / ~8d)
  end_date TEXT NOT NULL,              -- valid-window end   (lead ~10d / ~14d)
  cat TEXT NOT NULL,                   -- 'Above' | 'Below' | 'Normal' | 'EC'
  prob REAL,                           -- CPC probability %, NULL when cat='EC'
  knowledge_time TEXT NOT NULL,        -- fcst_date 21:00 UTC (see module docstring)
  first_seen_ts TEXT NOT NULL,
  PRIMARY KEY(product, fcst_date, region));
"""


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _knowledge_time(fcst_date: str) -> str:
    return datetime.combine(date.fromisoformat(fcst_date), time(21, 0),
                            tzinfo=timezone.utc).isoformat()


def _point_in_ring(lon: float, lat: float, ring) -> bool:
    """Even-odd ray cast, one ring."""
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and \
                lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _point_in_shape(lon: float, lat: float, shape) -> bool:
    """Even-odd across ALL parts of the record: outer rings and holes cancel."""
    pts, parts = shape.points, list(shape.parts) + [len(shape.points)]
    hits = 0
    for a, b in zip(parts[:-1], parts[1:]):
        if _point_in_ring(lon, lat, pts[a:b]):
            hits += 1
    return hits % 2 == 1


def parse_zip(blob: bytes) -> tuple[dict, list[dict]]:
    """→ (meta {fcst/start/end date}, [{region, cat, prob}] for every city).

    Lazy pyshp import so importing this module never requires it (repo pattern for
    optional heavy deps; only ingest jobs and tests reach this function).
    """
    import shapefile

    zf = zipfile.ZipFile(io.BytesIO(blob))
    names = {n[-3:].lower(): n for n in zf.namelist()
             if n[-3:].lower() in ("shp", "dbf", "shx")}
    if "shp" not in names or "dbf" not in names:
        raise ValueError(f"zip missing shp/dbf: {zf.namelist()}")
    rdr = shapefile.Reader(
        shp=io.BytesIO(zf.read(names["shp"])),
        dbf=io.BytesIO(zf.read(names["dbf"])),
        shx=io.BytesIO(zf.read(names["shx"])) if "shx" in names else None)
    flds = [f[0] for f in rdr.fields[1:]]
    meta = None
    best: dict[str, tuple[str, float]] = {}
    for sr in rdr.iterShapeRecords():
        rec = dict(zip(flds, sr.record))
        if meta is None:
            meta = {k: (v.isoformat() if isinstance(v, date) else str(v))
                    for k, v in ((x, rec[y]) for x, y in
                                 (("fcst", "Fcst_Date"), ("start", "Start_Date"),
                                  ("end", "End_Date")))}
        cat, prob = str(rec["Cat"]), float(rec["Prob"])
        for code, (lat, lon, _w) in CITIES.items():
            box = sr.shape.bbox
            if not (box[0] <= lon <= box[2] and box[1] <= lat <= box[3]):
                continue
            if _point_in_shape(lon, lat, sr.shape):
                if code not in best or prob > best[code][1]:
                    best[code] = (cat, prob)
    if meta is None:
        raise ValueError("empty shapefile")
    rows = [{"region": code, "cat": best[code][0], "prob": best[code][1]}
            if code in best else {"region": code, "cat": "EC", "prob": None}
            for code in CITIES]
    return meta, rows


def _fetch(product: str, day: str, timeout: int = 60, tries: int = 4) -> bytes | None:
    """One zip; None on 404 (early years skip some weekend/holiday issuances)."""
    url = f"{_BASE}/{product}_{day.replace('-', '')}.zip"
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == tries - 1:
                raise
            sleep(5 * (attempt + 1))
        except urllib.error.URLError:
            if attempt == tries - 1:
                raise
            sleep(5 * (attempt + 1))
    return None


def _store(conn, product: str, meta: dict, rows: list[dict], now: str) -> None:
    for r in rows:
        conn.execute(
            "INSERT OR REPLACE INTO weather_fcst(product, fcst_date, region, start_date,"
            " end_date, cat, prob, knowledge_time, first_seen_ts) VALUES(?,?,?,?,?,?,?,?,"
            " COALESCE((SELECT first_seen_ts FROM weather_fcst WHERE product=?"
            "           AND fcst_date=? AND region=?), ?))",
            (product, meta["fcst"], r["region"], meta["start"], meta["end"], r["cat"],
             r["prob"], _knowledge_time(meta["fcst"]),
             product, meta["fcst"], r["region"], now))


def backfill(conn, start: str = ARCHIVE_FLOOR, end: str | None = None,
             products=PRODUCTS, log_every: int = 200, workers: int = 4) -> dict:
    """Idempotent + resumable: dates already in the table are skipped, so a killed run
    restarts where it left off and a completed one is a no-op.

    Downloads run on `workers` threads (serial measured ~3s/file → ~8h for the full
    archive; the wait is network latency, not the server working). Parse + insert stay
    on this thread — sqlite connections don't cross threads, and parsing is ~50ms.
    """
    from concurrent.futures import ThreadPoolExecutor

    ensure_schema(conn)
    end = end or datetime.now(timezone.utc).date().isoformat()
    now = datetime.now(timezone.utc).isoformat()
    out = {p: {"fetched": 0, "missing": 0, "skipped": 0} for p in products}
    for product in products:
        have = {r[0] for r in conn.execute(
            "SELECT DISTINCT fcst_date FROM weather_fcst WHERE product=?", (product,))}
        pending = []
        d = date.fromisoformat(start)
        while d.isoformat() <= end:
            day = d.isoformat()
            (out[product].__setitem__("skipped", out[product]["skipped"] + 1)
             if day in have else pending.append(day))
            d += timedelta(days=1)

        def fetch_paced(day: str):
            sleep(_FETCH_DELAY_S)                  # per-thread pacing
            return day, _fetch(product, day)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for day, blob in ex.map(fetch_paced, pending):
                if blob is None:
                    out[product]["missing"] += 1
                    continue
                try:
                    meta, rows = parse_zip(blob)
                except Exception as e:              # one bad zip must not kill 4,800
                    print(f"{product} {day}: parse failed: {e}", flush=True)
                    out[product]["missing"] += 1
                    continue
                _store(conn, product, meta, rows, now)
                conn.commit()
                out[product]["fetched"] += 1
                if out[product]["fetched"] % log_every == 0:
                    print(f"{product}: {out[product]['fetched']} fetched, at {day}",
                          flush=True)
    return out


def refresh(conn) -> dict:
    """Daily tail pull: last 5 days (covers issuance gaps + our own downtime)."""
    start = (datetime.now(timezone.utc).date() - timedelta(days=5)).isoformat()
    return backfill(conn, start=start)


if __name__ == "__main__":
    import sys
    from pathlib import Path
    from prediction_market_macro.ingest.store import connect

    db = Path(__file__).resolve().parent.parent / "data" / "macro.db"
    conn = connect(db)
    args = sys.argv[1:]
    res = backfill(conn, start=args[0] if args else ARCHIVE_FLOOR,
                   end=args[1] if len(args) > 1 else None)
    print(res)
