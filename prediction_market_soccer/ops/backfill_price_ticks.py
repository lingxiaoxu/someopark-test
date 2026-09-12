"""Bounded history reference ticks; old price_tick rows are never updated."""
from __future__ import annotations


def _kickoff_epoch(iso):
    from prediction_market_soccer.util.research_inputs import epoch
    try:
        return epoch(iso)
    except (ValueError, TypeError):
        return None


def _map_sides(teams, hi, ai):
    """Legacy explicit adapter, with collision rejection; collection uses scoped identity."""
    from prediction_market_soccer.ingest.club_prior import canonical_team_name, team_id
    out = {}
    for label, token in teams.items():
        cid = team_id(canonical_team_name(label))
        side = "draw" if label.casefold().startswith("draw") else "home" if cid == hi else "away" if cid == ai else None
        if side:
            if side in out or token in out.values():
                raise ValueError("ambiguous history sides")
            out[side] = token
    return out


def backfill(conn=None, *, scope=None, fixture_ids=None, writer=None, reader=None, state_conn=None,
             fidelity=1, only_missing=True):
    from prediction_market_soccer.ops.backfill_milestones import collect
    return collect(conn, scope=scope, fixture_ids=fixture_ids, writer=writer, reader=reader,
                   state_conn=state_conn, ticks=True, fidelity=fidelity)


if __name__ == "__main__":
    raise SystemExit("Use an explicit CollectionScope and CandidateWriter; old ticks are immutable.")
