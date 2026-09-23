"""Local, read-only post-response market data for the isolated W9 model.

The existing recorder owns these files. This adapter never contacts an API,
changes recorder state, fills missing intervals, or rewrites historical values.
"""
from __future__ import annotations

import gzip
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

ASSETS = ("BTC", "ETH", "SOL", "DOGE", "XRP")
REPO = Path(__file__).resolve().parents[3]
SOURCE_ROOT = REPO / "crypto_trading/price_data/kalshi/perps/poll/prod/markets"
RESEARCH = REPO / "crypto_trading/trading_signals/reports/w9_rnn_pit_expanded_20260916T001013Z"
SEED_SOURCE = RESEARCH / "history_audit/recorded_market_observable_1m.parquet"
SEED_SHA256 = "f4ae735f3386880dd4f32173210fb66093bd10fbffab1f8155984663c9de2c72"


def _number(mapping, key):
    try:
        value = float(mapping[key])
        return value if math.isfinite(value) else float("nan")
    except (KeyError, TypeError, ValueError):
        return float("nan")


def read_received_snapshots(asset, start_ts, end_ts, source_root=SOURCE_ROOT):
    """Read complete JSONL records, strictly bounded by actual receipt time."""
    first = pd.Timestamp(start_ts - 120, unit="s", tz="UTC").strftime("%Y-%m-%d")
    last = pd.Timestamp(end_ts, unit="s", tz="UTC").strftime("%Y-%m-%d")
    rows = []
    for path in sorted((Path(source_root) / f"KX{asset}PERP").glob("*.jsonl*")):
        if not first <= path.name[:10] <= last:
            continue
        with (gzip.open(path, "rt") if path.suffix == ".gz" else path.open()) as stream:
            for line in stream:
                # A recorder may currently be appending its final line.
                if not line.endswith("\n"):
                    continue
                try:
                    record = json.loads(line)
                    ts = float(record["recv_ts"])
                    market = record["m"]
                except (ValueError, KeyError, TypeError):
                    continue
                if not start_ts - 120 <= ts <= end_ts:
                    continue
                rows.append((ts, *(_number(market, key) for key in
                    ("bid", "ask", "volume", "contract_size", "open_interest"))))
    columns = ["recv_ts", "bid", "ask", "reported_cumulative_contract_volume", "contract_size", "oi"]
    return pd.DataFrame(rows, columns=columns).sort_values("recv_ts").drop_duplicates("recv_ts", keep="first")


def minutes_from_snapshots(raw, asset, start_ts, end_ts):
    """Same endpoint/quality math as the audited research builder."""
    grid = np.arange(int(start_ts // 60) * 60, int(end_ts // 60) * 60 + 1, 60, dtype=np.int64)
    if raw.empty or not len(grid):
        return pd.DataFrame()
    ts = raw.recv_ts.to_numpy()
    idx = np.searchsorted(ts, grid, side="right") - 1
    found = idx >= 0
    end = raw.iloc[np.maximum(idx, 0)].reset_index(drop=True).copy()
    end.loc[~found, :] = np.nan
    end["asset"] = asset
    end["bar_end_ts"] = grid
    end["available_at_ts"] = grid
    end["quote_age_s"] = grid - end.recv_ts
    end["mid_contract_usd"] = (end.bid + end.ask) / 2
    end["spread_bp"] = (end.ask - end.bid) / end.mid_contract_usd * 1e4
    end["quote_valid"] = found & end.bid.gt(0) & end.ask.ge(end.bid) & end.ask.lt(1e12) & end.spread_bp.le(100) & end.quote_age_s.between(0, 15)
    end["sample_interval_s"] = end.recv_ts.diff()
    end["observed_contract_volume"] = end.reported_cumulative_contract_volume.diff()
    end["prior_recv_ts"] = end.recv_ts.shift()
    end["max_input_recv_ts"] = end.recv_ts
    end["ingested_at"] = end.recv_ts
    end["contract_size_base_units"] = end.contract_size
    end["volume_valid"] = found & end.reported_cumulative_contract_volume.ge(0) & end.reported_cumulative_contract_volume.shift().ge(0) & end.observed_contract_volume.ge(0) & end.observed_contract_volume.notna() & end.quote_age_s.between(0, 15) & end.quote_age_s.shift().between(0, 15) & end.sample_interval_s.between(45, 75)
    end["valid_bar"] = end.quote_valid & end.volume_valid
    end["volume"] = end.observed_contract_volume.where(end.volume_valid)
    end["quote_mid_underlying"] = end.mid_contract_usd / end.contract_size
    end["mid_close"] = end.mid_contract_usd.where(end.quote_valid)
    end["ts"] = end.bar_end_ts
    end["bar_start_ts"] = end.bar_end_ts - 60
    end["source"] = "kalshi_prod_markets_postresponse_recorded_cumulative_volume_delta"
    end["event_time_semantics"] = "received reporting increment over actual sampled interval; not final candle"
    assert end.loc[end.max_input_recv_ts.notna(), "max_input_recv_ts"].le(end.loc[end.max_input_recv_ts.notna(), "bar_end_ts"]).all()
    return end


class ReceivedTail:
    """Bounded startup tail, then only appended bytes; rotating files are read-only."""
    def __init__(self, source_root, asset):
        self.root = Path(source_root) / f"KX{asset}PERP"
        self.files = {}
        self.rows = []

    def read(self, start_ts, end_ts):
        lower = start_ts - 120
        first = pd.Timestamp(lower, unit="s", tz="UTC").strftime("%Y-%m-%d")
        last = pd.Timestamp(end_ts, unit="s", tz="UTC").strftime("%Y-%m-%d")
        for path in sorted(self.root.glob("*.jsonl*")):
            if not first <= path.name[:10] <= last:
                continue
            stat = path.stat()
            identity = (stat.st_ino,)
            previous = self.files.get(str(path))
            if path.suffix == ".gz":
                if previous == (identity, stat.st_size):
                    continue
                with gzip.open(path, "rb") as stream:
                    lines = stream.readlines()
                offset = stat.st_size
            else:
                with path.open("rb") as stream:
                    offset = previous[1] if previous and previous[0] == identity and previous[1] <= stat.st_size else None
                    if offset is None:
                        # Expand only when needed to cover the requested receipt
                        # boundary. No fixed byte bound silently truncates history.
                        window = 262144
                        while True:
                            offset = max(0, stat.st_size - window)
                            stream.seek(offset)
                            if offset:
                                stream.readline()
                            offset = stream.tell()
                            first_line = stream.readline()
                            try:
                                first_receipt = float(json.loads(first_line)["recv_ts"])
                            except (ValueError, KeyError, TypeError):
                                first_receipt = float("inf")
                            if offset == 0 or first_receipt <= lower:
                                break
                            window *= 2
                    stream.seek(offset)
                    lines = []
                    while True:
                        before = stream.tell()
                        line = stream.readline()
                        if not line or not line.endswith(b"\n"):
                            offset = before
                            break
                        lines.append(line)
            self.files[str(path)] = (identity, offset)
            for line in lines:
                try:
                    record = json.loads(line)
                    ts = float(record["recv_ts"])
                    market = record["m"]
                except (ValueError, KeyError, TypeError):
                    continue
                if math.isfinite(ts) and ts >= lower:
                    self.rows.append((ts, *(_number(market, key) for key in ("bid", "ask", "volume", "contract_size", "open_interest"))))
        self.rows = [row for row in self.rows if row[0] >= lower]
        columns = ["recv_ts", "bid", "ask", "reported_cumulative_contract_volume", "contract_size", "oi"]
        data = pd.DataFrame(self.rows, columns=columns)
        return data[data.recv_ts <= end_ts].sort_values("recv_ts").drop_duplicates("recv_ts", keep="first")


class MinuteStore:
    def __init__(self, runtime_dir, source_root=SOURCE_ROOT):
        self.runtime_dir = Path(runtime_dir)
        self.source_root = Path(source_root)
        self._recent = pd.DataFrame()
        self._last_grid = None
        self._tails = {asset: ReceivedTail(self.source_root, asset) for asset in ASSETS}

    def refresh(self, now_ts, lookback_minutes=60):
        grid = int(now_ts // 60) * 60
        if self._last_grid == grid and not self._recent.empty:
            return self._recent.copy()
        start = grid - (lookback_minutes + 2) * 60
        pieces = []
        for asset in ASSETS:
            raw = self._tails[asset].read(start, now_ts)
            piece = minutes_from_snapshots(raw, asset, start, now_ts)
            if not piece.empty:
                pieces.append(piece)
        self._recent = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()
        self._last_grid = grid
        return self._recent.copy()

    def recent_minutes(self, now_ts, lookback_minutes=60):
        return self.refresh(now_ts, lookback_minutes)

    def history(self, now_ts):
        """Immutable certified seed plus freshly read recorder rows after it."""
        import hashlib
        import os
        import shutil
        seed_copy = self.runtime_dir / "bootstrap_seed/recorded_market_observable_1m.parquet"
        if not seed_copy.exists():
            if hashlib.sha256(SEED_SOURCE.read_bytes()).hexdigest() != SEED_SHA256:
                raise ValueError("certified W9 seed source digest changed")
            seed_copy.parent.mkdir(parents=True, exist_ok=True)
            temporary = seed_copy.with_suffix(f".{os.getpid()}.tmp")
            shutil.copyfile(SEED_SOURCE, temporary)
            os.replace(temporary, seed_copy)
        if hashlib.sha256(seed_copy.read_bytes()).hexdigest() != SEED_SHA256:
            raise ValueError("certified W9 seed source digest changed")
        # 33 training/validation days plus feature lookback; older seed rows
        # cannot affect today's registered model and need no repeated parsing.
        earliest = pd.Timestamp(now_ts, unit="s", tz="UTC").floor("D").timestamp() - 33 * 86400 - 26 * 60
        seed = pd.read_parquet(seed_copy)
        last_grid = int(seed.bar_end_ts.max())
        seed = seed[seed.bar_end_ts >= earliest].copy()
        pieces = [seed]
        for asset in ASSETS:
            extension_start = max(last_grid - 120, earliest)
            raw = read_received_snapshots(asset, extension_start, now_ts, self.source_root)
            extra = minutes_from_snapshots(raw, asset, extension_start, now_ts)
            if not extra.empty:
                pieces.append(extra[extra.bar_end_ts > last_grid])
        data = pd.concat(pieces, ignore_index=True).sort_values(["asset", "bar_end_ts"])
        if data.duplicated(["asset", "bar_end_ts"]).any():
            raise ValueError("duplicate minute identities")
        return data.reset_index(drop=True)
