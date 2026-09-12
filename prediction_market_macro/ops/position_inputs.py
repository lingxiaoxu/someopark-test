"""Refresh the venue evidence required by every live held-position exit caller."""
from __future__ import annotations


def fresh_held_tickers(conn, md, *, before_each=None) -> set[str]:
    """Only successful metadata + quote requests authorize this exit pass.

    Old, age-fresh quotes remain useful evidence but cannot substitute for a failed
    request. Any whole-pass failure therefore returns an empty eligibility set.
    """
    from prediction_market_macro.ops.ledger import open_positions
    try:
        tickers = {f["ticker"] for pos in open_positions(conn) for f in pos["fills"]}
        if not tickers:
            return set()
        result = md.snapshot_tickers(tickers, before_each=before_each)
        if result["failed"]:
            print(f"  ! held exit input refresh: {result['failed']}")
        return tickers.intersection(result["refreshed"]).difference(result["failed"])
    except Exception as exc:
        print(f"  ! held exit input refresh failed: {exc}")
        return set()
