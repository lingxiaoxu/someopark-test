"""Immutable soccer identity with collision-aware Unicode name matching.

No fuzzy matching, transliteration, ID migration, DB writes or network access.
The catalog is the reviewed current roster. Callers may extend it with explicit
DB identities for future clubs; unknown names remain unresolved.
"""
from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

_PUNCT = str.maketrans({**{c: "'" for c in '‘’ʼ`´'}, **{c: '-' for c in '‐‑‒–—−'}})
_FOLD = str.maketrans({'ø':'o', 'ł':'l', 'ß':'ss', 'ı':'i', 'æ':'ae', 'œ':'oe', 'đ':'d'})


def normalize_club_name(value) -> str:
    raw = unicodedata.normalize('NFKC', str(value or '')).lower().translate(_PUNCT)
    return unicodedata.normalize('NFC', re.sub(r'\s+', ' ', raw).strip())


def fold_club_name(value) -> str:
    out = []
    latin = False
    for char in unicodedata.normalize('NFKD', normalize_club_name(value)):
        if unicodedata.category(char).startswith('M'):
            if not latin:
                out.append(char)
        else:
            latin = 'LATIN' in unicodedata.name(char, '')
            out.append(char)
    return unicodedata.normalize('NFC', ''.join(out).translate(_FOLD))


@lru_cache(maxsize=4)
def _catalog_records_cached(raw: str) -> tuple[dict, ...]:
    return tuple(json.loads(raw)['clubs'])


def catalog_records() -> tuple[dict, ...]:
    path = Path(__file__).resolve().parents[1] / 'config' / 'club_identity.json'
    return _catalog_records_cached(path.read_text(encoding='utf-8'))


class ClubIdentityIndex:
    def __init__(self, records=None, *, allowed_ids=None, aliases=()):
        self.allowed = None if allowed_ids is None else set(allowed_ids)
        self.ids: set[str] = set()
        self.api: dict[str, set[str]] = {}
        self.exact: dict[str, set[str]] = {}
        self.folded: dict[str, set[str]] = {}
        for rec in catalog_records() if records is None else records:
            cid = rec['club_id']
            if self.allowed is not None and cid not in self.allowed:
                continue
            self.ids.add(cid)
            aid = rec.get('api_football_id', rec.get('api_team_id'))
            if aid is not None:
                self.api.setdefault(str(aid), set()).add(cid)
            for name in (cid, rec.get('source_name', rec.get('name', '')), *(rec.get('aliases') or [])):
                self._add(name, cid)
        # Explicit IDs in the caller's DB remain valid before catalog review. Their
        # names must come from records/curated aliases, never fabricated slug words.
        for cid in self.allowed or ():
            self.ids.add(cid)
            self._add(cid, cid)
        for label,cid in aliases.items() if isinstance(aliases, dict) else aliases:
            if self.allowed is None or cid in self.allowed:
                self.ids.add(cid)
                self._add(label, cid)

    def _add(self, name, cid):
        if not name:
            return
        self.exact.setdefault(normalize_club_name(name), set()).add(cid)
        self.folded.setdefault(fold_club_name(name), set()).add(cid)

    def candidates(self, label) -> set[str]:
        raw = str(label or '').strip()
        if raw in self.ids:
            return {raw}
        return set(self.exact.get(normalize_club_name(raw),
                                  self.folded.get(fold_club_name(raw), set())))

    def resolve_labels(self, *labels) -> str | None:
        hits = [s for label in labels if (s := self.candidates(label))]
        if not hits:
            return None
        possible = set.intersection(*hits)
        return next(iter(possible)) if len(possible) == 1 else None

    def resolve(self, label=None, *, club_id=None, api_football_id=None) -> str | None:
        if club_id is not None or api_football_id is not None:
            hits = []
            if club_id is not None:
                hits.append({club_id} if club_id in self.ids else set())
            if api_football_id is not None:
                hits.append(self.api.get(str(api_football_id), set()))
            if any(len(s) != 1 for s in hits):
                return None
            ids = set.union(*hits)
            return next(iter(ids)) if len(ids) == 1 else None
        return self.resolve_labels(label)


def venue_identity_index(*, allowed_ids=None, aliases=(), records=()) -> ClubIdentityIndex:
    """Overlay current DB records and curated mappings without dropping collisions."""
    return ClubIdentityIndex([*catalog_records(), *records], allowed_ids=allowed_ids, aliases=aliases)


_FC_DOMESTIC_COMPS = {
    'Premier League': {'epl'}, 'EFL Championship': {'epl'},
    'LALIGA EA SPORTS': {'laliga'}, 'LALIGA HYPERMOTION': {'laliga'},
    'Serie A Enilive': {'seriea'}, 'Serie BKT': {'seriea'},
    'Bundesliga': {'bundesliga'}, 'Bundesliga 2': {'bundesliga'},
    "Ligue 1 McDonald's": {'ligue1'}, 'Ligue 2 BKT': {'ligue1'}, 'LPF': {'argentina'},
}
_FC_UEFA_LEAGUES = {
    'Trendyol Süper Lig', 'Liga Portugal', 'Eredivisie', 'Liga Hrvatska',
    'Hellas Liga', '1A Pro League', 'Scottish Prem', 'Česká Liga', 'Ukrayina Liha',
    'Brack Super League', 'Magyar Liga', 'Eliteserien', '3F Superliga',
    'Ö. Bundesliga', 'Allsvenskan', 'PKO BP Ekstraklasa', 'SUPERLIGA',
    'Liga Cyprus', 'Liga Azerbaijan', 'Finnliiga', 'SSE Airtricity PD',
}
_FC_CONMEBOL_LEAGUES = {'Libertadores', 'Sudamericana', 'Liga Chile'}
_FC_RESERVE_RE = re.compile(r'(\s(B|II|2)$)|U1[6-9]|U2[0-3]|Reserv', re.I)


class FCClubResolver:
    """FC26 labels need source-league context; venue/global names alone are unsafe.

    League-specific reviewed EA aliases outrank generic names. Reserves, unknown
    source leagues and ambiguous CONMEBOL short forms stay unresolved. The cache
    always includes the source league; no player or scoring data is changed here.
    """
    def __init__(self, records, *, league_aliases=(), aliases=()):
        self.records = list(records)
        self.comps: dict[str, set[str]] = {}
        for record in self.records:
            self.comps.setdefault(record['club_id'], set()).add(record['comp'])
        self.league_aliases = dict(league_aliases)
        self.aliases = list(aliases.items() if isinstance(aliases, dict) else aliases)
        self._indexes = {}
        self._cache = {}

    def resolve(self, team_name: str, league_name: str = '') -> str:
        key = (team_name, league_name)
        if key in self._cache:
            return self._cache[key]
        result = self._resolve(team_name, league_name)
        self._cache[key] = result
        return result

    def _resolve(self, team_name: str, league_name: str) -> str:
        if not team_name or _FC_RESERVE_RE.search(team_name):
            return ''
        allowed_comps = _FC_DOMESTIC_COMPS.get(league_name)
        if allowed_comps is None:
            allowed_comps = ({'ucl','uel','uecl'} if league_name in _FC_UEFA_LEAGUES else
                             {'libertadores','sudamericana'} if league_name in _FC_CONMEBOL_LEAGUES else set())
        if not allowed_comps:
            return ''
        if league_name not in self._indexes:
            pool = {cid for cid, comps in self.comps.items() if comps & allowed_comps}
            explicit = [(label,cid) for (league,label),cid in self.league_aliases.items() if league == league_name]
            # Only label aliases go into these indexes: a generic catalog spelling
            # must not override a reviewed source-specific alias such as EA Libertad.
            direct = ClubIdentityIndex([], aliases=[(s,cid) for s,cid in explicit if cid in pool])
            generic = venue_identity_index(allowed_ids=pool, records=self.records, aliases=self.aliases)
            self._indexes[league_name] = direct, generic
        direct, generic = self._indexes[league_name]
        if direct.candidates(team_name):
            return direct.resolve(team_name) or ''
        # These EA short forms refer to multiple real clubs in continental data;
        # neither an accent fold nor a generic global alias supplies the missing ID.
        if league_name in _FC_CONMEBOL_LEAGUES and fold_club_name(team_name) in {'u. catolica','racing club'}:
            return ''
        return generic.resolve(team_name) or ''


def identity_manifest_id():
    """Version all reviewed catalog/venue aliases; never cache away a changed file."""
    import hashlib
    root = Path(__file__).resolve().parents[1]
    files = [root / 'config' / 'club_identity.json', *sorted((root/'data'/'priors').glob('aliases_*.json'))]
    h = hashlib.sha256()
    for path in files:
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()
