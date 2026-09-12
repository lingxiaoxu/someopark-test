"""Bootstrap the club alias layer from live Kalshi markets (TRANSFORM_PLAN §3.6).

For every enabled competition: pull the open events of its GAME series (public
API, no quota), extract each market's team code (ticker suffix) + display name
(yes_sub_title, "Reg Time: " stripped), match names against club_registry
(reviewed collision-aware exact identity only), and

  * persist kalshi_code / kalshi_name onto club_registry (the precise runtime keys);
  * write data/priors/aliases_<comp>.json — {kalshi_name -> club_id} plus an
    ``unmatched`` list for the human pass (WC iron rule: live paths use EXACT
    aliases only; fuzzy suggestions never enter the live map).

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
from prediction_market_soccer.util.club_identity import venue_identity_index
from prediction_market_soccer.ops.run_status import atomic_json

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
    "ucl": {"Bodoe/Glimt": "bodo_glimt", "Sabah Masazir": "sabah_fa",
            "Eindhoven": "psv_eindhoven", "Shakhtar": "shakhtar_donetsk",
            "Slavia Prague": "slavia_praha"},
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
            "OFI Crete": "ofi", "Kairat": "kairat_almaty",
            "Alkmaar": "az_alkmaar", "Be`er Sheva": "hapoel_beer_sheva",
            "Besiktas": "beikta", "Ferencvarosi": "ferencvarosi_tc",
            "Lillestroem": "lillestrom", "NK Celje": "celje",
            "Nijmegen": "nec_nijmegen", "Olympiacos": "olympiakos_piraeus",
            "SL Benfica": "benfica", "Sparta Prague": "sparta_praha",
            "Union Gilloise": "union_st_gilloise"},
    "sudamericana": {"Independ. Santa Fe": "santa_fe",
                     "Montevideo City": "atletico_torque"},
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
    from prediction_market_soccer.venues.kalshi.market_data import KalshiMarketData
    reader = KalshiMarketData(PUB)
    events = reader.list_events(series, status=status)
    if not reader.last_discovery_status.get('complete'):
        raise ValueError('incomplete_alias_listing')
    return events


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
        alias_path = CONFIG.paths.priors / f"aliases_{comp.key}.json"
        try:
            existing_doc = json.loads(alias_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            existing_doc = {}
        existing = existing_doc.get('aliases') or {}
        index = venue_identity_index(allowed_ids=reg_ids,
            aliases=[*existing.items(), *CURATED.get(comp.key, {}).items()], records=regs)

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
                        cur['conflict'] = True
                        print(f"[aliases:{comp.key}] ⚠ code conflict for {name!r}: "
                              f"{cur['code']} vs {code}")
                    seen.setdefault(name, {"code": code, "club_id": None})

        # Partial listings cannot replace the reviewed alias projection or registry.
        if fetch_failed:
            summary[comp.key] = {'events': n_events, 'complete': False, 'skipped': 'incomplete_listing'}
            continue
        matched, unmatched = {}, []
        for name, rec in seen.items():
            rec['club_id'] = None if rec.get('conflict') else index.resolve(name)
            if rec['club_id']:
                matched[name] = rec
                store.upsert(conn, 'club_registry', {
                    'club_id': rec['club_id'], 'comp': comp.key,
                    'kalshi_code': rec['code'], 'kalshi_name': name, 'updated_at': store.utcnow(),
                }, pk=['club_id', 'comp'])
            else:
                unmatched.append({'kalshi_name': name, 'code': rec['code'],
                    'reason': 'conflicting_code' if rec.get('conflict') else 'unreviewed_identity',
                    'suggestions': difflib.get_close_matches(name, list(reg_names), n=3, cutoff=.6)})
        conn.commit()
        aliases = {**existing, **{name: rec['club_id'] for name, rec in matched.items()}}
        doc = {**existing_doc, 'comp': comp.key, 'series': series, 'as_of': store.utcnow(),
               'complete': True, 'aliases': aliases,
               'codes': {**(existing_doc.get('codes') or {}), **{r['club_id']:r['code'] for r in matched.values()}},
               'unmatched': unmatched}
        atomic_json(alias_path, doc)
        atomic_json(CONFIG.paths.priors / f'alias_candidates_{comp.key}.json',
                    {'as_of': store.utcnow(), 'complete': True, 'approved': False, 'candidates': unmatched})
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
    """Only reviewed exact identities become aliases; guesses remain review candidates."""
    from prediction_market_soccer.venues.polymarket_us.discovery import PolymarketUSDiscovery
    conn = store.init_db()
    records = [dict(r) for r in conn.execute('SELECT comp,club_id,name FROM club_registry')]
    reg = {}
    for row in records:
        reg.setdefault(row['comp'], {})[row['club_id']] = row['name']
    d = PolymarketUSDiscovery(conn=conn)
    series = d._series_ids()
    path = CONFIG.paths.priors / 'aliases_poly.json'
    try:
        previous = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        previous = {}
    existing = previous.get('aliases') or {}
    aliases, candidates, issues = dict(existing), [], []
    complete = bool(d._series_complete)
    for comp, ids in series.items():
        pool = reg.get(comp) or {}
        index = venue_identity_index(allowed_ids=pool, aliases=list(existing.items()), records=records)
        exhausted = False
        for offset in range(0, 1200, 100):
            try:
                result = d.c.events.list({'seriesId':ids, 'limit':100, 'offset':offset})
                events = (result.get('events') if isinstance(result,dict) else result) or []
            except Exception as exc:
                issues.append({'comp':comp,'offset':offset,'reason':'request_failed','error':type(exc).__name__})
                break
            for event in events:
                for team in event.get('teams') or []:
                    labels = [v.strip() for v in (team.get('safeName'),team.get('name')) if isinstance(v,str) and v.strip()]
                    cid = index.resolve_labels(*labels)
                    for label in labels:
                        if label in existing:
                            continue
                        if cid and index.resolve(label) == cid:
                            aliases[label] = cid
                        else:
                            candidate = {'comp':comp,'label':label,'reason':'unreviewed_or_conflicting_identity',
                                'suggestions':difflib.get_close_matches(label,list(pool.values()),n=3,cutoff=.6)}
                            if candidate not in candidates:
                                candidates.append(candidate)
            if len(events)<100:
                exhausted=True
                break
        if not exhausted:
            complete=False
            issues.append({'comp':comp,'reason':'listing_not_exhausted'})
    atomic_json(CONFIG.paths.priors / 'alias_candidates_poly.json',
        {'as_of':store.utcnow(),'complete':complete,'approved':False,'issues':issues,'candidates':candidates})
    if complete:
        atomic_json(path,{**previous,'source':'ops/bootstrap_aliases.bootstrap_poly',
            'as_of':store.utcnow(),'complete':True,'aliases':aliases,
            'unmatched':[f"{c['comp']}: {c['label']}" for c in candidates]})
    return {'aliases':len(aliases) if complete else len(existing),'unmatched':len(candidates),
            'complete':complete,'activated':len(aliases)-len(existing) if complete else 0,'issues':issues}
