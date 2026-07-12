"""disk_monitor tests — no real disk/notifications; monkeypatch state paths +
disk_usage + notifier. Verifies threshold logic + rate computation + alerting."""
import json
import time
from collections import namedtuple

import pytest

from crypto_trading.ops import disk_monitor as dm

Usage = namedtuple("Usage", "total used free")
GB = 1024 ** 3


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(dm, "LOGS", tmp_path)
    monkeypatch.setattr(dm, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(dm, "STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr(dm, "LOG_FILE", tmp_path / "mon.log")
    monkeypatch.setattr(dm, "WATCH_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir()
    fired = []
    monkeypatch.setattr(dm, "check.__globals__", dm.check.__globals__, raising=False)
    return tmp_path, fired


def run(monkeypatch, *, free_gb, used_gb=700.0, total_gb=926.0, now, fired):
    monkeypatch.setattr(dm.shutil, "disk_usage",
                        lambda p: Usage(int(total_gb * GB), int(used_gb * GB), int(free_gb * GB)))
    return dm.check(now=now, notifier=lambda t, m: fired.append((t, m)))


def test_ok_state_no_alert(sandbox, monkeypatch):
    _, fired = sandbox
    s = run(monkeypatch, free_gb=200, now=1000.0, fired=fired)
    assert s["level"] == "ok" and not s["alerts"] and not fired


def test_low_free_space_warns(sandbox, monkeypatch):
    _, fired = sandbox
    s = run(monkeypatch, free_gb=20, now=1000.0, fired=fired)
    assert s["level"] == "warn" and fired and "low free space" in s["alerts"][0]


def test_critical_free_space(sandbox, monkeypatch):
    _, fired = sandbox
    s = run(monkeypatch, free_gb=5, now=1000.0, fired=fired)
    assert s["level"] == "critical" and fired


def test_growth_rate_computed_and_flagged(sandbox, monkeypatch):
    _, fired = sandbox
    # first run seeds state at used=700GB
    run(monkeypatch, free_gb=200, used_gb=700.0, now=0.0, fired=fired)
    # 1 day later, used grew 3 GB → 3 GB/day > 2.0 ceiling → alert
    s = run(monkeypatch, free_gb=197, used_gb=703.0, now=86400.0, fired=fired)
    assert s["growth_gb_per_day"] == pytest.approx(3.0, abs=0.01)
    assert any("fast growth" in a for a in s["alerts"]) and s["level"] == "critical"


def test_rate_skipped_for_short_interval(sandbox, monkeypatch):
    _, fired = sandbox
    run(monkeypatch, free_gb=200, used_gb=700.0, now=0.0, fired=fired)
    # only 10 min later → below MIN_INTERVAL_FOR_RATE_S, no rate, no false alarm
    s = run(monkeypatch, free_gb=190, used_gb=710.0, now=600.0, fired=fired)
    assert s["growth_gb_per_day"] is None


def test_days_to_full_warning(sandbox, monkeypatch):
    _, fired = sandbox
    run(monkeypatch, free_gb=30, used_gb=700.0, now=0.0, fired=fired)
    # +1 GB/day, 30 GB free → 30 days to full < 45 window → warn
    s = run(monkeypatch, free_gb=29, used_gb=701.0, now=86400.0, fired=fired)
    assert s["days_to_full"] == 29 and any("days to full" in a for a in s["alerts"])


def test_state_and_log_persisted(sandbox, monkeypatch):
    tmp, fired = sandbox
    run(monkeypatch, free_gb=200, now=1000.0, fired=fired)
    assert json.loads((tmp / "state.json").read_text())["free_gb"] == 200
    assert (tmp / "status.json").exists()
    assert (tmp / "mon.log").read_text().strip()          # one jsonl line


def test_force_alert_fires_when_ok(sandbox, monkeypatch):
    _, fired = sandbox
    dm_free = run  # alias
    monkeypatch.setattr(dm.shutil, "disk_usage",
                        lambda p: Usage(int(926 * GB), int(700 * GB), int(200 * GB)))
    dm.check(now=1000.0, force_alert=True, notifier=lambda t, m: fired.append((t, m)))
    assert fired                                          # forced even though OK
