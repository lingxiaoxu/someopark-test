"""Read session-specific dividend inputs for historical QC reconciliation.

Account archives are preferred to mutable current accounts. A current account is
usable only on its exact ``as_of`` date. BDC has no daily account archive; its
formal OPEN/DRIP ledger can instead supply a dated dividend-only view, provided
the complete ledger agrees with a current account covering the requested date.
This module performs no writes, market-data requests, or strategy execution.
"""
from __future__ import annotations

import json
import math
import time
from datetime import date
from pathlib import Path

from inventory_source import LEDGER_ACCOUNT_FILES, REPO, stable_read


def _session(value: object) -> str:
    text = str(value)
    if date.fromisoformat(text).isoformat() != text:
        raise ValueError("date must be YYYY-MM-DD")
    return text


def _number(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} is not a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} is not a finite number")
    return result


def _account(path: Path, session: str) -> dict:
    account = stable_read(path)
    if not isinstance(account, dict) or account.get("as_of") != session:
        raise ValueError(f"account as_of is not {session}")
    _number(account.get("cumulative_dividends"), "cumulative_dividends")
    return dict(account)


def _ledger_rows(path: Path) -> list[dict]:
    # The producer replaces this file atomically; the double read additionally
    # refuses a concurrent regeneration rather than mixing two ledger versions.
    for _ in range(3):
        first = path.read_bytes()
        time.sleep(0.02)
        second = path.read_bytes()
        if first == second:
            rows = [json.loads(line) for line in second.splitlines() if line.strip()]
            if not rows:
                raise ValueError("BDC ledger is empty")
            return rows
    raise ValueError("BDC ledger changed during read")


def _bdc_dividend_view(session: str, repo: Path, current_path: Path) -> dict:
    """Replay dated cash dividends; current values only certify ledger coverage.

    bdc_inventory.sync_account defines cumulative_dividends as the sum of DRIP
    div_cash. Matching its full cumulative dividends and position quantities
    certifies the same formal replay reached coverage_as_of. The target view
    sums only dated events <= session and deliberately contains no current
    holdings, cash or equity masquerading as historical account values.
    """
    current = stable_read(current_path)
    if not isinstance(current, dict):
        raise ValueError("BDC coverage account is not an object")
    coverage = _session(current.get("as_of"))
    if coverage < session:
        raise ValueError(f"BDC ledger coverage ends {coverage}, before {session}")
    ledger_path = repo / "trade_ledger_bdc.jsonl"
    rows = _ledger_rows(ledger_path)
    positions: dict[str, float] = {}
    total_div = target_div = 0.0
    seen: set[str] = set()
    opened: set[str] = set()
    included = 0
    first_date = None
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("BDC ledger row is not an object")
        event_date = _session(row.get("date"))
        if event_date > coverage:
            raise ValueError("BDC ledger contains events beyond account coverage")
        first_date = min(first_date, event_date) if first_date else event_date
        key = row.get("dedup_key")
        if not isinstance(key, str) or not key or key in seen:
            raise ValueError("BDC ledger missing or duplicate dedup_key")
        seen.add(key)
        action, ticker = row.get("action"), row.get("ticker")
        if action not in ("OPEN", "DRIP") or not isinstance(ticker, str) or not ticker:
            raise ValueError("BDC ledger has unsupported action or ticker")
        if action == "OPEN":
            if ticker in opened:
                raise ValueError("BDC ledger repeats an OPEN ticker")
            opened.add(ticker)
        shares = _number(row.get("shares"), "BDC shares")
        if shares <= 0:
            raise ValueError("BDC OPEN/DRIP shares must be positive")
        positions[ticker] = positions.get(ticker, 0.0) + shares
        if action == "DRIP":
            dividend = _number(row.get("div_cash"), "BDC div_cash")
            total_div += dividend
            if event_date <= session:
                target_div += dividend
        if event_date <= session:
            included += 1
    if first_date is None or session < first_date:
        raise ValueError("session precedes BDC ledger inception")
    if set(positions) != opened:
        raise ValueError("BDC ledger is missing an OPEN event")
    expected_div = _number(current.get("cumulative_dividends"), "BDC cumulative_dividends")
    if abs(round(total_div, 2) - expected_div) > 0.005:
        raise ValueError("BDC ledger dividends disagree with coverage account")
    expected_positions = current.get("positions")
    if not isinstance(expected_positions, dict) or set(expected_positions) != set(positions):
        raise ValueError("BDC ledger tickers disagree with coverage account")
    for ticker, shares in positions.items():
        holding = expected_positions[ticker]
        expected = _number(holding.get("shares") if isinstance(holding, dict) else holding,
                           f"BDC {ticker} account shares")
        if abs(shares - expected) > 0.01:
            raise ValueError(f"BDC {ticker} ledger shares disagree with coverage account")
    return {
        "as_of": session,
        "cumulative_dividends": round(target_div, 2),
        "_reconcile_source": {
            "kind": "bdc_dividend_ledger",
            "path": str(ledger_path.relative_to(repo)),
            "session": session,
            "coverage_path": str(current_path.relative_to(repo)),
            "coverage_as_of": coverage,
            "rows_through_session": included,
            "rows_after_session_excluded": len(rows) - included,
            "scope": "cumulative_dividends only; date <= session",
        },
    }


def load_ledger_accounts(session: str, *, repo: Path = REPO,
                         account_files: dict[str, str] | None = None
                         ) -> tuple[dict[str, dict], list[str]]:
    """Return (dated accounts, missing reasons), with per-account provenance.

    A missing/corrupt exact archive is never replaced by an account from another
    date. Callers must treat missing required strategies as incomplete inputs.
    Source metadata lives in ``account['_reconcile_source']`` for report output.
    """
    session = _session(session)
    repo = Path(repo)
    paths = LEDGER_ACCOUNT_FILES if account_files is None else account_files
    accounts, missing = {}, []
    for strategy, relative in paths.items():
        current_path = repo / relative
        archive = current_path.parent / "account_history" / (
            f"{current_path.stem}_{session.replace('-', '')}.json")
        try:
            if archive.exists():
                account = _account(archive, session)
                source = {"kind": "account_archive", "path": str(archive.relative_to(repo)),
                          "session": session}
            elif current_path.exists():
                current = stable_read(current_path)
                if not isinstance(current, dict):
                    raise ValueError("current account is not an object")
                if current.get("as_of") == session:
                    _number(current.get("cumulative_dividends"), "cumulative_dividends")
                    account = dict(current)
                    source = {"kind": "current_account_exact_session",
                              "path": str(current_path.relative_to(repo)), "session": session}
                elif strategy == "bdc":
                    accounts[strategy] = _bdc_dividend_view(session, repo, current_path)
                    continue
                else:
                    raise ValueError(f"no exact archive; current as_of={current.get('as_of')}")
            else:
                raise ValueError("no exact archive or current account")
            account["_reconcile_source"] = source
            accounts[strategy] = account
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            missing.append(f"{strategy}:{exc}")
    return accounts, missing
