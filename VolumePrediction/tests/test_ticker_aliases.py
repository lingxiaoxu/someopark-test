"""ticker_aliases 统一改名处理(2026-08-15;BK→BNY / FB 回收实证)。

核心不变量:
1. 别名带日期窗口 —— 改名日前旧名有效(历史),改名日后解析到现名(查询);
2. 回收名(FB 今为 ProShares ETF)绝不误映射;
3. VP 查询面(forecast/adv/_ticker_frame)旧名自动解析且如实标注。
"""
import json

import pandas as pd
import pytest

import ticker_aliases as ta


@pytest.fixture()
def fake_aliases(monkeypatch, tmp_path):
    p = tmp_path / "ticker_aliases.json"
    p.write_text(json.dumps({"schema": "v1", "aliases": {
        "BK": {"current": "BNY", "changed": "2026-05-21",
               "cik": "0001390777", "verified": "polygon_events"},
        "FB": {"current": "META", "changed": "2022-06-09",
               "cik": "0001326801", "verified": "polygon_events"},
    }}))
    monkeypatch.setattr(ta, "ALIAS_PATH", p)
    ta._CACHE.update(mtime=None, data={})
    yield
    ta._CACHE.update(mtime=None, data={})


def test_resolve_is_date_windowed(fake_aliases):
    assert ta.resolve("BK") == "BNY"                     # 现在 → 现名
    assert ta.resolve("BK", "2026-08-14") == "BNY"       # 改名后 → 现名
    assert ta.resolve("BK", "2026-01-05") == "BK"        # 改名前 → 旧名(历史)
    assert ta.resolve("BNY") == "BNY"                    # 现名恒等
    assert ta.resolve("AAPL") == "AAPL"                  # 无别名恒等


def test_recycled_name_never_mismapped(fake_aliases):
    """FB 自 2025-06-26 被 ETF 回收: 归一映射只作用于 changed 之前的日子,
    回收后当日帧里的 FB(ETF)绝不被改成 META。"""
    m_old = ta.rename_map("2022-01-03")                  # META 还叫 FB 的年代
    m_new = ta.rename_map("2026-08-14")                  # FB=ETF 的年代
    assert m_old.get("FB") == "META"
    assert "FB" not in m_new
    df = pd.DataFrame({"ticker": ["FB", "AAPL"], "v": [1.0, 2.0]})
    out_new = ta.normalize_day_frame(df, "2026-08-14")
    assert list(out_new["ticker"]) == ["FB", "AAPL"]     # ETF 原样
    out_old = ta.normalize_day_frame(df, "2022-01-03")
    assert list(out_old["ticker"]) == ["META", "AAPL"]   # 历史归一


def test_normalize_zero_cost_when_no_hit(fake_aliases):
    df = pd.DataFrame({"ticker": ["AAPL"], "v": [1.0]})
    assert ta.normalize_day_frame(df, "2022-01-03") is df   # 零匹配返回原帧


def test_entry_from_events_rejects_recycled_entity():
    """回收实体(事件链里 old 即最新名)→ None;真改名 → 条目。"""
    recycled = {"name": "ProShares ETF", "events": [
        {"type": "ticker_change", "date": "2025-06-26",
         "ticker_change": {"ticker": "FB"}}]}
    assert ta._entry_from_events(recycled, "FB") is None
    renamed = {"name": "BNY", "cik": "1", "composite_figi": "F", "events": [
        {"type": "ticker_change", "date": "2007-07-02",
         "ticker_change": {"ticker": "BK"}},
        {"type": "ticker_change", "date": "2026-05-21",
         "ticker_change": {"ticker": "BNY"}}]}
    e = ta._entry_from_events(renamed, "BK")
    assert e and e["current"] == "BNY" and e["changed"] == "2026-05-21"


def test_resolve_hop_guard(fake_aliases, monkeypatch):
    """环状别名(数据错误)不得死循环。"""
    ta._CACHE.update(mtime=object(), data={
        "A": {"current": "B", "changed": "2020-01-01"},
        "B": {"current": "A", "changed": "2020-01-02"}})
    assert ta.resolve("A") in ("A", "B")                 # 有限步返回即可
    ta._CACHE.update(mtime=None, data={})


def test_vp_service_resolves_old_name_end_to_end():
    """真实服务: 用旧名 BK 查询 → 拿到 BNY 的数据并标注 resolved_ticker。
    (依赖生产 ticker_aliases.json 已 seed BK;raw 里有 BNY。)"""
    if "BK" not in ta.load_aliases():
        pytest.skip("production alias file lacks BK")
    from VolumePrediction.service import VolumeService
    svc = VolumeService()
    f = svc.forecast.get("BK")
    assert len(f) == 1
    r = f.iloc[0]
    assert r["symbol"] == "BK"                           # 调用方原 key 保留
    if r["source"] == "model":
        assert r.get("resolved_ticker") == "BNY"
        assert pd.notna(r["pred_V"]) and r["pred_V"] > 0
    adv = svc.adv.get_adv_forecast("BK")
    assert adv == adv and adv > 0                        # 不再 NaN
