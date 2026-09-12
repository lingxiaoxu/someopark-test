"""Polymarket US club-soccer single-match discovery (plan 07, 08; D1-3 / R2). Read-only.

Polymarket US lists our club competitions natively — LIVE-MEASURED 2026-08-26 across
all 12 registry comps (epl/lal/sea/bun/lg1/ucl/uel/uecl/lib/sud/bra/lpa). The WC-era
`fwc-` vocabulary is gone; what is on the venue now:

  event slug   ``<pfx>-{home}-{away}-{ET-date}``      e.g. ``epl-ast-ars-2026-08-31``
  3-way result ``atc-{event}-{teamcode}`` / ``atc-{event}-draw``
  totals       ``tsc-{event}-{N}pt5``                 (YES = Over)
  (also asc-… spreads and astatc-… props, which we do not price)

``<pfx>`` is the competition's ``pmus_slug_prefix`` in the league registry and is NOT
the same vocabulary as Polymarket Global's (`lg1` here vs `fl1` there, `uecl` vs `col`,
`lpa` vs `arg`) — that is why the registry carries both fields.

NO ADVANCE MARKET: 3,711 markets across 66 live club events contained zero
``aadc-…-to-advance`` (and zero market slug containing "advance") — the WC per-match
advance instrument simply is not listed for clubs. ``advance_quotes`` therefore returns
None by construction instead of building slugs that 404, and the two-way advance stays
a Kalshi-only reference (R2: single-venue operation, recorded rather than faked).

Discovery is series-scoped, not keyword-scoped: ``series.list`` gives each competition's
series ids and one ``events.list`` per competition returns every base match event of the
window WITH its ``teams[]`` (safeName / abbreviation / league) — so the club→code map,
the pairing index and the competition tag all come from the same response. That replaces
the WC keyword sweep ("World Cup", "FIFA", …), which returned a different subset on every
call. Prices still come from ``markets.bbo`` per leg (US REST is 60/min).

NOTE on the SDK envelope: ``events.retrieve_by_slug`` returns ``{"event": {...}}``
— the markets are under ``["event"]["markets"]`` (this bit me once).
"""
from __future__ import annotations

import json
from copy import deepcopy
import os
import re
from datetime import date, datetime, timedelta, timezone

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.config.leagues import active
# Shared identity policy across venue readers, chat and the UI.
from prediction_market_soccer.util.club_identity import venue_identity_index, identity_manifest_id
from prediction_market_soccer.util.market_identity import fixture_identity, make_binding, unique_event
from prediction_market_soccer.util.quote_evidence import make_receipt, quote_from_receipt, qualify_quote

# Base (non-derivative) match slug. Team codes are 2-6 chars and may carry digits
# (s04, aek1, icde) — measured over the live slug set.
_CODE = r"[a-z0-9]{2,6}"

# How far the pairing index reaches around "now". Poly US lists a match roughly two
# weeks ahead and keeps it after settlement, so this covers both upcoming_export
# (next fixtures) and live_poller (in-play) from one listing per competition.
_WINDOW_BACK_DAYS = 5
_WINDOW_FWD_DAYS = 21
_CACHE_TTL_SEC = 300

# upcoming_export builds one PolymarketUSDiscovery PER COMPETITION inside its loop, so
# the listing cache has to outlive the instance or we would pay 12x for the same data.
_CACHE: dict = {"at": None, "events": {}, "codes": {}, "series": None}


def _load_alias_pairs() -> list[tuple[str, str]]:
    """Frozen per-comp aliases plus Poly spellings, retaining conflicting pairs.

    aliases_poly.json exists because this venue writes LEGAL names ("FC Bayern
    München", "Stade Rennais FC 1901", "Olympique Lyonnais") that neither the Kalshi
    tables nor the identity catalog know; _parse_event drops an event when either team fails
    to resolve, and those spellings were silently costing 23 listed matches in one
    weekend window. It is a separate file (written by ops/bootstrap_aliases.bootstrap_poly)
    so the Kalshi bootstrap regenerating aliases_<comp>.json can never wipe it.
    Live resolution retains all mappings and refuses ambiguous cross-club labels.
    """
    out: list[tuple[str, str]] = []
    for c in active(include_disabled=True):
        p = CONFIG.paths.priors / f"aliases_{c.key}.json"
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for k, v in (doc.get("aliases") or {}).items():
            out.append((k, v))
    try:
        poly = json.loads((CONFIG.paths.priors / "aliases_poly.json").read_text(encoding="utf-8"))
        for k, v in (poly.get("aliases") or {}).items():
            out.append((k, v))
    except Exception:
        pass
    return out


def _load_aliases() -> dict[str, str]:
    """Compatibility view for bootstrap; live resolution preserves all collisions."""
    out: dict[str, str] = {}
    for label,cid in _load_alias_pairs():
        out.setdefault(label, cid)
    return out


class PolymarketUSDiscovery:
    def __init__(self, client=None, *, window_back_days: int = _WINDOW_BACK_DAYS,
                 window_fwd_days: int = _WINDOW_FWD_DAYS, conn=None):
        if client is None:
            from polymarket_us import PolymarketUS
            client = PolymarketUS(key_id=os.environ["PMUS_KEY_ID"], secret_key=os.environ["PMUS_SECRET"])
        self.c = client
        self.back, self.fwd = window_back_days, window_fwd_days
        self._aliases = _load_aliases()
        self._alias_pairs = _load_alias_pairs()
        from prediction_market_soccer.ingest import store
        conn = conn or store.init_db()
        self.conn = conn
        self.identity_version = identity_manifest_id()
        self.discovery_status = {"complete": False, "state": "not_requested"}
        self.quote_status = {}
        self._quote_status_by_family = {}
        self._club_ids = {r["club_id"] for r in conn.execute(
            "SELECT DISTINCT club_id FROM club_registry")}
        self._identity_records = [dict(r) for r in conn.execute(
            "SELECT club_id,api_team_id,name FROM club_registry")]
        # Club labels seen on the venue that no alias/exact rule could resolve. Kept
        # (not swallowed) so the R6 unmapped-market alert has something to count: with
        # ~500 clubs and winter renames, a silent drop is how a whole league goes dark.
        self.unmapped: list[str] = []

    def quote_status_for(self, family='match'):
        """Detached diagnostics for the last exact fixture query in this family."""
        d=deepcopy(getattr(self, '_quote_status_by_family', {}).get(family, {}))
        if not d: return d
        catalog=d.get('catalog') or {}; comp=(d.get('scope') or {}).get('comp')
        scoped=(catalog.get('competitions') or {}).get(comp) or {}
        summary={k:catalog.get(k) for k in ('window','last_attempt_at','last_complete_at')}
        complete=scoped.get('complete',catalog.get('complete',False))
        summary.update(comp=comp,complete=complete,state='ok' if complete else 'partial',
            pages=scoped.get('pages',0),catalog_complete=catalog.get('complete',False),
            catalog_pages=catalog.get('pages',0),catalog_unmapped_count=len(catalog.get('unmapped') or []),
            issues=deepcopy(scoped.get('issues') or [i for i in catalog.get('issues',[]) if i.get('scope') in (comp,'series')]))
        d['catalog']=summary
        rejections=d.get('identity_rejections_in_scope') or []
        if rejections:
            expected={(d.get('scope') or {}).get('home_id'),(d.get('scope') or {}).get('away_id')}
            d['date_scope_identity_rejection_count']=len(rejections)
            d['identity_rejections_in_scope']=[r for r in rejections if expected & set(r.get('resolved_ids') or [])]
        return d

    def _begin_quote(self, family, home_id, away_id, et_date, fixture, comp_key):
        self.quote_status = {'state':'unavailable', 'reason':'event_not_found',
            'scope':{'family':family,'comp':comp_key,'home_id':home_id,'away_id':away_id,
                     'date':et_date,'kickoff_ts':(fixture or {}).get('kickoff_ts'),'fixture_id':(fixture or {}).get('fixture_api_id', (fixture or {}).get('api_id'))},
            'books':{}}

    def _finish_quote(self, family, quotes=None):
        d = self.quote_status
        d['catalog'] = deepcopy(self.discovery_status)
        if quotes:
            for side,q in quotes.items():
                checks = {a:qualify_quote(q,a,side=side,market_kind=family,
                    settlement_scope='advance' if family=='advance' else 'regulation') for a in ('buy','sell')}
                d['books'][side] = {'state':'ok' if any(c['eligible'] for c in checks.values()) else 'unavailable',
                    'buy_reason':checks['buy']['reason'],'sell_reason':checks['sell']['reason']}
            good = [v for v in d['books'].values() if v['state']=='ok']
            expected = 3 if family=='match' else 2
            d['state'] = 'ok' if good else 'unavailable'
            d['reason'] = ('ok' if len(good)==expected else 'partial_book') if good else 'no_executable_bbo'
        self.__dict__.setdefault('_quote_status_by_family', {})[family] = deepcopy(d)
        return quotes

    # ── entity resolution: exact alias → exact (accent-folded) normalization ──
    def _resolve(self, *labels: str) -> str | None:
        """Resolve registered forms of one club; disagreement/ambiguity stays unknown.

        Venue provider IDs are not API-Football IDs and are never treated as such.
        """
        labels = tuple(s.strip() for s in labels if (s or "").strip())
        if not hasattr(self, "_identity_index"):
            self._identity_index = venue_identity_index(allowed_ids=self._club_ids,
                aliases=getattr(self, "_alias_pairs", self._aliases),
                records=getattr(self, "_identity_records", ()))
        cid = self._identity_index.resolve_labels(*labels)
        if cid:
            return cid
        if labels and labels[0] not in self.unmapped:
            self.unmapped.append(labels[0])   # deduped: this list is read as an alert count
        return None

    # ── series-scoped discovery ──────────────────────────────────────────────
    def _series_ids(self) -> dict[str, list[int]]:
        now = datetime.now(timezone.utc)
        scope = (self.back, self.fwd, identity_manifest_id(), str(getattr(self.c,'base_url','polymarket_us')))
        if _CACHE.get('series_scope') != scope:
            _CACHE.update(series=None, series_at=None, series_scope=scope)
        hit = _CACHE.get("series")
        if hit is not None and _CACHE.get("series_at") and (now-_CACHE["series_at"]).total_seconds() < _CACHE_TTL_SEC:
            self._series_complete = True
            return hit
        catalogue, complete, pages, error = {}, False, 0, None
        for off in range(0, 2000, 100):
            try:
                out = self.c.series.list({"limit":100,"offset":off})
                items = (out.get("series") if isinstance(out,dict) else out) or []
                pages += 1
            except Exception as exc:
                error = type(exc).__name__
                break
            for item in items:
                catalogue[item.get("slug") or ""] = item.get("id")
            if len(items) < 100:
                complete = True
                break
        by_comp = {}
        for c in active():
            pfx = c.pmus_slug_prefix
            ids = []
            for slug,sid in catalogue.items():
                if pfx and (slug == pfx or slug.startswith(pfx+'-')):
                    try: ids.append(int(sid))
                    except (ValueError,TypeError): pass
            if ids: by_comp[c.key] = ids
        self._series_complete = complete
        self._series_status = {"complete":complete,"pages":pages,"error":error,
                               "truncated":not complete and error is None}
        if complete:
            _CACHE.update(series=by_comp,series_at=now)
        return by_comp or hit or {}

    def _load(self, *, force: bool = False) -> None:
        now = datetime.now(timezone.utc)
        version = identity_manifest_id()
        key = (self.back,self.fwd,version,str(getattr(self.c,'base_url','polymarket_us')))
        if _CACHE.get('scope') != key:
            _CACHE.clear()
            _CACHE.update(at=None,events={},codes={},series=None,scope=key)
        if version != self.identity_version:
            self.identity_version = version
            self._alias_pairs = _load_alias_pairs()
            self.__dict__.pop('_identity_index',None)
        if not force and (_CACHE.get('status') or {}).get('state') == 'partial' and _CACHE.get('attempt_at') and (now-_CACHE['attempt_at']).total_seconds() < 15:
            self.discovery_status = dict(_CACHE['status'])
            return
        if not force and (_CACHE.get('status') or {}).get('complete') and _CACHE.get('at') and (now-_CACHE['at']).total_seconds() < _CACHE_TTL_SEC:
            self.discovery_status = dict(_CACHE['status'])
            return
        lo=(now-timedelta(days=self.back)).strftime('%Y-%m-%dT%H:%M:%SZ')
        hi=(now+timedelta(days=self.fwd)).strftime('%Y-%m-%dT%H:%M:%SZ')
        series=self._series_ids()
        events,codes,issues,pages={}, {}, [], 0
        self._parse_rejections = []
        comp_status = {}
        complete=self._series_complete
        if not complete: issues.append({'scope':'series','code':'incomplete_listing'})
        for c in active():
            ids=series.get(c.key)
            if not ids: continue
            exhausted=False
            comp_pages=0
            comp_errors=[]
            for offset in range(0,400,100):
                try:
                    out=self.c.events.list({'seriesId':ids,'limit':100,'offset':offset,'startTimeMin':lo,'startTimeMax':hi})
                    page=(out.get('events') if isinstance(out,dict) else out) or []
                    pages+=1
                    comp_pages+=1
                except Exception as exc:
                    comp_errors.append({'offset':offset,'code':'request_failed','error':type(exc).__name__})
                    issues.append({'scope':c.key,**comp_errors[-1]})
                    break
                for event in page:
                    rec=self._parse_event(c.pmus_slug_prefix,event)
                    if rec:
                        events.setdefault(rec['pair'],[]).append(rec)
                        for cid,code in rec['codes'].items():
                            if cid in codes and codes[cid] != code: codes[cid]=None
                            else: codes[cid]=code
                if len(page)<100:
                    exhausted=True
                    break
            comp_status[c.key]={'complete':exhausted,'pages':comp_pages,'issues':comp_errors}
            if not exhausted:
                complete=False
                issues.append({'scope':c.key,'code':'listing_not_exhausted'})
        status={'complete':complete,'state':'ok' if complete else 'partial','pages':pages,
                'issues':issues,'unmapped':list(self.unmapped),'identity_complete':not self.unmapped,
                'last_attempt_at':now.isoformat(),'last_complete_at':now.isoformat() if complete else (_CACHE.get('status') or {}).get('last_complete_at'),
                'window':{'start':lo,'end':hi},'competitions':comp_status,
                'identity_rejections':deepcopy(self._parse_rejections)}
        # Keep individually verified new events plus the last complete identity index.
        # Neither successful discovery nor cache reuse rejuvenates book quote clocks.
        if not complete:
            for pair,old in _CACHE.get('events',{}).items():
                current={e['slug']:e for e in old}
                current.update({e['slug']:e for e in events.get(pair,[])})
                events[pair]=list(current.values())
            codes={**_CACHE.get('codes',{}),**codes}
        _CACHE.update(events=events,codes={k:v for k,v in codes.items() if v},status=status,attempt_at=now)
        if complete: _CACHE['at']=now
        self.discovery_status=status

    def _parse_event(self, pfx: str, e: dict) -> dict | None:
        slug = (e.get("slug") or "").lower()
        m = re.match(rf"^{re.escape(pfx)}-({_CODE})-({_CODE})-(\d{{4}}-\d{{2}}-\d{{2}})$", slug)
        if not m:
            return None                      # derivative / season event, not a base match
        # teams[] carries the venue's own club identity (safeName is the short form that
        # actually joins to our registry; name keeps the legal suffix). Order in the array
        # is not contractual, so sides come from the slug codes, which are.
        by_code: dict[str, str] = {}
        codes: dict[str, str] = {}
        for t in (e.get("teams") or []):
            cid = self._resolve(t.get("safeName"), t.get("name"))
            code = (t.get("abbreviation") or "").lower()
            if cid and code:
                if (code in by_code and by_code[code] != cid) or (cid in codes and codes[cid] != code):
                    self.__dict__.setdefault('_parse_rejections', []).append({'slug':slug,'date':m.group(3),'prefix':pfx,'reason':'conflicting_team_code','resolved_ids':sorted(set(by_code.values()) | {cid})})
                    return None
                by_code[code] = cid
                codes[cid] = code
        hc, ac = m.group(1), m.group(2)
        hid, aid = by_code.get(hc), by_code.get(ac)
        if not (hid and aid) or hid == aid:
            self.__dict__.setdefault('_parse_rejections', []).append({'slug':slug,'date':m.group(3),'prefix':pfx,'reason':'team_identity_unresolved','resolved_ids':sorted(set(by_code.values()))})
            return None
        return {"slug": slug, "date": m.group(3), "home_code": hc, "away_code": ac,
                "home_id": hid, "away_id": aid, "codes": codes,
                "pair": frozenset({hid, aid}), "raw": e,
                "comp": next((c.key for c in active() if c.pmus_slug_prefix == pfx), None)}

    def code_map(self) -> dict[str, str]:
        """{canonical club_id: Poly US team abbreviation} learned from the listing.

        This is exactly what ``club_registry.poly_code`` is meant to hold (that column
        is still empty); an ingest step can persist it once someone owns that write.
        """
        self._load()
        return dict(_CACHE["codes"])

    def _find_event(self, home_id, away_id, et_date, *, comp_key=None):
        self._load()
        d=self.quote_status
        d['lookup']='catalog'
        window=self.discovery_status.get('window') or {}
        d['in_catalog_window'] = None
        try:
            clocks=[datetime.fromisoformat(str(v).replace('Z','+00:00')) for v in
                    (d.get('scope',{}).get('kickoff_ts'),window.get('start'),window.get('end'))]
            if all(x.tzinfo is not None for x in clocks):
                kickoff,lo,hi=[x.astimezone(timezone.utc) for x in clocks]
                if lo <= hi: d['in_catalog_window'] = lo <= kickoff <= hi
        except (TypeError,ValueError):
            pass
        cands=_CACHE['events'].get(frozenset({home_id,away_id}),[])
        relevant=[e for e in cands if e['date']==et_date and (comp_key is None or e.get('comp')==comp_key)]
        d['candidate_count']=len(relevant)
        exact=unique_event(cands,home_id=home_id,away_id=away_id,comp=comp_key,dates=(et_date,))
        if exact:
            d.update(reason='event_matched',event_id=exact['slug'])
            return exact
        if relevant:
            d.update(reason='ambiguous_event')
            return None
        scoped=(self.discovery_status.get('competitions') or {}).get(comp_key)
        complete=scoped.get('complete') if scoped is not None else self.discovery_status.get('complete')
        d['reason']=('outside_discovery_window' if d['in_catalog_window'] is False else
                     'discovery_scope_unverified' if d['in_catalog_window'] is None else
                     'event_not_found_in_complete_catalog' if complete else 'discovery_partial')
        rejected=[e for e in self.discovery_status.get('identity_rejections',[]) if e['date']==et_date and
            (comp_key is None or any(c.key==comp_key and c.pmus_slug_prefix==e['prefix'] for c in active()))]
        if rejected:
            d['identity_rejections_in_scope']=deepcopy(rejected)
            if any(set(e['resolved_ids']) & {home_id,away_id} for e in rejected): d['reason']='identity_rejected'
        return self._probe_slugs(home_id,away_id,et_date,comp_key=comp_key)

    def _probe_slugs(self, home_id, away_id, et_date, *, comp_key=None):
        d=self.quote_status
        codes=_CACHE['codes']; hc,ac=codes.get(home_id),codes.get(away_id)
        if not(hc and ac and et_date):
            d['probe']={'state':'not_requested','reason':'missing_team_code_or_date'}
            return None
        prefixes=[c.pmus_slug_prefix for c in active() if c.pmus_slug_prefix and (comp_key is None or c.key==comp_key)]
        slugs=[f'{p}-{a}-{b}-{et_date}' for p in prefixes for a,b in ((hc,ac),(ac,hc))]
        if not slugs:
            d['probe']={'state':'not_requested','reason':'unsupported_competition'}
            return None
        d['probe']={'state':'requested','slugs':slugs,'limit':len(slugs)}
        try:
            out=self.c.events.list({'slug':slugs,'limit':len(slugs)})
        except Exception as exc:
            d['probe'].update(state='failed',error=type(exc).__name__)
            d.update(reason='request_failed')
            return None
        events=[]; rejected=[]
        for e in ((out.get('events') if isinstance(out,dict) else out) or []):
            slug=(e.get('slug') or '').lower()
            if slug not in slugs: continue
            rec=self._parse_event(slug.split('-',1)[0],e)
            if rec: events.append(rec)
            else: rejected.append(slug)
        exact=unique_event(events,home_id=home_id,away_id=away_id,comp=comp_key,dates=(et_date,))
        d['probe'].update(state='complete',matched_candidates=len(events),identity_rejected=rejected)
        if exact:
            d.update(reason='event_matched',lookup='exact_slug_probe',event_id=exact['slug'])
        elif events: d.update(reason='ambiguous_event')
        elif rejected: d.update(reason='identity_rejected')
        # A bounded slug probe is not an exhaustive provider-wide listing proof.
        return exact

    # ── pricing ──────────────────────────────────────────────────────────────
    def _price(self, slug: str, *, binding=None):
        started=datetime.now(timezone.utc).isoformat()
        try:
            raw=self.c.markets.bbo(slug)
            received=datetime.now(timezone.utc).isoformat()
            md=raw.get('marketData',raw) if isinstance(raw,dict) else {}
            returned_slug = md.get('slug') or md.get('marketSlug') or (raw.get('slug') if isinstance(raw, dict) else None)
            if returned_slug is not None and returned_slug != slug:
                raise ValueError('market_response_identity_mismatch')
            ask=(md.get('bestAsk') or {}).get('value')
            bid=(md.get('bestBid') or {}).get('value')
            cur=(md.get('currentPx') or {}).get('value')
            if binding is None:
                # Legacy display-only caller. Never claims an executable receipt.
                return {'ask':float(ask) if ask is not None else None,'bid':float(bid) if bid is not None else None,
                        'reference_price':float(cur) if cur is not None else None}
            receipt=make_receipt(binding,ask=ask,bid=bid,reference_price=cur,reference_kind='current',
                request_started_at=started,received_at=received,raw=raw,
                ask_size=(md.get('bestAsk') or {}).get('size'),bid_size=(md.get('bestBid') or {}).get('size'),
                status=str(md.get('status') or 'active').lower())
            return quote_from_receipt(receipt)
        except Exception as exc:
            self.quote_status.setdefault('books', {})[binding['side'] if binding else slug]={'state':'unavailable','reason':'quote_request_failed','error':type(exc).__name__}
            self.quote_status.update(state='unavailable',reason='quote_request_failed')
            return None

    def match_quotes(self, home_id, away_id, et_date, *, fixture=None, comp_key=None):
        comp_key=comp_key or (fixture or {}).get('comp')
        self._begin_quote('match',home_id,away_id,et_date,fixture,comp_key)
        ev=self._find_event(home_id,away_id,et_date,comp_key=comp_key)
        if not ev: return self._finish_quote('match')
        legs={'home':ev['home_code'],'draw':'draw','away':ev['away_code']}
        if ev['home_id']!=home_id:
            legs={'home':ev['away_code'],'draw':'draw','away':ev['home_code']}
        out={}
        for side,code in legs.items():
            slug=f"atc-{ev['slug']}-{code}"
            binding=None
            if fixture is not None:
                binding=make_binding(fixture=fixture_identity(fixture,home_id,away_id,comp_key or ev['comp']),
                    provider='poly_us',environment='us',event_id=ev['slug'],market_id=slug,side=side,
                    identity_version=self.identity_version,evidence={'event':ev['raw']})
            q=self._price(slug,binding=binding)
            if q is not None: out[side]=q
        return self._finish_quote('match',out or None)

    def advance_quotes(self, home_id: str, away_id: str, et_date: str, *, fixture=None, comp_key=None) -> dict[str, dict] | None:
        """Always None: Polymarket US lists no per-match advance market for clubs.

        The WC edition read ``aadc-{event}-to-advance``; a full sweep of every market in
        every live club event (3,711 markets / 66 events, 2026-08-26) found no market
        whose slug contains "advance" in any competition — including the UEL/UECL playoff
        and CONMEBOL knockout events, which are exactly where one would live. Returning
        None keeps the caller on the Kalshi advance reference instead of spending a
        request per match on a slug that cannot exist.
        """
        self._begin_quote("advance",home_id,away_id,et_date,fixture,comp_key)
        self.quote_status.update(state="not_requested",reason="unsupported_market")
        return self._finish_quote("advance")

    def totals_quotes(self, home_id: str, away_id: str, et_date: str,
                      line: float = 2.5, *, fixture=None, comp_key=None) -> dict[str, dict] | None:
        """{over/under: {'ask','bid'}} for a match's total-goals line (None if not found).

        Totals slug: ``tsc-{event}-{N}pt5`` ("Will the total in X be more than N.5?") —
        YES = Over, Under is the complement of the YES book. ``line`` 2.5 → token ``2pt5``.
        The first-half/second-half variants carry an extra ``-fh``/``-sh`` token and are
        deliberately not matched.
        """
        ev = self._find_event(home_id, away_id, et_date, comp_key=comp_key or (fixture or {}).get("comp"))
        if not ev:
            return None
        tok = f"{int(line)}pt5" if line == int(line) + 0.5 else str(line).replace(".", "pt")
        slug=f"tsc-{ev['slug']}-{tok}"
        f=fixture_identity(fixture,home_id,away_id,comp_key or ev['comp']) if fixture is not None else None
        def binding(side):
            return make_binding(fixture=f,provider='poly_us',environment='us',event_id=ev['slug'],market_id=slug,
                side=side,market_kind='totals',line=line,outcome='yes' if side=='over' else 'no',
                identity_version=self.identity_version,evidence={'event':ev['raw']}) if f else None
        p=self._price(slug,binding=binding('over'))
        if not p: return None
        oa,ob=p.get('ask'),p.get('bid')
        under={'ask':1-ob if ob is not None else None,'bid':1-oa if oa is not None else None}
        if f:
            r=p['receipt']
            under=quote_from_receipt(make_receipt(binding('under'),**under,
                request_started_at=r['request_started_at'],received_at=r['received_at'],raw=r['raw'],
                status='active' if r['ask_state'] != 'suspended' and r['bid_state'] != 'suspended' else 'suspended',
                derivation={'rule':'binary_complement','source_receipt_id':r['receipt_id']}))
        return {'over':p,'under':under}



if __name__ == "__main__":
    d = PolymarketUSDiscovery()
    print("series per comp:", d._series_ids())
    d._load(force=True)
    idx = _CACHE["events"]
    print(f"pairing index: {len(idx)} pairings, code map: {len(_CACHE['codes'])} clubs, "
          f"unmapped labels: {len(set(d.unmapped))}")
    for pair, recs in list(idx.items())[:5]:
        r = recs[0]
        print(f"  {r['slug']:30s} {r['home_id']} vs {r['away_id']} ({r['date']})")
        print("    3-way:", d.match_quotes(r["home_id"], r["away_id"], r["date"]))
