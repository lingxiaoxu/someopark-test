"""EA Sports FC 26 player ratings → talent prior (plan 03 §6.1 grounding).

The golden-boot forecast was previously driven by tiny in-tournament samples — a
weak-team forward scoring twice in the opener inflated his rate and head start,
ranking him near the top even though his team plays few matches. FC ratings fix
that root cause: they give EVERY player a talent-grounded per-match goal rate
(finishing / positioning / shot power / overall), which becomes the Bayesian
PRIOR. The 1-game WC burst then regresses toward true talent, and the final boot
is dominated by talent x knockout depth (matches actually played) — exactly the
"半数金靴来自决赛/半决赛" structure.

Data: Kaggle `justdhia/ea-sports-fc-26-player-ratings` (CC0, refreshed in-season).
The CSV(s) live, un-versioned, under data/raw/fc26/ (download via the Kaggle CLI).
Only players whose nationality maps to one of the 48 WC teams are ingested.
"""
from __future__ import annotations

import math
from pathlib import Path

from prediction_market.config import CONFIG
from prediction_market.ingest.prior_ingest import load_prior, team_id

# ── FC rating → per-match international goal-rate map ─────────────────────────
# A "goal threat" composite (finishing-led) feeds an exponential anchored at two
# real reference points so the curve reproduces expert intuition:
#   elite ST  (gt~91, e.g. Kane/Mbappé) -> ~0.70 goals/match
#   squad ST  (gt~78, e.g. Balogun)      -> ~0.32 goals/match
# A position multiplier damps non-strikers (a CB's finishing is already low, but
# we damp again so a high-overall defender never reads as a boot threat).
_GT_W = {"finishing": 0.42, "positioning": 0.28, "shot_power": 0.15, "overall": 0.15}
_POS_MULT = {"Attack": 1.0, "Midfielder": 0.55, "Defense": 0.12}
_ANCHOR_HI, _RATE_HI = 91.0, 0.70
_ANCHOR_LO, _RATE_LO = 78.0, 0.32
_K = math.log(_RATE_HI / _RATE_LO) / (_ANCHOR_HI - _ANCHOR_LO)


def fc_goal_rate(finishing: float, positioning: float, shot_power: float,
                 overall: float, position_type: str) -> float:
    """Talent-grounded per-match goal-rate prior from FC 26 attributes."""
    gt = (_GT_W["finishing"] * finishing + _GT_W["positioning"] * positioning
          + _GT_W["shot_power"] * shot_power + _GT_W["overall"] * overall)
    pm = _POS_MULT.get(position_type, 0.30)
    return float(pm * _RATE_HI * math.exp(_K * (gt - _ANCHOR_HI)))


def _s(v) -> str:
    """Coerce a possibly-NaN cell to a clean string."""
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() == "nan" else s


def _player_name(row) -> str:
    common = _s(row.get("commonName"))
    if common:
        return common
    return (_s(row.get("firstName")) + " " + _s(row.get("lastName"))).strip() or "Unknown"


KAGGLE_DATASET = "justdhia/ea-sports-fc-26-player-ratings"


def download_fc26(dest: Path | str | None = None) -> Path:
    """Pull the latest FC 26 rating CSVs from Kaggle (CC0) into data/raw/fc26/.

    Auth is read from the env var KAGGLE_API_TOKEN (kept in the gitignored
    prediction_market/.env, never hardcoded). Returns the destination dir. Raises
    RuntimeError if the token is absent or the CLI call fails — callers that want
    the pipeline to continue on a stale-but-present CSV should catch it.
    """
    import os
    import subprocess

    dest = Path(dest) if dest else CONFIG.paths.fc_raw
    dest.mkdir(parents=True, exist_ok=True)
    if not os.environ.get("KAGGLE_API_TOKEN") and not os.environ.get("KAGGLE_KEY"):
        raise RuntimeError(
            "KAGGLE_API_TOKEN not set — add it to prediction_market/.env "
            "(gitignored) to refresh FC 26 data.")
    cmd = ["kaggle", "datasets", "download", "-d", KAGGLE_DATASET,
           "-p", str(dest), "--unzip", "--force"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"kaggle download failed: {res.stderr.strip()[:300]}")
    return dest


def load_fc_frame(csv_path: Path | str | None = None):
    """Read the FC 26 outfield+GK player table (pandas DataFrame)."""
    import pandas as pd

    path = Path(csv_path) if csv_path else CONFIG.paths.fc_raw / "ea_fc26_players.csv"
    if not path.exists():
        raise FileNotFoundError(
            f"FC 26 player CSV not found at {path}. Download via:\n"
            "  kaggle datasets download -d justdhia/ea-sports-fc-26-player-ratings "
            f"-p {CONFIG.paths.fc_raw} --unzip"
        )
    return pd.read_csv(path)


def ingest_fc_players(conn=None, csv_path: Path | str | None = None) -> int:
    """Map FC 26 players to WC teams, compute goal-rate prior, upsert fc_player.

    Keeps only players whose nationality resolves to one of the 48 WC teams.
    De-dupes (team, last-name) keeping the highest-overall entry (drops youth /
    namesake clutter). Ranks players within a team by goal-rate so the golden-boot
    builder can derive a start probability. Returns rows written.
    """
    import pandas as pd

    from prediction_market.ingest import store

    conn = conn or store.init_db()
    df = load_fc_frame(csv_path)

    wc_team_ids = {t.team_id for t in load_prior().teams}

    # Resolve nationality → canonical WC team id; drop non-WC nationalities.
    df = df.copy()
    df["canon"] = df["nationality"].fillna("").map(lambda n: team_id(n) if n else "")
    df = df[df["canon"].isin(wc_team_ids)]
    if df.empty:
        return 0

    # De-dupe per (team, last-name): keep highest overall.
    df["last"] = df["lastName"].fillna(df["commonName"].fillna("")).str.lower().str.strip()
    df = (df.sort_values("overallRating", ascending=False)
            .drop_duplicates(subset=["canon", "last"], keep="first"))

    # Designated penalty taker: the single best `penalties` attribute per team
    # among attackers/mids (informational; the sim folds PKs into the goal rate).
    pen_best: dict[str, tuple[int, float]] = {}
    for _, r in df.iterrows():
        if r["positionType"] in ("Attack", "Midfielder"):
            cur = pen_best.get(r["canon"])
            pen = float(r.get("penalties") or 0)
            if cur is None or pen > cur[1]:
                pen_best[r["canon"]] = (int(r["id"]), pen)

    # Compute rate + within-team attacking rank.
    df["goal_rate"] = df.apply(
        lambda r: fc_goal_rate(
            float(r.get("finishing") or 0), float(r.get("positioning") or 0),
            float(r.get("shotPower") or 0), float(r.get("overallRating") or 0),
            str(r.get("positionType") or ""),
        ),
        axis=1,
    )
    df["rank"] = df.groupby("canon")["goal_rate"].rank(ascending=False, method="first").astype(int)

    conn.execute("DELETE FROM fc_player")
    written = 0
    now = store.utcnow()
    for _, r in df.iterrows():
        cid = r["canon"]
        is_pen = 1 if pen_best.get(cid, (None,))[0] == int(r["id"]) else 0
        conn.execute(
            "INSERT OR REPLACE INTO fc_player (fc_id, name, nationality, canonical_team_id, "
            "position, position_type, overall, finishing, positioning, shot_power, penalties, "
            "sho, pen_taker, goal_rate, team_attack_rank, source, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                int(r["id"]), _player_name(r), _s(r.get("nationality")), cid,
                _s(r.get("position")), _s(r.get("positionType")),
                int(r.get("overallRating") or 0), int(r.get("finishing") or 0),
                int(r.get("positioning") or 0), int(r.get("shotPower") or 0),
                int(r.get("penalties") or 0), int(r.get("sho") or 0),
                is_pen, float(r["goal_rate"]), int(r["rank"]), "ea_fc26", now,
            ),
        )
        written += 1
    conn.commit()
    return written


if __name__ == "__main__":
    from prediction_market.ingest import store

    conn = store.init_db()
    n = ingest_fc_players(conn)
    print(f"ingested {n} FC 26 players mapped to WC teams")
    rows = conn.execute(
        "SELECT name, canonical_team_id, overall, finishing, goal_rate, pen_taker "
        "FROM fc_player ORDER BY goal_rate DESC LIMIT 12"
    ).fetchall()
    print(f"{'player':<22}{'team':<16}{'ovr':>4}{'fin':>4}{'rate':>7} pk")
    for r in rows:
        print(f"{r[0][:21]:<22}{r[1][:15]:<16}{r[2]:>4}{r[3]:>4}{r[4]:>7.3f}  {r[5]}")
