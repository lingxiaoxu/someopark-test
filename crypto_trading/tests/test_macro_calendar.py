"""Macro release calendar + the W7 prod macro guard (no network: feeds are stubbed)."""
import json, time
from datetime import date, datetime
from zoneinfo import ZoneInfo
import pytest
from crypto_trading.crypto_common import macro_calendar as mc

ET = ZoneInfo("America/New_York")
FOMC_HTML = """<h4><a id="42828">2026 FOMC Meetings</a></h4><div class="row fomc-meeting"><div class="fomc-meeting__month col-xs-5"><strong>January</strong></div>
<div class="fomc-meeting__date col-xs-4">27-28</div></div><div class="row fomc-meeting"><div class="fomc-meeting__month col-xs-5"><strong>March</strong></div>
<div class="fomc-meeting__date col-xs-4">17-18*</div></div><div class="row fomc-meeting"><div class="fomc-meeting__month"><strong>October</strong></div>
<div class="fomc-meeting__date">27-28</div></div><div class="row fomc-meeting"><div class="fomc-meeting__month"><strong>December</strong></div><div class="fomc-meeting__date">8-9*</div></div>
<h4><a id="45694">2027 FOMC Meetings</a></h4><div class="fomc-meeting__month"><strong>January</strong></div><div class="fomc-meeting__date">26-27</div>"""


@pytest.fixture
def cal(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "DIR", tmp_path); monkeypatch.setattr(mc, "FILE", tmp_path / "calendar.json"); mc._CACHE.update(mtime=None, doc={})
    monkeypatch.setattr(mc, "env", lambda k, d="": "k" if k == "FRED_API_KEY" else d)
    monkeypatch.setattr(mc, "fetch_fred_dates", lambda rid, key, today, days_ahead=150: {10: ["2026-10-14", "2026-11-10"], 50: ["2026-11-06"]}.get(rid, []))
    monkeypatch.setattr(mc, "fetch_fomc", lambda today, days_ahead=150: [dict(key="fomc", name="FOMC statement", date="2026-10-28", time_et="14:00", t_utc=mc._et_epoch(date(2026, 10, 28), "14:00"), source="fed")])
    return tmp_path


def test_fomc_page_parse_takes_the_statement_day():
    ds = mc.parse_fomc(FOMC_HTML, (2026, 2027))
    assert ds == [date(2026, 1, 28), date(2026, 3, 18), date(2026, 10, 28), date(2026, 12, 9), date(2027, 1, 27)]


def test_release_times_are_eastern_with_dst():
    assert mc._et_epoch(date(2026, 10, 14), "08:30") == datetime(2026, 10, 14, 8, 30, tzinfo=ET).timestamp()
    assert datetime.fromtimestamp(mc._et_epoch(date(2026, 10, 14), "08:30"), tz=ET).utcoffset().total_seconds() == -4 * 3600     # EDT
    assert datetime.fromtimestamp(mc._et_epoch(date(2026, 12, 10), "08:30"), tz=ET).utcoffset().total_seconds() == -5 * 3600     # EST


def test_rule_dates_ism_conf_board_umich():
    r = {(e["key"], e["date"]) for e in mc.rule_dates(date(2026, 10, 1), 40)}
    assert ("ism_mfg", "2026-10-01") in r and ("ism_svc", "2026-10-05") in r         # Oct 1 2026 is a Thursday; 3rd business day = Mon Oct 5
    assert ("conf_board", "2026-10-27") in r and ("umich_prelim", "2026-10-09") in r
    assert all(e["time_et"] == "10:00" for e in mc.rule_dates(date(2026, 10, 1), 40))


FIXED = datetime(2026, 10, 12, 12, 0, tzinfo=ET).timestamp()        # 'now' for the file's generation time: two days before the CPI event


def test_refresh_writes_events_and_active_window(cal, monkeypatch):
    monkeypatch.setattr(mc.time, "time", lambda: FIXED)
    doc = mc.refresh(days_ahead=60)
    assert doc["ok"] and doc["last_status"]["cpi"] == "ok 2" and doc["last_status"]["fomc"] == "ok 1"
    keys = {(e["key"], e["date"]) for e in doc["events"]}
    assert ("cpi", "2026-10-14") in keys and ("nfp", "2026-11-06") in keys and ("fomc", "2026-10-28") in keys and any(k == "ism_mfg" for k, _ in keys)
    cpi = mc._et_epoch(date(2026, 10, 14), "08:30")
    assert mc.active(cpi - 1) is None
    a = mc.active(cpi + 10); assert a["key"] == "cpi" and a["until"] == cpi + 45 * 60
    assert mc.active(cpi + 45 * 60 - 1)["key"] == "cpi" and mc.active(cpi + 45 * 60) is None
    assert mc.active(cpi + 5, after_s=60)["until"] == cpi + 60
    fr = mc.freshness(cpi); assert not fr["stale"] and fr["n_future"] >= 3


def test_failed_refresh_keeps_the_previous_file_and_stale_fails_open(cal, monkeypatch):
    monkeypatch.setattr(mc.time, "time", lambda: FIXED)
    doc = mc.refresh(days_ahead=60); n = len(doc["events"])
    monkeypatch.setattr(mc, "fetch_fred_dates", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(mc, "fetch_fomc", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    doc2 = mc.refresh(days_ahead=60)
    assert doc2["ok"] is False and len(doc2["events"]) >= n - 2 and doc2["generated_ts"] == doc["generated_ts"]          # previous events retained
    assert doc2["last_status"]["cpi"].startswith("error")
    cpi = mc._et_epoch(date(2026, 10, 14), "08:30"); assert mc.active(cpi + 10)["key"] == "cpi"                             # still usable
    # a stale file (generated long ago) -> no guard, flagged
    far = cpi + 30 * 86400; assert mc.freshness(far)["stale"] and mc.active(far) is None
    (cal / "calendar.json").unlink(); mc._CACHE.update(mtime=None, doc={}); assert mc.freshness()["stale"] and mc.active(cpi + 10) is None


def test_prod_router_cuts_to_five_contracts_after_a_release_and_fails_open(monkeypatch, tmp_path):
    import crypto_trading.crypto_common.execution_events as ee
    from crypto_trading.crypto_strategies.w7_scenarios import live_plan
    monkeypatch.setattr(ee.EventExecutionRouter, "TABLE_LIVE", {"w7_noisefade": {"enabled": True, "window_cap_mult": 1.5}})   # conftest disables the table path
    monkeypatch.setattr(ee.EventExecutionRouter, "PROD_BAND", {"w7_noisefade": (0.79, 0.98)})
    monkeypatch.setattr(ee.EventExecutionRouter, "_tail_rows", staticmethod(lambda path, n: []))
    monkeypatch.setattr(ee.EventExecutionRouter, "_utc_hour", staticmethod(lambda: 14))
    monkeypatch.setattr(ee.EventExecutionRouter, "_flow_gate_decision", lambda self, t, s: {"decision": "accept"})
    monkeypatch.setattr(live_plan, "SLOTS_FILE", tmp_path / "slots.json"); live_plan.reset_ledger()
    w7 = ee.EventExecutionRouter(strategy="w7_noisefade")
    ev = {"key": "cpi", "name": "CPI", "t_utc": time.time() - 60, "until": time.time() + 2640, "source": "fred"}
    monkeypatch.setattr(mc, "active", lambda now=None, after_s=2700, before_s=0: ev)
    monkeypatch.setattr(mc, "freshness", lambda now=None: {"stale": False})
    t = lambda slot, mult: {"action": "trade", "slot": slot, "mult": mult, "scenario": "S"}
    r = w7.submit(ticker="KXBTC15M-26OCT141230-30", side="yes", entry_price=0.85, contracts=25, size_mult=1.5, entry_source="table_top1_T-9.75", table_decision=t("top1", 1.5))
    assert r["status"] == "live_disarmed" and r["contracts"] == 5 and r["macro"]["event"] == "cpi" and r["window_cap"]["cap"] == 5      # 40 x1.5 = 60 -> 5
    r2 = w7.submit(ticker="KXBTC15M-26OCT141230-30", side="yes", entry_price=0.85, contracts=25, size_mult=1.0, entry_source="table_top2_T-6.75", table_decision=t("top2", 1.0))
    assert r2["status"] == "live_disarmed" and r2["contracts"] == 5 and r2["window_cap"]["cap"] == 5          # the disarmed first leg released its reservation
    # outside the window: untouched
    monkeypatch.setattr(mc, "active", lambda now=None, after_s=2700, before_s=0: None)
    r3 = w7.submit(ticker="KXETH15M-26OCT141300-00", side="yes", entry_price=0.85, contracts=25, size_mult=1.0, entry_source="table_top1_T-9.75", table_decision=t("top1", 1.0))
    assert r3["contracts"] == 30 and "macro" not in r3
    # calendar stale/missing or the module blowing up -> fail open, audited
    monkeypatch.setattr(mc, "active", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("calendar down")))
    r4 = w7.submit(ticker="KXSOL15M-26OCT141300-00", side="yes", entry_price=0.85, contracts=25, size_mult=1.0, entry_source="table_top1_T-9.75", table_decision=t("top1", 1.0))
    assert r4["contracts"] == 30 and r4.get("macro_guard") == "error"
    monkeypatch.setattr(mc, "active", lambda now=None, after_s=2700, before_s=0: None); monkeypatch.setattr(mc, "freshness", lambda now=None: {"stale": True})
    r5 = w7.submit(ticker="KXXRP15M-26OCT141300-00", side="yes", entry_price=0.85, contracts=25, size_mult=1.0, entry_source="table_top1_T-9.75", table_decision=t("top1", 1.0))
    assert r5["contracts"] == 30 and r5.get("macro_guard") == "stale"
    # other strategies never consult it
    assert "MACRO_GUARD" in ee.EventExecutionRouter.__dict__ and "w5_knockdown" not in ee.EventExecutionRouter.MACRO_GUARD


def test_macro_guard_clamps_without_the_table_path(monkeypatch):
    import crypto_trading.crypto_common.execution_events as ee
    monkeypatch.setattr(ee.EventExecutionRouter, "TABLE_LIVE", {"w7_noisefade": {"enabled": False}})
    monkeypatch.setattr(ee.EventExecutionRouter, "_tail_rows", staticmethod(lambda path, n: [])); monkeypatch.setattr(ee.EventExecutionRouter, "_utc_hour", staticmethod(lambda: 14))
    monkeypatch.setattr(ee.EventExecutionRouter, "_flow_gate_decision", lambda self, t, s: {"decision": "accept"})
    monkeypatch.setattr(mc, "active", lambda now=None, after_s=2700, before_s=0: {"key": "fomc", "name": "FOMC", "t_utc": time.time() - 100, "until": time.time() + 2600, "source": "fed"})
    r = ee.EventExecutionRouter(strategy="w7_noisefade").submit(ticker="KXETH15M-26OCT281415-15", side="no", entry_price=0.90, contracts=25)
    assert r["status"] == "live_disarmed" and r["contracts"] == 5 and r["macro"]["event"] == "fomc"
