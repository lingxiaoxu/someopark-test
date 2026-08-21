"""execution_shadow 单测 — E1-2 影子(2026-08-17)。全部 IO 走 tmp_path。"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from VolumePrediction import execution_shadow as es


class FakeAdv:
    def __init__(self, table):
        self.table = table          # ticker → adv_shares

    def info(self, ticker, date=None, **kw):
        v = self.table.get(ticker)
        if v is None:
            return {"ticker": ticker, "adv_shares": None, "source": "unavailable"}
        return {"ticker": ticker, "adv_shares": v, "last_price": 100.0,
                "source": "blend"}


class FakeSvc:
    def __init__(self, table):
        self.adv = FakeAdv(table)


def _signals_file(tmp_path, sd="2026-08-14"):
    doc = {"signal_date": sd,
           "mrpt": {"signals": [
               {"pair": "AAA/BBB", "action": "OPEN_LONG",
                "s1": "AAA", "s2": "BBB", "s1_shares": 1000, "s2_shares": -2000,
                "s1_price": 50.0, "s2_price": 25.0},
               {"pair": "CCC/DDD", "action": "MACRO_VETO",
                "s1": "CCC", "s2": "DDD", "s1_shares": 10, "s2_shares": -10,
                "s1_price": 1.0, "s2_price": 1.0},
               {"pair": "EEE/FFF", "action": "CLOSE_STOP",
                "s1": "EEE", "s2": "FFF", "s1_shares": None, "s2_shares": None,
                "s1_price": None, "s2_price": None}]},
           "mtfs": {"signals": []}}
    p = tmp_path / f"combined_signals_{sd.replace('-', '')}_090000.json"
    p.write_text(json.dumps(doc))
    return p


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(es, "SIGNALS_GLOB", str(tmp_path / "combined_signals_*.json"))
    monkeypatch.setattr(es, "LEDGERS", {"aiss": tmp_path / "trade_ledger_aiss.jsonl"})
    monkeypatch.setattr(es, "SHADOW_DIR", tmp_path / "execution_shadow")
    monkeypatch.setattr(es, "TRACKING",
                        tmp_path / "execution_shadow" / "exec_shadow_tracking.csv")
    # E1-2 真开关三件套必须一并隔离 —— 否则 run() 会读到生产 exec_switch.json
    # (2026-08-21 起全开)并把测试 plan 写进生产 execution_live/。
    monkeypatch.setattr(es, "LIVE_DIR", tmp_path / "execution_live")
    monkeypatch.setattr(es, "SWITCH_PATH",
                        tmp_path / "execution_live" / "exec_switch.json")
    monkeypatch.setattr(es, "CONSUMPTION",
                        tmp_path / "execution_live" / "consumption_log.csv")
    return tmp_path


def _switch_on(tmp_path, strategies=("mrpt", "mtfs", "aiss", "ssrs")):
    p = tmp_path / "execution_live" / "exec_switch.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema": "v1",
                             "strategies": {s: True for s in strategies}}))
    return p


def test_collect_pairs_filters_actions_and_dedups(sandbox):
    _signals_file(sandbox)
    _signals_file(sandbox)          # 同日重写 → 去重
    evs = es.collect_pair_trades("2026-08-01")
    assert len(evs) == 2            # MACRO_VETO 不是成交
    acts = {e["action"] for e in evs}
    assert acts == {"OPEN_LONG", "CLOSE_STOP"}
    assert [e for e in evs if e["action"] == "CLOSE_STOP"][0]["legs_ok"] is False
    assert es.collect_pair_trades("2026-08-15") == []      # since 过滤


def test_collect_basket_groups_by_day(sandbox):
    led = sandbox / "trade_ledger_aiss.jsonl"
    rows = [{"date": "2026-08-14", "ticker": "KLAC", "side": "BUY",
             "shares": 100, "price": 200.0},
            {"date": "2026-08-14", "ticker": "KLAC", "side": "SELL",
             "shares": 40, "price": 201.0},
            {"date": "2026-08-14", "ticker": "MU", "side": "SELL",
             "shares": 50, "price": 1000.0},
            {"date": "2026-08-14", "ticker": "X", "side": "DIV",
             "shares": 1, "price": 1.0}]
    led.write_text("\n".join(json.dumps(r) for r in rows))
    evs = es.collect_basket_trades("2026-08-01")
    assert len(evs) == 1
    b = evs[0]
    assert b["trades"] == {"KLAC": 60.0, "MU": -50.0}      # 净额;DIV 排除
    assert b["trade_type"] == "rebalance"


def test_shadow_pair_schedules_and_costs(sandbox):
    ev = {"strategy": "mrpt", "date": "2026-08-14", "unit": "AAA/BBB",
          "action": "OPEN_LONG", "trade_type": "entry", "legs_ok": True,
          "s1": "AAA", "s2": "BBB", "s1_shares": 1000, "s2_shares": -2000,
          "s1_price": 50.0, "s2_price": 25.0}
    svc = FakeSvc({"AAA": 100_000.0, "BBB": 200_000.0})
    rec = es.shadow_pair(svc, ev)
    assert rec["status"] == "ok"
    assert rec["profile"] == "pairs_entry"
    assert rec["completed"] is True
    assert rec["days_scheduled"] == 1                       # cap 远大于目标 → 1 天
    assert rec["est_cost_sched"] > 0
    assert rec["est_saving"] == pytest.approx(
        rec["est_cost_1d"] - rec["est_cost_sched"])
    # 对冲同步不变式: 排程首日两腿比例一致
    s0 = rec["schedule"][0]
    assert s0["s1_shares"] / 1000 == pytest.approx(s0["s2_shares"] / 2000)


def test_shadow_pair_illiquid_multiday_saving_positive(sandbox):
    ev = {"strategy": "mtfs", "date": "2026-08-14", "unit": "AAA/BBB",
          "action": "CLOSE", "trade_type": "exit", "legs_ok": True,
          "s1": "AAA", "s2": "BBB", "s1_shares": 50_000, "s2_shares": -50_000,
          "s1_price": 50.0, "s2_price": 25.0}
    svc = FakeSvc({"AAA": 50_000.0, "BBB": 500_000.0})      # AAA=瓶颈腿
    rec = es.shadow_pair(svc, ev)
    assert rec["status"] == "ok"
    assert rec["days_scheduled"] == 5                        # 50k/(0.2×50k)=5 天
    assert rec["bottleneck_leg"] == "AAA"
    assert rec["est_saving"] > 0                             # 排程摊薄冲击
    assert rec["max_participation"] <= 0.20 + 1e-9


def test_shadow_pair_missing_shares_recorded_not_scheduled(sandbox):
    ev = {"strategy": "mrpt", "date": "2026-08-14", "unit": "EEE/FFF",
          "action": "CLOSE_STOP", "trade_type": "stop", "legs_ok": False,
          "s1": "EEE", "s2": "FFF", "s1_shares": None, "s2_shares": None,
          "s1_price": None, "s2_price": None}
    rec = es.shadow_pair(FakeSvc({}), ev)
    assert rec["status"] == "no_shares_in_signal"


def test_shadow_basket_aligned_for_aiss(sandbox):
    ev = {"strategy": "aiss", "date": "2026-08-14", "unit": "basket",
          "action": "REBALANCE", "trade_type": "rebalance",
          "trades": {"KLAC": 60.0, "MU": -50.0},
          "prices": {"KLAC": 200.0, "MU": 1000.0}}
    svc = FakeSvc({"KLAC": 10_000.0, "MU": 5_000.0})
    rec = es.shadow_basket(svc, ev)
    assert rec["status"] == "ok"
    assert rec["profile"] == "aiss_rebalance"
    assert rec["scheduler"] == "basket_aligned"              # constraints.basket=true
    assert rec["completed"] is True
    assert rec["est_cost_sched"] > 0


def test_run_idempotent_via_tracking(sandbox, monkeypatch):
    _signals_file(sandbox)
    monkeypatch.setattr(es, "VolumeService", None, raising=False)
    # 用 FakeSvc 替换真服务(run 内部延迟 import → patch 模块引用)
    import VolumePrediction.service as _svc_mod
    monkeypatch.setattr(_svc_mod, "VolumeService",
                        lambda: FakeSvc({"AAA": 100_000.0, "BBB": 200_000.0}))
    r1 = es.run(since="2026-08-01")
    assert r1["new"] == 2
    assert r1["consumed"] == 0                               # 无开关文件 = 全关
    r2 = es.run(since="2026-08-01")
    assert r2["new"] == 0                                    # 幂等
    track = pd.read_csv(es.TRACKING)
    assert len(track) == 2
    day_json = es.SHADOW_DIR / "exec_shadow_mrpt_2026-08-14.json"
    doc = json.loads(day_json.read_text())
    assert len(doc["trades"]) == 2
    assert not (sandbox / "execution_live").exists()         # 关 = 零写入


# ── E1-2 真开关 ──────────────────────────────────────────────────────────────

def test_switch_missing_is_all_off_and_corrupt_raises(sandbox):
    assert es.load_switch() == {"strategies": {}}
    p = _switch_on(sandbox)
    p.write_text(json.dumps({"strategies": "yes"}))          # 坏结构
    with pytest.raises(ValueError):
        es.load_switch()


def test_compliance_verdicts(sandbox):
    assert es._compliance({"status": "ok", "days_scheduled": 1}) == "compliant_1day"
    assert es._compliance({"status": "partial", "days_scheduled": 3}) == "DIVERGENT_multiday"
    assert es._compliance({"status": "no_adv_data"}) == "no_schedule(no_adv_data)"


def test_run_with_switch_writes_instruction_of_record(sandbox, monkeypatch):
    _signals_file(sandbox)
    _switch_on(sandbox)
    import VolumePrediction.service as _svc_mod
    monkeypatch.setattr(_svc_mod, "VolumeService",
                        lambda: FakeSvc({"AAA": 100_000.0, "BBB": 200_000.0}))
    r = es.run(since="2026-08-01")
    assert r["consumed"] == 2
    assert r["divergent"] == 0                               # cap 充裕 → 全 1 天合规
    plan = json.loads((sandbox / "execution_live"
                       / "exec_plan_mrpt_2026-08-14.json").read_text())
    assert plan["instruction_of_record"] is True
    verd = {t["unit"]: t["compliance"] for t in plan["trades"]}
    assert verd["AAA/BBB"] == "compliant_1day"
    assert verd["EEE/FFF"].startswith("no_schedule")
    con = pd.read_csv(sandbox / "execution_live" / "consumption_log.csv")
    assert len(con) == 2
    assert list(con.columns) == es._CONSUME_COLS


def test_run_divergent_multiday_flags_loudly(sandbox, monkeypatch, caplog):
    # AAA ADV 压到 50k、目标 50k 股 → cap 0.2 逼出 5 天排程 = 绊线场景
    doc = {"signal_date": "2026-08-14",
           "mrpt": {"signals": [
               {"pair": "AAA/BBB", "action": "CLOSE",
                "s1": "AAA", "s2": "BBB", "s1_shares": 50_000,
                "s2_shares": -50_000, "s1_price": 50.0, "s2_price": 25.0}]},
           "mtfs": {"signals": []}}
    (sandbox / "combined_signals_20260814_090000.json").write_text(json.dumps(doc))
    _switch_on(sandbox)
    import VolumePrediction.service as _svc_mod
    monkeypatch.setattr(_svc_mod, "VolumeService",
                        lambda: FakeSvc({"AAA": 50_000.0, "BBB": 500_000.0}))
    with caplog.at_level("WARNING"):
        r = es.run(since="2026-08-01")
    assert r["divergent"] == 1
    assert any("EXEC_DIVERGENT" in m for m in caplog.messages)
    con = pd.read_csv(sandbox / "execution_live" / "consumption_log.csv")
    assert con.iloc[0]["compliance"] == "DIVERGENT_multiday"


def test_consumption_log_column_alignment_guard(sandbox, monkeypatch):
    # 已有 log 列序被打乱 → 追加必须按旧表头对齐;列集不同必须大声抛
    _signals_file(sandbox)
    _switch_on(sandbox)
    live = sandbox / "execution_live"; live.mkdir(parents=True, exist_ok=True)
    shuffled = list(reversed(es._CONSUME_COLS))
    (live / "consumption_log.csv").write_text(
        ",".join(shuffled) + "\n" + ",".join(["x"] * len(shuffled)) + "\n")
    import VolumePrediction.service as _svc_mod
    monkeypatch.setattr(_svc_mod, "VolumeService",
                        lambda: FakeSvc({"AAA": 100_000.0, "BBB": 200_000.0}))
    es.run(since="2026-08-01")
    con = pd.read_csv(live / "consumption_log.csv")
    assert list(con.columns) == shuffled                      # 尊重已有表头
    assert con.iloc[-1]["strategy"] == "mrpt"                 # 值落对了列
    # 列集不一致 → 抛
    (live / "consumption_log.csv").write_text("bogus_col\n1\n")
    _signals_file(sandbox, sd="2026-08-15")
    with pytest.raises(ValueError, match="表头不匹配"):
        es.run(since="2026-08-01")
