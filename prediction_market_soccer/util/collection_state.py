"""Bounded Soccer collection scopes and retry state, separate from observations."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json


def _dt(value):
    d = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('Collection clocks require UTC-aware timestamps')
    return d.astimezone(timezone.utc)


@dataclass(frozen=True)
class CollectionScope:
    scope_id: str
    fixture_ids: tuple[int, ...]
    market_kinds: tuple[str, ...]
    window_start: str
    window_end: str
    now: str
    max_requests: int = 48
    target_ids: tuple[str, ...] = ()
    collector_version: str = 'soccer-collection-v2'

    def __post_init__(self):
        if len(set(self.fixture_ids)) != len(self.fixture_ids):
            raise ValueError('Duplicate fixtures in fixed scope')
        if not self.scope_id or self.max_requests < 0 or _dt(self.window_start) > _dt(self.window_end):
            raise ValueError('Invalid collection scope')
        if _dt(self.window_end) > _dt(self.now):
            raise ValueError('Collection window cannot extend beyond fixed now')

    @property
    def fixture_hash(self):
        return hashlib.sha256(json.dumps(self.fixture_ids).encode()).hexdigest()

    def manifest(self):
        return {**vars(self), 'fixture_scope_hash': self.fixture_hash}


def ensure(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS collection_task_state_v1 (
        scope_id TEXT, collector TEXT, collector_version TEXT, fixture_id INTEGER, target_id TEXT,
        status TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
        last_attempt_at TEXT, next_retry_at TEXT, last_complete_at TEXT, reason TEXT,
        observation_ids TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY(scope_id,collector,collector_version,fixture_id,target_id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS collection_attempt_v1 (
        attempt_id INTEGER PRIMARY KEY, scope_id TEXT, collector TEXT, collector_version TEXT,
        fixture_id INTEGER, target_id TEXT, at TEXT, status TEXT, reason TEXT, observation_ids TEXT)''')
    for operation in ('UPDATE', 'DELETE'):
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS collection_attempt_v1_{operation.lower()}_guard
            BEFORE {operation} ON collection_attempt_v1
            BEGIN SELECT RAISE(ABORT, 'immutable collection attempt'); END''')


_TERMINAL = frozenset(('complete', 'reconstructed_usable', 'unsupported_market'))
_RETRYABLE = frozenset(('retryable', 'pending', 'partial', 'unavailable', 'source_unavailable',
                       'identity_unresolved', 'identity_conflict', 'time_ambiguous', 'invalid', 'not_listed'))


def record_attempt(conn, scope, collector, fixture_id, target_id, status, reason=None,
                   observation_ids=(), *, at=None):
    ensure(conn)
    if fixture_id not in scope.fixture_ids:
        raise ValueError('Collection result is outside the declared fixture scope')
    if scope.target_ids and target_id not in scope.target_ids:
        raise ValueError('Collection result is outside the declared targets')
    if status not in _TERMINAL | _RETRYABLE:
        raise ValueError('Unknown collection outcome')
    if status in ('complete', 'reconstructed_usable') and not observation_ids:
        raise ValueError('A completed collection must reference persisted observations')
    at = at or scope.now
    _dt(at)
    key = (scope.scope_id, collector, scope.collector_version, fixture_id, target_id)
    old = conn.execute('''SELECT attempt_count,last_complete_at FROM collection_task_state_v1
        WHERE scope_id=? AND collector=? AND collector_version=? AND fixture_id=? AND target_id=?''', key).fetchone()
    attempts = (old[0] if old else 0) + 1
    delay = min(3600, 60 * 2 ** min(attempts - 1, 6))
    next_retry = None if status in _TERMINAL else (_dt(at) + timedelta(seconds=delay)).isoformat()
    completed = at if status in ('complete', 'reconstructed_usable') else (old[1] if old else None)
    refs = json.dumps(list(observation_ids))
    conn.execute('INSERT INTO collection_attempt_v1 VALUES (NULL,?,?,?,?,?,?,?,?,?)',
                 (*key, at, status, reason, refs))
    conn.execute('''INSERT INTO collection_task_state_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(scope_id,collector,collector_version,fixture_id,target_id) DO UPDATE SET
        status=excluded.status,attempt_count=excluded.attempt_count,last_attempt_at=excluded.last_attempt_at,
        next_retry_at=excluded.next_retry_at,last_complete_at=excluded.last_complete_at,
        reason=excluded.reason,observation_ids=excluded.observation_ids''',
        (*key, status, attempts, at, next_retry, completed, reason, refs))


def due_tasks(conn, scope, collector, now=None):
    ensure(conn)
    at = _dt(now or scope.now)
    result = []
    targets = scope.target_ids or ('all',)
    for fid in scope.fixture_ids:
        for target in targets:
            row = conn.execute('''SELECT status,next_retry_at,attempt_count FROM collection_task_state_v1
                WHERE scope_id=? AND collector=? AND collector_version=? AND fixture_id=? AND target_id=?''',
                (scope.scope_id, collector, scope.collector_version, fid, target)).fetchone()
            if row and (row[0] in _TERMINAL or row[1] and _dt(row[1]) > at):
                continue
            result.append({'fixture_id': fid, 'target_id': target, 'attempt_count': row[2] if row else 0})
    return result


def daily_scope(conn, *, now=None, since_days=14, limit=12, collector='milestones'):
    """Select due fixtures fairly: untried first, then least recently serviced."""
    from prediction_market_soccer.config.leagues import active
    at = _dt(now or datetime.now(timezone.utc).isoformat())
    start = at - timedelta(days=since_days)
    leagues = [c.api_football_id for c in active()]
    if since_days <= 0 or limit < 0:
        raise ValueError('Daily scope requires a positive bounded window')
    rows = conn.execute('''SELECT api_id,kickoff_ts FROM fixture WHERE status_short IN ('FT','AET','PEN')
        AND home_goals IS NOT NULL AND kickoff_ts>=? AND kickoff_ts<=?
        AND league_id IN (''' + ','.join('?' for _ in leagues) + ') ORDER BY kickoff_ts DESC',
        [start.isoformat(), at.isoformat(), *leagues]).fetchall() if leagues else []
    targets = tuple(f'{m}:{side}:match' for m in ('PRE','T15','T30','HT','T60','T75')
                    for side in ('home','draw','away')) if collector == 'milestones' else tuple(
                        f'tick:{side}:match' for side in ('home','draw','away'))
    scope = CollectionScope('daily-soccer', tuple(r[0] for r in rows), ('match',),
                            start.isoformat(), at.isoformat(), at.isoformat(), target_ids=targets)
    due_fixtures = {r['fixture_id'] for r in due_tasks(conn, scope, collector)}
    # A persistently unavailable recent fixture must not monopolize every batch.
    # Count service to any target, including a budget-limited attempt, so each
    # still-eligible fixture gets its turn. Other collectors/versions and targets
    # cannot advance this queue's service clock. due_tasks retains all cooldowns.
    serviced = {}
    for fid, stamp in conn.execute('''SELECT fixture_id,last_attempt_at FROM collection_task_state_v1
        WHERE scope_id=? AND collector=? AND collector_version=? AND target_id IN ('''
            + ','.join('?' for _ in targets) + ')',
            (scope.scope_id, collector, scope.collector_version, *targets)):
        if stamp:
            parsed = _dt(stamp)
            if fid not in serviced or parsed > serviced[fid]:
                serviced[fid] = parsed
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    ordered = sorted((r for r in rows if r[0] in due_fixtures),
                     key=lambda row: (serviced.get(row[0], oldest), _dt(row[1]), row[0]))
    ids = tuple(row[0] for row in ordered[:limit])
    return replace(scope, fixture_ids=ids)
