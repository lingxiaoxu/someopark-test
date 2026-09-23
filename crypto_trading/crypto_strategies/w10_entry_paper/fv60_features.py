"""Read-only, bounded inputs for W10's frozen fresh-value/60-second experiment.

Both reused recorders timestamp the *start* of a blocking polling operation.
Their row is therefore eligible only once the next distinct polling start for
the same asset/series is recorded.  The next start is a conservative upper
bound on receipt, not a manufactured receive timestamp.  No network calls,
collector changes, interpolation, or writes to recorder/model files occur.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import gzip
import json
import math
from pathlib import Path

from ..downside_paper.tape import JsonlTail


HISTORY_SECONDS = 1200
SPOT_MAX_AGE_SECONDS = 15
MAX_GZIP_UNCOMPRESSED_BYTES = 64 * 1024 * 1024


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _timestamp(value):
    if isinstance(value, (int, float)):
        return _number(value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (TypeError, AttributeError, ValueError, OverflowError):
        return None


class ValuationTape:
    """Reuse existing recorder files below ``crypto_trading/price_data``.

    ``update`` only reads current/recent daily JSONL files. Initial plain reads
    are bounded tails; gzip rollover is streamed once per file signature and
    retained rows are restricted to twenty minutes. Historical decision times
    must still fit that retained range. Missing history is unclassified.
    """

    def __init__(self, root, assets=("BTC", "ETH", "DOGE", "XRP")):
        self.root = Path(root)
        self.assets = tuple(assets)
        self.tail = JsonlTail(initial_bytes=2_000_000)
        self.rows = defaultdict(dict)
        self.gz_loaded = {}
        self.read_errors = 0

    def _row_timestamp(self, kind, row):
        return _number(row.get("ts" if kind == "spot" else "recv_ts"))

    def _read_gzip(self, path, kind, now):
        try:
            st = path.stat()
            signature = (st.st_ino, st.st_size, st.st_mtime_ns)
            if self.gz_loaded.get(str(path)) == signature:
                return []
            rows, decompressed = [], 0
            with gzip.open(path, "rb") as stream:
                for line in stream:
                    decompressed += len(line)
                    if decompressed > MAX_GZIP_UNCOMPRESSED_BYTES:
                        raise ValueError("compressed source exceeds bounded daily read")
                    if not line.endswith(b"\n"):
                        self.read_errors += 1
                        continue
                    try:
                        row = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        self.read_errors += 1
                        continue
                    if isinstance(row, dict):
                        stamp = self._row_timestamp(kind, row)
                        if stamp is not None and now - HISTORY_SECONDS <= stamp <= now + 60:
                            rows.append(row)
            after = path.stat()
            if signature != (after.st_ino, after.st_size, after.st_mtime_ns):
                # A rotating writer has not finished publishing this archive.
                return []
            self.gz_loaded[str(path)] = signature
            return rows
        except FileNotFoundError:
            return []
        except (OSError, EOFError, ValueError):
            self.read_errors += 1
            return []

    def update(self, now):
        now = _number(now)
        if now is None or now <= 0:
            raise ValueError("now must be a positive finite timestamp")
        date = datetime.fromtimestamp(now, timezone.utc)
        days = [date.strftime("%Y-%m-%d")]
        midnight = date.replace(hour=0, minute=0, second=0, microsecond=0)
        if (date - midnight).total_seconds() < HISTORY_SECONDS:
            days.insert(0, (date - timedelta(days=1)).strftime("%Y-%m-%d"))
        paths_in_use = set()
        for asset in self.assets:
            for kind, directory in (
                ("spot", self.root / "index_proxy/live" / asset),
                ("strike", self.root / "kalshi/event_strips/prod" / ("KX" + asset + "15M") / "markets"),
            ):
                target = self.rows[kind, asset]
                for day in days:
                    path = directory / (day + ".jsonl")
                    compressed = Path(str(path) + ".gz")
                    paths_in_use.update((str(path), str(compressed)))
                    try:
                        incoming = self.tail.read(path)
                    except (OSError, ValueError):
                        self.read_errors += 1
                        incoming = []
                    # Read even if plain and compressed coexist briefly. A tail
                    # may have missed rows appended immediately before rotation.
                    incoming.extend(self._read_gzip(compressed, kind, now))
                    for row in incoming:
                        stamp = self._row_timestamp(kind, row)
                        if stamp is None or not now - HISTORY_SECONDS <= stamp <= now + 60:
                            continue
                        if kind == "spot" and row.get("asset") != asset:
                            self.read_errors += 1
                            continue
                        previous = target.get(stamp)
                        if previous is not None and previous != row:
                            # Duplicate timestamps cannot prove distinct polls;
                            # conflicting values must not be resolved by order.
                            target[stamp] = {"_conflict": True, "ts": stamp, "recv_ts": stamp}
                        else:
                            target[stamp] = row
                self.rows[kind, asset] = {
                    t: row for t, row in sorted(target.items())
                    if now - HISTORY_SECONDS <= t <= now + 60
                }
        self.tail.cursors = {p: cursor for p, cursor in self.tail.cursors.items() if p in paths_in_use}
        self.gz_loaded = {p: signature for p, signature in self.gz_loaded.items() if p in paths_in_use}
        return {"read_errors": self.read_errors + self.tail.errors,
                "retained_rows": sum(len(rows) for rows in self.rows.values())}

    def _available(self, kind, asset, decision_ts):
        samples = sorted(self.rows[kind, asset].items())
        return [(start, next_start, row)
                for (start, row), (next_start, _) in zip(samples, samples[1:])
                if start < next_start <= decision_ts]

    def features(self, asset, ticker, side, entry_price, decision_ts, close_ts, hl_features):
        """Compute the frozen initial valuation at original quote creation time.

        ``hl_features`` comes from the existing pure ``compute_features`` helper
        evaluated at that exact decision timestamp. Its sampled-flow validity
        is immaterial: the frozen candidate uses only the five-minute realized
        volatility. Actual durable publication is checked by the W10 observer.
        """
        t, close, price = (_number(v) for v in (decision_ts, close_ts, entry_price))
        out = {"decision_ts": t, "valid": False, "errors": [], "asset": asset,
               "ticker": ticker, "side": side, "quote_price": price, "close_ts": close,
               "timestamp_semantics": "next distinct same-asset/series polling start bounds prior receipt",
               "spot_source": "recorded spot composite; not the official settlement RTI",
               "strike_source": "recorded Kalshi Prod exact-market greater_or_equal threshold",
               "probability_interpretation": "uncalibrated normal digital-value proxy; not a direction guarantee"}

        def invalid(reason):
            out["errors"].append(reason)
            return out

        if (asset not in self.assets or not isinstance(ticker, str)
                or not ticker.startswith("KX" + asset + "15M-") or side not in ("yes", "no")
                or t is None or close is None or price is None or not 0 < price < 1):
            return invalid("invalid_candidate")
        if close - t <= 60:
            return invalid("outside_frozen_before_final_minute_formula")
        if not isinstance(hl_features, dict) or hl_features.get("valid") is not True:
            return invalid("invalid_hyperliquid_features")
        if _number(hl_features.get("decision_ts")) != t:
            return invalid("hyperliquid_decision_timestamp_mismatch")
        vol = _number(hl_features.get("realized_vol_5m_bp"))
        if vol is None or vol <= 0:
            return invalid("invalid_realized_volatility")
        out.update(realized_vol5_bp=vol, realized_vol_5m_bp=vol,
                   feature_available_at=t, volatility_source="existing receipt-clock Hyperliquid five-minute context")

        eligible = self._available("spot", asset, t)
        if not eligible:
            return invalid("no_conservatively_available_spot")
        start, available, row = eligible[-1]
        spot = _number(row.get("index"))
        venues = _number(row.get("n_venues"))
        out.update(spot_capture_started_at=start, spot_available_at=available,
                   spot_source_age_seconds=t - start, spot_availability_age_seconds=t - available,
                   spot=spot, spot_n_venues=venues)
        if row.get("_conflict"):
            return invalid("conflicting_spot_poll_timestamp")
        if spot is None or spot <= 0 or row.get("stale") is not False or venues is None or venues < 1:
            return invalid("invalid_or_stale_spot_record")
        if not 0 <= t - start <= SPOT_MAX_AGE_SECONDS:
            return invalid("spot_capture_age_over15s")

        matches = []
        for start, available, row in self._available("strike", asset, t):
            if row.get("_conflict"):
                return invalid("conflicting_market_poll_timestamp")
            markets = row.get("markets")
            if not isinstance(markets, list):
                continue
            exact = [m for m in markets if isinstance(m, dict) and m.get("ticker") == ticker]
            if exact:
                matches.append((start, available, exact))
        if not matches:
            return invalid("no_conservatively_available_exact_market")
        start, available, exact = matches[-1]
        if len(exact) != 1:
            return invalid("duplicate_exact_market_in_snapshot")
        market = exact[0]
        out.update(strike_capture_started_at=start, strike_available_at=available,
                   strike_type=market.get("strike_type"), strike_market_close_ts=_timestamp(market.get("close_time")))
        if out["strike_market_close_ts"] != close:
            return invalid("exact_market_close_mismatch")
        if market.get("strike_type") != "greater_or_equal":
            return invalid("unsupported_strike_type")
        custom = market.get("custom_strike")
        if custom is not None and not isinstance(custom, dict):
            return invalid("malformed_custom_strike")
        if isinstance(custom, dict) and "floor_strike" in custom:
            strike = _number(custom["floor_strike"])
            out["strike_field"] = "custom_strike.floor_strike"
        else:
            strike = _number(market.get("floor_strike"))
            out["strike_field"] = "floor_strike"
        if strike is None or strike <= 0:
            return invalid("missing_or_invalid_strike")
        out["strike"] = strike
        sigma = vol * math.sqrt((close - t - 40) / 300)
        # Difference of logs avoids intermediate overflow/underflow of spot / strike.
        distance = (1 if side == "yes" else -1) * (math.log(spot) - math.log(strike)) * 10000
        if not math.isfinite(sigma) or sigma <= 0 or not math.isfinite(distance):
            return invalid("nonfinite_valuation_scale")
        z = distance / sigma
        if not math.isfinite(z):
            return invalid("nonfinite_valuation_score")
        probability = .5 * (1 + math.erf(z / math.sqrt(2)))
        out.update(valid=True, probability_proxy=probability, edge_proxy=probability - price,
                   sigma_remaining_bp=sigma, aligned_distance_bp=distance, z_proxy=z,
                   max_input_available_at=max(out["feature_available_at"], out["spot_available_at"], out["strike_available_at"]),
                   reason="fresh_spot_normal_value_vs_original_quote")
        return out
