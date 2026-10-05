"""Per-user prod ledgers (2026-10-01): private journal -> owner-shaped rows."""
import json
import os

import crypto_trading.ops.export_prediction_frontend as ex

U1 = "aaaaaaa1-0000-4000-8000-000000000001"
U2 = "aaaaaaa2-0000-4000-8000-000000000002"


def _resp(fill, avg, fee=0.01):
    return json.dumps(dict(order_id=f"o{fill}{avg}", client_order_id="c", fill_count=str(fill),
                           average_fill_price=str(avg), average_fee_paid=str(fee),
                           remaining_count="0", ts_ms=1790700000000))


INTENT = dict(strategy="w7_noisefade", action="live_order_result", ticker="KXBTC15M-26OCT010615-15",
              side="yes", contracts=60, price_dollars=0.86, paper_price_dollars=0.85,
              limit_buffer_c=1, tif="immediate_or_cancel")


def _journal(tmp_path, rows):
    jd = tmp_path / "journal"
    jd.mkdir(exist_ok=True)
    (jd / "2026-10-01.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return jd


def _rows():
    owner = dict(INTENT, ts="2026-10-01T10:06:00+00:00", status="live_sent", status_code=201,
                 response=_resp(60, 0.85), body_sent={})
    u1 = [dict(INTENT, ts="2026-10-01T10:06:00+00:00", account=U1, status="pending_send", mirror_id="m1"),
          dict(INTENT, ts="2026-10-01T10:06:00+00:00", account=U1, status="live_sent", mirror_id="m1",
               status_code=201, response=_resp(60, 0.85), body_sent={})]
    u2 = [dict(INTENT, ts="2026-10-01T10:06:00+00:00", account=U2, status="pending_send", mirror_id="m2")]
    skip = dict(INTENT, ts="2026-10-01T10:21:00+00:00", account=U1, status="skipped_by_flow_gate",
                ticker="KXSOL15M-26OCT010630-30", contracts=40, price_dollars=0.9)
    owner_skip = {k: v for k, v in skip.items() if k != "account"}
    return owner, u1, u2, skip, owner_skip


STATE = {"trades": [dict(ticker="KXBTC15M-26OCT010615-15", side="yes", pnl_c=12.0, win=True,
                         closed="2026-10-01T10:15:30+00:00")]}


def test_identical_receipts_give_identical_ledgers(tmp_path):
    owner, u1, u2, skip, owner_skip = _rows()
    jrows = ex.read_user_journal(_journal(tmp_path, u1 + u2 + [skip]))
    mine = ex.user_prod_rows(jrows, U1)
    assert [r["status"] for r in mine] == ["live_sent", "skipped_by_flow_gate"]  # pending superseded
    strip = lambda rows: [{k: v for k, v in r.items() if k not in ("account", "mirror_id")} for r in rows]
    assert ex.fave_prod_ledger(strip(mine), STATE)[0] == ex.fave_prod_ledger([owner, owner_skip], STATE)[0]
    assert ex.fave_prod_ledger(mine, STATE)[2] == ex.fave_prod_ledger([owner, owner_skip], STATE)[2]
    # another user's rows never leak in; an interrupted request stays visible as unresolved
    other = ex.user_prod_rows(jrows, U2)
    assert len(other) == 1 and other[0]["status_code"] is None
    orders = ex.fave_prod_ledger(other, STATE)[0]
    assert orders[0]["status"] == "unknown" and orders[0]["verified"] is False


def test_reverify_never_drops_history_and_files_are_private(tmp_path, monkeypatch):
    _, u1, _, skip, _ = _rows()
    jd = _journal(tmp_path, u1 + [skip])
    kd = tmp_path / "kalshi"; kd.mkdir()
    # validated_at AFTER the orders (a re-verify) - attribution is by account, not time
    (kd / "users.json").write_text(json.dumps({
        U1: {"status": "active", "validated_at": "2026-10-02T00:00:00+00:00"},
        "../evil": {"status": "active"}}))
    monkeypatch.setattr(ex, "USER_KEY_DIR", kd)
    out = tmp_path / "ledgers"
    n = ex.write_user_ledgers(STATE, ex.timestamp("2026-10-02T01:00:00+00:00"), out_dir=out, journal_dir=jd)
    assert n == 1 and sorted(os.listdir(out)) == [f"{U1}.json"]
    assert os.stat(out / f"{U1}.json").st_mode & 0o777 == 0o600
    assert os.stat(out).st_mode & 0o777 == 0o700
    led = json.loads((out / f"{U1}.json").read_text())
    assert len(led["settlements"]) == 1                          # history survived re-verify
    assert abs(led["settlements"][0]["net_usd"] - (60 * (1 - 0.85) - 0.6)) < 1e-6
    assert set(led["execution"]) == {"all", "recent"}             # the user's OWN execution stats
    assert led["execution"]["all"]["filled_contracts"] == 60


def test_default_ledger_location_is_outside_the_repo():
    from pathlib import Path
    assert not str(ex.USER_KEY_DIR / "ledgers").startswith(str(Path(ex.__file__).resolve().parents[2]))


def test_one_id_everywhere_and_stale_books_removed(tmp_path, monkeypatch):
    """The registry key, the record's user_id and the ledger file name are the
    same Supabase id; a record naming another id is ignored; a disconnected
    user's book is deleted on the next export."""
    _, u1, _, _, _ = _rows()
    jd = _journal(tmp_path, u1)
    kd = tmp_path / "kalshi"; kd.mkdir()
    (kd / "users.json").write_text(json.dumps({
        U1: {"user_id": U1, "email": "u1@example.com", "status": "active"},
        U2: {"user_id": U1, "email": "u2@example.com", "status": "active"}}))   # id mismatch
    monkeypatch.setattr(ex, "USER_KEY_DIR", kd)
    out = tmp_path / "ledgers"; out.mkdir()
    gone = "aaaaaaa3-0000-4000-8000-000000000003"
    (out / f"{gone}.json").write_text("{}")                  # user who disconnected
    (out / "notes.json").write_text("{}")                    # not an id: left alone
    n = ex.write_user_ledgers(STATE, ex.timestamp("2026-10-02T01:00:00+00:00"), out_dir=out, journal_dir=jd)
    assert n == 1
    assert sorted(os.listdir(out)) == sorted([f"{U1}.json", "notes.json"])
    led = json.loads((out / f"{U1}.json").read_text())
    assert led["user_id"] == U1 and led["email"] == "u1@example.com"


def test_user_ledger_uses_that_users_own_exact_fill_cache(tmp_path, monkeypatch):
    """2026-10-05: each user's ledger reads ~/.kalshi/fills/<uid>.json (the read-only prod
    fill sync) for exact cost/fees; another user's cache can never apply."""
    _, u1, _, _, _ = _rows()
    jd = _journal(tmp_path, u1)
    kd = tmp_path / "kalshi"; kd.mkdir()
    (kd / "users.json").write_text(json.dumps({U1: {"status": "active"}}))
    (kd / "fills").mkdir()
    oid = json.loads(u1[1]["response"])["order_id"]
    (kd / "fills" / f"{U2}.json").write_text(json.dumps({"orders": {oid: {"count": 60, "cost": 1.0, "fees": 9.0}}}))
    monkeypatch.setattr(ex, "USER_KEY_DIR", kd)
    out = tmp_path / "ledgers"
    ex.write_user_ledgers(STATE, ex.timestamp("2026-10-02T01:00:00+00:00"), out_dir=out, journal_dir=jd)
    led = json.loads((out / f"{U1}.json").read_text())
    assert abs(led["settlements"][0]["net_usd"] - (60 * (1 - 0.85) - 0.6)) < 1e-6      # U2's cache ignored
    (kd / "fills" / f"{U1}.json").write_text(json.dumps({"orders": {oid: {"count": 60, "cost": 50.97, "fees": 0.6312}}}))
    ex.write_user_ledgers(STATE, ex.timestamp("2026-10-02T01:00:00+00:00"), out_dir=out, journal_dir=jd)
    led = json.loads((out / f"{U1}.json").read_text())
    assert abs(led["settlements"][0]["net_usd"] - (60 - 50.97 - 0.6312)) < 1e-6
    assert abs(led["prod"]["net_pnl_usd"] - (60 - 50.97 - 0.6312)) < 1e-6
