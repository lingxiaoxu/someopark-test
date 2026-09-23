"""Causal features from existing recorder caches; no I/O or execution imports.

All inputs must belong to one coin and contain at least 310 seconds of received
history. ``valid`` covers prices/books; ``flow_valid`` additionally requires a
fresh, minimally populated observed-print sample. Neither proves complete venue
trade coverage. No field is a difference of rolling 24-hour volume.
"""
from collections import defaultdict
import math


def _finite(value):
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _known(rows, at):
    return sorted((r for r in rows
                   if _finite(r.get("recv_ts")) is not None
                   and float(r["recv_ts"]) <= at),
                  key=lambda r: float(r["recv_ts"]))


def compute_features(context_rows, book_rows, trade_rows, decision_ts):
    """Return price/book features and separately qualified sampled-flow features.

    Flow requires >=3 distinct nonempty valid batches in the past 60 seconds and
    the newest valid batch <=15 seconds old. Prints delayed >15 seconds are
    excluded. Missing both trade ID and meaningful hash makes flow unavailable;
    anonymous rows are fingerprinted rather than all collapsing into one None ID.
    """
    t = _finite(decision_ts)
    out = {"decision_ts": t, "valid": False, "flow_valid": False,
           "errors": [], "flow_errors": []}
    if t is None:
        out["errors"].append("invalid_decision_timestamp")
        return out
    ctx = _known(context_rows, t)
    books = _known(book_rows, t)
    if not ctx or not books:
        out["errors"].append("missing_context_or_book")
        return out
    anchors = []
    for delta in (0, 60, 300):
        eligible = [r for r in ctx if float(r["recv_ts"]) <= t - delta]
        if not eligible or t - delta - float(eligible[-1]["recv_ts"]) > 10:
            out["errors"].append("missing_or_stale_context_anchor_" + str(delta))
            return out
        anchors.append(eligible[-1])
    path = [r for r in ctx if float(r["recv_ts"]) >= float(anchors[-1]["recv_ts"])]
    prices = [_finite(r.get("mid_px")) for r in path]
    if any(p is None or p <= 0 for p in prices):
        out["errors"].append("invalid_context_price")
        return out
    p0, p1, p5 = [float(r["mid_px"]) for r in anchors]
    gaps = [float(b["recv_ts"]) - float(a["recv_ts"])
            for a, b in zip(path, path[1:])]
    maxgap = max(gaps, default=0.0)
    if maxgap > 15:
        out["errors"].append("context_gap_over15s")
        return out
    book = books[-1]
    recv = float(book["recv_ts"])
    venue = _finite(book.get("venue_ts"))
    if t - recv > 15:
        out["errors"].append("book_recv_age_over15s")
        return out
    if venue is None:
        out["errors"].append("book_venue_timestamp_missing")
        return out
    venue /= 1000
    if venue > recv + 1e-6 or venue > t:
        out["errors"].append("book_venue_timestamp_in_future")
        return out
    if t - venue > 15:
        out["errors"].append("book_venue_age_over15s")
        return out
    def ladder(rows, descending):
        result = []
        for row in rows:
            if len(row) < 2:
                raise ValueError("malformed level")
            p, q = _finite(row[0]), _finite(row[1])
            if p is None or q is None or p <= 0 or q <= 0 or not math.isfinite(p * q):
                raise ValueError("invalid level")
            result.append((p, q))
        return sorted(result, reverse=descending)
    try:
        bids = ladder(book.get("bids", []), True)
        asks = ladder(book.get("asks", []), False)
        if not bids or not asks or asks[0][0] < bids[0][0]:
            raise ValueError("crossed or empty book")
    except (TypeError, ValueError, KeyError):
        out["errors"].append("invalid_book")
        return out
    biddep = sum(p * q for p, q in bids[:5])
    askdep = sum(p * q for p, q in asks[:5])
    depth = biddep + askdep
    if not math.isfinite(depth) or depth <= 0:
        out["errors"].append("invalid_book_depth")
        return out
    mid = bids[0][0] / 2 + asks[0][0] / 2
    out.update({
        "mid_px": p0,
        "momentum_1m_bp": (math.log(p0) - math.log(p1)) * 10000,
        "momentum_5m_bp": (math.log(p0) - math.log(p5)) * 10000,
        "realized_vol_5m_bp": math.sqrt(sum((math.log(b) - math.log(a)) ** 2
                                           for a, b in zip(prices, prices[1:]))) * 10000,
        "spread_bp": (asks[0][0] - bids[0][0]) / mid * 10000,
        "depth5_usd": depth, "book_imbalance5": (biddep - askdep) / depth,
        "context_age_seconds": t - float(anchors[0]["recv_ts"]),
        "book_age_seconds": t - recv, "book_venue_age_seconds": t - venue,
        "max_context_gap_5m_seconds": maxgap,
    })
    seen = set()
    valid_trades = []
    raw_batches = defaultdict(int)
    discarded = []
    ambiguous = []
    for r in _known(trade_rows, t):
        received = float(r["recv_ts"])
        if received <= t - 300:
            continue
        tid = r.get("tid")
        coin = r.get("coin")
        h = r.get("hash")
        meaningful_hash = isinstance(h, str) and bool(h.removeprefix("0x").strip("0"))
        if tid is not None:
            key = ("tid", coin, str(tid))
        elif meaningful_hash:
            key = ("hash", coin, h, r.get("venue_ts"), r.get("px"), r.get("sz"), r.get("side"))
        else:
            key = ("anonymous", coin, r.get("venue_ts"), r.get("px"), r.get("sz"), r.get("side"))
            ambiguous.append(received)
        if key in seen:
            continue
        seen.add(key)
        raw_batches[received] += 1
        vt, p, q = (_finite(r.get(k)) for k in ("venue_ts", "px", "sz"))
        side = r.get("side")
        if (vt is None or p is None or q is None or p <= 0 or q <= 0
                or not math.isfinite(p * q) or side not in ("A", "B")):
            discarded.append(received)
            continue
        delay = received - vt / 1000
        if not 0 <= delay <= 15:
            discarded.append(received)
            continue
        valid_trades.append((received, p * q, 1 if side == "B" else -1, delay))
    for seconds, suffix in ((60, "1m"), (300, "5m")):
        rows = [r for r in valid_trades if r[0] > t - seconds]
        notional = sum(r[1] for r in rows)
        signed = sum(r[1] * r[2] for r in rows)
        if not math.isfinite(notional) or not math.isfinite(signed):
            out["valid"] = True
            out["flow_errors"].append("nonfinite_aggregate_notional")
            return out
        batches = [n for received, n in raw_batches.items() if received > t - seconds]
        out["observed_notional_" + suffix] = notional
        out["observed_signed_notional_" + suffix] = signed
        out["observed_flow_imbalance_" + suffix] = signed / notional if notional else 0.0
        out["observed_print_count_" + suffix] = len(rows)
        out["observed_nonempty_batch_count_" + suffix] = len(batches)
        out["valid_nonempty_batch_count_" + suffix] = len({r[0] for r in rows})
        out["batch_saturation_fraction_" + suffix] = sum(n >= 10 for n in batches) / len(batches) if batches else 0.0
        out["discarded_stale_or_invalid_print_count_" + suffix] = sum(received > t - seconds for received in discarded)
        out["ambiguous_trade_identity_count_" + suffix] = sum(received > t - seconds for received in ambiguous)
        out["max_observed_print_delay_" + suffix] = max((r[3] for r in rows), default=None)
    latest = max((r[0] for r in valid_trades), default=None)
    out["last_observed_batch_age_seconds"] = t - latest if latest is not None else None
    if out["valid_nonempty_batch_count_1m"] < 3:
        out["flow_errors"].append("fewer_than3_fresh_nonempty_batches_1m")
    if latest is None or t - latest > 15:
        out["flow_errors"].append("latest_valid_print_batch_older_than15s")
    if out["ambiguous_trade_identity_count_1m"]:
        out["flow_errors"].append("unresolved_trade_identity_1m")
    out["flow_valid"] = not out["flow_errors"]
    out["trade_poll_complete"] = False
    out["coverage_note"] = ("Only fresh deduplicated prints are recorded; original response lengths "
                            "and empty/successful-poll telemetry are unavailable. Full venue volume is unverified.")
    out["valid"] = True
    return out
