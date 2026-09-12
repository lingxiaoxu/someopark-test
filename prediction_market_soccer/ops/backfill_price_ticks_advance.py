"""Advance reference ticks, gated by competition/round support and explicit scope."""
from __future__ import annotations


def backfill(conn=None, *, scope=None, fixture_ids=None, writer=None, reader=None, state_conn=None,
             fidelity=1, only_missing=True):
    from prediction_market_soccer.ops.backfill_milestones import collect
    return collect(conn, scope=scope, fixture_ids=fixture_ids, writer=writer, reader=reader,
                   state_conn=state_conn, ticks=True, fidelity=fidelity, market_kind="advance")


if __name__ == "__main__":
    raise SystemExit("Use an explicit CollectionScope and CandidateWriter; unsupported markets are not guessed.")
