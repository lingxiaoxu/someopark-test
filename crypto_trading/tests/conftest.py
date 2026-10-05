"""Session guard: no test may append to the PRODUCTION live_watch logs.

W7's live/demo order paths log from a daemon thread (common.mirror_async)
that can outlive the test's own monkeypatch of common.STATE_DIR, and some W8
tests call the verdict latch directly. Both appended fake rows to
trading_signals/live_watch/log_<day>.jsonl (found 2026-10-01: KXBTC15M-LIVE
live_disarmed rows and synthetic W8 verdict_latched rows). Tests that need
their own directory still monkeypatch it per test (the `sandbox` fixture).
"""
import importlib
import os
from urllib.parse import urlparse

import pytest

from crypto_trading.crypto_strategies.live_watch import common


@pytest.fixture(scope="session", autouse=True)
def _no_real_user_accounts_or_venue_network(tmp_path_factory):
    """2026-10-05 incident: with a real user allowlisted, a test's W7 submit spawned the
    per-user mirror daemon thread; it outlived the test's monkeypatch, read the REAL
    ~/.kalshi + allowlist and sent one real order request (fake ticker -> HTTP 404, no
    order) with that user's key. For the WHOLE session, never restored (a late daemon
    thread must stay isolated too): the user key dir is an empty tmp dir, the owner's
    allowlist is empty, and any HTTP request to a Kalshi or Supabase host raises.
    Tests of the mirror still set their own dir/allowlist per test (monkeypatch)."""
    import crypto_trading.crypto_common.config as cfg
    d = tmp_path_factory.mktemp("kalshi_users")
    cfg.USER_KEY_DIR = d
    cfg._CRYPTO_ENV.pop("KALSHI_PROD_TRADING_USER_IDS", None)
    cfg._ROOT_ENV.pop("KALSHI_PROD_TRADING_USER_IDS", None)
    os.environ.pop("KALSHI_PROD_TRADING_USER_IDS", None)
    for name in ("crypto_trading.ops.export_prediction_frontend", "crypto_trading.ops.kalshi_users"):
        importlib.import_module(name).USER_KEY_DIR = d

    def _blocked(url) -> bool:
        host = (urlparse(str(url)).hostname or "").lower()
        return "kalshi" in host or "supabase" in host

    import requests
    real_request = requests.sessions.Session.request

    def guarded_request(self, method, url, *a, **k):
        if _blocked(url):
            raise RuntimeError(f"network call to {urlparse(str(url)).hostname} blocked in tests")
        return real_request(self, method, url, *a, **k)
    requests.sessions.Session.request = guarded_request
    import urllib.request
    real_urlopen = urllib.request.urlopen

    def guarded_urlopen(req, *a, **k):
        if _blocked(getattr(req, "full_url", req)):
            raise RuntimeError("network call to a venue blocked in tests")
        return real_urlopen(req, *a, **k)
    urllib.request.urlopen = guarded_urlopen
    yield


@pytest.fixture(scope="session", autouse=True)
def _live_watch_writes_go_to_tmp(tmp_path_factory):
    common.STATE_DIR = tmp_path_factory.mktemp("live_watch")
    yield
    # Not restored on purpose: a late daemon thread must keep writing to tmp.


@pytest.fixture(autouse=True)
def _table_live_off_unless_asked(monkeypatch):
    """The W7 table-driven prod entry (2026-10-02) reads the production scenario
    tables; tests of the older sizing/gate rules must not depend on them. Tests
    of the table path switch it back on explicitly."""
    from crypto_trading.crypto_common.execution_events import EventExecutionRouter
    monkeypatch.setattr(EventExecutionRouter, "TABLE_LIVE", {"w7_noisefade": {"enabled": False}})
    monkeypatch.setattr(EventExecutionRouter, "PROD_BAND", {})      # the 0.79 floor is tested where it is switched on


@pytest.fixture(autouse=True)
def _table_slots_in_tmp(monkeypatch, tmp_path):
    """The once-per-window top1/top2 ledger must never be the production file."""
    from crypto_trading.crypto_strategies.w7_scenarios import live_plan
    monkeypatch.setattr(live_plan, "SLOTS_FILE", tmp_path / "w7_table_slots.json")
    live_plan.reset_ledger()                 # the in-process copy is authoritative: never leak across tests
    yield
    live_plan.reset_ledger()
