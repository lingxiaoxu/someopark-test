"""Layer-1 risk engine — per-strategy pre-trade + in-trade guards (Plan 00 §5,
Plan 06 §1/§4). Fast, local, runs inside the strategy loop; Layer-2 portfolio
aggregation lives in crypto_common/risk/ (Plan 06).

Design points:
  * A tripped kill is PERSISTENT: it writes a halt file under
    trading_signals/state/ and stays tripped across restarts until the
    operator clears it (`clear_halt`). Restart-amnesia on a risk halt is how
    real accounts die.
  * Guards return actions, they don't execute them — the strategy/execution
    layer owns order flow (and the demo-first gate).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from crypto_trading.crypto_common.config import SIGNALS_DIR

logger = logging.getLogger(__name__)

STATE_DIR = SIGNALS_DIR / "state"


class Action(str, Enum):
    NONE = "none"
    BLOCK_NEW = "block_new"          # amber: no new risk, manage existing
    FLATTEN_HALT = "flatten_halt"    # red: flatten everything and stop


@dataclass(frozen=True)
class Breach:
    guard: str
    action: Action
    detail: str


@dataclass
class GuardConfig:
    """Plan 06 §3/§4 Layer-1 limits (per strategy; starting points, calibrate)."""
    max_daily_loss_pct: float = 0.05          # red → flatten+halt
    daily_loss_amber_pct: float = 0.03        # amber → block new
    max_position_contracts: int | None = None
    liq_distance_min_pct: float = 0.15        # red (vs /margin/risk or mark-vs-liq)
    liq_distance_amber_pct: float = 0.25
    staleness_max_s: float = 30.0             # index/book staleness → block new
    ws_disconnect_max_s: float = 60.0         # feed loss → block new (manage only)
    max_events_per_hour: int | None = None    # Plan 04-style event budget
    max_hold_s: float | None = None           # time-stop (in-trade)


@dataclass
class RiskState:
    """The strategy loop keeps this updated; the engine only reads it."""
    equity_sod: float = 0.0                   # equity at start of UTC day
    equity_now: float = 0.0
    position_contracts: float = 0.0
    liq_distance_pct: float | None = None     # None = flat / unknown
    last_index_ts: float = 0.0
    last_book_ts: float = 0.0
    entry_ts: float | None = None
    events_this_hour: int = 0


class RiskKill:
    def __init__(self, strategy: str, cfg: GuardConfig | None = None):
        self.strategy = strategy
        self.cfg = cfg or GuardConfig()
        self.halt_file = STATE_DIR / f"halt_{strategy}.json"

    # ── persistent halt ────────────────────────────────────────────────────
    def halted(self) -> bool:
        return self.halt_file.exists()

    def trip(self, breach: Breach) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.halt_file.write_text(json.dumps({
            "strategy": self.strategy, "ts": time.time(),
            "guard": breach.guard, "detail": breach.detail}, indent=1))
        logger.error("[%s] KILL TRIPPED by %s: %s", self.strategy, breach.guard,
                     breach.detail)

    def clear_halt(self, operator_note: str) -> None:
        """Operator-only. Requires an explicit note (audit trail)."""
        if not operator_note.strip():
            raise ValueError("clearing a halt requires an operator note")
        if self.halt_file.exists():
            cleared = STATE_DIR / f"halt_{self.strategy}_cleared_{int(time.time())}.json"
            payload = json.loads(self.halt_file.read_text())
            payload["cleared_note"] = operator_note
            cleared.write_text(json.dumps(payload, indent=1))
            self.halt_file.unlink()
        logger.warning("[%s] halt cleared: %s", self.strategy, operator_note)

    # ── evaluation ─────────────────────────────────────────────────────────
    def evaluate(self, s: RiskState, *, now: float | None = None) -> list[Breach]:
        """All current breaches, worst first. Trips the persistent halt on red."""
        now = now or time.time()
        cfg = self.cfg
        breaches: list[Breach] = []

        if self.halted():
            breaches.append(Breach("halt-file", Action.FLATTEN_HALT,
                                   f"persistent halt present: {self.halt_file}"))

        if s.equity_sod > 0:
            dd = (s.equity_now - s.equity_sod) / s.equity_sod
            if dd <= -cfg.max_daily_loss_pct:
                breaches.append(Breach("daily-loss", Action.FLATTEN_HALT,
                                       f"day P&L {dd:.2%} <= -{cfg.max_daily_loss_pct:.0%}"))
            elif dd <= -cfg.daily_loss_amber_pct:
                breaches.append(Breach("daily-loss-amber", Action.BLOCK_NEW,
                                       f"day P&L {dd:.2%}"))

        if s.liq_distance_pct is not None:
            if s.liq_distance_pct < cfg.liq_distance_min_pct:
                breaches.append(Breach("liq-distance", Action.FLATTEN_HALT,
                                       f"{s.liq_distance_pct:.1%} < {cfg.liq_distance_min_pct:.0%}"))
            elif s.liq_distance_pct < cfg.liq_distance_amber_pct:
                breaches.append(Breach("liq-distance-amber", Action.BLOCK_NEW,
                                       f"{s.liq_distance_pct:.1%}"))

        if s.last_index_ts and now - s.last_index_ts > cfg.staleness_max_s:
            breaches.append(Breach("stale-index", Action.BLOCK_NEW,
                                   f"index {now - s.last_index_ts:.0f}s stale"))
        if s.last_book_ts and now - s.last_book_ts > cfg.ws_disconnect_max_s:
            breaches.append(Breach("stale-book", Action.BLOCK_NEW,
                                   f"book {now - s.last_book_ts:.0f}s stale"))

        if (cfg.max_events_per_hour is not None
                and s.events_this_hour >= cfg.max_events_per_hour):
            breaches.append(Breach("event-budget", Action.BLOCK_NEW,
                                   f"{s.events_this_hour}/h >= {cfg.max_events_per_hour}"))

        if (cfg.max_hold_s is not None and s.entry_ts
                and now - s.entry_ts > cfg.max_hold_s and s.position_contracts != 0):
            breaches.append(Breach("time-stop", Action.BLOCK_NEW,
                                   f"held {now - s.entry_ts:.0f}s > {cfg.max_hold_s:.0f}s — exit"))

        for b in breaches:
            if b.action is Action.FLATTEN_HALT and b.guard != "halt-file":
                self.trip(b)
                break
        breaches.sort(key=lambda b: 0 if b.action is Action.FLATTEN_HALT else 1)
        return breaches

    def pre_trade_ok(self, s: RiskState, *, opens_new_risk: bool = True,
                     order_contracts: float = 0.0) -> tuple[bool, str]:
        """Gate one order. Closing/reducing orders pass amber blocks."""
        breaches = self.evaluate(s)
        for b in breaches:
            if b.action is Action.FLATTEN_HALT:
                return False, f"{b.guard}: {b.detail}"
            if b.action is Action.BLOCK_NEW and opens_new_risk:
                return False, f"{b.guard}: {b.detail}"
        cfg = self.cfg
        if (cfg.max_position_contracts is not None and opens_new_risk
                and abs(s.position_contracts + order_contracts) > cfg.max_position_contracts):
            return False, (f"max-position: |{s.position_contracts}+{order_contracts}| "
                           f"> {cfg.max_position_contracts}")
        return True, "ok"
