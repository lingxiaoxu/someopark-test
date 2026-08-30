"""Bootstrap the club alias layer from live Kalshi markets (TRANSFORM_PLAN §3.6).

For every enabled competition: pull the open events of its GAME series (public
API, no quota), extract each market's team code (ticker suffix) + display name
(yes_sub_title, "Reg Time: " stripped), match names against club_registry
(exact club_id_of first, then comp-constrained fuzzy), and

  * persist kalshi_code / kalshi_name onto club_registry (the precise runtime keys);
  * write data/priors/aliases_<comp>.json — {kalshi_name -> club_id} plus an
    ``unmatched`` list for the human pass (WC iron rule: live paths use EXACT
    aliases only; fuzzy is bootstrap/backfill-only).

Re-runnable any time (new events accumulate through the season); the monitor's
unmapped-market alert (Phase 4) is the steady-state safety net.

Run: conda run -n someopark_run python -m prediction_market_soccer.ops.bootstrap_aliases
"""
from __future__ import annotations

import difflib
import json

import requests

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.config.leagues import active
from prediction_market_soccer.ingest import store
from prediction_market_soccer.ingest.soccer_ingest import club_id_of

PUB = "https://api.elections.kalshi.com/trade-api/v2"

# Human-curated Kalshi-name → club_id map (§3.6 "人工过目一次 → 固化"; 2026-08-26
# pass over the 53 auto-unmatched names). Checked before any fuzzy matching, so
# re-runs self-heal. comp-scoped to avoid cross-league collisions ("Austria").
CURATED: dict[str, dict[str, str]] = {
    # 2026-08-27 pass over the SEASON markets (champion / top-4 / relegation), which the
    # first pass — scoped to GAME markets — never walked. 24 subjects were unresolvable;
    # these 15 were each checked against club_registry before being written down, and the
    # other 9 are UCL league-phase clubs that are not in the registry yet because the draw
    # has not happened. They stay unresolved on purpose.
    #
    # Two of them are why this table is curated rather than fuzzy. "Paris" is NOT Paris
    # Saint-Germain: Kalshi lists PSG separately at 87c and "Paris" at 2c, so it is the
    # promoted Paris FC, and a nearest-name match would have put PSG's club id on a 2c
    # contract. Likewise the closest names to "Slavia Prague", "Porto" and "Feyenoord" in
    # our registry are Sparta Praha, Cerro Porteno and Brentford — three different clubs.
    "libertadores": {"Coquimbo": "coquimbo_unido", "Estudiantes de La Plata": "estudiantes_l_p", "Ind. del Valle": "independiente_del_valle", "Independiente Rivadavia": "independ_rivadavia", "LDU Quito": "ldu_de_quito", "Tolima": "deportes_tolima", },
    "ucl": {"Bodoe/Glimt": "bodo_glimt", "Sabah Masazir": "sabah_fa", },
    "epl": {"Coventry City": "coventry", "Newcastle United": "newcastle", "Leeds United": "leeds"},
    "laliga": {"Athletic Bilbao": "athletic_club", "Betis": "real_betis", "Bilbao": "athletic_club", "Atletico": "atletico_madrid"},
    "seriea": {"Parma Calcio": "parma"},
    "bundesliga": {"FC Cologne": "1_fc_kln", 
        "Mainz": "fsv_mainz_05", "M´gladbach": "borussia_mnchengladbach",
        "M'gladbach": "borussia_mnchengladbach", "Frankfurt": "eintracht_frankfurt",
        "Dortmund": "borussia_dortmund", "Bremen": "werder_bremen",
        "Schalke": "fc_schalke_04", "Bayern Munich": "bayern_mnchen",
        "Koln": "1_fc_kln", "Cologne": "1_fc_kln",
    },
    "ligue1": {"Paris": "paris_fc", "Stade Rennes": "rennes", "PSG": "paris_saint_germain", "Troyes": "estac_troyes",
               "Stade Rennais": "rennes"},
    "uel": {"Uni Craiova": "universitatea_craiova", "Iberia": "fc_iberia_1999",
            "Kauno": "kauno_algiris", "Salzburg": "red_bull_salzburg",
            "OFI Crete": "ofi", "Kairat": "kairat_almaty"},
    "uecl": {"Czestochowa": "rakw_czstochowa", "SK Rapid": "rapid_vienna", "Kuopion Palloseura": "kups",
             "Shamrock": "shamrock_rovers", "Enschede": "twente",
             "Hajduk": "hnk_hajduk_split", "IC Escaldes": "inter_club_d_escaldes",
             "Dinamo City": "dinamo_tirana", "Austria": "austria_vienna",
             "Partizan Belgrade": "fk_partizan"},
    "brasileirao": {"Atletico Mineiro": "atletico_mg", "Paranaense": "atletico_paranaense"},
    "argentina": {
        "Junin": "sarmiento_junin", "Riestra": "deportivo_riestra",
        "Rosario": "rosario_central", "Rio Cuarto": "estudiantes_de_rio_cuarto",
        "Central Cordoba": "central_cordoba_de_santiago", "Tucuman": "atletico_tucuman",
        "Independiente Avellaneda": "independiente", "Mendoza": "gimnasia_m",
        "Rivadavia": "independ_rivadavia", "Racing Avellaneda": "racing_club",
        "San Lorenzo de Almagro": "san_lorenzo", "Barracas": "barracas_central",
    },
}


def _events(series: str, status: str = "open", limit: int = 200) -> list[dict]:
    import time
    out, cursor = [], None
    while True:
        params = {"series_ticker": series, "status": status, "limit": limit,
                  "with_nested_markets": "true"}
        if cursor:
            params["cursor"] = cursor
        backoff = 2.0
        for attempt in range(5):
            r = requests.get(f"{PUB}/events", params=params, timeout=30)
            if r.status_code == 429:      # public-API rate limit: back off and retry
                time.sleep(backoff)
                backoff *= 2
                continue
            r.raise_for_status()
            break
        else:
            r.raise_for_status()
        j = r.json()
        out.extend(j.get("events") or [])
        cursor = j.get("cursor")
        time.sleep(0.7)                    # throttle between pages/series (429-shy)
        if not cursor or not j.get("events"):
            return out


def _clean_name(sub: str) -> str:
    s = (sub or "").strip()
    low = s.lower()
    if low.startswith("reg time:"):
        s = s[len("reg time:"):].strip()
    return s


def bootstrap(statuses: tuple[str, ...] = ("open",)) -> dict:
    conn = store.init_db()
    summary = {}
    for comp in active():
        # The GAME series plus every SEASON series the registry lists. Scoping this to
        # `game` alone was why 24 season-market subjects stayed unresolvable no matter
        # how many times this ran: Kalshi writes a club's name differently on a champion
        # contract than on a match contract ("Newcastle United" vs "Newcastle",
        # "Athletic Bilbao" vs "Athletic Club"), and the season spellings were never
        # walked, so they were never learned — and the curated map, which is only
        # consulted for a name this scan actually sees, could not reach them either.
        _fams = ["game", "champion", "top4", "top8", "relegation", "last", "advance"]
        series_list = [comp.kalshi.get(f) for f in _fams]
        series_list = [x for i, x in enumerate(series_list) if x and x not in series_list[:i]]
        if not series_list:
            continue
        series = series_list[0]
        regs = [dict(r) for r in conn.execute(
            "SELECT club_id, name FROM club_registry WHERE comp=?", (comp.key,))]
        reg_ids = {r["club_id"] for r in regs}
        reg_names = {r["name"]: r["club_id"] for r in regs}

        seen: dict[str, dict] = {}   # kalshi_name -> {code, club_id|None}
        n_events = 0
        fetch_failed = False
        for st in statuses:
          for series in series_list:
            try:
                evs = _events(series, status=st)
            except Exception as e:  # noqa: BLE001 — venue hiccup must not kill the run
                print(f"[aliases:{comp.key}] {series} {st}: fetch failed ({e})")
                fetch_failed = True
                continue
            n_events += len(evs)
            for ev in evs:
                for m in ev.get("markets") or []:
                    tk = m.get("ticker") or ""
                    code = tk.rsplit("-", 1)[-1] if "-" in tk else ""
                    name = _clean_name(m.get("yes_sub_title") or "")
                    if not name or code == "TIE" or name.lower() in ("tie", "draw"):
                        continue
                    cur = seen.get(name)
                    if cur and cur["code"] != code:
                        print(f"[aliases:{comp.key}] ⚠ code conflict for {name!r}: "
                              f"{cur['code']} vs {code}")
                    seen.setdefault(name, {"code": code, "club_id": None})

        curated = CURATED.get(comp.key, {})
        matched, unmatched = {}, []
        for name, rec in seen.items():
            if name in curated and curated[name] in reg_ids:
                rec["club_id"] = curated[name]
            else:
                cid = club_id_of(name)
                if cid in reg_ids:
                    rec["club_id"] = cid
                else:
                    best = difflib.get_close_matches(name, list(reg_names), n=1, cutoff=0.72)
                    if best:
                        rec["club_id"] = reg_names[best[0]]
            if rec["club_id"]:
                matched[name] = rec
                store.upsert(conn, "club_registry", {
                    "club_id": rec["club_id"], "comp": comp.key,
                    "kalshi_code": rec["code"], "kalshi_name": name,
                    "updated_at": store.utcnow(),
                }, pk=["club_id", "comp"])
            else:
                unmatched.append({"kalshi_name": name, "code": rec["code"]})
        conn.commit()

        if fetch_failed and not seen:
            # never clobber a previous good alias file with an empty rate-limited result
            print(f"[aliases:{comp.key}] fetch failed & nothing seen — keeping existing file")
            summary[comp.key] = {"events": 0, "skipped": "fetch_failed"}
            continue
        doc = {
            "comp": comp.key, "series": series, "as_of": store.utcnow(),
            "aliases": {name: rec["club_id"] for name, rec in sorted(matched.items())},
            "codes": {rec["club_id"]: rec["code"] for rec in matched.values()},
            "unmatched": unmatched,
        }
        (CONFIG.paths.priors / f"aliases_{comp.key}.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        summary[comp.key] = {"events": n_events, "teams_seen": len(seen),
                             "matched": len(matched), "unmatched": len(unmatched)}
        tail = f" ⚠ unmatched: {[u['kalshi_name'] for u in unmatched]}" if unmatched else ""
        print(f"[aliases:{comp.key}] {series}: {n_events} events, "
              f"{len(matched)}/{len(seen)} teams matched{tail}")
    return summary


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--include-closed", action="store_true",
                    help="also scan settled events (season backfill; slower)")
    args = ap.parse_args()
    st = ("open", "closed") if args.include_closed else ("open",)
    bootstrap(statuses=st)


# ── Polymarket US spellings (2026-08-29) ─────────────────────────────────────
# The venue writes LEGAL names ("FC Bayern München", "Stade Rennais FC 1901");
# _parse_event drops an event when either team fails to resolve, and 85 such
# spellings were silently costing us 23 listed matches in one weekend window.
# Persisted to data/priors/aliases_poly.json, which the Poly US discovery merges
# AFTER the per-comp files — a separate file so the Kalshi bootstrap regenerating
# aliases_<comp>.json can never wipe these.

_LEGAL_TOKENS = {
    "fc", "cf", "sk", "fk", "sv", "ac", "as", "bv", "kk", "jk", "sc", "ca", "cd",
    "afc", "kks", "vv", "bk", "if", "sl", "ec", "rc", "es", "ogc", "rcd", "1901",
    "09", "04", "07", "1999", "de", "e", "club",
    # second pass over the live venue list: Latin-American and German legal prefixes
    "aa", "cs", "cr", "fr", "fbc", "fbpa", "cp", "ud", "sd", "ss", "us", "aek1",
    "1846", "1910", "vfl", "vfb", "tsg", "rb", "y",
}


def _fold(s: str) -> str:
    import re
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(w for w in re.sub(r"[^a-z0-9 ]", " ", s.lower()).split()
                    if w not in _LEGAL_TOKENS)


def bootstrap_poly() -> dict:
    """Learn the Poly US spellings for our clubs and persist the exact-alias table.

    Conservative on purpose: an auto-match needs the folded venue name to hit exactly
    one registry club IN THE SAME COMPETITION (folded-equal, or containment with a
    difflib ratio >= 0.85). Anything else lands in `unmatched` for the human pass —
    a wrong club on a price is worse than a missing one (AEK alone could be Athens or
    Larnaca). Re-runnable; existing entries are kept unless re-derived identically.
    """
    import difflib

    from prediction_market_soccer.venues.polymarket_us.discovery import PolymarketUSDiscovery
    conn = store.init_db()
    reg: dict[str, dict[str, str]] = {}
    for r in conn.execute("SELECT comp, club_id, name FROM club_registry"):
        reg.setdefault(r["comp"], {})[r["club_id"]] = r["name"]

    d = PolymarketUSDiscovery()
    sids = d._series_ids()
    out_path = CONFIG.paths.priors / "aliases_poly.json"
    try:
        existing = json.loads(out_path.read_text(encoding="utf-8")).get("aliases") or {}
    except Exception:
        existing = {}
    aliases: dict[str, str] = dict(existing)
    unmatched: list[str] = []

    for comp, ids in sids.items():
        pool = reg.get(comp) or {}
        folded = {cid: _fold(nm) for cid, nm in pool.items()}
        for sid in ids:
            for off in range(0, 1200, 100):
                try:
                    page = d.c.events.list({"series_id": sid, "limit": 100, "offset": off})
                except Exception:
                    break
                evs = (page.get("events") if isinstance(page, dict) else page) or []
                if not evs:
                    break
                for e in evs:
                    for t in (e.get("teams") or []):
                        for label in (t.get("safeName"), t.get("name")):
                            label = (label or "").strip()
                            if not label or label in aliases:
                                continue
                            if d._resolve(label):
                                continue          # already resolvable without help
                            f = _fold(label)
                            exact = [cid for cid, fn in folded.items() if fn == f]
                            if len(exact) == 1:
                                aliases[label] = exact[0]
                                continue
                            near = [cid for cid, fn in folded.items()
                                    if fn and (fn in f or f in fn)
                                    and difflib.SequenceMatcher(None, f, fn).ratio() >= 0.85]
                            if len(near) == 1:
                                aliases[label] = near[0]
                                continue
                            # Token-subset within the SAME competition: the registry
                            # holds the short form ("Brighton") and the venue the legal
                            # one ("Brighton & Hove Albion FC"). Either direction, and
                            # only when exactly ONE club in the comp satisfies it — two
                            # Gimnasias in Argentina both fail this and stay for the
                            # human pass, which is the point.
                            ft = set(f.split())
                            sub = [cid for cid, fn in folded.items()
                                   if fn and (set(fn.split()) <= ft or ft <= set(fn.split()))]
                            if len(sub) == 1:
                                aliases[label] = sub[0]
                            elif label not in unmatched:
                                unmatched.append(f"{comp}: {label}")

    out_path.write_text(json.dumps(
        {"source": "ops/bootstrap_aliases.bootstrap_poly", "aliases": aliases,
         "unmatched": unmatched}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[aliases:poly] {len(aliases)} aliases ({len(aliases) - len(existing)} new), "
          f"{len(unmatched)} left for the human pass")
    for u in unmatched:
        print("   ?", u)
    return {"aliases": len(aliases), "unmatched": unmatched}
