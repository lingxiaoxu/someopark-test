"""ops/kalshi_users (read-only owner tool): one Supabase id + email everywhere; live-trading review."""
import json
import os

import crypto_trading.ops.kalshi_users as ku

U1 = "aaaaaaa1-0000-4000-8000-000000000001"
U2 = "aaaaaaa2-0000-4000-8000-000000000002"
GONE = "aaaaaaa9-0000-4000-8000-000000000009"


def _dir(tmp_path, monkeypatch, allow=()):
    d = tmp_path / "kalshi"
    for sub in ("pending", "disabled", "ledgers"):
        (d / sub).mkdir(parents=True)
    reg, env = {}, []
    for uid, email in ((U1, "u1@example.com"), (U2, "u2@example.com")):
        pem = d / f"prod_{uid}.pem"
        pem.write_text("x")
        os.chmod(pem, 0o600)
        reg[uid] = {"user_id": uid, "email": email, "status": "active", "validated_at": "2026-10-01T05:00:00Z"}
        env += [f"KALSHI_PROD_API_KEY_ID_{uid}=k-{uid[:8]}", f"KALSHI_PROD_PRIVATE_KEY_PATH_{uid}={pem}"]
        (d / "ledgers" / f"{uid}.json").write_text(json.dumps({"user_id": uid}))
    (d / "users.json").write_text(json.dumps(reg))
    (d / "users.env").write_text("\n".join(env) + "\n")
    monkeypatch.setattr(ku, "USER_KEY_DIR", d)
    monkeypatch.setattr(ku, "trading_user_ids", lambda: set(allow))
    sb = {U1: {"id": U1, "email": "U1@example.com", "email_confirmed_at": "x"},
          U2: {"id": U2, "email": "u2@example.com", "email_confirmed_at": "x"}}
    monkeypatch.setattr(ku, "supabase_users", lambda: list(sb.values()))
    return d, sb


def test_consistent_layout_has_no_problems(tmp_path, monkeypatch):
    d, sb = _dir(tmp_path, monkeypatch, allow=[U1])
    (d / "trading" / "approved").mkdir(parents=True)                   # an allowlisted user is approved
    (d / "trading" / "approved" / f"{U1}.json").write_text(json.dumps({"user_id": U1, "ratio": 0.5}))
    _apply(d, U2)                                                      # a pending application is consistent
    assert ku.consistency_problems(sb) == []


def test_every_kind_of_drift_is_reported(tmp_path, monkeypatch):
    d, sb = _dir(tmp_path, monkeypatch, allow=[U1, GONE])
    sb[U2]["email"] = "changed@example.com"                              # email changed in Supabase
    reg = json.loads((d / "users.json").read_text())
    reg[U1]["user_id"] = U2                                               # record names another id
    (d / "users.json").write_text(json.dumps(reg))
    (d / f"prod_{GONE}.pem").write_text("x")                              # orphan key file
    (d / "ledgers" / f"{GONE}.json").write_text("{}")                     # disconnected user's book
    (d / "pending" / f"{GONE}.json").write_text("{}")                     # not a Supabase user
    p = "\n".join(ku.consistency_problems(sb))
    for needle in (f"users.json[{U1}]: 记录内 user_id", f"users.json[{U2}]: 邮箱",
                   f"prod_{GONE}.pem", f"ledgers/{GONE}.json", f"pending/{GONE}.json",
                   f"名单: {GONE}"):
        assert needle in p, needle


def _apply(d, uid, ratio=0.25, **extra):
    (d / "trading" / "requests").mkdir(parents=True, exist_ok=True)
    (d / "trading" / "requests" / f"{uid}.json").write_text(json.dumps(
        {"user_id": uid, "ratio": ratio, "ack_version": "2026-10-04", "requested_at": "2026-10-05T01:00:00Z", **extra}))


def _review_env(monkeypatch, i2=202.61, http=200, rp=None):
    monkeypatch.setattr(ku, "user_balance", lambda uid: {"http": http, "instance2": i2, "key_type": "Ed25519"})
    monkeypatch.setattr(ku, "risk_preview", lambda: rp if rp is not None else dict(
        since="2026-10-02 19:40", windows=193, spend_med=62.0, spend_p90=149.0, spend_max=223.0,
        worst_1h=-252.0, worst_24h=-387.0))


def test_pending_lists_only_applications_awaiting_the_owner(tmp_path, monkeypatch, capsys):
    d, _ = _dir(tmp_path, monkeypatch, allow=[U1])
    assert ku.cmd_pending() == 0
    assert "没有等待你审核" in capsys.readouterr().out          # validated keys alone are not applications
    _apply(d, U2)
    assert ku.cmd_pending() == 0
    out = capsys.readouterr().out
    assert "u2@example.com" in out and "25%" in out and "review u2@example.com" in out
    assert "u1@example.com" not in out and "KALSHI_PROD_TRADING_USER_IDS" not in out
    (d / "trading" / "approved").mkdir(parents=True)
    (d / "trading" / "approved" / f"{U2}.json").write_text(json.dumps({"user_id": U2, "ratio": 0.25}))
    ku.cmd_pending()
    assert "没有等待你审核" in capsys.readouterr().out          # approved: no longer pending


def test_tool_never_writes(tmp_path, monkeypatch, capsys):
    d, sb = _dir(tmp_path, monkeypatch)
    _apply(d, U2)
    _review_env(monkeypatch)
    before = {p: p.stat().st_mtime_ns for p in d.rglob("*")}
    ku.cmd_status(); ku.cmd_pending(); ku.cmd_check(); ku.cmd_lookup("u2@example.com")
    assert ku.cmd_review("u2@example.com") == 0
    assert {p: p.stat().st_mtime_ns for p in d.rglob("*")} == before


def test_review_passes_and_prints_the_three_manual_steps(tmp_path, monkeypatch, capsys):
    d, _ = _dir(tmp_path, monkeypatch, allow=[U1])
    _apply(d, U2, ratio=0.25)
    _review_env(monkeypatch)
    assert ku.cmd_review("u2@example.com") == 0
    out = capsys.readouterr().out
    assert out.count("✓") >= 6 and "✗" not in out
    assert f'{{"user_id": "{U2}", "ratio": 0.25}}' in out                       # step 1: approval record
    assert f"KALSHI_PROD_TRADING_USER_IDS={','.join(sorted([U1, U2]))}" in out   # step 2: list keeps U1
    assert "launchctl kickstart -k" in out and "3.0–4.4" in out                 # step 3: safe restart
    assert "$16" in out and "$56" in out and "$-63" in out                      # 25% of 62 / 223, worst hour


def test_review_refuses_until_every_check_passes(tmp_path, monkeypatch, capsys):
    d, sb = _dir(tmp_path, monkeypatch)
    _review_env(monkeypatch)
    assert ku.cmd_review("u2@example.com") == 1                                  # never applied
    assert "还没有申请" in capsys.readouterr().out
    _apply(d, U2, ratio=0.3)
    assert ku.cmd_review("u2@example.com") == 1                                  # ratio not offered
    assert "只能是 10% / 25% / 50% / 100%" in capsys.readouterr().out
    assert ku.cmd_review("u2@example.com", ratio=0.1) == 0                       # owner picks a lower one
    capsys.readouterr()
    _apply(d, U2, ratio=0.5)
    _review_env(monkeypatch, i2=12.0)
    assert ku.cmd_review("u2@example.com") == 1                                  # instance 2 underfunded
    assert "2 号交易实例现金 $12.00" in capsys.readouterr().out
    _review_env(monkeypatch, http=401)
    assert ku.cmd_review("u2@example.com") == 1
    assert "读余额失败" in capsys.readouterr().out
    _review_env(monkeypatch)
    (d / "disabled" / U2).write_text("{}")
    assert ku.cmd_review("u2@example.com") == 1                                  # auto-disabled key
    capsys.readouterr()
    assert ku.cmd_review("lxu912@gmail.com") == 1                                # the owner never mirrors


def test_allowlisted_user_without_approval_is_flagged(tmp_path, monkeypatch, capsys):
    d, sb = _dir(tmp_path, monkeypatch, allow=[U1])
    assert any("没有批准记录" in p for p in ku.consistency_problems(sb))
    (d / "trading" / "approved").mkdir(parents=True)
    (d / "trading" / "approved" / f"{U1}.json").write_text(json.dumps({"user_id": U2, "ratio": 1}))
    p = "\n".join(ku.consistency_problems(sb))
    assert "内部 user_id 与文件名不同" in p and "没有批准记录" in p
    (d / "trading" / "approved" / f"{U1}.json").write_text(json.dumps({"user_id": U1, "ratio": 1}))
    assert ku.consistency_problems(sb) == []


def test_in_progress_upload_is_info_not_inconsistency(tmp_path, monkeypatch, capsys):
    d, sb = _dir(tmp_path, monkeypatch)
    U3 = "aaaaaaa3-0000-4000-8000-000000000003"
    sb[U3] = {"id": U3, "email": "u3@example.com", "email_confirmed_at": "x"}
    (d / f"prod_{U3}.pem").write_text("x")                  # uploaded, not verified yet
    assert ku.consistency_problems(sb) == []
    assert ku.cmd_check() == 0
    assert "进行中" in capsys.readouterr().out
    ku.cmd_status()
    assert "u3@example.com" in capsys.readouterr().out


def test_shared_kalshi_account_is_flagged_and_not_recommended(tmp_path, monkeypatch, capsys):
    import hashlib
    d, sb = _dir(tmp_path, monkeypatch)
    reg = json.loads((d / "users.json").read_text())
    reg[U2]["account_key_hashes"] = [hashlib.sha256(f"k-{U1[:8]}".encode()).hexdigest()]
    (d / "users.json").write_text(json.dumps(reg))
    assert any("同一个 Kalshi 账户" in p for p in ku.consistency_problems(sb))
    _apply(d, U2)
    ku.cmd_pending()
    out = capsys.readouterr().out
    assert "不推荐" in out and "KALSHI_PROD_TRADING_USER_IDS" not in out and "review" not in out


def test_every_mirrored_user_needs_a_fresh_exact_fill_cache(tmp_path, monkeypatch, capsys):
    """2026-10-05: ledgers are exact only while ops/prod_fill_sync keeps each mirrored
    account's cache fresh; check flags a missing or stale one (auto-disabled users excepted)."""
    import time as _t
    from datetime import datetime, timezone
    d, sb = _dir(tmp_path, monkeypatch)
    (d / "journal").mkdir()
    (d / "journal" / "2026-10-05.jsonl").write_text(json.dumps(dict(account=U1, status="live_sent", ts="2026-10-05T01:00:00+00:00")) + "\n")
    p = "\n".join(ku.consistency_problems(sb))
    assert f"fills/{U1}.json: 被跟单过但没有精确成交缓存" in p and U2 not in p
    (d / "fills").mkdir()
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
    (d / "fills" / f"{U1}.json").write_text(json.dumps({"as_of": iso(_t.time() - 3600), "orders": {"o": {}}}))
    assert any("60 分钟没更新" in x for x in ku.consistency_problems(sb))
    (d / "fills" / f"{U1}.json").write_text(json.dumps({"as_of": iso(_t.time() - 60), "orders": {"o": {}}}))
    assert not any("fills/" in x for x in ku.consistency_problems(sb))
    ku.cmd_status()
    assert "精确成交同步: 1 分钟前,1 笔订单" in capsys.readouterr().out
    (d / "fills" / f"{U1}.json").unlink(); (d / "disabled" / U1).write_text("{}")
    assert not any("fills/" in x for x in ku.consistency_problems(sb))   # disabled: not polled, not flagged
