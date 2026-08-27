"""One-off backfill of untapped intra-game data for finished WC fixtures:
lineups, per-player match stats, missing fixture_stats (xG), and injuries.
Deliberate per-run cap raise (analysis backfill, not the daily pipeline)."""
from __future__ import annotations

import dataclasses

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.ingest import store
from prediction_market_soccer.ingest.api_football import ApiFootball
from prediction_market_soccer.ingest import soccer_ingest as si

conn = store.init_db()
# deliberate: one-off historical backfill needs >60 calls
cfg = dataclasses.replace(CONFIG.soccer, max_requests_per_run=300)
api = ApiFootball(conn, cfg)

c = conn
c.row_factory = store.sqlite3.Row if hasattr(store, "sqlite3") else None
fin = [r[0] for r in conn.execute(
    "SELECT api_id FROM fixture WHERE status_short IN ('FT','AET','PEN') ORDER BY kickoff_ts")]
print(f"finished fixtures: {len(fin)}")

have_stats = set(r[0] for r in conn.execute("SELECT DISTINCT fixture_api_id FROM fixture_stats"))
missing_stats = [f for f in fin if f not in have_stats]
print(f"backfilling fixture_stats for {len(missing_stats)} fixtures: {missing_stats}")
si.sync_fixture_stats(api, conn, missing_stats)

si.sync_lineups(api, conn, fin)
si.sync_fixture_players(api, conn, fin)
si.sync_injuries(api, conn, force=True)

print("\n=== coverage after pull ===")
for t in ("fixture_stats", "lineup", "fixture_player_stats", "injury"):
    n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    nf = conn.execute(f"SELECT COUNT(DISTINCT fixture_api_id) FROM {t}").fetchone()[0]
    print(f"  {t:22s} rows={n} fixtures={nf}")
print("daily_request_count now:", store.daily_request_count(conn))
