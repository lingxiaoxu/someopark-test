"""Splits refresh failure safety; synthetic data only, all files under /tmp/vp_tests."""
from __future__ import annotations

import json
import logging
import tempfile
import traceback
from datetime import date, timedelta
from pathlib import Path

import pytest

from VolumePrediction.data import splits_loader as sl


SPLITS = [{"ticker": "TEST", "execution_date": "2026-06-08",
           "split_from": 1, "split_to": 10}]


@pytest.fixture
def cache(monkeypatch):
    root = Path("/tmp/vp_tests/splits_refresh")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as work:
        path = Path(work) / "splits_cache.json"
        monkeypatch.setattr(sl, "CACHE", path)
        monkeypatch.setattr(sl, "_failed_refresh_at", {})
        monkeypatch.setattr(sl, "log", logging.getLogger("test.vp.splits"))
        # Every test must explicitly provide an offline upstream implementation.
        monkeypatch.setattr(sl, "fetch_all_splits", lambda *a, **kw: pytest.fail("unmocked fetch"))
        yield path


def write_cache(path, *, results=None, fetched_at=None, since="2019-01-01"):
    path.write_text(json.dumps({"fetched_at": fetched_at or str(date.today() - timedelta(days=1)),
                                "since": since, "results": SPLITS if results is None else results}))
    return path.read_bytes()


def test_valid_fresh_cache_avoids_fetch(cache):
    write_cache(cache, fetched_at=str(date.today()))
    assert sl.refresh() == SPLITS


@pytest.mark.parametrize("response", [[], None, {}, [{"ticker": "TEST"}],
                                      [{**SPLITS[0], "split_from": 0}],
                                      [{**SPLITS[0], "split_to": float("nan")}],
                                      [{**SPLITS[0], "execution_date": "bad-date"}]])
def test_invalid_response_preserves_cache_and_date(cache, monkeypatch, caplog, response):
    before = write_cache(cache)
    monkeypatch.setattr(sl, "fetch_all_splits", lambda *a, **kw: response)
    with caplog.at_level(logging.WARNING):
        assert sl.refresh() == SPLITS
    assert cache.read_bytes() == before
    assert "cache freshness unchanged" in caplog.text


def test_failure_cooldown_then_retry(cache, monkeypatch):
    before = write_cache(cache)
    calls = []
    now = [100.0]
    monkeypatch.setattr(sl.time, "monotonic", lambda: now[0])

    def fetch(since, *, use_cache):
        calls.append((since, use_cache))
        return [] if len(calls) == 1 else SPLITS

    monkeypatch.setattr(sl, "fetch_all_splits", fetch)
    assert sl.refresh() == SPLITS
    assert sl.refresh() == SPLITS
    assert calls == [("2019-01-01", False)]
    assert cache.read_bytes() == before
    now[0] += sl._RETRY_SECONDS + 1
    assert sl.refresh() == SPLITS
    assert len(calls) == 2
    assert json.loads(cache.read_text())["fetched_at"] == str(date.today())


def test_today_empty_cache_is_not_a_hit(cache, monkeypatch):
    write_cache(cache, results=[], fetched_at=str(date.today()))
    calls = []

    def fetch(since, *, use_cache):
        calls.append((since, use_cache))
        return SPLITS

    monkeypatch.setattr(sl, "fetch_all_splits", fetch)
    assert sl.refresh() == SPLITS
    assert calls == [("2019-01-01", False)]
    assert json.loads(cache.read_text())["results"] == SPLITS
    assert not cache.with_suffix(".tmp").exists()


@pytest.mark.parametrize("bad_cache", ["missing", "empty", "corrupt", "narrow", "future", "bad-date"])
def test_no_usable_cache_fails_without_publishing(cache, monkeypatch, bad_cache):
    if bad_cache == "empty":
        write_cache(cache, results=[], fetched_at=str(date.today()))
    elif bad_cache == "corrupt":
        cache.write_text("invalid json")
    elif bad_cache == "narrow":
        write_cache(cache, since="2025-01-01")
    elif bad_cache == "future":
        write_cache(cache, fetched_at=str(date.today() + timedelta(days=1)))
    elif bad_cache == "bad-date":
        write_cache(cache, fetched_at="unknown")
    before = cache.read_bytes() if cache.exists() else None
    monkeypatch.setattr(sl, "fetch_all_splits", lambda *a, **kw: [])
    with pytest.raises(RuntimeError, match="no valid covering cache"):
        sl.refresh()
    with pytest.raises(RuntimeError, match="retry cooling down"):
        sl.refresh()
    assert (cache.read_bytes() if cache.exists() else None) == before


@pytest.mark.parametrize("has_cache", [False, True])
def test_raised_exception_and_upstream_logs_do_not_leak_key(cache, monkeypatch, caplog, capsys, has_cache):
    if has_cache:
        before = write_cache(cache)
    upstream = logging.getLogger("corporate_actions")
    original_filters = list(upstream.filters)
    secret = "synthetic-private-key"

    def fetch(*args, **kwargs):
        upstream.warning("request failed: https://example.invalid/?apiKey=%s", secret)
        raise RuntimeError(f"failed url apiKey={secret}")

    monkeypatch.setattr(sl, "fetch_all_splits", fetch)
    with caplog.at_level(logging.WARNING):
        if has_cache:
            assert sl.refresh() == SPLITS
            assert cache.read_bytes() == before
        else:
            with pytest.raises(RuntimeError) as exc:
                sl.refresh()
            assert secret not in "".join(traceback.format_exception(exc.type, exc.value, exc.tb))
    assert secret not in caplog.text
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert upstream.filters == original_filters


def test_real_shared_fetch_network_failure_keeps_vp_cache_and_never_touches_shared(cache, monkeypatch, caplog, capsys):
    """Exercise the real swallowed-error path, replacing only the HTTP boundary."""
    import CorporateActions as ca

    before = write_cache(cache)
    secret = "synthetic-polygon-secret"
    monkeypatch.setenv("POLYGON_API_KEY", secret)
    monkeypatch.setattr(sl, "fetch_all_splits", ca.fetch_all_splits)
    monkeypatch.setattr(ca, "_load_splits_cache", lambda: pytest.fail("shared cache read"))
    monkeypatch.setattr(ca, "_save_splits_cache", lambda value: pytest.fail("shared cache write"))
    calls = []

    class FirstPage:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": SPLITS, "next_url": "https://example.invalid/next"}

    def get(url, *, params, timeout):
        calls.append(url)
        if len(calls) == 1:
            return FirstPage()
        raise RuntimeError(f"connection failed for {url}?apiKey={secret}")

    monkeypatch.setattr(ca.requests, "get", get)
    with caplog.at_level(logging.WARNING):
        assert sl.refresh() == SPLITS
    assert len(calls) == 2
    assert cache.read_bytes() == before
    assert secret not in caplog.text
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert "splits refresh degraded" in caplog.text
