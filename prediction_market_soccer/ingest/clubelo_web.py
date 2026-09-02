"""ingest/clubelo_web.py — ClubElo ratings from the WEBSITE, the primary source since 2026-09-02.

WHY THIS EXISTS
    api.clubelo.com — the CSV endpoint club_prior has always read — was found on 2026-09-01 to
    have served a FROZEN snapshot since early July (594 clubs, 0 changes across 8 weeks of
    fixtures) before failing outright with 502s. The website itself is alive: its tables and
    per-club history charts agree with each other and move with results. So the order is now
    WEBSITE FIRST, API only as the fallback, with a freeze detector on whatever answers.

WHAT THE SITE EXPOSES (measured 2026-09-01, all pages ~570 KB)
    * Every page carries the same WORLD TABLE — HTML <tr> rows for the ~493 strongest clubs:
      country link (/GER), club link (/Bayern), rank, name, integer Elo.
    * A COUNTRY page (/NOR, /ROU, …) adds a JS array literal with that country's top-25
      (small clubs the world table lacks): ['<td …><a href="/Aalesund">Aalesund…</td>', '1364', …].
    * A CLUB page (/Liverpool) embeds a Vega-Lite spec whose dataset is the full rating history
      [{Date, Elo, …}] at full precision. Only some clubs have one (others 302 to /).
    Site naming keeps diacritics (Bayern München, Bodø/Glimt); the API transliterates (Bayern,
    Bodoe Glimt). Country codes differ in three systems: site links (GER, SUI, ROU, SVK…), flag
    files (ISO-3: deu, che), API (GER, SUI, ROM, SLK, BHZ, FAR, LAT, LIT, MAC, MNT, MOL).

WHAT THIS MODULE GUARANTEES DOWNSTREAM
    club_prior keeps reading data/priors/clubelo_<date>.csv in the API's own schema
    (Rank,Club,Country,Level,Elo,From,To) with Club in the API's naming and Country in the
    API's codes — so the prior, its alias tables and _match_elo are untouched whichever
    source produced the file. A sidecar clubelo_<date>.source records the provenance.

WHERE THE DATA LIVES (nothing is fetched twice, nothing is thrown away)
    data/priors/clubelo_web/
      daily/<date>/world.json        parsed world table (slug, site name, site country, rank, elo)
      daily/<date>/countries.json    parsed country top-25 arrays, all European pages
      daily/<date>/manifest.json     what was fetched, http codes, counts, timings
      raw/<date>/<page>.html.gz      the pages themselves (gzip), so a parser fix can re-read a day
      history/<slug>.json            club-page rating history [{date, elo}], refreshed weekly
      history/_missing.json          slugs whose club page redirects (no history available)
      name_map.json                  site slug → API name/country, with the method per entry
      README.md                      this layout, for whoever finds the directory later
    ~/clubelo_web_backup/            a mirror of daily/ and history/ (same policy as macro.db)

POLITENESS
    One session, a real User-Agent, ~1 request/second, two attempts, connect 8s / read 60s.
    A full European sweep is ~56 pages ≈ 1 minute; history is fetched once and refreshed
    weekly. The site is one person's project — stay well under anything it would notice.
"""
from __future__ import annotations

import csv
import io
import json
import re
import shutil
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

import requests

from prediction_market_soccer.config import CONFIG

BASE = "https://clubelo.com"
PRIORS = CONFIG.paths.priors            # module-level so tests can point it at a tmp dir
BACKUP = Path.home() / "clubelo_web_backup"


class _P:
    """Every store path derived from PRIORS at call time, so a test that patches PRIORS
    isolates ALL writes (daily/, raw/, history/, name_map) — not just the csv files."""
    @property
    def ROOT(self) -> Path: return PRIORS / "clubelo_web"
    @property
    def DAILY(self) -> Path: return self.ROOT / "daily"
    @property
    def RAW(self) -> Path: return self.ROOT / "raw"
    @property
    def HIST(self) -> Path: return self.ROOT / "history"
    @property
    def NAME_MAP(self) -> Path: return self.ROOT / "name_map.json"


_paths = _P()


def __getattr__(name: str):
    if name in ("ROOT", "DAILY", "RAW", "HIST", "NAME_MAP"):
        return getattr(_paths, name)
    raise AttributeError(name)
_UA = "someopark-soccer/1.0 (club-football research; polite scraper; 1 req/s)"
_PACE_S = 0.9
_HIST_MAX_AGE_DAYS = 7

# The 55 UEFA members as the SITE spells them in URLs (verified: /ROU 200, /ROM 302).
EURO_SITE_CODES = [
    "ALB", "AND", "ARM", "AUT", "AZE", "BLR", "BEL", "BIH", "BUL", "CRO", "CYP", "CZE", "DEN",
    "ENG", "EST", "FRO", "FIN", "FRA", "GEO", "GER", "GIB", "GRE", "HUN", "ISL", "IRL", "ISR",
    "ITA", "KAZ", "KOS", "LVA", "LIE", "LTU", "LUX", "MLT", "MDA", "MNE", "NED", "NIR", "MKD",
    "NOR", "POL", "POR", "ROU", "RUS", "SMR", "SCO", "SRB", "SVK", "SVN", "ESP", "SWE", "SUI",
    "TUR", "UKR", "WAL",
]
# Every country code the site links with a flag (90, measured 2026-09-01). A 3-letter
# upper-case href is a COUNTRY only if it is one of these; "AEK", "AIK", "PSV", "QPR",
# "IDV" are clubs and the first parser dropped them as if they were countries.
SITE_COUNTRY_CODES = frozenset(EURO_SITE_CODES) | frozenset("""
ALG ARG AUS BOL BRA CHI CHN COD COK COL CRC ECU EGY HON IRN JPN KOR KSA MAR MAS MEX NZL PAR PER
QAT RSA SDN SOL THA TUN UAE URU USA UZB VAN VEN""".split())

# site URL code → API CSV code (identical unless listed)
SITE_TO_API_CODE = {"ROU": "ROM", "SVK": "SLK", "BIH": "BHZ", "FRO": "FAR", "LVA": "LAT",
                    "LTU": "LIT", "MKD": "MAC", "MNE": "MNT", "MDA": "MOL"}
API_TO_SITE_CODE = {v: k for k, v in SITE_TO_API_CODE.items()}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> str:
    return _now().strftime("%Y-%m-%d")


# ── HTTP ─────────────────────────────────────────────────────────────────────
class _Site:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = _UA
        self._last = 0.0
        self.log: list[dict] = []

    def get(self, path: str, *, allow_redirect: bool = False) -> tuple[int, str]:
        """(status, body). Club pages that do not exist 302 to '/' — reported as 302, not
        followed, so a missing club can never be mistaken for the homepage."""
        wait = _PACE_S - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        url = f"{BASE}{path}"
        last = None
        for attempt in range(2):
            t0 = time.time()
            try:
                r = self.s.get(url, timeout=(8, 60), allow_redirects=allow_redirect)
                self._last = time.time()
                self.log.append({"path": path, "status": r.status_code, "bytes": len(r.content),
                                 "s": round(time.time() - t0, 2)})
                if r.status_code >= 500:
                    last = RuntimeError(f"HTTP {r.status_code} for {path}")
                    time.sleep(1.5)
                    continue
                return r.status_code, (r.text if r.status_code == 200 else "")
            except requests.RequestException as e:
                last = e
                self._last = time.time()
                time.sleep(1.5)
        self.log.append({"path": path, "status": None, "error": str(last)[:120]})
        raise RuntimeError(f"clubelo.com fetch failed for {path}: {last}")


# ── parsing ──────────────────────────────────────────────────────────────────
_SLUG_RE = re.compile(r"^/([A-Za-z][A-Za-z0-9_'.-]*)$")


def _is_country_href(h: str) -> bool:
    m = re.fullmatch(r"/([A-Z]{3})", h or "")
    return bool(m) and m.group(1) in SITE_COUNTRY_CODES


class _Rows(HTMLParser):
    """Collect <tr> rows: their <td> (class, text), the hrefs inside, and the club-name span."""

    def __init__(self):
        super().__init__()
        self.rows: list[dict] = []
        self.cur: dict | None = None
        self.in_td = False
        self.td_cls = None
        self.txt = ""
        self.span = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self.cur = {"cells": [], "links": [], "ast": "", "small": ""}
        elif tag == "td" and self.cur is not None:
            self.in_td, self.td_cls, self.txt = True, a.get("class"), ""
        elif tag == "a" and self.cur is not None and self.in_td:
            self.cur["links"].append(a.get("href", ""))
        elif tag == "span" and self.in_td:
            self.span = a.get("class")
        elif tag == "small" and self.in_td:
            self.span = "small"

    def handle_data(self, d):
        if self.in_td and self.cur is not None:
            self.txt += d
            if self.span == "Ast":
                self.cur["ast"] += d
            elif self.span == "small":
                self.cur["small"] += d

    def handle_endtag(self, tag):
        if tag in ("span", "small"):
            self.span = None
        elif tag == "td" and self.cur is not None:
            self.cur["cells"].append((self.td_cls, " ".join(self.txt.split())))
            self.in_td = False
        elif tag == "tr" and self.cur is not None:
            self.rows.append(self.cur)
            self.cur = None


def parse_world_table(html: str) -> list[dict]:
    """The HTML world table present on every page → [{slug, name, site_cc, rank, elo}]."""
    p = _Rows()
    p.feed(html)
    out, seen = [], set()
    for r in p.rows:
        clubs = [m.group(1) for h in r["links"] if (m := _SLUG_RE.match(h)) and not _is_country_href(h)]
        ccs = [h[1:] for h in r["links"] if _is_country_href(h)]
        elos = [c for cls, c in r["cells"] if cls == "r" and re.fullmatch(r"\d{3,4}", c)]
        if not (clubs and elos):
            continue
        slug = clubs[0]
        if slug in seen:
            continue
        name = " ".join(r["ast"].split())
        if not name:  # rows without the Ast span: cell text minus the leading rank
            lcell = next((c for cls, c in r["cells"] if cls == "l" and c), "")
            name = re.sub(r"^\s*\d+\s+", "", lcell).strip()
        if not name:
            continue
        rank = int(r["small"].strip()) if r["small"].strip().isdigit() else None
        seen.add(slug)
        out.append({"slug": slug, "name": name, "site_cc": (ccs[0] if ccs else None),
                    "rank": rank, "elo": int(elos[0])})
    return out


_JS_ROW = re.compile(r"\['<td class=\"l\">(.*?)</td>',\s*'(\d+)',\s*'([^']*)',\s*'([^']*)'\]", re.S)


def parse_country_array(html: str) -> list[dict]:
    """The country page's JS top-25 array → [{slug, name, site_cc, elo}] (rank absent)."""
    out, seen = [], set()
    for cell, elo, _chg, _x in _JS_ROW.findall(html):
        links = re.findall(r'href="/([^"]+)"', cell)
        ccs = [l for l in links if _is_country_href("/" + l)]
        clubs = [l for l in links if not _is_country_href("/" + l) and _SLUG_RE.match("/" + l)]
        names = re.findall(r'href="/(?:[^"]+)"[^>]*>([^<]+)<', cell)
        if not (clubs and names):
            continue
        slug = clubs[0]
        if slug in seen:
            continue
        seen.add(slug)
        out.append({"slug": slug, "name": " ".join(names[-1].split()), "site_cc": (ccs[0] if ccs else None),
                    "elo": int(elo)})
    return out


def parse_history(html: str) -> list[dict]:
    """The club page's Vega-Lite dataset → [{date: 'YYYY-MM-DD', elo: float}] ascending."""
    i = html.find("var vegaJson")
    if i < 0:
        return []
    j = html.find("{", i)
    depth = 0
    blob = None
    for k in range(j, len(html)):
        ch = html[k]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                blob = html[j:k + 1]
                break
    if not blob:
        return []
    spec = json.loads(blob)
    for _name, arr in (spec.get("datasets") or {}).items():
        if isinstance(arr, list) and arr and isinstance(arr[0], dict) and "Elo" in arr[0]:
            pts = [{"date": str(p["Date"])[:10], "elo": float(p["Elo"])} for p in arr if p.get("Elo") is not None]
            pts.sort(key=lambda p: p["date"])
            return pts
    return []


# ── sanity gate at the point of use ─────────────────────────────────────────
ANCHOR_CLUBS = {"liverpool": (1750, 2150), "realmadrid": (1750, 2150), "bayern": (1800, 2200),
                "arsenal": (1750, 2150), "barcelona": (1750, 2150), "mancity": (1750, 2150),
                "parissg": (1750, 2150)}       # norm_name of site name OR slug; the band a top club sits in


def validate_daily(rows: list[dict]) -> list[str]:
    """Problems that mean the site's markup changed or a page came back wrong. Empty list =
    usable. Called before a website fetch is allowed to become clubelo_<date>.csv; the same
    checks run again, more thoroughly, in ops/clubelo_quality."""
    probs: list[str] = []
    if not rows:
        return ["no rows"]
    world = [r for r in rows if r.get("src") == "world"]
    if len(world) < 400:
        probs.append(f"world table {len(world)} rows (< 400)")
    slugs = [r["slug"] for r in rows]
    if len(slugs) != len(set(slugs)):
        probs.append("duplicate slugs")
    by_key: dict[str, dict] = {}
    for r in rows:
        by_key.setdefault(norm_name(r.get("name", "")), r)
        by_key.setdefault(norm_name(r["slug"]), r)
    for key, (lo, hi) in ANCHOR_CLUBS.items():
        r = by_key.get(key)
        # big-5 clubs only: a same-named foreign club (Barcelona SC, Arsenal de Sarandí) must not stand in
        if r is not None and r.get("site_cc") not in ("ENG", "ESP", "GER", "ITA", "FRA"):
            r = next((x for x in rows if norm_name(x.get("name", "")) == key and x.get("site_cc") in ("ENG", "ESP", "GER", "ITA", "FRA")), None)
        if r is None:
            probs.append(f"anchor club {key} missing")
        elif not (lo <= r["elo"] <= hi):
            probs.append(f"anchor club {key} elo {r['elo']} outside [{lo},{hi}]")
    bad_elo = [r["slug"] for r in rows if not (900 <= int(r["elo"]) <= 2400)]
    if bad_elo:
        probs.append(f"{len(bad_elo)} rows with elo outside [900,2400] e.g. {bad_elo[:3]}")
    bad_name = [r["slug"] for r in rows if not r.get("name") or "<" in r["name"] or "&" in r["name"] and ";" in r["name"]]
    if bad_name:
        probs.append(f"{len(bad_name)} rows with empty/HTML names e.g. {bad_name[:3]}")
    bad_cc = [r["slug"] for r in rows if r.get("site_cc") and r["site_cc"] not in SITE_COUNTRY_CODES]
    if bad_cc:
        probs.append(f"{len(bad_cc)} rows with unknown country e.g. {bad_cc[:3]}")
    ranks = sorted(r["rank"] for r in world if r.get("rank") is not None)
    if ranks and (ranks[0] != 1 or len(ranks) < 0.9 * len(world)):
        probs.append(f"world ranks look wrong (first {ranks[0]}, {len(ranks)}/{len(world)} ranked)")
    euro = {r["site_cc"] for r in rows if r.get("src", "").startswith("country:")}
    if len(euro) < 40:
        probs.append(f"country arrays parsed for only {len(euro)} countries (< 40)")
    return probs


# ── naming: site → API ───────────────────────────────────────────────────────
_TRANSLIT = str.maketrans({"ø": "oe", "ö": "oe", "ü": "ue", "ä": "ae", "æ": "ae", "å": "aa",
                           "Ø": "Oe", "Ö": "Oe", "Ü": "Ue", "Ä": "Ae", "Æ": "Ae", "Å": "Aa", "ß": "ss"})


def norm_name(s: str) -> str:
    """API-style key: the API transliterates ö/ø/ü → oe/oe/ue (Bodoe Glimt, Malmoe, Zuerich,
    Fuerth); everything else is accent-stripped; case and punctuation dropped."""
    s = (s or "").translate(_TRANSLIT)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]", "", s.lower())


# Site spellings the normaliser cannot reach the API name from (both verified in the two
# sources' own rows). Keep short; the value/fuzzy passes below handle the long tail.
MANUAL_SITE_TO_API = {
    "Internazionale": "Inter", "Atlético": "Atletico", "Athletic": "Bilbao", "Man United": "Man United",
    "St Gillis": "Union SG", "Forest": "Forest", "Bodø/Glimt": "Bodoe Glimt", "Malmö": "Malmoe",
    "Zürich": "Zuerich", "Fürth": "Fuerth", "Köln": "Koeln", "Düsseldorf": "Duesseldorf",
    "Mönchengladbach": "Gladbach", "Sociedad": "Sociedad", "Bayern München": "Bayern",
    "Athletic Club": "Bilbao", "Başakşehir": "Bueyueksehir", "AEK Athens": "AEK", "AEK Athina": "AEK",
    "Eintracht Braunschweig": "Braunschweig", "Braunschweig": "Braunschweig",
}


def _api_reference() -> tuple[dict[str, dict], dict[str, dict[str, float]]]:
    """(name → {country}) from the last LIVE API snapshot and the frozen one, plus the API's
    per-date values on a few dates the API was still alive (for value-proximity matching)."""
    names: dict[str, dict] = {}
    by_date: dict[str, dict[str, float]] = {}
    for d in ("2026-05-31", "2026-08-31", "2026-04-13", "2026-03-13"):
        p = _api_file(d)
        if p is None:
            continue
        rows = list(csv.DictReader(p.open(encoding="utf-8")))
        by_date[d] = {r["Club"]: float(r["Elo"]) for r in rows}
        for r in rows:
            names.setdefault(r["Club"], {"country": r["Country"]})
    return names, by_date


def _api_file(d: str) -> Path | None:
    """The API's OWN csv for a date: the .frozen_api backup once a reconstruction replaced the
    original, else the csv if its provenance is the API (no sidecar = pre-website era file).
    Never a web / web_history / web_stale file — those carry site names and must not seed
    the "API naming" reference (the first build did exactly that through the rebuilt 08-31)."""
    bak = PRIORS / f"clubelo_{d}.csv.frozen_api"
    if bak.exists():
        return bak
    p = PRIORS / f"clubelo_{d}.csv"
    if p.exists() and _read_source(d) in (None, "api", "api_frozen"):
        return p
    return None


def latest_api_rows() -> tuple[str, list[dict]] | None:
    """The most recent snapshot the API itself produced (for the freeze comparison)."""
    dates = sorted({p.name[8:18] for p in PRIORS.glob("clubelo_????-??-??.csv*")}, reverse=True)
    for d in dates:
        f = _api_file(d)
        if f is not None:
            return d, list(csv.DictReader(f.open(encoding="utf-8")))
    return None


def build_name_map(site_rows: list[dict], *, histories: dict[str, list[dict]] | None = None) -> dict:
    """site slug → {api, site, country_api, method}. Country-scoped; two passes:
       1. normalised name equality / manual alias (exact, no guessing);
       2. difflib ≥ 0.86 within the country, unique (audited entry by entry).
    Unmapped slugs keep their site name — _match_elo's own fuzzy pass still sees them."""
    import difflib
    api_names, api_by_date = _api_reference()
    api_by_cc: dict[str, list[str]] = {}
    for n, info in api_names.items():
        api_by_cc.setdefault(info["country"], []).append(n)
    # EVERY pass is scoped to the club's country. The first version keyed the exact-name
    # pass globally and ClubElo's world table carries a Uruguayan "Liverpool" and a
    # Portuguese/Uruguayan "Nacional": the wrong club took the API name first and the
    # cross-validation against the live-API dates read a 360-point error on Liverpool.
    api_norm_by_cc: dict[str, dict[str, str]] = {}
    for n, info in api_names.items():
        api_norm_by_cc.setdefault(info["country"], {})[norm_name(n)] = n
    out: dict = {}
    taken: set[str] = set()
    live_dates = [d for d in api_by_date if d != "2026-08-31"]
    # world-table clubs first (they carry the rank and are the ones with histories), so a
    # top club can never lose its API name to a lower one of the same normalised spelling
    ordered = sorted(site_rows, key=lambda r: (r.get("rank") is None, r.get("rank") or 10**6))
    for r in ordered:
        slug, site_name = r["slug"], r["name"]
        cc_api = SITE_TO_API_CODE.get(r.get("site_cc") or "", r.get("site_cc") or "")
        cands = [n for n in api_by_cc.get(cc_api, []) if n not in taken]
        api = None
        method = None
        manual = MANUAL_SITE_TO_API.get(site_name)
        norm_cc = api_norm_by_cc.get(cc_api, {})
        if manual and manual in cands:
            api, method = manual, "manual"
        elif norm_name(site_name) in norm_cc and norm_cc[norm_name(site_name)] in cands:
            api, method = norm_cc[norm_name(site_name)], "norm"
        elif norm_name(slug) in norm_cc and norm_cc[norm_name(slug)] in cands:
            api, method = norm_cc[norm_name(slug)], "norm_slug"
        # (A value-proximity pass — match by rating level on dates the API was live — was
        # tried here and REMOVED: audited 4/4 wrong (Athletic Club→Getafe, Vicenza→Pisa…);
        # with the site/API scales 46 Elo apart in sd, rating level cannot identify a club.)
        if api is None and cands:
            best = difflib.get_close_matches(site_name, cands, n=2, cutoff=0.86)
            if len(best) == 1 or (len(best) == 2 and difflib.SequenceMatcher(None, site_name, best[0]).ratio()
                                  - difflib.SequenceMatcher(None, site_name, best[1]).ratio() > 0.08):
                api, method = best[0], "fuzzy"
        if api is not None:
            taken.add(api)
        out[slug] = {"api": api, "site": site_name, "country_api": cc_api, "method": method}
    return out


# ── storage helpers ──────────────────────────────────────────────────────────
def _write_json(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


def _mirror(src: Path) -> None:
    """Copy one file under data/priors/clubelo_web/ into ~/clubelo_web_backup/ (same relative path)."""
    try:
        rel = src.relative_to(_paths.ROOT)
        dst = BACKUP / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    except Exception:  # noqa: BLE001 — the backup must never break the fetch
        pass


def _save_raw(date: str, name: str, html: str) -> None:
    """Keep the page itself (gzip, ~90 KB) so a parser fix can re-read a past day without
    asking the site again. raw/<date>/<name>.html.gz; mirrored like everything else."""
    import gzip
    p = _paths.RAW / date / f"{name}.html.gz"
    p.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(p, "wt", encoding="utf-8") as f:
        f.write(html)
    _mirror(p)


def reparse_daily(date: str) -> list[dict] | None:
    """Rebuild daily/<date>/ from the stored raw pages (after a parser change)."""
    import gzip
    home = _paths.RAW / date / "home.html.gz"
    if not home.exists():
        return None
    with gzip.open(home, "rt", encoding="utf-8") as f:
        world = parse_world_table(f.read())
    merged = {r["slug"]: {**r, "src": "world"} for r in world}
    country_rows: dict[str, list[dict]] = {}
    for p in sorted((_paths.RAW / date).glob("*.html.gz")):
        cc = p.name[:-8]
        if cc == "home":
            continue
        with gzip.open(p, "rt", encoding="utf-8") as f:
            rows = parse_country_array(f.read())
        country_rows[cc] = rows
        for r in rows:
            r = {**r, "site_cc": r.get("site_cc") or cc}
            if r["slug"] not in merged:
                merged[r["slug"]] = {**r, "rank": None, "src": f"country:{cc}"}
    ddir = _paths.DAILY / date
    _write_json(ddir / "world.json", world)
    _write_json(ddir / "countries.json", country_rows)
    for f in ("world.json", "countries.json"):
        _mirror(ddir / f)
    return list(merged.values())


def _ensure_readme() -> None:
    p = _paths.ROOT / "README.md"
    if p.exists():
        return
    _paths.ROOT.mkdir(parents=True, exist_ok=True)
    body = __doc__.split("WHERE THE DATA LIVES")[1].split("POLITENESS")[0]
    body = body.split("\n", 1)[1] if "\n" in body else body       # drop the heading's own line
    p.write_text("# ClubElo website data store\n\nWhat is here and why (nothing is fetched twice, "
                 "nothing is thrown away):\n\n" + body.strip("\n") + "\n\n"
                 "See prediction_market_soccer/ingest/clubelo_web.py for the code and the full story.\n",
                 encoding="utf-8")


# ── daily fetch ──────────────────────────────────────────────────────────────
def fetch_daily(date: str | None = None, *, site: _Site | None = None,
                countries: list[str] | None = None) -> list[dict]:
    """Homepage world table + every European country's top-25 → merged rows, stored under
    daily/<date>/ and mirrored. Returns [{slug, name, site_cc, rank, elo, src}]."""
    date = date or _today()
    site = site or _Site()
    _ensure_readme()
    t0 = time.time()
    # single-flight per date: the first process sweeps the site (~70s); any other process
    # asking for the same date waits for its files instead of running its own 56 requests
    lock = _paths.DAILY / date / ".fetching"
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists() and (time.time() - lock.stat().st_mtime) < 300:
        for _ in range(30):
            time.sleep(5)
            got = load_daily(date)
            if got:
                return got
        raise RuntimeError(f"clubelo.com fetch for {date} is running in another process and did not finish")
    lock.write_text(str(time.time()), encoding="utf-8")
    try:
        status, html = site.get("/")
        if status != 200 or not html:
            raise RuntimeError(f"clubelo.com homepage HTTP {status}")
        _save_raw(date, "home", html)
        world = parse_world_table(html)
        if len(world) < 300:
            raise RuntimeError(f"clubelo.com world table parsed only {len(world)} rows — layout changed?")
        merged: dict[str, dict] = {r["slug"]: {**r, "src": "world"} for r in world}
        country_rows: dict[str, list[dict]] = {}
        failed: list[str] = []
        for cc in (countries or EURO_SITE_CODES):
            try:
                st, body = site.get(f"/{cc}")
            except RuntimeError:
                failed.append(cc)
                continue
            if st != 200 or not body:
                failed.append(cc)
                continue
            _save_raw(date, cc, body)
            rows = parse_country_array(body)
            country_rows[cc] = rows
            for r in rows:
                r = {**r, "site_cc": r.get("site_cc") or cc}
                if r["slug"] not in merged:
                    merged[r["slug"]] = {**r, "rank": None, "src": f"country:{cc}"}
        ddir = _paths.DAILY / date
        _write_json(ddir / "world.json", world)
        _write_json(ddir / "countries.json", country_rows)
        _write_json(ddir / "manifest.json", {
            "fetched_at": _now().isoformat(timespec="seconds"), "world_rows": len(world),
            "countries_ok": sorted(country_rows), "countries_failed": failed,
            "merged_rows": len(merged), "elapsed_s": round(time.time() - t0, 1), "http": site.log})
        for f in ("world.json", "countries.json", "manifest.json"):
            _mirror(ddir / f)
        if failed:
            print(f"[clubelo_web] {date}: {len(failed)} country page(s) failed: {failed}")
        print(f"[clubelo_web] {date}: world {len(world)} + countries → {len(merged)} clubs in {time.time() - t0:.0f}s")
        return list(merged.values())
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def load_daily(date: str) -> list[dict] | None:
    ddir = _paths.DAILY / date
    if not (ddir / "world.json").exists():
        return None
    world = json.loads((ddir / "world.json").read_text(encoding="utf-8"))
    merged = {r["slug"]: {**r, "src": "world"} for r in world}
    try:
        for cc, rows in json.loads((ddir / "countries.json").read_text(encoding="utf-8")).items():
            for r in rows:
                if r["slug"] not in merged:
                    merged[r["slug"]] = {**r, "site_cc": r.get("site_cc") or cc, "rank": None, "src": f"country:{cc}"}
    except OSError:
        pass
    return list(merged.values())


# ── history ──────────────────────────────────────────────────────────────────
def fetch_history(slug: str, *, site: _Site | None = None, force: bool = False) -> list[dict] | None:
    """Club-page rating history, cached under history/<slug>.json (refreshed weekly).
    None when the club has no page (recorded in history/_missing.json so it is not re-probed
    for a week)."""
    site = site or _Site()
    _paths.HIST.mkdir(parents=True, exist_ok=True)
    p = _paths.HIST / f"{slug}.json"
    if p.exists() and not force:
        doc = json.loads(p.read_text(encoding="utf-8"))
        age = _now() - datetime.fromisoformat(doc["fetched_at"])
        if age < timedelta(days=_HIST_MAX_AGE_DAYS):
            return doc["points"]
    missing_p = _paths.HIST / "_missing.json"
    missing = json.loads(missing_p.read_text(encoding="utf-8")) if missing_p.exists() else {}
    if not force and slug in missing:
        try:
            if _now() - datetime.fromisoformat(missing[slug]) < timedelta(days=_HIST_MAX_AGE_DAYS):
                return None
        except ValueError:
            pass
    from urllib.parse import quote
    st, html = site.get("/" + quote(slug, safe="'._-"))
    if st != 200 or not html:
        missing[slug] = _now().isoformat(timespec="seconds")
        _write_json(missing_p, missing)
        return None
    pts = parse_history(html)
    if not pts:
        missing[slug] = _now().isoformat(timespec="seconds")
        _write_json(missing_p, missing)
        return None
    _write_json(p, {"slug": slug, "fetched_at": _now().isoformat(timespec="seconds"), "points": pts})
    _mirror(p)
    return pts


def refresh_histories(*, max_age_days: int = _HIST_MAX_AGE_DAYS, limit: int | None = None) -> dict:
    """Refresh every stored club history older than ``max_age_days`` (and probe slugs that
    appear in the latest daily tables but have no history yet, once a week). Meant for the
    daily refresh: self-throttled by a marker so the ~10-minute sweep runs once a week."""
    marker = _paths.ROOT / ".histories_refreshed"
    try:
        if marker.exists() and (_now() - datetime.fromisoformat(marker.read_text(encoding="utf-8").strip())) < timedelta(days=max_age_days):
            return {"skipped": "fresh", "marker": marker.read_text(encoding="utf-8").strip()}
    except ValueError:
        pass
    slugs: list[str] = []
    latest = sorted(_paths.DAILY.glob("*")) if _paths.DAILY.exists() else []
    if latest:
        rows = load_daily(latest[-1].name) or []
        slugs = [r["slug"] for r in rows]
    if not slugs:
        slugs = [p.stem for p in _paths.HIST.glob("*.json") if not p.name.startswith("_")]
    site = _Site()
    ok = miss = err = 0
    t0 = time.time()
    for i, slug in enumerate(slugs[:limit] if limit else slugs, 1):
        try:
            pts = fetch_history(slug, site=site)
            ok += 1 if pts else 0
            miss += 0 if pts else 1
        except Exception as e:  # noqa: BLE001 — one club's failure must not stop the sweep
            err += 1
            print(f"[clubelo_web] history {slug}: {str(e)[:100]}")
    marker.write_text(_now().isoformat(timespec="seconds"), encoding="utf-8")
    out = {"refreshed": ok, "no_page": miss, "errors": err, "elapsed_s": round(time.time() - t0)}
    print(f"[clubelo_web] histories: {out}")
    return out


def load_histories() -> dict[str, list[dict]]:
    out = {}
    if not _paths.HIST.exists():
        return out
    for p in _paths.HIST.glob("*.json"):
        if p.name.startswith("_"):
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            out[doc["slug"]] = doc["points"]
        except (OSError, ValueError, KeyError):
            continue
    return out


def history_as_of(points: list[dict], date: str) -> float | None:
    v = None
    for p in points:
        if p["date"] <= date:
            v = p["elo"]
        else:
            break
    return v


# ── API-format rows ──────────────────────────────────────────────────────────
def to_api_rows(site_rows: list[dict], as_of: str, *, name_map: dict | None = None,
                histories: dict[str, list[dict]] | None = None) -> list[dict]:
    """Rows in the API CSV schema. Club = API name when mapped (else the site name),
    Country = API code, Elo = the club's history value as of `as_of` when a fresh history
    exists (full precision), else the table integer."""
    name_map = name_map if name_map is not None else load_name_map()
    api_names, _ = _api_reference()
    api_norms = {norm_name(n) for n in api_names}
    rows = []
    for r in site_rows:
        m = name_map.get(r["slug"]) or {}
        cc = SITE_TO_API_CODE.get(r.get("site_cc") or "", r.get("site_cc") or "")
        club = m.get("api") or _unmapped_label(r["name"], cc, api_norms)
        elo = float(r["elo"])
        if histories and r["slug"] in histories:
            hv = history_as_of(histories[r["slug"]], as_of)
            if hv is not None and abs(hv - elo) <= 1.0:   # same rating, better precision
                elo = hv
        rows.append({"Rank": (r.get("rank") if r.get("rank") is not None else ""), "Club": club,
                     "Country": cc, "Level": "", "Elo": elo, "From": as_of, "To": as_of, "_slug": r["slug"]})
    # one label per club: two unmapped clubs can share a name across countries (Guadalajara
    # MEX / ESP) — a duplicate label would let _match_elo pick whichever comes first
    seen: dict[str, int] = {}
    for x in rows:
        seen[x["Club"]] = seen.get(x["Club"], 0) + 1
    used: set[str] = set()
    for x in rows:
        label = x["Club"]
        if seen[label] > 1 or label in used:
            cand = f"{label} ({x['Country']})" if not label.endswith(")") else label
            if cand in used:
                cand = f"{label} [{x['_slug']}]"
            label = cand
        used.add(label)
        x["Club"] = label
        del x["_slug"]
    rows.sort(key=lambda x: -x["Elo"])
    return rows


def _unmapped_label(site_name: str, cc: str, api_norms: set[str]) -> str:
    """Club label for a site club with no API counterpart. If its spelling coincides with an
    API club's (ClubElo has a Uruguayan Liverpool, a Montevideo Nacional, an Ecuadorian
    Barcelona) the country is appended — the API-schema file must never carry two clubs
    under one API name, or the alias/fuzzy matching downstream picks whichever comes first."""
    if norm_name(site_name) in api_norms:
        return f"{site_name} ({cc})"
    return site_name


def write_csv(rows: list[dict], as_of: str, *, source: str) -> Path:
    """Write clubelo_<as_of>.csv (API schema) + the .source sidecar; an existing API file is
    kept as clubelo_<as_of>.csv.frozen_api the first time it is replaced."""
    p = PRIORS / f"clubelo_{as_of}.csv"
    if p.exists():
        bak = p.with_suffix(".csv.frozen_api")
        prev_src = _read_source(as_of)
        if not bak.exists() and prev_src in (None, "api", "api_frozen"):
            shutil.copy2(p, bak)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["Rank", "Club", "Country", "Level", "Elo", "From", "To"])
    w.writeheader()
    for r in rows:
        w.writerow({**r, "Elo": f"{float(r['Elo']):.8g}"})
    # atomic: a concurrent reader (the live loop, the daily refresh) never sees a half file
    tmp = p.with_suffix(".csv.tmp")
    tmp.write_text(buf.getvalue(), encoding="utf-8")
    tmp.replace(p)
    p.with_suffix(".source").write_text(source, encoding="utf-8")
    if source != "web_history":
        try:
            (PRIORS / f"clubelo_{as_of}.provenance.json").unlink()   # per-club provenance only for reconstructions
        except OSError:
            pass
    return p


def _read_source(as_of: str) -> str | None:
    p = PRIORS / f"clubelo_{as_of}.source"
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def read_csv(as_of: str) -> list[dict] | None:
    p = PRIORS / f"clubelo_{as_of}.csv"
    if not p.exists():
        return None
    return list(csv.DictReader(p.open(encoding="utf-8")))


def load_name_map() -> dict:
    try:
        return json.loads(_paths.NAME_MAP.read_text(encoding="utf-8")).get("map") or {}
    except (OSError, ValueError):
        return {}


def save_name_map(m: dict) -> None:
    _write_json(_paths.NAME_MAP, {"built_at": _now().isoformat(timespec="seconds"),
                           "n": len(m), "n_mapped": sum(1 for v in m.values() if v.get("api")),
                           "map": m})
    _mirror(_paths.NAME_MAP)


# ── freeze detector ──────────────────────────────────────────────────────────
def is_frozen(rows: list[dict], prev_rows: list[dict] | None, *, min_common: int = 100) -> bool:
    """True when every club common to both snapshots carries the identical Elo. On a normal
    day dozens of clubs move; 594 identical values across a matchday is what the API served
    for eight weeks. (A genuine no-match day would also read frozen — callers treat the
    verdict as 'try the other source first', not as proof.)"""
    if not rows or not prev_rows:
        return False
    a = {r["Club"]: float(r["Elo"]) for r in rows}
    b = {r["Club"]: float(r["Elo"]) for r in prev_rows}
    common = set(a) & set(b)
    if len(common) < min_common:
        return False
    return all(abs(a[c] - b[c]) < 1e-6 for c in common)


def previous_csv(as_of: str, *, max_back_days: int = 10) -> tuple[str, list[dict]] | None:
    d = datetime.fromisoformat(as_of)
    for k in range(1, max_back_days + 1):
        prev = (d - timedelta(days=k)).strftime("%Y-%m-%d")
        rows = read_csv(prev)
        if rows:
            return prev, rows
    return None


# ── reconstruction of past dates from history ────────────────────────────────
# Site scale minus API scale, estimated on 2026-05-31 (the last live API day) over the
# clubs present in both: mean +66.3, sd 45.9, Spearman 0.94 (ClubElo re-estimates its
# history; the website and the May API are two runs of the model). Used ONLY to place the
# api_frozen filler clubs on the site's scale inside a reconstructed date.
_DEFAULT_SCALE_OFFSET = 66.3


def scale_offset(histories: dict[str, list[dict]], name_map: dict, *, on: str = "2026-05-31") -> float:
    """Mean (site history as of ``on`` − API value on ``on``) across mapped clubs with both;
    falls back to _DEFAULT_SCALE_OFFSET when the API file for ``on`` is unavailable."""
    p = PRIORS / f"clubelo_{on}.csv"
    if not p.exists():
        return _DEFAULT_SCALE_OFFSET
    api = {r["Club"]: float(r["Elo"]) for r in csv.DictReader(p.open(encoding="utf-8"))}
    ds = []
    for slug, m in name_map.items():
        a = m.get("api")
        if a in api and slug in histories:
            v = history_as_of(histories[slug], on)
            if v is not None:
                ds.append(v - api[a])
    return (sum(ds) / len(ds)) if len(ds) >= 50 else _DEFAULT_SCALE_OFFSET


def reconstruct_date(as_of: str, *, histories: dict[str, list[dict]], name_map: dict,
                     frozen_api_rows: list[dict] | None, offset: float | None = None,
                     table_rows: list[dict] | None = None) -> tuple[list[dict], dict]:
    """API-schema rows for a PAST date: history value as of the date for every mapped club
    with a history; for the rest the frozen API value RESCALED onto the site's scale
    (+offset, flagged 'api_frozen_rescaled') so the club universe stays whole WITHOUT mixing
    two scales inside one file — the prior z-scores Elo within each competition, so an
    un-rescaled filler club would read ~66 Elo weaker than its peers.
    Returns (rows, provenance{club: 'web_history'|'api_frozen_rescaled'})."""
    rows, prov = [], {}
    api_cc = {r["Club"]: r["Country"] for r in (frozen_api_rows or [])}
    api_names, _ = _api_reference()
    api_norms = {norm_name(n) for n in api_names}
    if offset is None:
        offset = scale_offset(histories, name_map)
    done: set[str] = set()
    # mapped clubs first (they own the API names), then the unmapped with a disambiguated
    # label — the first version let an unmapped Uruguayan "Liverpool" claim the English
    # club's row and the cross-validation read a 360-point error
    ordered = sorted(histories.items(), key=lambda kv: (name_map.get(kv[0], {}).get("api") is None, kv[0]))
    for slug, pts in ordered:
        m = name_map.get(slug) or {}
        cc = m.get("country_api") or ""
        club = m.get("api") or _unmapped_label(m.get("site") or slug, cc, api_norms)
        v = history_as_of(pts, as_of)
        if v is None or club in done:
            continue
        done.add(club)
        rows.append({"Rank": "", "Club": club, "Country": cc or api_cc.get(club, ""),
                     "Level": "", "Elo": v, "From": as_of, "To": as_of})
        prov[club] = "web_history"
    # clubs without a history page: that day's own website table first (exact, site scale —
    # stored daily from 2026-09-02 on), then the API snapshot rescaled onto the site's scale
    for r in to_api_rows(table_rows or [], as_of, name_map=name_map):
        if r["Club"] in done:
            continue
        done.add(r["Club"])
        rows.append(r)
        prov[r["Club"]] = "web_table"
    for r in (frozen_api_rows or []):
        if r["Club"] in done:
            continue
        rows.append({**r, "Elo": round(float(r["Elo"]) + offset, 4), "From": as_of, "To": as_of})
        prov[r["Club"]] = "api_frozen_rescaled"
    rows.sort(key=lambda x: -float(x["Elo"]))
    return rows, prov


def _api_fillers_for(ds: str) -> list[dict] | None:
    """The API's rows to fill a reconstructed date: that date's own API file when the API
    still answered (the .frozen_api backup), else the LATEST API snapshot — a date the API
    never served (09-01 onward) must not lose the ~340 small clubs that have no history page,
    or the cup competitions' Elo coverage collapses (measured: uecl 83 → 22)."""
    f = _api_file(ds)
    if f is not None:
        return list(csv.DictReader(f.open(encoding="utf-8")))
    latest = latest_api_rows()
    return latest[1] if latest else None


def rebuild_frozen_period(start: str, end: str) -> dict:
    """Overwrite clubelo_<date>.csv for every date in [start, end] from the club histories,
    keeping the API file as .frozen_api and writing a provenance sidecar per date."""
    histories = load_histories()
    name_map = load_name_map()
    offset = scale_offset(histories, name_map)
    d = datetime.fromisoformat(start)
    # never today or the future: today's file is the website's own (900 clubs, source=web);
    # a reconstruction (493 clubs with pages + fillers) must not replace it
    yesterday = (_now() - timedelta(days=1)).strftime("%Y-%m-%d")
    stop = datetime.fromisoformat(min(end, yesterday))
    n = 0
    summary = {"dates": 0, "web_history_clubs": None, "api_frozen_clubs": None, "scale_offset": round(offset, 2)}
    while d <= stop:
        ds = d.strftime("%Y-%m-%d")
        # the API's own rows come from the .frozen_api backup once it exists (the .csv may
        # already be a reconstruction from an earlier run)
        frozen = _api_fillers_for(ds)
        rows, prov = reconstruct_date(ds, histories=histories, name_map=name_map, frozen_api_rows=frozen,
                                      offset=offset, table_rows=load_daily(ds))
        if rows:
            write_csv(rows, ds, source="web_history")
            _write_json(PRIORS / f"clubelo_{ds}.provenance.json", prov)
            n += 1
            # per-date counts, reported as the max seen (today has no API file → no fillers)
            summary["web_history_clubs"] = max(summary["web_history_clubs"] or 0, sum(1 for v in prov.values() if v == "web_history"))
            summary["api_frozen_clubs"] = max(summary["api_frozen_clubs"] or 0, sum(1 for v in prov.values() if v == "api_frozen_rescaled"))
            summary["web_table_clubs"] = max(summary.get("web_table_clubs") or 0, sum(1 for v in prov.values() if v == "web_table"))
        d += timedelta(days=1)
    summary["dates"] = n
    return summary
