"""weather_fcst: the properties that make the CPC outlook table trustworthy.

What is pinned and why:
  1. City extraction picks the INNERMOST nested contour (50 inside 40 inside 33) —
     off-by-one here silently halves every probability in the table.
  2. Holes subtract: a city inside a donut hole is NOT in the polygon. The even-odd
     ray cast gets this by construction, but only if parts are iterated correctly.
  3. knowledge_time = fcst_date 21:00 UTC — after both 3PM EDT (19Z) and 3PM EST
     (20Z) issuance. A forecast known before it was published is the §29.7 look-ahead
     all over again, this time at the source instead of the probe.
  4. Store is idempotent and preserves first_seen_ts across re-runs (weather.py
     contract, same COALESCE idiom).

Fixtures are synthetic shapefiles written with pyshp — no network, no archive files.
"""
from __future__ import annotations

import io
import sqlite3
import zipfile
from datetime import date

import pytest

shapefile = pytest.importorskip("shapefile")

from prediction_market_macro.ingest.weather_fcst import (
    _knowledge_time, _point_in_shape, _store, ensure_schema, parse_zip)

# CHI (41.88, -87.63), PHX (33.45, -112.07) — the two cities the fixtures target.
CHI_LON, CHI_LAT = -87.63, 41.88
PHX_LON, PHX_LAT = -112.07, 33.45


def _square(cx, cy, half):
    return [(cx - half, cy - half), (cx - half, cy + half), (cx + half, cy + half),
            (cx + half, cy - half), (cx - half, cy - half)]


def _fixture_zip() -> bytes:
    """Below 33 over the Midwest, Below 50 nested inside it around CHI; Above 33
    around PHX. NYC etc. are in no polygon → EC."""
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    w = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYGON)
    w.field("Fcst_Date", "D")
    w.field("Start_Date", "D")
    w.field("End_Date", "D")
    w.field("Prob", "N", decimal=1)
    w.field("Cat", "C")
    meta = (date(2022, 1, 10), date(2022, 1, 16), date(2022, 1, 20))
    w.poly([_square(CHI_LON, CHI_LAT, 10.0)])
    w.record(*meta, 33.0, "Below")
    w.poly([_square(CHI_LON, CHI_LAT, 2.0)])
    w.record(*meta, 50.0, "Below")
    w.poly([_square(PHX_LON, PHX_LAT, 3.0)])
    w.record(*meta, 33.0, "Above")
    w.close()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("fx.shp", shp.getvalue())
        zf.writestr("fx.shx", shx.getvalue())
        zf.writestr("fx.dbf", dbf.getvalue())
    return buf.getvalue()


def test_innermost_nested_contour_wins_and_outside_is_ec():
    meta, rows = parse_zip(_fixture_zip())
    assert meta == {"fcst": "2022-01-10", "start": "2022-01-16", "end": "2022-01-20"}
    by = {r["region"]: r for r in rows}
    assert by["CHI"] == {"region": "CHI", "cat": "Below", "prob": 50.0}
    assert by["PHX"] == {"region": "PHX", "cat": "Above", "prob": 33.0}
    assert by["NYC"] == {"region": "NYC", "cat": "EC", "prob": None}
    # every city gets exactly one row, always — panel completeness is load-bearing
    assert len(rows) == 14


def test_hole_subtracts():
    class Shape:
        points = _square(0, 0, 10) + _square(0, 0, 3)
        parts = [0, 5]

    assert _point_in_shape(5.0, 5.0, Shape)          # in outer, outside hole
    assert not _point_in_shape(0.0, 0.0, Shape)      # inside the hole
    assert not _point_in_shape(20.0, 20.0, Shape)    # outside everything


def test_knowledge_time_is_after_both_est_and_edt_issuance():
    kt = _knowledge_time("2022-01-10")
    assert kt == "2022-01-10T21:00:00+00:00"
    # 21Z is later than 3PM EST (20Z) and 3PM EDT (19Z) — never before publication
    assert kt > "2022-01-10T20:00:00+00:00"


def test_store_idempotent_preserves_first_seen():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    meta = {"fcst": "2022-01-10", "start": "2022-01-16", "end": "2022-01-20"}
    rows = [{"region": "CHI", "cat": "Below", "prob": 50.0}]
    _store(conn, "610temp", meta, rows, "2026-08-10T00:00:00+00:00")
    _store(conn, "610temp", meta, rows, "2026-08-11T00:00:00+00:00")   # re-run later
    got = conn.execute("SELECT COUNT(*), MAX(first_seen_ts) FROM weather_fcst").fetchone()
    assert got == (1, "2026-08-10T00:00:00+00:00")
