"""discover_series.py — enumerate EVERY open Kalshi series and build the macro catalog.

Walks the public API (no auth): paginates /events?status=open, groups by series_ticker,
then fetches /series/{ticker} for metadata (title, category, frequency, tags). Writes
the full catalog to data/output/kalshi_macro_catalog.json, prints the macro-relevant
slice (Economics / Financials / commodities / rates / FX), grouped by cadence.

    conda run -n someopark_run python -m prediction_market_macro.research.discover_series
"""
import json
import os
import time
import urllib.request

BASE = "https://api.elections.kalshi.com/trade-api/v2"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, "..", "data", "output"))

# categories we consider macro-relevant (everything else kept in the JSON, excluded from print)
MACRO_CATS = {"Economics", "Financials", "Climate and Weather", "Energy"}
MACRO_KEYWORDS = ("fed", "cpi", "inflation", "gdp", "payroll", "unemploy", "jobless",
                  "claims", "oil", "wti", "gas", "recession", "treasury", "yield",
                  "mortgage", "rate", "pce", "ppi", "retail", "housing", "home",
                  "tariff", "debt", "deficit", "eurusd", "usd", "s&p", "nasdaq")


def get(path: str, params: dict | None = None) -> dict:
    q = "&".join(f"{k}={v}" for k, v in (params or {}).items())
    url = f"{BASE}{path}" + (f"?{q}" if q else "")
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    url, headers={"User-Agent": "someopark-macro-discovery"}), timeout=30) as r:
                return json.load(r)
        except Exception as e:
            if attempt == 3:
                raise
            time.sleep(1.5 * (attempt + 1))
    return {}


def main():
    os.makedirs(OUT, exist_ok=True)
    # 1. all open events → series tickers (+ sample event titles)
    series: dict[str, dict] = {}
    cursor, pages = "", 0
    while True:
        params = {"status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        d = get("/events", params)
        for ev in d.get("events", []):
            st = ev.get("series_ticker") or ""
            if not st:
                continue
            s = series.setdefault(st, {"n_open_events": 0, "sample_titles": [],
                                       "category": ev.get("category")})
            s["n_open_events"] += 1
            if len(s["sample_titles"]) < 3:
                s["sample_titles"].append(ev.get("title"))
        cursor = d.get("cursor") or ""
        pages += 1
        if not cursor or pages > 60:
            break
    print(f"[discover] {pages} pages, {sum(s['n_open_events'] for s in series.values())} open events, "
          f"{len(series)} distinct series")

    # 2. series metadata (frequency lives here)
    for i, st in enumerate(sorted(series)):
        try:
            meta = get(f"/series/{st}").get("series") or {}
            series[st].update({
                "title": meta.get("title"), "frequency": meta.get("frequency"),
                "category": meta.get("category") or series[st].get("category"),
                "tags": meta.get("tags"), "settlement_sources":
                    [s.get("name") for s in (meta.get("settlement_sources") or [])],
            })
        except Exception as e:
            series[st]["meta_error"] = str(e)
        if i % 40 == 0:
            print(f"  meta {i}/{len(series)}")
        time.sleep(0.12)

    path = os.path.join(OUT, "kalshi_macro_catalog.json")
    json.dump(series, open(path, "w"), ensure_ascii=False, indent=1)
    print(f"[discover] wrote {path}")

    # 3. print the macro slice grouped by frequency
    def is_macro(st, s):
        if (s.get("category") or "") in MACRO_CATS:
            return True
        blob = " ".join([st, s.get("title") or "", " ".join(s.get("tags") or [])]).lower()
        return any(k in blob for k in MACRO_KEYWORDS)

    macro = {st: s for st, s in series.items() if is_macro(st, s)}
    by_freq: dict[str, list] = {}
    for st, s in sorted(macro.items()):
        by_freq.setdefault(s.get("frequency") or "?", []).append((st, s))
    for freq in sorted(by_freq):
        print(f"\n════ frequency: {freq} ({len(by_freq[freq])}) ════")
        for st, s in by_freq[freq]:
            src = ",".join(s.get("settlement_sources") or [])[:40]
            print(f"  {st:26} {(s.get('category') or ''):22} ev={s['n_open_events']:<3} "
                  f"{(s.get('title') or '')[:52]:52} src={src}")


if __name__ == "__main__":
    main()
