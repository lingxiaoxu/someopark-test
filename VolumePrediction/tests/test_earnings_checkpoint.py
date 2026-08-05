"""财报缓存分块落盘(2026-08-04)。

守的教训: 逐票节流 0.3-0.8s,全宇宙一轮要 35-60 分钟。原实现只在整批结束后
存一次盘 —— 任何中断都让整段工作作废,下次从零开始,实测表现为"永远跑不完"。
本测试断言: 每块都落盘(中断只损失当前块),且断点续跑不重复已完成的票。
"""
import json

import pytest

from VolumePrediction.data import earnings_loader as el


def test_saves_each_chunk_and_resumes(tmp_path, monkeypatch):
    cache_p = tmp_path / "earnings_cache_vp.json"
    monkeypatch.setattr(el, "CACHE", cache_p)

    calls, saves = [], []
    orig_save = el._save_cache

    def fake_fetch(batch, cache=None, quiet=True):
        calls.append(list(batch))
        cache = cache or {"symbols": {}}
        cache.setdefault("symbols", {})
        for s in batch:
            cache["symbols"][s] = [{"earnings_date": "2026-01-15"}]
        return cache

    def spy_save(c):
        saves.append(len(c.get("symbols", {})))
        orig_save(c)

    monkeypatch.setattr(el.MFE, "run_fetch", fake_fetch)
    monkeypatch.setattr(el, "_save_cache", spy_save)

    syms = [f"T{i}" for i in range(600)]          # 600 票 → 3 块(250/250/100)
    el.fetch_symbols(syms)
    assert len(calls) == 3, f"应分 3 块,实际 {len(calls)}"
    assert saves == [250, 500, 600], f"每块都应落盘,实际 {saves}"
    assert cache_p.exists()

    # 断点续跑: 已缓存的票不再重拉
    calls.clear(); saves.clear()
    el.fetch_symbols(syms)
    assert calls == [], "已缓存的票被重复拉取"


def test_empty_result_is_cached(tmp_path, monkeypatch):
    """无财报的票也要落缓存([]),否则每次全量重拉。"""
    monkeypatch.setattr(el, "CACHE", tmp_path / "c.json")
    monkeypatch.setattr(el.MFE, "run_fetch",
                        lambda batch, cache=None, quiet=True: cache or {"symbols": {}})
    el.fetch_symbols(["NOEARN"])
    d = json.loads((tmp_path / "c.json").read_text())
    assert d["symbols"]["NOEARN"] == []
    assert d["_meta"]["NOEARN"]["fetched_at"]
