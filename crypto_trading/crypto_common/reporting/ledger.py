"""Fee & funding accounting ledger (Plan 07 §1).

P&L identity, per strategy + aggregate (both fee scenarios everywhere;
HEADLINE = projected — the launch zero-fee promo must not flatter results):

    net_pnl = gross_trading_pnl + funding_pnl − fees − slippage

(funding_pnl is the signed holder P&L from costs.funding_payment — "paid"
negative, "received" positive — which is the plan identity's
"− funding_paid(+received)" written as one signed term.)

Sources (all read-only):
  * fills           trading_signals/fills/<strategy>/<date>.jsonl      (live, later)
  * dry-run orders  trading_signals/orders_dryrun/<strategy>/<date>.jsonl
  * inventory       trading_signals/inventory/inventory_<strategy>.json
  * backtests       trading_signals/<strategy>/backtests/*.json
  * funding         price_data/kalshi/funding/<TICKER>.parquet (authoritative)

Inventory schema (Plan 01 §5 / Plan 07 §1 — one file per strategy):
    {"strategy": str, "updated_ts": epoch,
     "equity": float, "equity_sod": float, "equity_peak": float,
     "positions": [{"ticker": str, "side": "long"|"short", "contracts": int,
                    "entry_price": float, "entry_ts": epoch,
                    "funding_accrued": float, "liq_price": float|null}]}

Canonical fill record (the live fills log MUST write this shape):
    {"ts": epoch, "strategy": str, "ticker": str, "side": "buy"|"sell",
     "count": float, "price": float, "role": "maker"|"taker",
     "mode": "live"|"dry_run"|"backtest", "client_order_id": str,
     "decision_mid": float|null}       # mid at decision time → realized slippage
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import pandas as pd

from crypto_trading.crypto_common import config as _config
from crypto_trading.crypto_common.costs import fee_dollars, funding_payment
from crypto_trading.crypto_common.loader import _read_jsonl_days  # shared tape reader

logger = logging.getLogger(__name__)


# ── source discovery / loading ──────────────────────────────────────────────

def _sig_dir():
    return _config.SIGNALS_DIR


def strategies_present() -> list[str]:
    """Every strategy with any artifact (orders, fills, inventory, backtests)."""
    names: set[str] = set()
    sig = _sig_dir()
    for sub in ("orders_dryrun", "fills"):
        d = sig / sub
        if d.exists():
            names.update(p.name for p in d.iterdir() if p.is_dir())
    inv = sig / "inventory"
    if inv.exists():
        names.update(p.stem.removeprefix("inventory_")
                     for p in inv.glob("inventory_*.json"))
    for p in sig.glob("*/backtests"):
        names.add(p.parent.name)
    return sorted(names)


def load_inventory(strategy: str) -> dict | None:
    path = _sig_dir() / "inventory" / f"inventory_{strategy}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        logger.exception("bad inventory %s", path)
        return None


def load_backtests(strategy: str) -> list[dict]:
    out = []
    for p in sorted((_sig_dir() / strategy / "backtests").glob("backtest_*.json")):
        try:
            out.append({"file": p.name, **json.loads(p.read_text())})
        except json.JSONDecodeError:
            logger.warning("bad backtest json %s — skipped", p)
    return out


def load_fills(strategy: str, *, days: list[str] | None = None) -> pd.DataFrame:
    """Live fills log (canonical records). Empty frame until live trading."""
    rows = list(_read_jsonl_days(_sig_dir() / "fills" / strategy, days=days))
    return pd.DataFrame(rows)


def load_dryrun_orders(strategy: str, *, days: list[str] | None = None) -> pd.DataFrame:
    """ExecutionRouter dry-run records → intended-order frame."""
    rows = []
    for line in _read_jsonl_days(_sig_dir() / "orders_dryrun" / strategy, days=days):
        o = line.get("order") or {}
        if not o:
            continue
        rows.append({"ts": line.get("ts"), "mode": line.get("mode"),
                     "ticker": o.get("ticker"), "side": o.get("side"),
                     "count": float(o.get("count") or 0),
                     "price": float(o.get("price") or 0),
                     "client_order_id": o.get("client_order_id")})
    return pd.DataFrame(rows)


# ── the identity engine ─────────────────────────────────────────────────────

@dataclass
class LedgerRow:
    strategy: str
    ticker: str
    gross_trading: float = 0.0
    funding: float = 0.0
    fees_zero: float = 0.0
    fees_projected: float = 0.0
    slippage: float = 0.0
    n_fills: int = 0
    end_position: float = 0.0

    @property
    def net_zero(self) -> float:
        return self.gross_trading + self.funding - self.fees_zero - self.slippage

    @property
    def net_projected(self) -> float:
        return self.gross_trading + self.funding - self.fees_projected - self.slippage

    def as_dict(self) -> dict:
        return {**self.__dict__, "net_zero": self.net_zero,
                "net_projected": self.net_projected}


def position_timeline(fills: pd.DataFrame) -> pd.DataFrame:
    """Signed position after each fill: columns [ts, position]. PIT-ordered."""
    if fills.empty:
        return pd.DataFrame(columns=["ts", "position"])
    f = fills.sort_values("ts")
    signed = f.apply(lambda r: r["count"] if r["side"] == "buy" else -r["count"], axis=1)
    return pd.DataFrame({"ts": f.ts.to_numpy(), "position": signed.cumsum().to_numpy()})


def position_at(timeline: pd.DataFrame, ts: float) -> float:
    """Position strictly BEFORE ts (funding accrues on the held position)."""
    if timeline.empty:
        return 0.0
    prior = timeline[timeline.ts < ts]
    return float(prior.position.iloc[-1]) if len(prior) else 0.0


def compute_ledger_row(strategy: str, ticker: str, fills: pd.DataFrame,
                       *, mark: float | None = None,
                       funding_events: pd.DataFrame | None = None) -> LedgerRow:
    """The P&L identity over one strategy×ticker fill set.

    gross_trading = realized (avg-cost) + unrealized (needs ``mark``);
    funding from ``funding_events`` (columns funding_time[dt-index ok],
    funding_rate, mark_price) applied to the position timeline (PIT: position
    held going INTO each settlement);
    fees recomputed from notional under BOTH scenarios (recorded fees may be
    promo-zero — the projected scenario must not inherit that);
    slippage = Σ side·(fill − decision_mid)·count where decision_mid present.
    """
    row = LedgerRow(strategy, ticker)
    if fills.empty:
        return row
    f = fills.sort_values("ts")
    row.n_fills = len(f)

    pos = 0.0
    avg = 0.0
    realized = 0.0
    for _, r in f.iterrows():
        qty = float(r["count"])
        px = float(r["price"])
        signed = qty if r["side"] == "buy" else -qty
        if pos * signed < 0:                                   # reducing / flipping
            reduce_qty = min(abs(signed), abs(pos))
            realized += (px - avg) * reduce_qty * (1 if pos > 0 else -1)
        new_pos = pos + signed
        if new_pos != 0 and pos * signed >= 0:
            avg = (abs(pos) * avg + qty * px) / (abs(pos) + qty)
        elif new_pos != 0 and abs(signed) > abs(pos):
            avg = px
        elif new_pos == 0:
            avg = 0.0
        pos = new_pos

        notional = qty * px
        role = str(r.get("role") or "taker")
        row.fees_zero += fee_dollars(notional, role=role, scenario="zero", ticker=ticker)
        row.fees_projected += fee_dollars(notional, role=role, scenario="projected",
                                          ticker=ticker)
        dmid = r.get("decision_mid")
        if dmid is not None and not pd.isna(dmid):
            row.slippage += (px - float(dmid)) * qty * (1 if r["side"] == "buy" else -1)

    row.end_position = pos
    unrealized = (mark - avg) * pos if (mark is not None and pos != 0) else 0.0
    row.gross_trading = realized + unrealized

    if funding_events is not None and len(funding_events):
        tl = position_timeline(f)
        fe = funding_events.reset_index()
        ts_col = "dt" if "dt" in fe.columns else "funding_time"
        for _, ev in fe.iterrows():
            ev_ts = pd.Timestamp(ev[ts_col]).timestamp()
            held = position_at(tl, ev_ts)
            if held:
                row.funding += funding_payment(held, float(ev["mark_price"]),
                                               float(ev["funding_rate"]))
    return row


def build_ledger(*, strategies: list[str] | None = None,
                 marks: dict[str, float] | None = None,
                 funding_by_ticker: dict[str, pd.DataFrame] | None = None,
                 include_dryrun: bool = True) -> dict:
    """Everything knowable now. Live fills are authoritative; dry-run orders
    are reported as a separate hypothetical view (mode-labeled, never mixed)."""
    marks = marks or {}
    funding_by_ticker = funding_by_ticker or {}
    out: dict = {"strategies": {}, "totals": {}}
    for s in (strategies or strategies_present()):
        entry: dict = {"backtests": load_backtests(s),
                       "inventory": load_inventory(s)}
        rows: list[LedgerRow] = []
        fills = load_fills(s)
        source = "live"
        if fills.empty and include_dryrun:
            fills = load_dryrun_orders(s)          # hypothetical: as-if-filled
            source = "dryrun_hypothetical"
        if not fills.empty:
            for tkr, grp in fills.groupby("ticker"):
                rows.append(compute_ledger_row(
                    s, str(tkr), grp, mark=marks.get(str(tkr)),
                    funding_events=funding_by_ticker.get(str(tkr))))
        entry["fill_source"] = source
        entry["rows"] = [r.as_dict() for r in rows]
        out["strategies"][s] = entry
    all_rows = [r for e in out["strategies"].values() for r in e["rows"]]
    for k in ("gross_trading", "funding", "fees_zero", "fees_projected",
              "slippage", "net_zero", "net_projected"):
        out["totals"][k] = sum(r[k] for r in all_rows)
    return out


# ── reconciliation (Plan 07 §1; injectable → testable now, live later) ─────

def reconcile_fills(intended: pd.DataFrame, actual: pd.DataFrame,
                    *, price_tol: float = 1e-6) -> dict:
    """Intended-vs-actual by client_order_id: missing / count / price breaks."""
    breaks = []
    act = ({} if actual.empty else
           {r["client_order_id"]: r for _, r in actual.iterrows()})
    for _, r in intended.iterrows():
        cid = r["client_order_id"]
        a = act.pop(cid, None)
        if a is None:
            breaks.append({"type": "missing_fill", "client_order_id": cid,
                           "intended": {"ticker": r["ticker"], "count": r["count"]}})
            continue
        if float(a["count"]) != float(r["count"]):
            breaks.append({"type": "count_mismatch", "client_order_id": cid,
                           "intended": float(r["count"]), "actual": float(a["count"])})
        if abs(float(a["price"]) - float(r["price"])) > price_tol:
            breaks.append({"type": "price_mismatch", "client_order_id": cid,
                           "intended": float(r["price"]), "actual": float(a["price"])})
    for cid in act:
        breaks.append({"type": "unexpected_fill", "client_order_id": cid})
    return {"ok": not breaks, "n_intended": len(intended), "breaks": breaks}


def reconcile_funding(ledger_funding: pd.DataFrame, venue_history: list[dict],
                      *, tol: float = 1e-6) -> dict:
    """Tie our accrued funding out against /margin/funding_history rows
    ({'funding_time': iso, 'amount': $}). Injectable for tests; live later."""
    breaks = []
    ours = {pd.Timestamp(r["funding_time"]).isoformat(): float(r["amount"])
            for _, r in ledger_funding.iterrows()} if len(ledger_funding) else {}
    for v in venue_history:
        key = pd.Timestamp(v["funding_time"]).isoformat()
        amt = float(v["amount"])
        mine = ours.pop(key, None)
        if mine is None:
            breaks.append({"type": "missing_ours", "funding_time": key, "venue": amt})
        elif abs(mine - amt) > tol:
            breaks.append({"type": "amount_mismatch", "funding_time": key,
                           "ours": mine, "venue": amt})
    for key, amt in ours.items():
        breaks.append({"type": "missing_venue", "funding_time": key, "ours": amt})
    return {"ok": not breaks, "breaks": breaks}


# ── fee-tier snapshot (keeps costs.load_fee_rates in sync) ─────────────────

def snapshot_fee_tiers() -> bool:
    """Authed /margin/fee_tiers → price_data/kalshi/refdata/fee_tiers.json.
    Degrades gracefully (False) while the account/key can't reach it."""
    from crypto_trading.crypto_common.costs import FEE_SNAPSHOT
    try:
        from crypto_trading.crypto_common.kalshi.rest_margin import KalshiMarginClient
        data = KalshiMarginClient().fee_tiers()
    except Exception as e:
        logger.warning("fee_tiers snapshot unavailable (%s: %s) — using defaults",
                       type(e).__name__, str(e)[:120])
        return False
    FEE_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    FEE_SNAPSHOT.write_text(json.dumps(data, indent=1))
    logger.info("fee tiers snapshot → %s", FEE_SNAPSHOT)
    return True
