"""[LEGACY / NOT WIRED] Static World Cup prior ingestion (plan file 10).

Replaced in the club edition by ``ingest/club_prior`` (TRANSFORM_PLAN §3.2 / C3),
which builds a per-competition prior from last season's final table + ClubElo +
the market-implied champion devig. Every production caller imports ``club_prior``;
this module is reachable only by hand, against the archived
``data/priors/ext_sim_v0.json`` snapshot, whose 48-team / 12-group shape the
validator below still enforces. Kept so that snapshot stays loadable — do not
add club logic here.

Loads the fully-specified pre-tournament prior (12 groups x 100k advancement
simulation + FIFA ranks + group draw), validates the advancement identity
(p_first + p_second + p_third_revive ~= p_advance, +/- 2pp), and exposes the
draw/ranks as the single source of truth for the modeling layer.

IRON RULE (plan 10): these are a *stale starting line*, never a tradable signal
or "true" probability. Real results from played matches OVERRIDE them; the prior
weight decays with time. This module only ingests/validates — the decay and
override live in the strength/ensemble layer.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from prediction_market_soccer.config import CONFIG

# Identity-check tolerance (plan 10 §1): source rounding allows +/- 2pp.
_IDENTITY_TOL = 0.02

# Name normalization (plan 10 §4): map source aliases to a canonical English name.
# canonical -> set of accepted aliases (all lowercased on lookup).
TEAM_ALIASES: dict[str, str] = {
    "turkiye": "Turkey",
    "türkiye": "Turkey",
    "usa": "United States",
    "united states of america": "United States",
    "korea": "Korea Republic",
    "south korea": "Korea Republic",
    "ivory coast": "Cote d'Ivoire",
    "cote divoire": "Cote d'Ivoire",
    "dr congo": "DR Congo",
    "congo dr": "DR Congo",
    # API-Football spellings → our prior canonical names.
    "bosnia & herzegovina": "Bosnia and Herzegovina",
    "cape verde islands": "Cape Verde",
    "curaçao": "Curacao",
    # EA Sports FC spellings → our prior canonical names.
    "czech republic": "Czechia",
    "côte d'ivoire": "Cote d'Ivoire",
    "holland": "Netherlands",
    # Kalshi / Polymarket venue spellings → our prior canonical names.
    "ir iran": "Iran",
    "bosnia-herzegovina": "Bosnia and Herzegovina",
    "cabo verde": "Cape Verde",
}


def canonical_team_name(name: str) -> str:
    """Resolve a possibly-aliased team name to its canonical form."""
    key = name.strip().lower()
    return TEAM_ALIASES.get(key, name.strip())


def team_id(name: str) -> str:
    """Stable team_id used across priors/draw/model (canonical, snake_case)."""
    canon = canonical_team_name(name)
    return canon.lower().replace("'", "").replace(".", "").replace(" ", "_")


@dataclass(frozen=True)
class PriorTeam:
    team_id: str
    name: str
    zh: str
    group: str
    fifa_rank: int
    exp_points: float
    p_first: float
    p_second: float
    p_third_revive: float
    p_advance: float

    @property
    def identity_residual(self) -> float:
        """|（p_first+p_second+p_third_revive) - p_advance|."""
        return abs((self.p_first + self.p_second + self.p_third_revive) - self.p_advance)


@dataclass(frozen=True)
class PriorSnapshot:
    prior_id: str
    source: str
    as_of: str
    is_stale: bool
    sim_count: int
    format_rules: str
    teams: tuple[PriorTeam, ...]

    @property
    def by_id(self) -> dict[str, PriorTeam]:
        return {t.team_id: t for t in self.teams}

    @property
    def groups(self) -> dict[str, list[PriorTeam]]:
        out: dict[str, list[PriorTeam]] = {}
        for t in self.teams:
            out.setdefault(t.group, []).append(t)
        return out

    def draw(self) -> dict[str, list[str]]:
        """Group draw: group_code -> [team_id, ...] (plan 10 §3)."""
        return {g: [t.team_id for t in ts] for g, ts in sorted(self.groups.items())}

    def ranks(self) -> dict[str, int]:
        """team_id -> pre-tournament FIFA rank (plan 10 §3)."""
        return {t.team_id: t.fifa_rank for t in self.teams}


class PriorValidationError(ValueError):
    """Raised when the loaded prior fails structural or identity checks."""


def load_prior(json_path: Path | str | None = None) -> PriorSnapshot:
    """Load + validate the static prior JSON (plan 10 §6 step ①).

    Validates the archived WC snapshot's shape: 48 teams, 12 groups of 4, advancement
    identity (+/- 2pp). Club priors are validated by ``club_prior`` instead.
    Raises PriorValidationError on any structural failure; identity violations
    above tolerance are collected and raised together.
    """
    path = Path(json_path) if json_path else CONFIG.paths.prior_ext_sim_v0
    if not path.exists():
        raise PriorValidationError(f"prior file not found: {path}")

    raw = json.loads(path.read_text(encoding="utf-8"))
    teams = tuple(
        PriorTeam(
            team_id=team_id(rec["team"]),
            name=canonical_team_name(rec["team"]),
            zh=rec.get("zh", ""),
            group=rec["group"],
            fifa_rank=int(rec["fifa_rank"]),
            exp_points=float(rec["exp_points"]),
            p_first=float(rec["p_first"]),
            p_second=float(rec["p_second"]),
            p_third_revive=float(rec["p_third_revive"]),
            p_advance=float(rec["p_advance"]),
        )
        for rec in raw["teams"]
    )

    snap = PriorSnapshot(
        prior_id=raw["prior_id"],
        source=raw["source"],
        as_of=raw["as_of"],
        is_stale=bool(raw.get("is_stale", True)),
        sim_count=int(raw["sim_count"]),
        format_rules=raw["format_rules"],
        teams=teams,
    )

    _validate(snap)
    return snap


def _validate(snap: PriorSnapshot) -> None:
    errors: list[str] = []

    if len(snap.teams) != 48:
        errors.append(f"expected 48 teams, got {len(snap.teams)}")

    ids = [t.team_id for t in snap.teams]
    if len(set(ids)) != len(ids):
        dupes = {i for i in ids if ids.count(i) > 1}
        errors.append(f"duplicate team_ids: {sorted(dupes)}")

    groups = snap.groups
    if len(groups) != 12:
        errors.append(f"expected 12 groups, got {len(groups)}")
    for g, members in sorted(groups.items()):
        if len(members) != 4:
            errors.append(f"group {g} has {len(members)} teams (expected 4)")

    # Advancement identity (plan 10 §1) — collect all violators for one report.
    bad_identity = [
        f"{t.name} (residual={t.identity_residual:.3f})"
        for t in snap.teams
        if t.identity_residual > _IDENTITY_TOL
    ]
    if bad_identity:
        errors.append(
            "advancement identity p_first+p_second+p_third_revive != p_advance "
            f"beyond +/-{_IDENTITY_TOL:.0%}: " + "; ".join(bad_identity)
        )

    if errors:
        raise PriorValidationError("; ".join(errors))


if __name__ == "__main__":
    s = load_prior()
    print(f"loaded prior {s.prior_id} (as_of={s.as_of}, stale={s.is_stale})")
    print(f"  {len(s.teams)} teams, {len(s.groups)} groups — identity check PASSED")
    worst = max(s.teams, key=lambda t: t.identity_residual)
    print(f"  worst identity residual: {worst.name} = {worst.identity_residual:.3f}")
