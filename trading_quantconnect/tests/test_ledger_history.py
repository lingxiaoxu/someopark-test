"""Historical dividend inputs use exact dates and proven BDC event history.

All account/ledger writes are confined to pytest's temporary directory. No
production report, current account, network client, or strategy is invoked.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from reconcile import ledger_history as lh  # noqa: E402


@pytest.fixture(autouse=True)
def fast_reads(monkeypatch):
    monkeypatch.setattr(lh, "stable_read", lambda path: json.loads(path.read_text()))
    monkeypatch.setattr(lh.time, "sleep", lambda _: None)


def write(root, relative, value):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


@pytest.mark.parametrize("strategy,relative", list(lh.LEDGER_ACCOUNT_FILES.items()))
def test_exact_archive_precedes_future_current_account(tmp_path, strategy, relative):
    current = write(tmp_path, relative, {"as_of": "2026-09-10", "cumulative_dividends": 9999})
    archive = current.parent / "account_history" / f"{current.stem}_20260908.json"
    write(tmp_path, archive.relative_to(tmp_path), {
        "as_of": "2026-09-08", "cumulative_dividends": 964.5, "equity": 1022452.02})
    accounts, missing = lh.load_ledger_accounts("2026-09-08", repo=tmp_path,
                                              account_files={strategy: relative})
    assert not missing
    assert accounts[strategy]["cumulative_dividends"] == 964.5
    assert accounts[strategy]["equity"] == 1022452.02
    assert accounts[strategy]["_reconcile_source"] == {
        "kind": "account_archive", "path": str(archive.relative_to(tmp_path)),
        "session": "2026-09-08"}


def test_current_account_is_usable_only_for_exact_session(tmp_path):
    write(tmp_path, "account_mrpt.json", {"as_of": "2026-09-09", "cumulative_dividends": -998.48})
    mapping = {"mrpt": "account_mrpt.json"}
    accounts, missing = lh.load_ledger_accounts("2026-09-09", repo=tmp_path, account_files=mapping)
    assert not missing
    assert accounts["mrpt"]["cumulative_dividends"] == -998.48
    assert accounts["mrpt"]["_reconcile_source"]["kind"] == "current_account_exact_session"
    for session in ("2026-09-08", "2026-09-10"):
        accounts, missing = lh.load_ledger_accounts(session, repo=tmp_path, account_files=mapping)
        assert accounts == {}
        assert len(missing) == 1 and "current as_of=2026-09-09" in missing[0]


@pytest.mark.parametrize("bad", [
    {"as_of": "2026-09-09", "cumulative_dividends": 12},
    {"as_of": "2026-09-08"},
    {"as_of": "2026-09-08", "cumulative_dividends": float("nan")},
])
def test_bad_archive_is_incomplete_and_does_not_fall_back(tmp_path, bad):
    write(tmp_path, "account_mrpt.json", {"as_of": "2026-09-08", "cumulative_dividends": 1234})
    write(tmp_path, "account_history/account_mrpt_20260908.json", bad)
    accounts, missing = lh.load_ledger_accounts("2026-09-08", repo=tmp_path,
                                              account_files={"mrpt": "account_mrpt.json"})
    assert accounts == {} and len(missing) == 1


def test_missing_account_is_explicit_and_zero_dividend_is_valid(tmp_path):
    write(tmp_path, "account_aeus.json", {"as_of": "2026-09-08", "cumulative_dividends": 0})
    accounts, missing = lh.load_ledger_accounts("2026-09-08", repo=tmp_path,
        account_files={"aeus": "account_aeus.json", "mrpt": "account_mrpt.json"})
    assert accounts["aeus"]["cumulative_dividends"] == 0
    assert "mrpt" not in accounts and missing[0].startswith("mrpt:")


def bdc_fixture(tmp_path):
    current = {"as_of": "2026-09-09", "cumulative_dividends": 30.0,
               "positions": {"BIL": {"shares": 103.0}}, "equity": 99999.0}
    rows = [
        {"date": "2026-09-01", "action": "OPEN", "ticker": "BIL", "shares": 100,
         "dedup_key": "BIL-open"},
        {"date": "2026-09-08", "action": "DRIP", "ticker": "BIL", "shares": 1,
         "div_cash": 10, "dedup_key": "BIL-drip-08"},
        {"date": "2026-09-09", "action": "DRIP", "ticker": "BIL", "shares": 2,
         "div_cash": 20, "dedup_key": "BIL-drip-09"},
    ]
    return current, rows


def save_bdc(tmp_path, current, rows):
    write(tmp_path, "account_bdc.json", current)
    (tmp_path / "trade_ledger_bdc.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n")


def read_bdc(tmp_path, session="2026-09-08"):
    return lh.load_ledger_accounts(session, repo=tmp_path,
                                   account_files={"bdc": "account_bdc.json"})


def test_bdc_replays_only_events_through_session_and_does_not_invent_equity(tmp_path):
    current, rows = bdc_fixture(tmp_path)
    save_bdc(tmp_path, current, rows)
    accounts, missing = read_bdc(tmp_path)
    assert not missing
    account = accounts["bdc"]
    assert account["as_of"] == "2026-09-08"
    assert account["cumulative_dividends"] == 10.0
    assert "equity" not in account and "positions" not in account and "cash" not in account
    source = account["_reconcile_source"]
    assert source["kind"] == "bdc_dividend_ledger"
    assert source["coverage_as_of"] == "2026-09-09"
    assert source["rows_through_session"] == 2
    assert source["rows_after_session_excluded"] == 1


@pytest.mark.parametrize("defect", ["dividends", "shares", "duplicate", "future", "missing_open"])
def test_bdc_replay_requires_complete_consistent_coverage(tmp_path, defect):
    current, rows = bdc_fixture(tmp_path)
    if defect == "dividends":
        current["cumulative_dividends"] = 999
    elif defect == "shares":
        current["positions"]["BIL"]["shares"] = 999
    elif defect == "duplicate":
        rows.append(dict(rows[-1]))
    elif defect == "future":
        rows[-1]["date"] = "2026-09-10"
    elif defect == "missing_open":
        rows = rows[1:]
        current["positions"]["BIL"]["shares"] = 3
    save_bdc(tmp_path, current, rows)
    accounts, missing = read_bdc(tmp_path)
    assert accounts == {} and len(missing) == 1


def test_bdc_cannot_claim_day_after_formal_coverage(tmp_path):
    current, rows = bdc_fixture(tmp_path)
    save_bdc(tmp_path, current, rows)
    accounts, missing = read_bdc(tmp_path, "2026-09-10")
    assert accounts == {}
    assert "coverage ends 2026-09-09" in missing[0]


def test_bdc_cannot_invent_zero_before_ledger_inception(tmp_path):
    current, rows = bdc_fixture(tmp_path)
    save_bdc(tmp_path, current, rows)
    accounts, missing = read_bdc(tmp_path, "2026-08-31")
    assert accounts == {} and "inception" in missing[0]


@pytest.mark.parametrize("session", ["20260908", "2026-09-31", "2026-09-08T16:00:00"])
def test_invalid_session_is_rejected_before_read(tmp_path, session):
    with pytest.raises(ValueError):
        lh.load_ledger_accounts(session, repo=tmp_path)
