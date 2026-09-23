"""M5覆盖回归:只用/tmp输入输出和内存行情,不读真实持仓、不调用网络。"""
import csv
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

# 与同目录测试一致,支持 repo 根/ controller 目录的 -m 和 console 启动。
_REPO = str(Path(__file__).resolve().parents[2])
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from controller import reconcile_eod as mod


DATE = "2026-09-10"
CLOSE = datetime(2026, 9, 10, 15, 59, tzinfo=ZoneInfo("America/New_York"))
STRATEGIES = ("mrpt", "mtfs", "ssrs", "aiss", "bdc", "aeus")


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "OUT_DIR", str(tmp_path))
    monkeypatch.setattr(mod, "official_info", lambda: {
        st: {"source": "official.json", "column": st + "_equity",
             "rows": [(DATE, 200.0)]} for st in STRATEGIES
    })

    def make(strategies=STRATEGIES):
        nodes = {
            st: {"kind": "strategy", "children": [(st + "_stock", 10.0)],
                 "attrs": {"display_name": st.upper(), "cash_const": 100.0}}
            for st in strategies
        }
        nodes["portfolio"] = {"kind": "portfolio",
                              "children": [(st, 1.0) for st in strategies],
                              "attrs": {}}
        rows = {st: 200.0 for st in strategies}
        rows["portfolio"] = 200.0 * len(strategies)
        state = SimpleNamespace(nodes=nodes, rows=rows, missing=set(),
                                daily_requested=[], minute_requested=[],
                                close=CLOSE, snapshot=True, hashes={})
        registry = SimpleNamespace(
            nodes={st: {"canonical_key": "strategy:" + st} for st in strategies},
            render=lambda nid: nid,
        )
        monkeypatch.setattr(mod, "Registry", lambda: registry)

        class Feed:
            def __init__(self, reg):
                assert reg is registry

            def daily_close(self, leaves, date):
                assert date == DATE
                state.daily_requested[:] = leaves
                return {leaf: 11.0 for leaf in leaves if leaf not in state.missing}

            def minute_closes(self, leaves, date):
                assert date == DATE
                state.minute_requested[:] = leaves
                return {
                    leaf: [] if leaf in state.missing else [
                        ((state.close - timedelta(minutes=20)).timestamp(), 9.0),
                        ((state.close - timedelta(minutes=16)).timestamp(), 10.0),
                        ((state.close - timedelta(minutes=15)).timestamp(), 11.0),
                    ] for leaf in leaves
                }

        monkeypatch.setattr(mod, "PriceFeed", Feed)

        def run(*, write_report=True):
            if state.snapshot:
                (tmp_path / "structure_snapshot_test.json").write_text(
                    json.dumps({"nodes": state.nodes}))
            with (tmp_path / "nav_stream_20260910.csv").open("w") as fh:
                writer = csv.writer(fh)
                writer.writerow(["ts", "node_id", "value", "structure_hash"])
                writer.writerows((state.close.isoformat(), nid, value,
                                  state.hashes.get(nid, "test"))
                                 for nid, value in state.rows.items())
            return mod.reconcile(DATE, write_report=write_report)

        state.run = run
        return state

    return make


def test_all_six_strategies_and_portfolio_use_independent_prices(scenario):
    state = scenario()
    report = state.run()
    assert set(report["strategies"]) == set(STRATEGIES)
    assert set(state.minute_requested) == {st + "_stock" for st in STRATEGIES}
    assert state.daily_requested == state.minute_requested
    assert report["price_lag_min"] == 16
    assert report["verdict"] == report["portfolio_check"]["status"] == "ok"
    assert report["portfolio_check"]["independent_value"] == 1200.0
    assert all(row["position_check"]["diff_usd"] == 0.0
               for row in report["strategies"].values())


def test_aeus_error_cannot_hide_behind_five_good_strategies(scenario):
    state = scenario()
    state.rows["aeus"] += 5.0
    state.rows["portfolio"] += 5.0
    report = state.run()
    assert report["verdict"] == "breach"
    assert report["strategies"]["aeus"]["position_check"]["status"] == "breach"
    assert report["portfolio_check"]["status"] == "breach"
    assert all(report["strategies"][st]["position_check"]["status"] == "ok"
               for st in STRATEGIES if st != "aeus")


def test_missing_aeus_minute_price_makes_coverage_incomplete(scenario):
    state = scenario()
    state.missing.add("aeus_stock")
    report = state.run()
    assert report["verdict"] == "incomplete"
    assert report["strategies"]["aeus"]["position_check"]["status"] == "incomplete"
    assert report["portfolio_check"]["status"] == "incomplete"
    assert report["portfolio_check"]["missing_minute_bars"] == ["aeus_stock"]


@pytest.mark.parametrize("missing", ["aeus", "mrpt", "portfolio"])
def test_missing_close_row_is_explicitly_incomplete(scenario, missing):
    state = scenario()
    state.rows.pop(missing)
    report = state.run()
    assert report["verdict"] == "incomplete"
    check = (report["portfolio_check"] if missing == "portfolio"
             else report["strategies"][missing]["position_check"])
    assert check["status"] == "incomplete"
    assert "close row missing" in check["note"]


def test_portfolio_value_is_checked_even_when_strategies_all_pass(scenario):
    state = scenario()
    state.rows["portfolio"] += 10.0
    report = state.run()
    assert all(row["position_check"]["status"] == "ok"
               for row in report["strategies"].values())
    assert report["portfolio_check"]["status"] == report["verdict"] == "breach"


def test_portfolio_direct_holding_has_price_coverage(scenario):
    state = scenario()
    state.nodes["portfolio"]["children"].append(("direct_stock", 2.0))
    state.rows["portfolio"] += 20.0
    assert state.run()["verdict"] == "ok"
    assert "direct_stock" in state.minute_requested
    state.missing.add("direct_stock")
    report = state.run()
    assert report["verdict"] == "incomplete"
    assert report["portfolio_check"]["missing_minute_bars"] == ["direct_stock"]


@pytest.mark.parametrize("strategies", [STRATEGIES[:-1], STRATEGIES + ("new_strategy",)])
def test_strategy_coverage_follows_snapshot_not_current_anchor_list(scenario, strategies):
    report = scenario(strategies).run()
    assert set(report["strategies"]) == set(strategies)
    assert report["verdict"] == "ok"


@pytest.mark.parametrize("case", ["no_portfolio", "multiple_portfolios", "no_strategy",
                                 "missing_snapshot", "mixed_hashes", "missing_hash", "missing_prices",
                                 "too_early"])
def test_incomplete_inputs_never_emit_false_ok(scenario, case):
    state = scenario()
    if case == "no_portfolio":
        state.nodes.pop("portfolio")
    elif case == "multiple_portfolios":
        state.nodes["other_portfolio"] = state.nodes["portfolio"]
    elif case == "no_strategy":
        state.nodes = {"portfolio": {"kind": "portfolio", "children": [], "attrs": {}}}
    elif case == "missing_snapshot":
        state.snapshot = False
    elif case == "mixed_hashes":
        state.hashes["aeus"] = "different"
    elif case == "missing_hash":
        state.hashes["aeus"] = ""
    elif case == "missing_prices":
        state.missing = {st + "_stock" for st in STRATEGIES}
    elif case == "too_early":
        state.close = CLOSE - timedelta(hours=1)
    report = state.run()
    assert report["verdict"] == "incomplete"
    assert report["portfolio_check"]["status"] == "incomplete"


def test_known_breach_is_retained_when_another_strategy_is_incomplete(scenario):
    state = scenario()
    state.rows["aiss"] += 5.0
    state.missing.add("aeus_stock")
    report = state.run()
    assert report["verdict"] == "breach"
    assert report["strategies"]["aiss"]["position_check"]["status"] == "breach"
    assert report["portfolio_check"]["status"] == "incomplete"


def test_all_cash_strategies_still_reconcile_without_prices(scenario):
    state = scenario()
    for st in STRATEGIES:
        state.nodes[st]["children"] = []
        state.rows[st] = 100.0
    state.rows["portfolio"] = 600.0
    report = state.run()
    assert report["verdict"] == "ok"
    assert state.minute_requested == []
    assert report["portfolio_check"]["independent_value"] == 600.0


def test_dry_run_does_not_replace_report(scenario, tmp_path):
    state = scenario()
    path = tmp_path / f"reconcile_{DATE}.json"
    path.write_text('existing report stays untouched')
    assert state.run(write_report=False)["verdict"] == "ok"
    assert path.read_text() == 'existing report stays untouched'
