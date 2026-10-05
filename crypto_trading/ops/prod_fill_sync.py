"""Read-only sync of PROD fills into private per-account caches (2026-10-05, owner directive).

Why: each prod order receipt carries only ``average_fee_paid`` / ``average_fill_price``
rounded to 4 decimals, so a ledger built from receipts under-counts fees by a fraction
of a cent per order (the owner's ledger was $1.75 short over 1,383 markets on 10-05).
Kalshi's own fills carry the exact fee and price of every partial fill.

The publisher (ops/export_prediction_frontend) still makes no prod-account call: it only
reads the local files written here and, per order, replaces the receipt-rounded cost and
fee with these exact sums when the fill counts agree.

    python -m crypto_trading.ops.prod_fill_sync            # owner + every mirrored user, incremental
    python -m crypto_trading.ops.prod_fill_sync --full     # rebuild from each account's start date

Scheduled every 5 minutes by the LaunchAgent com.someopark.crypto.prodfillsync (owner-approved 2026-10-05).

Only GET /portfolio/fills is ever called. Caches (never in the repo's tracked files):
    owner  crypto_trading/trading_signals/frontend_prediction/prod_fills_owner.json
    user   ~/.kalshi/fills/<uid>.json        (dir 0700, files 0600)
Each cache: {"as_of", "since", "account", "orders": {order_id: {ticker, side, count, cost, fees, fills, last_fill}}}
cost = sum(count x price of the side actually acquired), fees = sum(fee_cost); dollars.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OWNER_CACHE = ROOT / "crypto_trading" / "trading_signals" / "frontend_prediction" / "prod_fills_owner.json"
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXXRP15M")   # W7 trades only these
OWNER_SINCE = datetime(2026, 9, 28, tzinfo=timezone.utc).timestamp()     # W7 prod armed 2026-09-28
OVERLAP_S = 3600            # re-read the last hour on every incremental run (late fills, clock skew)
PAGE_DELAY_S = 0.25
MAX_PAGES = 300


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _ts(iso: str) -> float:
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()


def aggregate(fills: list[dict]) -> dict:
    """order_id -> exact totals of that order's W7-series fills (the acquired side's price)."""
    acc: dict = {}
    for f in fills:
        tk = f.get("ticker") or f.get("market_ticker") or ""
        oid = f.get("order_id")
        if not oid or not tk.startswith(SERIES):
            continue
        side = f.get("outcome_side")
        if side not in ("yes", "no"):
            raise ValueError("fill without outcome_side")
        n, px, fee = Decimal(str(f["count_fp"])), Decimal(str(f[f"{side}_price_dollars"])), Decimal(str(f.get("fee_cost") or 0))
        if n <= 0 or fee < 0 or not (0 <= px <= 1):
            raise ValueError("invalid fill values")
        a = acc.setdefault(oid, {"ticker": tk, "side": side, "count": Decimal(0), "cost": Decimal(0),
                                 "fees": Decimal(0), "fills": 0, "last_fill": ""})
        if a["ticker"] != tk or a["side"] != side:
            raise ValueError("one order id on two markets or sides")
        a["count"] += n; a["cost"] += n * px; a["fees"] += fee; a["fills"] += 1
        a["last_fill"] = max(a["last_fill"], str(f.get("created_time") or ""))
    return {oid: {**a, "count": float(a["count"]), "cost": float(round(a["cost"], 6)),
                  "fees": float(round(a["fees"], 6))} for oid, a in acc.items()}


def merge(prev: dict, new: dict) -> dict:
    """An order's filled count only grows: a later window that saw only part of an
    order's fills (boundary of the incremental read) never replaces a complete record."""
    out = dict(prev)
    for oid, a in new.items():
        if oid not in out or a["count"] >= out[oid]["count"] - 1e-9:
            out[oid] = a
    return out


def fetch_fills(client, min_ts: float) -> list[dict]:
    """GET /portfolio/fills only, paged; any non-200 or malformed page aborts the account."""
    out, cur, seen = [], None, set()
    for _ in range(MAX_PAGES):
        path = f"/portfolio/fills?limit=1000&min_ts={int(min_ts)}" + (f"&cursor={cur}" if cur else "")
        r = client._authed("GET", path)
        if r.status_code != 200:
            raise RuntimeError(f"fills HTTP {r.status_code}")
        j = r.json()
        if not isinstance(j.get("fills"), list):
            raise ValueError("fills response shape")
        out += j["fills"]
        cur = j.get("cursor")
        if not cur:
            return out
        if cur in seen:
            raise ValueError("repeated pagination cursor")
        seen.add(cur)
        time.sleep(PAGE_DELAY_S)
    raise ValueError("pagination limit reached")


def _read(path: Path) -> dict | None:
    try:
        j = json.loads(path.read_text())
        return j if isinstance(j, dict) and isinstance(j.get("orders"), dict) else None
    except (OSError, ValueError):
        return None


def _write(path: Path, obj: dict, private_dir: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if private_dir:
        os.chmod(path.parent, 0o700)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def sync_account(label: str, client, cache: Path, since: float, full: bool = False,
                 private_dir: bool = False, now=time.time) -> dict:
    prev = None if full else _read(cache)
    started = now()
    min_ts = since if prev is None else max(since, _ts(prev["as_of"]) - OVERLAP_S)
    new = aggregate(fetch_fills(client, min_ts))
    orders = merge(prev["orders"] if prev else {}, new)
    _write(cache, {"as_of": _iso(started), "since": _iso(since), "account": label, "orders": orders}, private_dir)
    return {"account": label, "read_from": _iso(min_ts), "orders_seen": len(new), "orders_cached": len(orders)}


def user_accounts(key_dir: Path) -> list[tuple[str, object, float]]:
    """(uid, KalshiKey, since) for every user the W7 mirror ever sent an order for and
    whose key is still on file; since = an hour before that user's first journal row.
    New users join automatically after their first mirrored order. A user auto-disabled
    for a rejected key (disabled/<uid>) is skipped until they re-verify on the panel."""
    from crypto_trading.crypto_common.config import KalshiKey, _parse_env_file
    first: dict = {}
    for f in sorted((key_dir / "journal").glob("*.jsonl")):
        for line in f.read_text(errors="ignore").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            uid, ts = r.get("account"), r.get("ts")
            if uid and ts and r.get("status") in ("pending_send", "live_sent"):
                first[uid] = min(first.get(uid, float("inf")), _ts(ts))
    envf = _parse_env_file(key_dir / "users.env")
    out = []
    for uid, t0 in sorted(first.items()):
        kid, kpath = envf.get(f"KALSHI_PROD_API_KEY_ID_{uid}", ""), envf.get(f"KALSHI_PROD_PRIVATE_KEY_PATH_{uid}", "")
        if (key_dir / "disabled" / uid).exists():
            continue
        if kid and kpath and Path(kpath).is_file():
            out.append((uid, KalshiKey(kid, kpath, borrowed=False), t0 - 3600))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--full", action="store_true", help="rebuild every cache from its start date")
    ap.add_argument("--owner-only", action="store_true")
    ap.add_argument("--users-only", action="store_true")
    a = ap.parse_args(argv)
    import fcntl
    OWNER_CACHE.parent.mkdir(parents=True, exist_ok=True)
    lock = open(OWNER_CACHE.parent / "prod_fill_sync.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)      # one sync at a time (launchd job vs a manual run)
    except OSError:
        print(f"{_iso(time.time())} another prod fill sync is running; skipped")
        return 0
    from crypto_trading.crypto_common.config import USER_KEY_DIR
    from crypto_trading.crypto_common.kalshi.rest_event import KalshiEventOrderClient
    rc = 0
    jobs = []
    if not a.users_only:
        jobs.append(("owner", lambda: KalshiEventOrderClient(env="prod"), OWNER_CACHE, OWNER_SINCE, False))
    if not a.owner_only:
        for uid, key, since in user_accounts(USER_KEY_DIR):
            jobs.append((uid, lambda key=key: KalshiEventOrderClient(env="prod", key=key),
                         USER_KEY_DIR / "fills" / f"{uid}.json", since, True))
    for label, make, cache, since, private in jobs:
        try:
            s = sync_account(label, make(), cache, since, a.full, private)
            print(f"{_iso(time.time())} {label[:8]}: read from {s['read_from']}, {s['orders_seen']} orders seen, {s['orders_cached']} cached")
        except Exception as e:                                    # noqa: BLE001  (never print key material)
            rc = 1
            print(f"{_iso(time.time())} {label[:8]}: FAILED {type(e).__name__}: {str(e)[:80]}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
