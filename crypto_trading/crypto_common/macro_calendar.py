"""Forward-looking US macro release calendar for the W7 PROD guard (and its paper mirrors W11/W13).

Sources (all public):
  * FRED `release/dates` with include_release_dates_with_no_data=true -> the SCHEDULED future dates of the
    BLS / BEA / Census / DOL releases (CPI, Employment Situation, PPI, GDP, Personal Income & Outlays = PCE,
    retail sales, weekly claims, JOLTS). Release times are the agencies' standard ET times.
  * The Federal Reserve FOMC calendar page -> statement days (second meeting day, 14:00 ET).
  * Calendar rules for releases without a feed: ISM Manufacturing (1st business day, 10:00 ET), ISM Services
    (3rd business day, 10:00 ET), Conference Board consumer confidence (last Tuesday, 10:00 ET), University of
    Michigan preliminary sentiment (2nd Friday, 10:00 ET).
The file SIGNALS_DIR/macro_calendar/calendar.json is refreshed by the 4-hourly w7_scenarios launchd job and
is NEVER replaced by an empty or failed fetch. Consumers fail OPEN (no guard) when it is missing or older than
STALE_DAYS and say so in their audit rows. Everything here is read-only with respect to trading.

    python -m crypto_trading.crypto_common.macro_calendar refresh | status | next [--days 14]
"""
from __future__ import annotations
import argparse, calendar, json, logging, os, re, time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from crypto_trading.crypto_common.config import SIGNALS_DIR, env

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
DIR = SIGNALS_DIR / "macro_calendar"
FILE = DIR / "calendar.json"
STALE_DAYS = 7                    # older than this -> consumers fail open
DAYS_AHEAD = 150
FRED = "https://api.stlouisfed.org/fred"
FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
# key, name, FRED release id, release time (ET)
FRED_RELEASES = [("cpi", "CPI", 10, "08:30"), ("nfp", "Employment Situation (NFP)", 50, "08:30"), ("ppi", "PPI", 46, "08:30"),
                 ("gdp", "GDP", 53, "08:30"), ("pce", "Personal Income & Outlays (PCE)", 54, "08:30"), ("retail", "Retail Sales", 9, "08:30"),
                 ("claims", "Initial Jobless Claims", 180, "08:30"), ("jolts", "JOLTS", 192, "10:00")]
RULE_RELEASES = ["ism_mfg", "ism_svc", "conf_board", "umich_prelim"]
UA = {"User-Agent": "someopark-crypto/0.1 (macro calendar)"}


def _et_epoch(d: date, hhmm: str) -> float:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=ET).timestamp()


def _business_days(y: int, m: int) -> list[date]:
    n = calendar.monthrange(y, m)[1]
    return [date(y, m, d) for d in range(1, n + 1) if date(y, m, d).weekday() < 5]


def rule_dates(today: date, days_ahead: int = DAYS_AHEAD) -> list[dict]:
    out = []; end = today + timedelta(days=days_ahead); y, m = today.year, today.month
    while date(y, m, 1) <= end:
        bd = _business_days(y, m)
        cands = [("ism_mfg", "ISM Manufacturing PMI", bd[0], "10:00"), ("ism_svc", "ISM Services PMI", bd[2], "10:00")]
        tues = [d for d in (date(y, m, dd) for dd in range(1, calendar.monthrange(y, m)[1] + 1)) if d.weekday() == 1]
        fris = [d for d in (date(y, m, dd) for dd in range(1, calendar.monthrange(y, m)[1] + 1)) if d.weekday() == 4]
        cands += [("conf_board", "Conference Board Consumer Confidence", tues[-1], "10:00"), ("umich_prelim", "UMich Sentiment (prelim)", fris[1], "10:00")]
        for key, name, d, hm in cands:
            if today <= d <= end: out.append(dict(key=key, name=name, date=d.isoformat(), time_et=hm, t_utc=_et_epoch(d, hm), source="rule"))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def fetch_fred_dates(release_id: int, key: str, today: date, days_ahead: int = DAYS_AHEAD) -> list[str]:
    import requests
    end = (today + timedelta(days=days_ahead)).isoformat(); last = None
    for attempt in range(3):
        try:
            r = requests.get(f"{FRED}/release/dates", timeout=20, headers=UA,
                             params=dict(api_key=key, file_type="json", release_id=release_id, include_release_dates_with_no_data="true",
                                         realtime_start=today.isoformat(), realtime_end="9999-12-31", sort_order="asc", limit=200))
            if r.status_code == 200:
                return sorted({d["date"] for d in r.json().get("release_dates", []) if today.isoformat() <= d["date"] <= end})
            last = f"http {r.status_code}"
        except Exception as e:                                   # noqa: BLE001
            last = str(e)[:120]
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"FRED release {release_id}: {last}")


def parse_fomc(html: str, years: tuple[int, ...]) -> list[date]:
    """Statement days (the meeting's last day) from the Fed calendar page."""
    out = []
    for y in years:
        i = html.find(f"{y} FOMC Meetings")
        if i < 0: continue
        j = html.find("FOMC Meetings", i + 20); seg = html[i:j if j > 0 else i + 80000]
        months = [re.sub(r"<[^>]+>", "", x).strip() for x in re.findall(r'fomc-meeting__month[^>]*>(.*?)</div>', seg, re.S)]
        days = [re.sub(r"<[^>]+>", "", x).strip() for x in re.findall(r'fomc-meeting__date[^>]*>(.*?)</div>', seg, re.S)]
        for mo, dd in zip(months, days):
            dd = dd.replace("*", "").strip(); mo = mo.replace("*", "").strip()
            mm = re.match(r"(\d{1,2})(?:-(\d{1,2}))?", dd)
            if not mm: continue
            d1, d2 = int(mm.group(1)), int(mm.group(2) or mm.group(1))
            names = [x.strip()[:3].lower() for x in mo.split("/")]
            mon2 = names[-1]; month_num = {m.lower()[:3]: k for k, m in enumerate(calendar.month_name) if m}.get(mon2)
            if not month_num: continue
            try: out.append(date(y, month_num, d2))
            except ValueError: continue
    return sorted(set(out))


def fetch_fomc(today: date, days_ahead: int = DAYS_AHEAD) -> list[dict]:
    import requests
    r = requests.get(FOMC_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0 (macro calendar; someopark)"})
    if r.status_code != 200: raise RuntimeError(f"FOMC page http {r.status_code}")
    end = today + timedelta(days=days_ahead)
    ds = [d for d in parse_fomc(r.text, (today.year, today.year + 1)) if today <= d <= end]
    if not ds and today.month < 12: raise RuntimeError("FOMC page parsed no dates")
    return [dict(key="fomc", name="FOMC statement", date=d.isoformat(), time_et="14:00", t_utc=_et_epoch(d, "14:00"), source="fed") for d in ds]


def build(today: date | None = None, days_ahead: int = DAYS_AHEAD) -> tuple[list[dict], dict]:
    today = today or datetime.now(ET).date(); events, status = [], {}
    key = env("FRED_API_KEY")
    for k, name, rid, hm in FRED_RELEASES:
        try:
            if not key: raise RuntimeError("FRED_API_KEY missing")
            ds = fetch_fred_dates(rid, key, today, days_ahead)
            events += [dict(key=k, name=name, date=d, time_et=hm, t_utc=_et_epoch(date.fromisoformat(d), hm), source="fred") for d in ds]
            status[k] = f"ok {len(ds)}"
        except Exception as e:                                   # noqa: BLE001
            status[k] = f"error {str(e)[:120]}"
    try:
        f = fetch_fomc(today, days_ahead); events += f; status["fomc"] = f"ok {len(f)}"
    except Exception as e:                                       # noqa: BLE001
        status["fomc"] = f"error {str(e)[:120]}"
    events += rule_dates(today, days_ahead); status["rules"] = "ok"
    events.sort(key=lambda e: e["t_utc"])
    return events, status


def load() -> dict:
    try: return json.loads(FILE.read_text())
    except (OSError, ValueError): return {}


def refresh(days_ahead: int = DAYS_AHEAD) -> dict:
    """Fetch and write calendar.json; keeps the previous file (and its events) when every feed failed."""
    today = datetime.now(ET).date(); events, status = build(today, days_ahead)
    fed = [e for e in events if e["source"] in ("fred", "fed")]
    ok_feeds = sum(1 for v in status.values() if v.startswith("ok")) - 1          # rules always ok
    prev = load()
    if ok_feeds <= 0 or not fed:
        # keep what we have; merge the still-future rule dates so the file does not go empty
        old = [e for e in prev.get("events", []) if e["t_utc"] > time.time() - 86400]
        doc = dict(prev, last_attempt_utc=datetime.now(timezone.utc).isoformat(), last_status=status, ok=False, events=old or events)
    else:
        doc = dict(generated_utc=datetime.now(timezone.utc).isoformat(), generated_ts=time.time(), horizon_days=days_ahead, ok=True,
                   last_attempt_utc=datetime.now(timezone.utc).isoformat(), last_status=status, events=events)
    DIR.mkdir(parents=True, exist_ok=True); tmp = FILE.with_suffix(".tmp"); tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1)); os.replace(tmp, FILE)
    return doc


_CACHE: dict = {"mtime": None, "doc": {}}


def _doc() -> dict:
    try: mt = FILE.stat().st_mtime
    except OSError: _CACHE.update(mtime=None, doc={}); return {}
    if _CACHE["mtime"] != mt: _CACHE.update(mtime=mt, doc=load())
    return _CACHE["doc"]


def freshness(now: float | None = None) -> dict:
    now = now or time.time(); d = _doc(); gen = d.get("generated_ts")
    n_future = sum(1 for e in d.get("events", []) if e["t_utc"] > now)
    stale = (gen is None) or (now - gen > STALE_DAYS * 86400) or n_future == 0
    return {"generated_ts": gen, "age_s": None if gen is None else round(now - gen), "n_future": n_future, "stale": bool(stale), "ok": bool(d.get("ok"))}


def active(now: float | None = None, after_s: float = 45 * 60, before_s: float = 0.0) -> dict | None:
    """The scheduled release whose guard window [t - before_s, t + after_s) contains `now`; None otherwise or
    when the calendar is missing/stale (fail OPEN - the caller records freshness())."""
    now = now or time.time()
    if freshness(now)["stale"]: return None
    hits = [e for e in _doc().get("events", []) if e["t_utc"] - before_s <= now < e["t_utc"] + after_s]
    if not hits: return None
    e = max(hits, key=lambda x: x["t_utc"])                      # the latest release when two overlap
    return dict(key=e["key"], name=e["name"], t_utc=e["t_utc"], until=e["t_utc"] + after_s, source=e["source"])


def upcoming(days: float = 14, now: float | None = None) -> list[dict]:
    now = now or time.time(); return [e for e in _doc().get("events", []) if now <= e["t_utc"] <= now + days * 86400]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["refresh", "status", "next"]); ap.add_argument("--days", type=float, default=14); a = ap.parse_args(argv)
    if a.cmd == "refresh":
        doc = refresh(); print(json.dumps({"ok": doc.get("ok"), "events": len(doc.get("events", [])), "status": doc.get("last_status")}, ensure_ascii=False)); return 0 if doc.get("ok") else 1
    if a.cmd == "status":
        print(json.dumps(dict(freshness(), file=str(FILE), active=active()), ensure_ascii=False)); return 0
    for e in upcoming(a.days):
        print(f"{datetime.fromtimestamp(e['t_utc'], timezone.utc):%Y-%m-%d %H:%M} UTC  {e['date']} {e['time_et']} ET  {e['key']:<12} {e['name']}  [{e['source']}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
