"""API-Football (api-sports.io) client — the primary live + 2026 source (plan 02).

Host: https://v3.football.api-sports.io   Auth header: x-apisports-key
Standard envelope: {get, parameters, errors, results, paging, response}.

FRUGALITY IS A FIRST-CLASS CONCERN (user constraint: 7000 req/month, pull once,
store centrally, never re-pull). This client:
  * refuses to exceed the monthly budget or a per-run safety cap (budget guard);
  * logs EVERY call (endpoint, params, rate-limit headers) to the store;
  * writes the raw response to an append-only snapshot for replay;
  * auto-follows pagination (each page is one billed request);
  * retries 429 / 5xx with exponential backoff.

The orchestrator (soccer_ingest) decides WHETHER to call (TTL/watermark gate);
this client decides HOW to call safely. Separation keeps request usage auditable.
"""
from __future__ import annotations

import sqlite3
import time

import requests

from prediction_market_soccer.config import CONFIG, SoccerConfig
from prediction_market_soccer.ingest import store


class BudgetExceededError(RuntimeError):
    """Raised before any call that would breach the monthly or per-run budget."""


class ApiFootballError(RuntimeError):
    """Raised when the API returns an error envelope or a non-retryable status."""


class ApiFootball:
    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: SoccerConfig | None = None,
        *,
        session: requests.Session | None = None,
        max_retries: int = 6,   # must outlast one minute-cap window (5+10+20+40s > 60s)
        timeout: float = 20.0,
    ):
        self.conn = conn
        self.cfg = cfg or CONFIG.soccer
        if not self.cfg.api_key:
            raise ApiFootballError("API_FOOTBALL_KEY not set; source prediction_market_soccer/.env first")
        self.session = session or requests.Session()
        self.session.headers.update({"x-apisports-key": self.cfg.api_key})
        self.max_retries = max_retries
        self.timeout = timeout
        self._run_count = 0  # billed calls this client instance has made

    # ── budget guard ─────────────────────────────────────────────────────────
    def _check_budget(self, *, billed: bool) -> None:
        if self._run_count >= self.cfg.max_requests_per_run:
            raise BudgetExceededError(
                f"per-run cap reached ({self.cfg.max_requests_per_run}); raise max_requests_per_run "
                "deliberately if this run genuinely needs more"
            )
        if billed:
            # API-Football bills per DAY (UTC reset) — the daily cap is the real guard.
            day_used = store.daily_request_count(self.conn)
            if day_used >= getattr(self.cfg, "daily_budget", 7500):
                raise BudgetExceededError(
                    f"daily budget exhausted ({day_used}/{self.cfg.daily_budget})"
                )
            used = store.monthly_request_count(self.conn)
            if used >= self.cfg.monthly_budget:
                raise BudgetExceededError(
                    f"monthly budget exhausted ({used}/{self.cfg.monthly_budget}) — backstop"
                )

    def requests_used_this_month(self) -> int:
        return store.monthly_request_count(self.conn)

    # ── core request ─────────────────────────────────────────────────────────
    # Per-MINUTE pacing: club scale does per-fixture detail loops (a busy Saturday
    # finishes 20+ matches at once) and API-Football signals the minute cap as
    # HTTP 200 + {"errors": {"rateLimit": ...}} — not 429. 0.25s ⇒ ≤240/min,
    # under every plan tier's minute cap; negligible for the small live calls.
    _MIN_INTERVAL_S = 0.25
    _last_request_ts = 0.0

    def _request_page(self, endpoint: str, params: dict, *, billed: bool) -> dict:
        self._check_budget(billed=billed)
        url = f"{self.cfg.api_host}/{endpoint.lstrip('/')}"
        backoff = 1.0
        rl_backoff = 5.0
        for attempt in range(self.max_retries):
            wait = ApiFootball._last_request_ts + self._MIN_INTERVAL_S - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            resp = self.session.get(url, params=params, timeout=self.timeout)
            ApiFootball._last_request_ts = time.monotonic()
            remaining = resp.headers.get("x-ratelimit-requests-remaining")
            limit = resp.headers.get("x-ratelimit-requests-limit")
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(backoff)
                backoff *= 2
                continue
            j = resp.json()
            self._run_count += 1 if billed else 0
            store.log_api_call(
                self.conn, endpoint, params,
                http_status=resp.status_code, results_count=j.get("results", 0),
                req_remaining=int(remaining) if remaining and remaining.isdigit() else None,
                req_limit=int(limit) if limit and limit.isdigit() else None,
            )
            store.write_raw_snapshot(self.conn, endpoint.strip("/").replace("/", "_"), params, j)
            self.conn.commit()
            errors = j.get("errors")
            if errors:  # API-Football returns {} or [] when clean
                if isinstance(errors, dict) and "rateLimit" in errors:
                    # minute-cap hit: wait out the window and retry (5s→10s→20s…)
                    print(f"[api] minute rate-limit on {endpoint} — backing off {rl_backoff:.0f}s")
                    time.sleep(rl_backoff)
                    rl_backoff = min(rl_backoff * 2, 65.0)
                    continue
                raise ApiFootballError(f"{endpoint} errors: {errors}")
            return j
        raise ApiFootballError(f"{endpoint} failed after {self.max_retries} retries (last {resp.status_code})")

    def get(self, endpoint: str, params: dict | None = None, *, paginate: bool = True, billed: bool = True) -> list:
        """GET an endpoint, auto-following pagination. Returns the merged ``response`` list."""
        params = dict(params or {})
        out: list = []
        page = 1
        while True:
            if paginate:
                params["page"] = page
            j = self._request_page(endpoint, params, billed=billed)
            out.extend(j.get("response", []))
            paging = j.get("paging") or {}
            current, total = paging.get("current", 1), paging.get("total", 1)
            if not paginate or current >= total:
                return out
            page += 1

    # ── convenience wrappers (exact API-Football endpoints, plan 02 §2b) ──────
    def status(self) -> dict:
        """Account/subscription/usage. NOT billed by API-Football."""
        j = self._request_page("status", {}, billed=False)
        return j.get("response", {})

    def leagues(self, **params) -> list:
        return self.get("leagues", params, paginate=False)

    def teams(self, **params) -> list:
        return self.get("teams", params, paginate=False)

    def standings(self, **params) -> list:
        return self.get("standings", params, paginate=False)

    # NOTE: most API-Football endpoints are NOT paginated and reject a `page`
    # param ("The Page field do not exist."). Only `players` paginates.
    def fixtures(self, **params) -> list:
        return self.get("fixtures", params, paginate=False)

    def fixtures_by_ids(self, fixture_ids: list[int]) -> list:
        """Batch fixture details, ≤20 ids per call, with EMBEDDED events.

        The big frugality win (official guide): one request returns up to 20
        fixtures each carrying its `events` list, instead of one /events call
        per fixture. Auto-chunks larger id lists into 20-wide batches.
        """
        out: list = []
        for i in range(0, len(fixture_ids), 20):
            chunk = fixture_ids[i:i + 20]
            ids = "-".join(str(x) for x in chunk)
            out.extend(self.get("fixtures", {"ids": ids}, paginate=False))
        return out

    def fixture_events(self, fixture_id: int) -> list:
        return self.get("fixtures/events", {"fixture": fixture_id}, paginate=False)

    def rounds(self, *, current: bool = False) -> list:
        params = {"league": self.cfg.league_id, "season": self.cfg.season}
        if current:
            params["current"] = "true"
        return self.get("fixtures/rounds", params, paginate=False)

    def lineups(self, fixture_id: int) -> list:
        return self.get("fixtures/lineups", {"fixture": fixture_id}, paginate=False)

    def fixture_players(self, fixture_id: int) -> list:
        """Per-player per-match statistics (rating, shots, passes, dribbles, duels,
        tackles, fouls). One request per fixture; the richest intra-game source —
        previously never pulled. Returns the per-team `response` blocks, each with
        a `players` list carrying a single-element `statistics` array."""
        return self.get("fixtures/players", {"fixture": fixture_id}, paginate=False)

    def head_to_head(self, h2h: str, **params) -> list:
        return self.get("fixtures/headtohead", {"h2h": h2h, **params}, paginate=False)

    def injuries(self, **params) -> list:
        return self.get("injuries", params, paginate=False)

    def predictions(self, fixture_id: int) -> list:
        return self.get("predictions", {"fixture": fixture_id}, paginate=False)

    def odds(self, fixture_id: int) -> list:
        return self.get("odds", {"fixture": fixture_id}, paginate=False)

    def odds_live(self, **params) -> list:
        """In-play bookmaker odds (Odds In-Play). One call returns every live fixture's
        current odds; optionally filter by `fixture=` / `league=`. Used as an independent
        third price source for the live-odds cross-validation tactic."""
        return self.get("odds/live", params, paginate=False)

    def players(self, **params) -> list:
        return self.get("players", params, paginate=True)

    def squads(self, team_id: int) -> list:
        return self.get("players/squads", {"team": team_id}, paginate=False)

    def topscorers(self, **params) -> list:
        return self.get("players/topscorers", params, paginate=False)


if __name__ == "__main__":
    conn = store.init_db()
    api = ApiFootball(conn)
    st = api.status()
    acct = st.get("account", {})
    sub = st.get("subscription", {})
    reqs = st.get("requests", {})
    print("API-Football status OK")
    print(f"  account: {acct.get('firstname','?')} plan={sub.get('plan','?')} active={sub.get('active','?')}")
    print(f"  requests today: {reqs.get('current','?')}/{reqs.get('limit_day','?')}")
    print(f"  our local monthly count (excl status): {api.requests_used_this_month()}")
