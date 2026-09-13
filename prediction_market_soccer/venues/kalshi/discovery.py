"""Kalshi club-soccer market discovery — per-competition (TRANSFORM_PLAN §2.2).

Forked from the WC discovery; every parsing subtlety is kept verbatim (the
"Reg Time:" sub-title strip — live-verified on KXUCLGAME qualifiers — the
``-TIE`` suffix draw leg, complement NO-side books, floor/cap-strike lines).
What changed (C1/C3): series tickers come from the LEAGUE REGISTRY and entity
mapping goes through the FROZEN per-comp alias table (aliases_<comp>.json,
bootstrap + human-curated §3.6) — live paths resolve EXACT names only; anything
unmapped is dropped AND counted (monitor surfaces the count, never silent).

Usage: one KalshiDiscovery instance per competition:
    d = KalshiDiscovery("epl"); d.match_quotes(home_club_id, away_club_id)
"""
from __future__ import annotations

import json
from copy import deepcopy
from functools import lru_cache
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from uuid import UUID

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.config.leagues import get
from prediction_market_soccer.util.club_identity import venue_identity_index, identity_manifest_id
from prediction_market_soccer.util.market_identity import fixture_identity, make_binding, content_hash
from prediction_market_soccer.util.quote_evidence import make_receipt, quote_from_receipt, qualify_quote
from prediction_market_soccer.venues.base import OrderBook
from prediction_market_soccer.venues.kalshi.market_data import KalshiMarketData


# Kalshi's own league string per competition, harvested from live milestone details on
# 2026-09-13 (one open event per series). The schedule proof compared it to comp.name
# verbatim, so SEVEN of twelve competitions could never pass it — any fixture whose
# Kalshi ticker date differs from ours falls back to that proof and was rejected as
# schedule_metadata_unverified, losing Kalshi quotes entirely (2 fixtures on
# 2026-09-12; the shape recurs on Brasileirão/Argentine evening kickoffs, whose tickers
# are dated to the following day, e.g. KXBRASILEIROGAME-26SEP13BOTRBB for 09-12T23:30Z).
# ucl/uecl are deliberately absent: no milestone was returned for the events sampled, so
# their venue string is unverified and they keep exact-match behaviour (fail closed).
_VENUE_LEAGUE_NAMES = {
    'epl': 'EPL',
    'uel': 'Europa League',
    'libertadores': 'CONMEBOL Libertadores',
    'sudamericana': 'CONMEBOL Sudamericana',
    'brasileirao': 'Brasileiro Serie A',
    'argentina': 'Argentina Primera Division',
}


def _fold_league(value):
    """Case/space/diacritic-insensitive form, or None for a non-string."""
    import unicodedata
    if not isinstance(value, str):
        return None
    decomposed = unicodedata.normalize('NFKD', value)
    return ' '.join(''.join(c for c in decomposed if not unicodedata.combining(c)).casefold().split())


def _same_league(venue_name, comp_name, comp_key=None):
    """True when the venue's league string denotes this competition.

    Accepts the registry name or the venue's own verified spelling for that competition,
    each compared case/space/diacritic-insensitively. Nothing else in the schedule proof
    is relaxed: teams, kickoff instant, event ticker, status and cancellation flags are
    all still asserted, so a genuinely different competition is still rejected.
    """
    venue = _fold_league(venue_name)
    if venue is None:
        return False
    accepted = {_fold_league(comp_name)}
    alias = _VENUE_LEAGUE_NAMES.get(comp_key)
    if alias:
        accepted.add(_fold_league(alias))
    return venue in accepted - {None}

PROD_PUBLIC = "https://api.elections.kalshi.com/trade-api/v2"


@dataclass(frozen=True)
class KalshiMarketRef:
    ticker: str
    real_entity_type: str        # "team" | "player"
    real_entity_id: str | None   # canonical club_id (None if unmapped)
    label: str                   # yes_sub_title


@lru_cache(maxsize=64)
def _load_aliases_cached(comp_key: str, mtime: float) -> tuple:
    """(name, club_id) pairs for one competition, memoised on the file's mtime.

    Every KalshiDiscovery reads THIRTEEN alias files at construction (its own plus the
    twelve it merges into the global map), and the live poller builds one per competition
    with fixtures in the window — so a single cycle re-read and re-parsed the same files
    dozens of times. Keying on mtime keeps a bootstrap_aliases run picked up immediately
    without a restart.
    """
    p = CONFIG.paths.priors / f"aliases_{comp_key}.json"
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        return tuple((dict(doc.get("aliases") or {})).items())
    except Exception:
        return ()


def _load_aliases(comp_key: str) -> dict[str, str]:
    p = CONFIG.paths.priors / f"aliases_{comp_key}.json"
    try:
        mt = p.stat().st_mtime
    except OSError:
        mt = 0.0
    return dict(_load_aliases_cached(comp_key, mt))


class KalshiDiscovery:
    def __init__(self, comp_key: str, base_url: str = PROD_PUBLIC, *, conn=None):
        self.comp = get(comp_key)
        self.md = KalshiMarketData(base_url)
        self.aliases = _load_aliases(comp_key)
        from prediction_market_soccer.ingest import store
        conn = conn or store.init_db()
        self.conn = conn
        self.identity_version = identity_manifest_id()
        self.discovery_status = {}
        self.quote_status = {}
        self._quote_status_by_family = {}
        self._club_ids = {r["club_id"] for r in conn.execute(
            "SELECT club_id FROM club_registry WHERE comp=?", (comp_key,))}
        # season-level markets (champion/top-N) can list clubs not yet in THIS comp's
        # registry (UCL league-phase clubs before the draw enters the fixture list) —
        # they resolve against the global registry; match markets stay comp-exact.
        self._global_ids = {r["club_id"] for r in conn.execute(
            "SELECT DISTINCT club_id FROM club_registry")}
        self._identity_records = [dict(r) for r in conn.execute(
            "SELECT club_id,api_team_id,name FROM club_registry")]
        self._global_aliases: dict[str, str] = {}
        self._global_alias_pairs: list[tuple[str, str]] = []
        for c in ("epl", "laliga", "seriea", "bundesliga", "ligue1", "ucl", "uel",
                  "uecl", "brasileirao", "argentina", "libertadores", "sudamericana"):
            for k, v in _load_aliases(c).items():
                self._global_aliases.setdefault(k, v)
                self._global_alias_pairs.append((k, v))
        self.unmapped: list[str] = []   # names seen but unresolvable (monitor reads this)

    def quote_status_for(self, family='match'):
        """Detached diagnostics for an exact fixture/family query; no book caching."""
        d=deepcopy(getattr(self, '_quote_status_by_family', {}).get(family, {}))
        if not d: return d
        catalog=d.get('catalog') or {}
        d['catalog']={k:catalog.get(k) for k in ('complete','state','series','pages','last_attempt_at','last_complete_at','reason') if k in catalog}
        d['catalog'].update(comp=(d.get('scope') or {}).get('comp'),
            unmapped_count=len(catalog.get('unmapped') or []),
            identity_rejection_count=len(catalog.get('identity_rejections') or []),
            issues=deepcopy(catalog.get('issues') or []))
        rejections=d.get('identity_rejections_in_scope') or []
        if rejections:
            expected={(d.get('scope') or {}).get('home_id'),(d.get('scope') or {}).get('away_id')}
            d['date_scope_identity_rejection_count']=len(rejections)
            d['identity_rejections_in_scope']=[r for r in rejections if expected & set(r.get('resolved_ids') or [])]
        return d

    def _finish_quote(self, family, quotes=None):
        d=self.quote_status
        d['catalog']=deepcopy(self.discovery_status.get(family, {}))
        if quotes:
            for side,q in quotes.items():
                checks={a:qualify_quote(q,a,side=side,market_kind=family,
                    settlement_scope='advance' if family=='advance' else 'regulation') for a in ('buy','sell')}
                d['books'][side]={'state':'ok' if any(c['eligible'] for c in checks.values()) else 'unavailable',
                    'buy_reason':checks['buy']['reason'],'sell_reason':checks['sell']['reason']}
            good=[v for v in d['books'].values() if v['state']=='ok']
            d['state']='ok' if good else 'unavailable'
            d['reason']=('ok' if len(good)==(3 if family=='match' else 2) else 'partial_book') if good else 'no_executable_bbo'
        self.__dict__.setdefault('_quote_status_by_family', {})[family]=deepcopy(d)
        return quotes

    # ── entity resolution: exact alias → exact normalization → drop+count ─────
    def _resolve(self, label: str, *, scope: str = "comp") -> str | None:
        s = (label or "").strip()
        if s.lower().startswith("reg time:"):
            s = s.split(":", 1)[1].strip()
        pool = self._club_ids if scope == "comp" else self._global_ids
        indexes = self.__dict__.setdefault("_identity_indexes", {})
        if scope not in indexes:
            pairs = list(getattr(self, "_global_alias_pairs", self._global_aliases.items()))
            if scope == "comp":
                pairs.extend(self.aliases.items())
            indexes[scope] = venue_identity_index(allowed_ids=pool, aliases=pairs,
                                                 records=getattr(self, "_identity_records", ()))
        cid = indexes[scope].resolve(s)
        if cid:
            return cid
        if s and s.lower() not in ("tie", "draw"):
            self.unmapped.append(s)
        return None

    def _team_ref(self, ticker: str, label: str, *, scope: str = "global") -> KalshiMarketRef:
        return KalshiMarketRef(ticker=ticker, real_entity_type="team",
                               real_entity_id=self._resolve(label, scope=scope), label=label)

    # ── season-level: champion series (KXPREMIERLEAGUE-27 …) ─────────────────
    def champion_markets(self) -> list[KalshiMarketRef]:
        series = self.comp.kalshi.get("champion")
        if not series:
            return []
        refs: list[KalshiMarketRef] = []
        for ev in self.md.list_events(series, status="open"):
            for m in ev.get("markets", []):
                refs.append(self._team_ref(m["ticker"], m.get("yes_sub_title", "")))
        return refs

    def season_markets(self, family: str) -> list[KalshiMarketRef]:
        """Any season family from the registry map: 'top4' / 'relegation' / 'last' /
        'top8' / 'ro16' / 'ro8' / 'ro4' / 'finalist' — one market per club."""
        series = self.comp.kalshi.get(family)
        if not series:
            return []
        refs: list[KalshiMarketRef] = []
        for ev in self.md.list_events(series, status="open"):
            for m in ev.get("markets", []):
                refs.append(self._team_ref(m["ticker"], m.get("yes_sub_title", "")))
        return refs

    def topscorer_markets(self) -> list[dict]:
        series = self.comp.kalshi.get("topscorer")
        if not series:
            return []
        return [m for ev in self.md.list_events(series, status="open")
                for m in ev.get("markets", [])]

    def orderbook(self, ticker: str) -> OrderBook:
        return self.md.get_orderbook(ticker)

    # Event indexes retain date and duplicate candidates; legacy views omit ambiguity.
    def _pair_from_title(self, title):
        parts=re.split(r'\s+vs\.?\s+',(title or '').split(':')[0],flags=re.I)
        if len(parts)!=2: return None
        ids=[self._resolve(p.strip()) for p in parts]
        return frozenset(ids) if all(ids) and ids[0]!=ids[1] else None

    def _events_index(self, family):
        version=identity_manifest_id()
        cache=self.__dict__.setdefault('_scoped_indexes',{})
        if version != self.identity_version:
            self.identity_version=version; self.aliases=_load_aliases(self.comp.key)
            from prediction_market_soccer.config.leagues import active
            self._global_alias_pairs = [(label,cid) for c in active(include_disabled=True) for label,cid in _load_aliases(c.key).items()]
            self.__dict__.pop('_identity_indexes',None); cache.clear()
        if family in cache: return cache[family]
        series=self.comp.kalshi.get({'match':'game','advance':'advance','totals':'total','corners':'corners'}[family])
        if not series:
            self.discovery_status[family]={'complete':False,'state':'not_requested','reason':'unsupported_market','series':None}
            return []
        entries=[]
        rejected=[]
        for event in self.md.list_events(series,status='open'):
            eid=event.get('event_ticker') or ''
            stamp=re.match(r'^[^-]+-(\d{2}[A-Z]{3}\d{2})',eid)
            try: day=datetime.strptime(stamp[1],'%y%b%d').date().isoformat() if stamp else None
            except ValueError: day=None
            pair=self._pair_from_title(event.get('title'))
            teams,lines,draw={}, {}, None
            conflict=False
            for market in event.get('markets') or []:
                ticker=market.get('ticker')
                if not ticker or not ticker.startswith(eid+'-'): continue
                if family in ('totals','corners'):
                    strike=market.get('floor_strike')
                    if strike is None and family=='corners': strike=market.get('cap_strike')
                    if strike is None: continue
                    try: line=float(strike)-(0.5 if family=='corners' else 0)
                    except (TypeError,ValueError):
                        conflict=True; continue
                    if line in lines and lines[line]!=ticker: conflict=True
                    lines[line]=ticker
                    continue
                sub=(market.get('yes_sub_title') or '').strip()
                if sub.lower().startswith('reg time:'): sub=sub.split(':',1)[1].strip()
                if family=='advance':
                    if sub.lower().endswith(' advances'): sub=sub[:-len(' advances')].strip()
                    if sub.lower().startswith('to advance:'): sub=sub.split(':',1)[1].strip()
                if family=='match' and (sub.lower() in ('tie','draw') or ticker.endswith('-TIE')):
                    if ticker.endswith('-TIE') and sub and sub.lower() not in ('tie','draw'):
                        conflict=True; continue
                    if draw and draw!=ticker: conflict=True
                    draw=ticker
                    continue
                cid=self._resolve(sub)
                if cid:
                    if cid in teams and teams[cid]!=ticker: conflict=True
                    teams[cid]=ticker
            tokens=list(teams.values())+([draw] if draw else [])
            if len(tokens) != len(set(tokens)): conflict=True
            if pair and not set(teams)<=pair: conflict=True
            if pair is None and len(teams)==2: pair=frozenset(teams)
            if conflict or not pair or len(pair)!=2:
                rejected.append({'event_id':eid,'date':day,'reason':'conflicting_market_identity' if conflict else 'team_identity_unresolved','resolved_ids':sorted(set(teams))})
                continue
            if not teams and not lines and not draw: continue
            entries.append({'pair':pair,'teams':teams,'tie':draw,'lines':lines,'event':eid,
                            'date':day,'raw':event,'comp':self.comp.key})
        self.discovery_status[family]=dict(getattr(self.md,'last_discovery_status',{'complete':False,'state':'unknown'}))
        self.discovery_status[family]['unmapped']=sorted(set(self.unmapped))
        self.discovery_status[family].update(series=series,identity_rejections=rejected)
        # Don't retain a failed listing as an authoritative object-lifetime cache.
        if self.discovery_status[family].get('complete'): cache[family]=entries
        return entries

    def _legacy_index(self,family):
        grouped={}
        for entry in self._events_index(family): grouped.setdefault(entry['pair'],[]).append(entry)
        return {pair:items[0] for pair,items in grouped.items() if len(items)==1}

    def match_index(self): return self._legacy_index('match')
    def advance_index(self): return self._legacy_index('advance')
    def totals_index(self): return self._legacy_index('totals')
    def corners_index(self): return self._legacy_index('corners')

    def _milestone_schedule_binding(self, entry, fixture, home_id, away_id, capture):
        """Confirm an official current schedule without interpreting an old ticker date.

        The original ticker, event text and all settlement contingencies are kept.
        Expected expiration is never used as an inferred original kickoff, and
        this proof does not assert a numerical 48-hour rescheduling exemption.
        """
        def dt(value):
            d = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if d.tzinfo is None: raise ValueError('timezone_required')
            return d.astimezone(timezone.utc)
        try:
            f = fixture_identity(fixture, home_id, away_id, self.comp.key)
            at = datetime.now(timezone.utc)
            start, received = dt(capture['request_started_at']), dt(capture['received_at'])
            raw = capture['raw']
            if (capture.get('complete') is not True or capture.get('raw_hash') != content_hash(raw)
                    or capture.get('endpoint') != self.md.base + '/milestones'
                    or capture.get('params') != {'related_event_ticker': entry['event'], 'limit': 50}
                    or not start <= received <= at or (at-start).total_seconds() > 120
                    or not isinstance(raw, dict) or raw.get('cursor') not in ('', None)
                    or not isinstance(raw.get('milestones'), list) or len(raw['milestones']) != 1):
                return None
            if f.get('season') != self.comp.season:
                return None
            for cid, api in ((home_id, f['home_api_id']), (away_id, f['away_api_id'])):
                actual = {r['api_team_id'] for r in self._identity_records if r['club_id'] == cid}
                if actual != {api}: return None
            if (set(entry['teams']) != {home_id, away_id} or not entry['tie']
                    or len(set(entry['teams'].values()) | {entry['tie']}) != 3):
                return None
            milestone = raw['milestones'][0]
            detail = milestone.get('details') or {}
            primary = milestone.get('primary_event_tickers')
            related = milestone.get('related_event_tickers')
            game_prefix = self.comp.kalshi['game'] + '-'
            if any(not isinstance(items, list) or not all(isinstance(t, str) for t in items)
                   or len(items) != len(set(items))
                   or [t for t in items if t.startswith(game_prefix)] != [entry['event']]
                   for items in (primary, related)):
                return None
            if (not milestone.get('id') or milestone.get('category') != 'Sports'
                    or milestone.get('type') not in ('soccer_game', 'soccer_tournament_multi_leg')
                    or self._pair_from_title(milestone.get('title')) != frozenset((home_id, away_id))
                    or not _same_league(detail.get('league'), self.comp.name, self.comp.key)
                    or detail.get('main_game_event_ticker') != entry['event']
                    or dt(milestone['start_date']) != dt(f['kickoff_ts'])
                    or dt(milestone['last_updated_ts']) > received
                    or detail.get('status') not in ('not_started', 'in_progress', 'live', 'halftime')
                    or any(detail.get(k) not in (None, False) for k in
                           ('cancelled', 'canceled', 'is_cancelled', 'postponed', 'is_postponed'))):
                return None
            phase = fixture.get('status_short', fixture.get('status'))
            if (phase not in ('NS', 'TBD', '1H', 'HT', '2H', 'LIVE')
                    or (phase in ('NS', 'TBD') and detail['status'] != 'not_started')
                    or (phase in ('1H', 'HT', '2H', 'LIVE') and detail['status'] == 'not_started')):
                return None
            ids = {s: detail.get(k) for s,k in (('home','home_team_id'), ('away','away_team_id'), ('draw','tie_id'))}
            if not all(isinstance(v,str) and v for v in ids.values()) or len(set(ids.values())) != 3:
                return None
            if any(str(UUID(v)) != v.lower() for v in ids.values()): return None
            tickers = {'home': entry['teams'][home_id], 'away': entry['teams'][away_id], 'draw': entry['tie']}
            markets = entry['raw'].get('markets') or []
            if len(markets) != 3: return None
            for side, ticker in tickers.items():
                matches = [m for m in markets if m.get('ticker') == ticker]
                if len(matches) != 1: return None
                m = matches[0]
                rules = ' '.join((m.get('rules_primary') or '').lower().split())
                if (m.get('event_ticker') != entry['event'] or m.get('market_type') != 'binary'
                        or m.get('status') != 'active' or m.get('strike_type') != 'structured'
                        or m.get('custom_strike') != {'soccer_team': ids[side]}
                        or 'after 90 minutes plus stoppage time' not in rules
                        or '(does not include extra time or penalties)' not in rules):
                    return None
            return {'basis': 'official_current_milestone', 'milestone_id': milestone['id'],
                    'event_ticker': entry['event'], 'original_ticker_date': entry['date'],
                    'fixture_kickoff': f['kickoff_ts'], 'official_start_date': milestone['start_date'],
                    'structured_team_ids': ids, 'milestone_raw_hash': content_hash(milestone),
                    'capture_raw_hash': capture['raw_hash'],
                    'settlement_rules_preserved': True, 'original_kickoff_inferred': False}
        except (KeyError, TypeError, ValueError, AttributeError):
            return None

    def _schedule_entry(self, entries, fixture, home_id, away_id):
        """At most two exact-pair metadata GETs, only after the ordinary date miss."""
        d = self.quote_status
        d['reason'] = 'schedule_mismatch'
        d.update(metadata_request_limit=2, metadata_http_requests=0)
        if not entries or len(entries) > 2:
            if len(entries) > 2: d['reason'] = 'ambiguous_schedule_candidates'
            return None
        catalog = self.discovery_status.get('match', {})
        if catalog.get('complete') is not True:
            d['reason'] = 'discovery_partial'
            return None
        if any(set(e['teams']) != {home_id, away_id} or not e['tie'] for e in entries):
            d['reason'] = 'incomplete_schedule_identity'
            return None
        approved = []
        receipts = []
        for entry in entries:
            try:
                capture = self.md.get_event_milestones_capture(entry['event'])
                receipts.append(deepcopy(capture))
                proof = self._milestone_schedule_binding(entry, fixture, home_id, away_id, capture)
                if proof:
                    approved.append({**entry, 'schedule_binding': proof, 'milestone_receipt': capture})
                else:
                    # An unverified competing event cannot be silently discarded.
                    d['reason'] = 'schedule_metadata_unverified'
                    return None
            except Exception as exc:
                d.update(reason='schedule_metadata_unavailable', metadata_error=type(exc).__name__)
                return None
            finally:
                d['metadata_http_requests'] += getattr(self.md, 'last_milestone_requests', 1)
                catalog['schedule_receipts'] = receipts
        if len(approved) == 1:
            d.update(reason='event_matched_by_official_schedule', event_id=approved[0]['event'],
                     candidate_count=1, schedule_binding=deepcopy(approved[0]['schedule_binding']))
            return approved[0]
        d['reason'] = 'ambiguous_official_schedule'
        return None

    def _entry(self,family,home_id,away_id,fixture):
        self.quote_status={'state':'unavailable','reason':'event_not_found','books':{},
            'scope':{'family':family,'comp':self.comp.key,'home_id':home_id,'away_id':away_id,
                     'fixture_id':(fixture or {}).get('fixture_api_id',(fixture or {}).get('api_id'))}}
        candidates=[e for e in self._events_index(family) if e['pair']==frozenset((home_id,away_id))]
        pair_candidates = list(candidates)
        dates=None
        if fixture is not None:
            f=fixture_identity(fixture,home_id,away_id,self.comp.key)
            ko=datetime.fromisoformat(f['kickoff_ts'].replace('Z','+00:00'))
            dates={ko.astimezone(timezone.utc).date().isoformat(),ko.astimezone(ZoneInfo('America/New_York')).date().isoformat()}
            candidates=[e for e in candidates if e['date'] in dates]
            self.quote_status['scope']['dates']=sorted(dates)
        catalog=self.discovery_status.get(family,{})
        d=self.quote_status; d['candidate_count']=len(candidates)
        if len(candidates)==1:
            d.update(reason='event_matched',event_id=candidates[0]['event'])
            return candidates[0]
        if not candidates and pair_candidates and fixture is not None and family == 'match':
            return self._schedule_entry(pair_candidates, fixture, home_id, away_id)
        if len(candidates)>1: d['reason']='ambiguous_event'
        elif catalog.get('reason')=='unsupported_market': d.update(state='not_requested',reason='unsupported_market')
        else:
            d['reason']='event_not_found_in_complete_catalog' if catalog.get('complete') else 'discovery_partial'
            rejected=[e for e in catalog.get('identity_rejections',[]) if dates is None or e['date'] in dates]
            if rejected:
                d['identity_rejections_in_scope']=deepcopy(rejected)
                if any(set(e['resolved_ids']) & {home_id,away_id} for e in rejected): d['reason']='identity_rejected'
        return None

    def _quote(self,ticker,side,entry,fixture,home_id,away_id,*,family='match',line=None,capture=None):
        ob,raw,started,received=capture or self.md.get_orderbook_capture(ticker)
        returned_ticker = raw.get('ticker') or raw.get('market_ticker')
        if returned_ticker is not None and returned_ticker != ticker:
            raise ValueError('market_response_identity_mismatch')
        no=side=='under'
        ask,bid=(ob.no_ask,ob.no_bid) if no else (ob.yes_ask,ob.yes_bid)
        if fixture is None:
            return {'ask':float(ask) if ask is not None else None,'bid':float(bid) if bid is not None else None}
        evidence = {'event': entry['raw']}
        if entry.get('schedule_binding'):
            evidence.update(schedule_binding=entry['schedule_binding'], milestone_receipt=entry['milestone_receipt'])
        binding=make_binding(fixture=fixture_identity(fixture,home_id,away_id,self.comp.key),provider='kalshi',
            environment='demo' if 'demo' in self.md.base else 'public',event_id=entry['event'],market_id=ticker,
            side=side,outcome='no' if no else 'yes',market_kind=family,line=line,
            identity_version=self.identity_version,evidence=evidence)
        return quote_from_receipt(make_receipt(binding,ask=ask,bid=bid,raw=raw,
            request_started_at=started,received_at=received,
            ask_size=str(ob.yes_depth if no else ob.no_depth) if (ob.yes_depth if no else ob.no_depth) is not None else None,
            bid_size=str(ob.no_depth if no else ob.yes_depth) if (ob.no_depth if no else ob.yes_depth) is not None else None,
            status=next((m.get('status','active') for m in entry['raw'].get('markets',[]) if m.get('ticker')==ticker),'active'),
            derivation={'rule':'binary_complement','ask_from':'yes_bid' if no else 'no_bid'}))

    def match_quotes(self,home_id,away_id,*,fixture=None):
        entry=self._entry('match',home_id,away_id,fixture)
        if not entry: return self._finish_quote('match')
        targets={('home' if cid==home_id else 'away'):t for cid,t in entry['teams'].items()}
        if entry['tie']: targets['draw']=entry['tie']
        out={}
        for side,ticker in targets.items():
            try: out[side]=self._quote(ticker,side,entry,fixture,home_id,away_id)
            except Exception as exc:
                self.quote_status['books'][side]={'state':'unavailable','reason':'quote_request_failed','error':type(exc).__name__}
                self.quote_status['reason']='quote_request_failed'
        return self._finish_quote('match',out or None)

    def advance_quotes(self,home_id,away_id,*,fixture=None):
        entry=self._entry('advance',home_id,away_id,fixture)
        if not entry: return self._finish_quote('advance')
        out={}
        for cid,ticker in entry['teams'].items():
            side='home' if cid==home_id else 'away'
            try: out[side]=self._quote(ticker,side,entry,fixture,home_id,away_id,family='advance')
            except Exception as exc:
                self.quote_status['books'][side]={'state':'unavailable','reason':'quote_request_failed','error':type(exc).__name__}
                self.quote_status['reason']='quote_request_failed'
        return self._finish_quote('advance',out or None)

    def totals_quotes(self,home_id,away_id,line=2.5,*,fixture=None):
        entry=self._entry('totals',home_id,away_id,fixture)
        if not entry or line not in entry['lines']: return None
        capture=self.md.get_orderbook_capture(entry['lines'][line])
        return {s:self._quote(entry['lines'][line],s,entry,fixture,home_id,away_id,family='totals',line=line,capture=capture) for s in ('over','under')}

    def corners_quotes(self,home_id,away_id,*,fixture=None):
        entry=self._entry('corners',home_id,away_id,fixture)
        if not entry: return None
        out={}
        for line,ticker in entry['lines'].items():
            capture=self.md.get_orderbook_capture(ticker)
            over=self._quote(ticker,'over',entry,fixture,home_id,away_id,family='corners',line=line,capture=capture)
            under=self._quote(ticker,'under',entry,fixture,home_id,away_id,family='corners',line=line,capture=capture)
            out[line]={**over,'under_ask':under['ask'],'under_bid':under['bid'],'under_receipt':under.get('receipt')}
        return out or None


if __name__ == "__main__":
    for key in ("epl", "ucl"):
        d = KalshiDiscovery(key)
        mi = d.match_index()
        print(f"— {key}: {len(mi)} match events mapped; unmapped names: {sorted(set(d.unmapped))}")
        for pair, entry in list(mi.items())[:2]:
            a, b = sorted(pair)
            q = d.match_quotes(a, b)
            print(f"  {a} v {b}: {q}")
        champ = d.champion_markets()
        ok = [r for r in champ if r.real_entity_id]
        print(f"  champion series: {len(champ)} markets, {len(ok)} mapped")
