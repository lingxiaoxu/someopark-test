"""ops/clubelo_quality.py — daily data-quality gate for the ClubElo website store.

Runs at the END of the daily refresh (ops/refresh_all) and on demand. It answers one
question with evidence: is what we parsed from clubelo.com today fit to anchor the prior?
Every check names the file it looked at and the number it saw. Verdict levels:
  FAIL — the day's data must not be trusted (markup drift, empty/garbled pages, anchors
         missing, coverage collapse). The refresh prints it loudly and exits non-zero from
         the CLI; the fetch-time gate (clubelo_web.validate_daily) already keeps such a
         parse out of clubelo_<date>.csv, this is the deeper post-mortem.
  WARN — worth a look, not blocking (a country page missed, a club's rating jumped, the
         site identical to yesterday on a matchday).
Writes data/output/clubelo_quality.json (+ the frontend copy is NOT made — internal only).

    python -m prediction_market_soccer.ops.clubelo_quality [--date YYYY-MM-DD]
"""
from __future__ import annotations

import csv
import gzip
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.ingest import clubelo_web as W

# registry coverage a healthy day always reaches (measured 2026-09-02: 19/18/20/9/16/28/31/82)
MIN_COVERAGE = {"epl": 17, "laliga": 16, "seriea": 17, "bundesliga": 8, "ligue1": 14,
                "ucl": 20, "uel": 22, "uecl": 65}
MICRO_STATES = {"SMR", "AND", "GIB", "LIE", "FRO", "KOS", "MLT", "LUX"}   # may have no top-25 array


class _Report:
    def __init__(self, date: str):
        self.date = date
        self.checks: list[dict] = []

    def add(self, name: str, level: str, detail: str, **num):
        self.checks.append({"check": name, "level": level, "detail": detail, **num})

    def ok(self, name, detail="", **num): self.add(name, "OK", detail, **num)
    def warn(self, name, detail, **num): self.add(name, "WARN", detail, **num)
    def fail(self, name, detail, **num): self.add(name, "FAIL", detail, **num)

    @property
    def verdict(self) -> str:
        lv = {c["level"] for c in self.checks}
        return "FAIL" if "FAIL" in lv else ("WARN" if "WARN" in lv else "OK")


def _read_csv(p: Path) -> list[dict]:
    return list(csv.DictReader(p.open(encoding="utf-8")))


def run(date: str | None = None, conn=None) -> dict:
    date = date or W._today()
    R = _Report(date)
    priors = W.PRIORS
    ddir = W.DAILY / date
    yday = (datetime.fromisoformat(date) - timedelta(days=1)).strftime("%Y-%m-%d")

    # 1. the day's fetch exists and the manifest is sane
    man_p = ddir / "manifest.json"
    if not man_p.exists():
        R.fail("daily.present", f"{ddir} has no manifest — no website fetch stored for {date}")
        return _finish(R)
    man = json.loads(man_p.read_text(encoding="utf-8"))
    failed = man.get("countries_failed") or []
    (R.warn if failed else R.ok)("daily.countries", f"{len(man.get('countries_ok') or [])} ok, failed={failed}",
                                 ok=len(man.get("countries_ok") or []), failed=len(failed))
    if len(failed) > 5:
        R.fail("daily.countries_many_failed", f"{len(failed)} country pages failed: {failed}")
    http = man.get("http") or []
    non200 = [h for h in http if h.get("status") not in (200,)]
    (R.warn if non200 else R.ok)("daily.http", f"{len(http)} requests, non-200: {len(non200)}", requests=len(http), non200=len(non200))
    R.ok("daily.elapsed", f"{man.get('elapsed_s')}s", elapsed_s=man.get("elapsed_s"))

    # 2. parsed rows: structure, ranges, anchors, duplicates (the fetch-time gate, re-run)
    rows = W.load_daily(date) or []
    probs = W.validate_daily(rows)
    if probs:
        R.fail("parse.sanity", "; ".join(probs))
    else:
        R.ok("parse.sanity", f"{len(rows)} clubs; anchors in band; ranks/elo/countries valid", clubs=len(rows))
    world = [r for r in rows if r.get("src") == "world"]
    R.ok("parse.world_rows", f"{len(world)} world-table rows", world=len(world)) if len(world) >= 450 else R.warn("parse.world_rows", f"only {len(world)} world rows", world=len(world))
    cs = json.loads((ddir / "countries.json").read_text(encoding="utf-8")) if (ddir / "countries.json").exists() else {}
    empty = [cc for cc in W.EURO_SITE_CODES if len(cs.get(cc) or []) == 0 and cc not in MICRO_STATES]
    (R.warn if empty else R.ok)("parse.country_arrays", f"{sum(1 for cc in W.EURO_SITE_CODES if cs.get(cc))}/55 with rows; empty non-micro: {empty}", empty=len(empty))
    wrong_cc = [(cc, r["slug"]) for cc, rs in cs.items() for r in rs if r.get("site_cc") and r["site_cc"] != cc]
    (R.warn if wrong_cc else R.ok)("parse.country_attribution", f"{len(wrong_cc)} rows whose flag disagrees with the page e.g. {wrong_cc[:3]}", n=len(wrong_cc))

    # 3. raw pages re-parse to the same numbers (parser determinism; markup drift shows here first)
    raw_dir = W.RAW / date
    raws = sorted(raw_dir.glob("*.html.gz")) if raw_dir.exists() else []
    if not raws:
        R.warn("raw.present", "no raw pages stored for the day (re-parse impossible)")
    else:
        try:
            with gzip.open(raw_dir / "home.html.gz", "rt", encoding="utf-8") as f:
                rew = W.parse_world_table(f.read())
            stored = json.loads((ddir / "world.json").read_text(encoding="utf-8"))
            same = {r["slug"]: r["elo"] for r in rew} == {r["slug"]: r["elo"] for r in stored}
            (R.ok if same else R.fail)("raw.reparse_world", f"{len(raws)} raw pages; re-parsed world table {'matches' if same else 'DIFFERS from'} stored", pages=len(raws))
        except Exception as e:  # noqa: BLE001
            R.fail("raw.reparse_world", f"re-parse error: {str(e)[:160]}")

    # 4. day-over-day movement: not frozen, no absurd jumps, stable top-50
    prev = W.load_daily(yday)
    if prev:
        a = {r["slug"]: r["elo"] for r in rows}
        b = {r["slug"]: r["elo"] for r in prev}
        common = [s for s in a if s in b]
        deltas = [a[s] - b[s] for s in common]
        moved = sum(1 for d in deltas if d != 0)
        jumps = [(s, b[s], a[s]) for s in common if abs(a[s] - b[s]) > 60]
        if common and moved == 0:
            R.warn("dod.frozen", f"identical to {yday} for all {len(common)} common clubs (no matches, or the site stopped updating)", common=len(common))
        else:
            R.ok("dod.moved", f"{moved}/{len(common)} clubs moved vs {yday}; max |Δ| {max((abs(d) for d in deltas), default=0)}", moved=moved, common=len(common))
        (R.warn if jumps else R.ok)("dod.jumps", f"{len(jumps)} clubs moved > 60 e.g. {jumps[:3]}", n=len(jumps))
        top_a = {r["slug"] for r in sorted(world, key=lambda r: -r["elo"])[:50]}
        top_b = {r["slug"] for r in sorted([r for r in prev if r.get("src") == "world"], key=lambda r: -r["elo"])[:50]}
        ov = len(top_a & top_b)
        (R.ok if ov >= 44 else R.warn)("dod.top50_overlap", f"{ov}/50 of yesterday's top-50 still in today's", overlap=ov)
        missing = [s for s in b if s not in a and (prev_by := {r["slug"]: r for r in prev})[s].get("src") == "world"]
        (R.warn if len(missing) > 20 else R.ok)("dod.world_missing", f"{len(missing)} yesterday's world clubs absent today e.g. {missing[:3]}", n=len(missing))
    else:
        R.ok("dod", f"no stored day before {date} to compare (first days of the store)")

    # 5. the csv actually produced for the prior
    csv_p = priors / f"clubelo_{date}.csv"
    src = W._read_source(date)
    if not csv_p.exists():
        R.fail("csv.present", f"clubelo_{date}.csv missing")
        api_rows = []
    else:
        api_rows = _read_csv(csv_p)
        hdr = list(api_rows[0].keys()) if api_rows else []
        (R.ok if hdr == ["Rank", "Club", "Country", "Level", "Elo", "From", "To"] else R.fail)("csv.schema", f"header {hdr}")
        (R.ok if src == "web" else R.warn)("csv.source", f"source={src}", )
        n = len(api_rows)
        (R.ok if n >= 800 else R.fail)("csv.rows", f"{n} rows", rows=n)
        try:
            elos = [float(r["Elo"]) for r in api_rows]
            (R.ok if all(900 <= e <= 2400 for e in elos) else R.fail)("csv.elo_range", f"min {min(elos):.0f} max {max(elos):.0f}")
        except ValueError as e:
            R.fail("csv.elo_parse", str(e)[:120])
        names = [r["Club"] for r in api_rows]
        dup = {x for x in names if names.count(x) > 1}
        (R.ok if not dup else R.fail)("csv.unique_clubs", f"duplicates: {sorted(dup)[:5]}" if dup else "all club names unique")
        api_codes = {W.SITE_TO_API_CODE.get(c, c) for c in W.SITE_COUNTRY_CODES}
        badcc = {r["Country"] for r in api_rows if r["Country"] and r["Country"] not in api_codes}
        (R.ok if not badcc else R.warn)("csv.country_codes", f"unknown codes: {sorted(badcc)[:6]}" if badcc else "all country codes known")
        # the anchor clubs under their API names
        by = {r["Club"]: float(r["Elo"]) for r in api_rows}
        miss = [c for c in ("Liverpool", "Real Madrid", "Bayern", "Arsenal", "Barcelona", "Man City", "Paris SG") if c not in by]
        (R.ok if not miss else R.fail)("csv.anchor_names", f"missing under API naming: {miss}" if miss else "all anchor clubs present under API names")

    # 6. registry coverage per competition (what the prior will actually get)
    try:
        from prediction_market_soccer.ingest import store
        from prediction_market_soccer.ingest.club_prior import _match_elo
        conn = conn or store.init_db()
        cov = {}
        for comp, floor in MIN_COVERAGE.items():
            reg = [dict(r) for r in conn.execute("SELECT club_id, api_team_id, name FROM club_registry WHERE comp=?", (comp,))]
            got = len(_match_elo(api_rows, reg, comp)) if (api_rows and reg) else 0
            cov[comp] = (got, len(reg))
            (R.ok if got >= floor else R.fail)(f"coverage.{comp}", f"{got}/{len(reg)} clubs with Elo (floor {floor})", got=got, registry=len(reg))
    except Exception as e:  # noqa: BLE001
        R.warn("coverage", f"could not compute: {str(e)[:120]}")

    # 7. name map health
    nm = W.load_name_map()
    mapped = {v["api"] for v in nm.values() if v.get("api")}
    ref = W._api_file("2026-08-31")
    if ref is not None:
        api_names = {r["Club"] for r in _read_csv(ref)}
        n_ok = len(mapped & api_names)
        (R.ok if n_ok >= 480 else R.warn)("namemap.coverage", f"{n_ok}/{len(api_names)} API names mapped from the site", mapped=n_ok)
    dupe_api = [a for a in mapped if sum(1 for v in nm.values() if v.get("api") == a) > 1]
    (R.ok if not dupe_api else R.fail)("namemap.unique", f"API names claimed by two slugs: {dupe_api[:5]}" if dupe_api else "no API name claimed twice")
    # a suffix is only wrong on a club the API knows (a real big club lost its API name);
    # an unmapped lower-division club sharing a name (CD Guadalajara ESP) is meant to carry one
    api_known, _ = W._api_reference()
    leak = [r["Club"] for r in api_rows if "(" in r["Club"] and r["Club"].split(" (")[0] in api_known
            and api_known[r["Club"].split(" (")[0]]["country"] == r["Country"]]
    (R.ok if not leak else R.fail)("namemap.suffix_leak", f"API clubs that lost their name to a suffix: {leak[:5]}" if leak else "no API club carries a disambiguation suffix")

    # 8. histories: count, ordering, freshness vs today's table
    hist = W.load_histories()
    (R.ok if len(hist) >= 450 else R.warn)("history.count", f"{len(hist)} club histories stored", n=len(hist))
    unordered = [s for s, pts in hist.items() if any(pts[i]["date"] > pts[i + 1]["date"] for i in range(len(pts) - 1))]
    (R.ok if not unordered else R.fail)("history.ordered", f"unordered series: {unordered[:3]}" if unordered else "all series date-ascending")
    by_slug = {r["slug"]: r for r in rows}
    drift = []
    for s, pts in hist.items():
        r = by_slug.get(s)
        if not r or not pts:
            continue
        last = pts[-1]
        # the club page's chart can trail the table by a matchday (a point dated ≤2 days ago
        # should agree with the table; a larger gap on such a fresh point means the page we
        # stored is not this club's)
        if (datetime.fromisoformat(date) - datetime.fromisoformat(last["date"])).days <= 2 and abs(last["elo"] - r["elo"]) > 15.0:
            drift.append((s, round(last["elo"], 1), r["elo"]))
    (R.ok if len(drift) <= 3 else R.warn)("history.vs_table", f"{len(drift)} fresh histories (≤2d) disagree with today's table by > 15 e.g. {drift[:3]}", n=len(drift))

    # 9. reconstruction sidecars for the freeze period (one-off data, must stay intact)
    bad_prov = []
    for d in ("2026-07-06", "2026-08-01", "2026-08-31"):
        p = priors / f"clubelo_{d}.csv"
        s_ = W._read_source(d)
        if not p.exists() or s_ != "web_history" or not (priors / f"clubelo_{d}.provenance.json").exists() or not (priors / f"clubelo_{d}.csv.frozen_api").exists():
            bad_prov.append((d, s_))
    (R.ok if not bad_prov else R.fail)("reconstruction.intact", f"freeze-period files missing/altered: {bad_prov}" if bad_prov else "freeze-period reconstructions, provenance and API backups all present")

    # 10. backup mirror has today
    mirror = W.BACKUP / "daily" / date / "world.json"
    (R.ok if mirror.exists() else R.warn)("backup.mirror", f"{mirror} {'present' if mirror.exists() else 'MISSING'}")
    return _finish(R)


def _finish(R: _Report) -> dict:
    doc = {"date": R.date, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "verdict": R.verdict, "n_fail": sum(1 for c in R.checks if c["level"] == "FAIL"),
           "n_warn": sum(1 for c in R.checks if c["level"] == "WARN"), "checks": R.checks}
    CONFIG.paths.ensure()
    (CONFIG.paths.output / "clubelo_quality.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return doc


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="ClubElo website data-quality gate")
    ap.add_argument("--date", default=None)
    a = ap.parse_args()
    doc = run(a.date)
    for c in doc["checks"]:
        mark = {"OK": "✓", "WARN": "△", "FAIL": "✗"}[c["level"]]
        print(f"  {mark} {c['check']:28s} {c['detail']}")
    print(f"[clubelo_quality] {doc['date']}: {doc['verdict']} ({doc['n_fail']} fail, {doc['n_warn']} warn)")
    sys.exit(2 if doc["verdict"] == "FAIL" else 0)


if __name__ == "__main__":
    main()
