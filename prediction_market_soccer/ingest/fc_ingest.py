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

CLUB EDITION (TRANSFORM_PLAN C6/§3.8-a): the WC module filtered players by
NATIONALITY onto 48 national teams; here the axis flips to the native `team`
(+`leagueName` disambiguation) columns — verified present in the CSV — matched
against club_registry. Coverage gaps (Brasileirão has no FC26 license) fall back
per §3.8-e: missing clubs simply have no fc_player rows; downstream blends
z-score over present clubs only and neutral-fill the rest.
"""
from __future__ import annotations

import difflib
import math
import re
from pathlib import Path

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.ingest.soccer_ingest import club_id_of

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

# ── curated EA→registry club aliases (§3.8-a) ─────────────────────────────────
# EA short forms ("Man Utd", "OM"), sponsor/legal suffixes ("Sevilla FC"), and
# the unlicensed Serie A fantasy names (verified by star players in the CSV:
# Lombardia=Lautaro/Barella⇒Inter, Milano=Maignan/Leão⇒AC Milan, Latium⇒Lazio,
# Bergamo=Lookman⇒Atalanta). Keyed by (EA leagueName, EA team) because bare
# names collide across leagues ('Nacional' exists in Liga Portugal too).
# Never-guess rule: ambiguous EA rows (the second 'Racing Club', 'U. Católica')
# are deliberately NOT mapped. EA clubs absent from our registry (relegated in
# this timeline) simply drop.
_EA_ALIASES: dict[tuple[str, str], str] = {
    # Premier League (+ promoted, licensed under EFL Championship)
    ("Premier League", "Man Utd"): "manchester_united",
    ("Premier League", "Newcastle Utd"): "newcastle",
    ("Premier League", "Spurs"): "tottenham",
    ("Premier League", "Nott'm Forest"): "nottingham_forest",
    ("Premier League", "Leeds United"): "leeds",
    ("Premier League", "AFC Bournemouth"): "bournemouth",
    ("EFL Championship", "Coventry City"): "coventry",
    # LaLiga (+ promoted, licensed under LALIGA HYPERMOTION)
    ("LALIGA EA SPORTS", "Atlético de Madrid"): "atletico_madrid",
    ("LALIGA EA SPORTS", "Celta"): "celta_vigo",
    ("LALIGA EA SPORTS", "CA Osasuna"): "osasuna",
    ("LALIGA EA SPORTS", "Valencia CF"): "valencia",
    ("LALIGA EA SPORTS", "Getafe CF"): "getafe",
    ("LALIGA EA SPORTS", "RCD Espanyol"): "espanyol",
    ("LALIGA EA SPORTS", "Sevilla FC"): "sevilla",
    ("LALIGA EA SPORTS", "D. Alavés"): "alaves",
    ("LALIGA EA SPORTS", "Elche CF"): "elche",
    ("LALIGA EA SPORTS", "Levante UD"): "levante",
    ("LALIGA HYPERMOTION", "Málaga CF"): "malaga",
    ("LALIGA HYPERMOTION", "RC Deportivo"): "deportivo_la_coruna",
    ("LALIGA HYPERMOTION", "R. Racing Club"): "racing_santander",
    # Serie A (unlicensed fantasy names + legal forms)
    ("Serie A Enilive", "Lombardia FC"): "inter",
    ("Serie A Enilive", "Milano FC"): "ac_milan",
    ("Serie A Enilive", "Latium"): "lazio",
    ("Serie A Enilive", "Bergamo Calcio"): "atalanta",
    ("Serie A Enilive", "SSC Napoli"): "napoli",
    # Bundesliga
    ("Bundesliga", "Leverkusen"): "bayer_leverkusen",
    ("Bundesliga", "TSG Hoffenheim"): "1899_hoffenheim",
    ("Bundesliga", "Frankfurt"): "eintracht_frankfurt",
    ("Bundesliga", "M'gladbach"): "borussia_mnchengladbach",
    # Ligue 1 (+ Le Mans from Ligue 2)
    ("Ligue 1 McDonald's", "Paris SG"): "paris_saint_germain",
    ("Ligue 1 McDonald's", "OM"): "marseille",
    ("Ligue 1 McDonald's", "OL"): "lyon",
    ("Ligue 1 McDonald's", "AS Monaco"): "monaco",
    ("Ligue 1 McDonald's", "Stade Rennais FC"): "rennes",
    ("Ligue 1 McDonald's", "OGC Nice"): "nice",
    ("Ligue 1 McDonald's", "LOSC Lille"): "lille",
    ("Ligue 1 McDonald's", "Toulouse FC"): "toulouse",
    ("Ligue 1 McDonald's", "RC Lens"): "lens",
    ("Ligue 1 McDonald's", "AJ Auxerre"): "auxerre",
    ("Ligue 1 McDonald's", "Angers SCO"): "angers",
    ("Ligue 1 McDonald's", "FC Lorient"): "lorient",
    ("Ligue 1 McDonald's", "Havre AC"): "le_havre",
    ("Ligue 2 BKT", "Le Mans FC"): "le_mans",
    # Argentina LPF (EA short names; 'Estudiantes'/'Gimnasia' = the La Plata
    # first-division clubs — the Rio Cuarto / Mendoza namesakes stay unmapped)
    ("LPF", "Belgrano"): "belgrano_cordoba",
    ("LPF", "Central Córdoba"): "central_cordoba_de_santiago",
    ("LPF", "Defensa"): "defensa_y_justicia",
    ("LPF", "Dep. Riestra"): "deportivo_riestra",
    ("LPF", "Estudiantes"): "estudiantes_l_p",
    ("LPF", "Gimnasia"): "gimnasia_l_p",
    ("LPF", "Ind. Rivadavia"): "independ_rivadavia",
    ("LPF", "Instituto"): "instituto_cordoba",
    ("LPF", "Lanús"): "lanus",
    ("LPF", "Newell's"): "newells_old_boys",
    ("LPF", "Sarmiento"): "sarmiento_junin",
    ("LPF", "Talleres"): "talleres_cordoba",
    ("LPF", "Unión"): "union_santa_fe",
    # CONMEBOL pseudo-leagues (EA context pins country identity)
    ("Libertadores", "Atl. Nacional"): "atletico_nacional",
    ("Libertadores", "Nacional"): "club_nacional",
    ("Libertadores", "U. de Chile"): "universidad_de_chile",
    ("Libertadores", "IDV"): "independiente_del_valle",
    ("Libertadores", "Dep. Táchira"): "deportivo_tachira_fc",
    ("Libertadores", "San Antonio"): "san_antonio_bulo_bulo",
    ("Libertadores", "Libertad"): "libertad_asuncion",
    ("Sudamericana", "Guaraní"): "club_guarani",
}


def download_fc26(dest: Path | str | None = None) -> Path:
    """Pull the latest FC 26 rating CSVs from Kaggle (CC0) into data/raw/fc26/.

    Auth is read from the env var KAGGLE_API_TOKEN (kept in the gitignored
    prediction_market_soccer/.env, never hardcoded). Returns the destination dir. Raises
    RuntimeError if the token is absent or the CLI call fails — callers that want
    the pipeline to continue on a stale-but-present CSV should catch it.
    """
    import os
    import subprocess

    dest = Path(dest) if dest else CONFIG.paths.fc_raw
    dest.mkdir(parents=True, exist_ok=True)
    if not os.environ.get("KAGGLE_API_TOKEN") and not os.environ.get("KAGGLE_KEY"):
        raise RuntimeError(
            "KAGGLE_API_TOKEN not set — add it to prediction_market_soccer/.env "
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
    """Map FC 26 players to CLUBS (team column), compute goal-rate prior, upsert
    fc_player. canonical_team_id now holds the club_id from club_registry.

    Matching: exact `club_id_of(team)` against the registry first; a constrained
    fuzzy pass (cutoff 0.85) catches EA spelling variants; everything else drops
    (never guessed — §3.8-e). De-dupes (club, last-name) keeping highest overall;
    ranks players within a club by goal-rate. Returns rows written.
    """
    import pandas as pd

    from prediction_market_soccer.ingest import store

    conn = conn or store.init_db()
    df = load_fc_frame(csv_path)

    regs = conn.execute(
        "SELECT DISTINCT club_id, name FROM club_registry").fetchall()
    registry_ids = {r["club_id"] for r in regs}
    reg_names = {r["name"]: r["club_id"] for r in regs}
    if not registry_ids:
        print("[fc_ingest] club_registry empty — run soccer_ingest --scope static first")
        return 0

    fuzzy_cache: dict[str, str] = {}

    def club_of(team_name: str, league_name: str = "") -> str:
        ali = _EA_ALIASES.get((league_name, team_name))
        if ali in registry_ids:
            return ali
        cid = club_id_of(team_name)
        if cid in registry_ids:
            return cid
        if team_name in fuzzy_cache:
            return fuzzy_cache[team_name]
        best = difflib.get_close_matches(team_name, list(reg_names), n=1, cutoff=0.85)
        out = reg_names[best[0]] if best else ""
        fuzzy_cache[team_name] = out
        return out

    df = df.copy()
    df["canon"] = df.apply(
        lambda r: club_of(_s(r.get("team")), _s(r.get("leagueName"))) if _s(r.get("team")) else "",
        axis=1)
    n_total = len(df)
    df = df[df["canon"] != ""]
    print(f"[fc_ingest] matched {len(df)}/{n_total} FC26 players to registry clubs "
          f"({len(set(df['canon']))} clubs)")
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
    from prediction_market_soccer.ingest import store

    conn = store.init_db()
    n = ingest_fc_players(conn)
    print(f"ingested {n} FC 26 players mapped to registry clubs")
    rows = conn.execute(
        "SELECT name, canonical_team_id, overall, finishing, goal_rate, pen_taker "
        "FROM fc_player ORDER BY goal_rate DESC LIMIT 12"
    ).fetchall()
    print(f"{'player':<22}{'team':<16}{'ovr':>4}{'fin':>4}{'rate':>7} pk")
    for r in rows:
        print(f"{r[0][:21]:<22}{r[1][:15]:<16}{r[2]:>4}{r[3]:>4}{r[4]:>7.3f}  {r[5]}")
