"""ops/prod_fill_sync (2026-10-05): read-only PROD fills -> exact per-order cost/fee caches."""
import json
import os

import pytest

import crypto_trading.ops.prod_fill_sync as s


def F(oid, tk, side, n, yes_px, fee, t="2026-10-05T01:00:00Z"):
    return dict(order_id=oid, ticker=tk, outcome_side=side, count_fp=f"{n:.2f}",
                yes_price_dollars=f"{yes_px:.4f}", no_price_dollars=f"{1 - yes_px:.4f}",
                fee_cost=f"{fee:.6f}", created_time=t)


def test_aggregate_uses_the_acquired_side_and_exact_fees():
    agg = s.aggregate([F("a", "KXETH15M-X", "no", 1, 0.045, 0.003), F("a", "KXETH15M-X", "no", 2, 0.044, 0.0059),
                       F("b", "KXBTC15M-Y", "yes", 30, 0.80, 0.336), F("c", "KXPRESNOMD-28-GN", "yes", 5, 0.2, 0.01)])
    assert set(agg) == {"a", "b"}                                   # only W7's crypto 15M series
    assert agg["a"]["side"] == "no" and agg["a"]["count"] == 3.0 and agg["a"]["fills"] == 2
    assert abs(agg["a"]["cost"] - (0.955 + 2 * 0.956)) < 1e-9       # the NO price, not the YES price
    assert abs(agg["a"]["fees"] - 0.0089) < 1e-9
    assert abs(agg["b"]["cost"] - 24.0) < 1e-9 and agg["b"]["side"] == "yes"


def test_aggregate_rejects_inconsistent_fills():
    with pytest.raises(ValueError):
        s.aggregate([F("a", "KXETH15M-X", "no", 1, 0.04, 0.001), F("a", "KXETH15M-X", "yes", 1, 0.04, 0.001)])
    with pytest.raises(ValueError):
        s.aggregate([F("a", "KXETH15M-X", "no", 1, 0.04, -0.001)])
    with pytest.raises(ValueError):
        s.aggregate([dict(F("a", "KXETH15M-X", "no", 1, 0.04, 0.001), outcome_side=None)])


def test_merge_never_shrinks_an_order():
    prev = {"a": {"count": 20.0, "cost": 18.0, "fees": 0.1}}
    out = s.merge(prev, {"a": {"count": 5.0, "cost": 4.5, "fees": 0.02}, "b": {"count": 3.0, "cost": 2.7, "fees": 0.01}})
    assert out["a"]["count"] == 20.0 and out["b"]["count"] == 3.0     # partial re-read never replaces
    assert s.merge(prev, {"a": {"count": 20.0, "cost": 17.99, "fees": 0.11}})["a"]["cost"] == 17.99


class _Resp:
    def __init__(self, body): self.status_code, self._b = 200, body
    def json(self): return self._b


class _Client:
    """Fake prod client: records every request and only answers GET /portfolio/fills."""
    def __init__(self, fills): self.fills, self.paths = fills, []

    def _authed(self, method, path):
        assert method == "GET" and path.startswith("/portfolio/fills?"), path
        self.paths.append(path)
        return _Resp({"fills": self.fills, "cursor": ""})


def test_sync_reads_only_fills_incrementally_into_a_private_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "PAGE_DELAY_S", 0)
    cache = tmp_path / "fills" / "u.json"
    c = _Client([F("a", "KXBTC15M-Y", "yes", 30, 0.80, 0.336)])
    r = s.sync_account("u", c, cache, since=1_000_000.0, private_dir=True, now=lambda: 2_000_000.0)
    assert r["orders_cached"] == 1 and "min_ts=1000000" in c.paths[0]
    assert os.stat(cache).st_mode & 0o777 == 0o600 and os.stat(cache.parent).st_mode & 0o777 == 0o700
    c2 = _Client([F("b", "KXSOL15M-Z", "no", 20, 0.10, 0.12)])
    s.sync_account("u", c2, cache, since=1_000_000.0, private_dir=True, now=lambda: 2_100_000.0)
    assert f"min_ts={2_000_000 - s.OVERLAP_S}" in c2.paths[0]       # re-reads the last hour only
    orders = json.loads(cache.read_text())["orders"]
    assert set(orders) == {"a", "b"}                               # earlier orders kept
    s.sync_account("u", _Client([]), cache, since=1_000_000.0, full=True, private_dir=True, now=lambda: 2_200_000.0)
    assert json.loads(cache.read_text())["orders"] == {}           # --full rebuilds from the start date


def test_user_accounts_come_from_the_journal_and_need_a_key_on_file(tmp_path):
    kd = tmp_path / "kalshi"; (kd / "journal").mkdir(parents=True)
    U1, U2 = "aaaaaaa1-0000-4000-8000-000000000001", "aaaaaaa2-0000-4000-8000-000000000002"
    (kd / "journal" / "2026-10-05.jsonl").write_text("\n".join(json.dumps(r) for r in [
        dict(account=U1, ts="2026-10-05T01:08:17+00:00", status="pending_send"),
        dict(account=U2, ts="2026-10-05T01:09:00+00:00", status="skipped_below_one_contract")]) + "\n")
    pem = kd / f"prod_{U1}.pem"; pem.write_text("x")
    (kd / "users.env").write_text(f"KALSHI_PROD_API_KEY_ID_{U1}=kid-1\nKALSHI_PROD_PRIVATE_KEY_PATH_{U1}={pem}\n")
    acc = s.user_accounts(kd)
    assert [(u, k.key_id) for u, k, _ in acc] == [(U1, "kid-1")]   # U2 never had an order sent
    assert acc[0][2] == s._ts("2026-10-05T01:08:17+00:00") - 3600


def test_a_user_disabled_for_a_rejected_key_is_not_polled(tmp_path):
    kd = tmp_path / "kalshi"; (kd / "journal").mkdir(parents=True); (kd / "disabled").mkdir()
    U1 = "aaaaaaa1-0000-4000-8000-000000000001"
    (kd / "journal" / "2026-10-05.jsonl").write_text(json.dumps(dict(account=U1, ts="2026-10-05T01:08:17+00:00", status="live_sent")) + "\n")
    pem = kd / f"prod_{U1}.pem"; pem.write_text("x")
    (kd / "users.env").write_text(f"KALSHI_PROD_API_KEY_ID_{U1}=kid-1\nKALSHI_PROD_PRIVATE_KEY_PATH_{U1}={pem}\n")
    assert [u for u, _, _ in s.user_accounts(kd)] == [U1]
    (kd / "disabled" / U1).write_text("{}")                         # 401/403 on an order
    assert s.user_accounts(kd) == []                                # skipped until re-verified
