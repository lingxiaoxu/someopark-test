"""Owner-side, READ-ONLY overview of per-user Kalshi PROD accounts.

    ops/kalshi_users.sh status           every user who uploaded keys
    ops/kalshi_users.sh pending          verified users awaiting the owner's decision
    ops/kalshi_users.sh lookup <email>   Supabase id + readiness for one user
    ops/kalshi_users.sh check            one id + one email in every place?
    ops/kalshi_users.sh review <email>   every pre-enable check + risk at the ratio + the manual steps

Never writes anything. Live-trading flow (2026-10-04): the user applies on the
web panel (risk acknowledgement + requested ratio, ~/.kalshi/trading/requests);
the owner reviews here, then BY HAND records the approval with the ratio
(~/.kalshi/trading/approved/<uid>.json), adds the id to
KALSHI_PROD_TRADING_USER_IDS in crypto_trading/.env and restarts the runner
3.0-4.4 min before a 15-minute close. The user (panel) or the owner (by hand,
~/.kalshi/trading/stopped/<uid>.json) can stop at any time, no restart.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

from crypto_trading.crypto_common.config import (
    USER_KEY_DIR, _parse_env_file, trading_user_ids)

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

OWNER_EMAIL = "lxu912@gmail.com"
REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_ENV = REPO_ROOT / "someo-park-investment-management" / ".env"


def supabase_users() -> list[dict]:
    env = _parse_env_file(WEB_ENV)
    url, key = env.get("SUPABASE_URL", "").strip('"\''), env.get("SUPABASE_SECRET_KEY", "").strip('"\'')
    if not url or not key:
        raise SystemExit(f"Supabase 未配置({WEB_ENV} 缺 SUPABASE_URL / SUPABASE_SECRET_KEY)")
    out: list[dict] = []
    for page in range(1, 50):
        req = urllib.request.Request(f"{url}/auth/v1/admin/users?page={page}&per_page=200",
                                     headers={"apikey": key, "Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            batch = json.load(r).get("users") or []
        out += batch
        if len(batch) < 200:
            break
    return out


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def runner_mirroring() -> tuple[set[str], bool]:
    """User ids the RUNNING W7 process published, and whether it is alive."""
    j = _read_json(USER_KEY_DIR / "trading_active.json", {})
    try:
        os.kill(int(j.get("pid")), 0)
        alive = True
    except (TypeError, ValueError, OSError):
        alive = False
    return (set(j.get("user_ids") or []) if alive else set()), alive


def readiness(uid: str, rec: dict | None) -> list[str]:
    """Empty list = this user's key passed the live check and is usable."""
    if not rec or rec.get("status") != "active":
        return ["没有通过检测(未在面板上完成「检测并连接」)"]
    problems = []
    envf = _parse_env_file(USER_KEY_DIR / "users.env")
    if not envf.get(f"KALSHI_PROD_API_KEY_ID_{uid}"):
        problems.append("users.env 缺 API Key 行")
    pem = Path(envf.get(f"KALSHI_PROD_PRIVATE_KEY_PATH_{uid}", ""))
    if not pem.is_file():
        problems.append("私钥文件不存在")
    elif pem.stat().st_mode & 0o777 != 0o600:
        problems.append(f"私钥文件权限 {oct(pem.stat().st_mode & 0o777)} 不是 600")
    marker = USER_KEY_DIR / "disabled" / uid
    if marker.exists():
        reason = _read_json(marker, {}).get("reason", "")
        problems.append(f"已被自动停用(Kalshi 拒绝认证:{reason[:60]}),需用户重新检测")
    return problems


def cmd_status() -> int:
    registry = _read_json(USER_KEY_DIR / "users.json", {})
    allow = trading_user_ids()
    live, alive = runner_mirroring()
    print(f"凭证目录 {USER_KEY_DIR}  |  W7 runner 发布名单: "
          f"{'在线' if alive else '无(runner 未发布或用旧代码)'}  |  .env 名单 {len(allow)} 人")
    print(f"主账户精确成交同步: {fill_cache_line(OWNER_FILLS)}")
    sb = {u["id"]: u for u in supabase_users()}
    for uid, what in in_progress(registry, sb):
        print(f"\n{sb[uid].get('email')}  ({uid})\n  {what}")
    if not registry:
        print("还没有任何用户通过检测。")
        return 0
    for uid, rec in sorted(registry.items(), key=lambda kv: str(kv[1].get("validated_at"))):
        u = sb.get(uid) or {}
        problems = readiness(uid, rec)
        print(f"\n{rec.get('email') or '?'}  ({uid})")
        sb_line = "存在" if u else "找不到该 id"
        sb_line += ",邮箱已验证" if u.get("email_confirmed_at") else ",邮箱未验证"
        if u and u.get("email") != rec.get("email"):
            sb_line += f",当前邮箱 {u.get('email')}"
        print(f"  Supabase: {sb_line}")
        print(f"  检测: {'通过 ' + str(rec.get('validated_at', ''))[:19] if not problems else ';'.join(problems)}"
              f"  |  Key {rec.get('key_id_masked', '?')}")
        print(f"  实盘跟单: {trading_line(uid, allow, live)}")
        if uid in mirrored_user_ids():
            print(f"  精确成交同步: {fill_cache_line(USER_KEY_DIR / 'fills' / f'{uid}.json')}")
    return 0


def cmd_lookup(email: str) -> int:
    email = email.strip().lower()
    hits = [u for u in supabase_users() if (u.get("email") or "").lower() == email]
    if not hits:
        print(f"Supabase 里没有 {email}")
        return 0
    u = hits[0]
    uid = u["id"]
    print(f"{email} → Supabase id {uid}")
    print(f"  邮箱已验证: {'是' if u.get('email_confirmed_at') else '否'}  |  最近登录 "
          f"{str(u.get('last_sign_in_at') or '')[:19]}  |  登录方式 {u.get('app_metadata', {}).get('provider', '?')}")
    if email == OWNER_EMAIL:
        print("  这是主账户:用平台自己的 KALSHI_PROD_* 下单,不需要也不能加入用户名单。")
        return 0
    rec = _read_json(USER_KEY_DIR / "users.json", {}).get(uid)
    problems = readiness(uid, rec)
    allow = trading_user_ids()
    live, alive = runner_mirroring()
    if problems:
        print("  还不能开通:" + ";".join(problems))
        return 0
    print(f"  检测通过 {str(rec.get('validated_at', ''))[:19]}  |  Key {rec.get('key_id_masked', '?')}")
    print(f"  实盘跟单: {trading_line(uid, allow, live)}")
    t = read_trading(uid)
    if t["request"] or t["approved"]:
        print(f"  下一步: crypto_trading/ops/kalshi_users.sh review {email}")
    else:
        print("  用户还没在网页面板上申请实盘跟单;申请后用 review 审核。")
    return 0


def cmd_pending() -> int:
    """Applications awaiting the owner: applied on the panel and not yet
    approved (or re-applied after a stop)."""
    registry = _read_json(USER_KEY_DIR / "users.json", {})
    sb = {u["id"]: u for u in supabase_users()} if registry else {}
    shared = {u for pair in shared_kalshi_accounts(registry, _parse_env_file(USER_KEY_DIR / "users.env"))
              for u in pair}
    waiting = []
    for uid, rec in sorted(registry.items()):
        t = read_trading(uid)
        if not t["request"] or (t["approved"] and not t["stopped"]):
            continue
        if uid in shared:
            print(f"不推荐: {rec.get('email')} ({uid}) 与另一平台账户共用同一个 Kalshi 账户")
            continue
        problems = readiness(uid, rec)
        if not (sb.get(uid) or {}).get("email_confirmed_at"):
            problems = problems + ["Supabase 邮箱未验证"]
        waiting.append((uid, rec, t["request"], problems))
    if not waiting:
        print("没有等待你审核的实盘跟单申请。")
        return 0
    print("等待你审核的实盘跟单申请:")
    for uid, rec, req, problems in waiting:
        print(f"  {rec.get('email')}  ({uid})  比例 {_pct(req.get('ratio'))}  申请于 {str(req.get('requested_at', ''))[:19]}"
              + (f"  [暂不能开通:{';'.join(problems)}]" if problems else ""))
        print(f"    审核: crypto_trading/ops/kalshi_users.sh review {rec.get('email')}")
    return 0


def _account_hashes(uid: str, rec: dict, envf: dict) -> set[str]:
    import hashlib
    hs = set(rec.get("account_key_hashes") or [])
    kid = envf.get(f"KALSHI_PROD_API_KEY_ID_{uid}")
    if kid:
        hs.add(hashlib.sha256(kid.encode()).hexdigest())
    return hs


def shared_kalshi_accounts(registry: dict, envf: dict) -> list[tuple[str, str]]:
    """Pairs of user ids that reach the same Kalshi account."""
    ids = sorted(u for u in registry if UUID.fullmatch(u))
    hs = {u: _account_hashes(u, registry[u], envf) for u in ids}
    return [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:] if hs[a] & hs[b]]


def in_progress(registry: dict, sb: dict[str, dict]) -> list[tuple[str, str]]:
    """Real Supabase users who uploaded something but have not passed the check."""
    found: dict[str, str] = {}
    for pem in USER_KEY_DIR.glob("prod_*.pem"):
        uid = pem.stem[len("prod_"):]
        if uid in sb and uid not in registry:
            found[uid] = "已上传私钥,尚未通过检测"
    for f in (USER_KEY_DIR / "pending").glob("*"):
        uid = f.name.split(".")[0]
        if uid in sb and uid not in registry:
            found.setdefault(uid, "已保存 API Key,尚未通过检测")
    return sorted(found.items())


def consistency_problems(sb: dict[str, dict]) -> list[str]:
    """Every per-user artifact must name a real Supabase account by the same
    id, and the registry's email must equal that account's email."""
    out: list[str] = []
    registry = _read_json(USER_KEY_DIR / "users.json", {})
    if not isinstance(registry, dict):
        return ["users.json 不是对象"]
    for uid, rec in registry.items():
        if not UUID.fullmatch(uid):
            out.append(f"users.json: 非法 id {uid!r}")
            continue
        if rec.get("user_id", uid) != uid:
            out.append(f"users.json[{uid}]: 记录内 user_id={rec.get('user_id')} 与键不同")
        u = sb.get(uid)
        if not u:
            out.append(f"users.json[{uid}]: Supabase 里没有这个 id(账户已删除?)")
        elif (u.get("email") or "").lower() != str(rec.get("email") or "").lower():
            out.append(f"users.json[{uid}]: 邮箱 {rec.get('email')} ≠ Supabase {u.get('email')}")
    envf = _parse_env_file(USER_KEY_DIR / "users.env")
    for k, v in envf.items():
        m = re.fullmatch(r"KALSHI_PROD_(API_KEY_ID|PRIVATE_KEY_PATH)_(.+)", k)
        if not m or not UUID.fullmatch(m.group(2)):
            out.append(f"users.env: 非法行 {k}")
            continue
        uid = m.group(2)
        if uid not in registry:
            out.append(f"users.env: {uid} 不在 users.json")
        if m.group(1) == "PRIVATE_KEY_PATH" and Path(v) != USER_KEY_DIR / f"prod_{uid}.pem":
            out.append(f"users.env: {uid} 的私钥路径不是 prod_{uid}.pem")
    for pem in USER_KEY_DIR.glob("prod_*.pem"):
        uid = pem.stem[len("prod_"):]
        if uid not in registry and (not UUID.fullmatch(uid) or uid not in sb):
            out.append(f"{pem.name}: 不是现存 Supabase 用户")
    for sub in ("pending", "disabled"):
        for f in (USER_KEY_DIR / sub).glob("*"):
            uid = f.name.split(".")[0]
            if not UUID.fullmatch(uid) or uid not in sb:
                out.append(f"{sub}/{f.name}: 不是现存 Supabase 用户")
    for f in (USER_KEY_DIR / "ledgers").glob("*.json"):
        led = _read_json(f, {})
        if f.stem not in registry:
            out.append(f"ledgers/{f.name}: 用户已断开,账本将在下次导出时删除")
        elif led.get("user_id") != f.stem:
            out.append(f"ledgers/{f.name}: 内部 user_id={led.get('user_id')} 与文件名不同")
    for uid in trading_user_ids():
        if uid not in registry:
            out.append(f"crypto_trading/.env 名单: {uid} 不是已检测用户(不会下单,建议删掉)")
        elif uid not in sb:
            out.append(f"crypto_trading/.env 名单: {uid} 在 Supabase 已不存在")
    for a_uid, b_uid in shared_kalshi_accounts(registry, envf):
        out.append(f"同一个 Kalshi 账户绑定了两个平台账户: {a_uid} 与 {b_uid}(W7 不会给其中任何一个下单)")
    for kind in ("requests", "approved", "stopped"):
        for f in (USER_KEY_DIR / "trading" / kind).glob("*.json"):
            rec = _read_json(f, {})
            if not UUID.fullmatch(f.stem) or f.stem not in sb:
                out.append(f"trading/{kind}/{f.name}: 不是现存 Supabase 用户")
            elif not isinstance(rec, dict) or rec.get("user_id") != f.stem:
                out.append(f"trading/{kind}/{f.name}: 内部 user_id 与文件名不同")
    for uid in trading_user_ids():
        if uid in registry and read_trading(uid)["approved"] is None:
            out.append(f"crypto_trading/.env 名单: {uid} 没有批准记录 trading/approved/{uid}.json(不会给他下单)")
    for uid in sorted(mirrored_user_ids()):
        if (USER_KEY_DIR / "disabled" / uid).exists():
            continue
        age = fill_cache_age(USER_KEY_DIR / "fills" / f"{uid}.json")
        if age is None:
            out.append(f"fills/{uid}.json: 被跟单过但没有精确成交缓存(网页账本只能用回执估计;查 prodfillsync 任务)")
        elif age > FILL_CACHE_STALE_S:
            out.append(f"fills/{uid}.json: 精确成交缓存 {age/60:.0f} 分钟没更新(查 logs/prod_fill_sync.log)")
    live, alive = runner_mirroring()
    for uid in live - trading_user_ids():
        out.append(f"trading_active.json: {uid} 在跟单但已不在 .env 名单(重启 livewatch 后生效)")
    return out


def cmd_check() -> int:
    sb = {u["id"]: u for u in supabase_users()}
    problems = consistency_problems(sb)
    if not USER_KEY_DIR.exists():
        print(f"{USER_KEY_DIR} 不存在:还没有用户上传过密钥。")
    for uid, what in in_progress(_read_json(USER_KEY_DIR / "users.json", {}), sb):
        print(f"进行中(不算不一致): {sb[uid].get('email')} ({uid}) {what}")
    if problems:
        print("发现不一致:")
        for p in problems:
            print("  - " + p)
        return 1
    print("一致:每个用户在 users.json / users.env / 私钥文件名 / 待检测 / 停用标记 / 账本 / 跟单名单里"
          "都是同一个 Supabase id,邮箱与 Supabase 一致。")
    return 0


# ── exact prod fills (2026-10-05): ops/prod_fill_sync, LaunchAgent prodfillsync every 5 min ──
OWNER_FILLS = REPO_ROOT / "crypto_trading" / "trading_signals" / "frontend_prediction" / "prod_fills_owner.json"
FILL_CACHE_STALE_S = 15 * 60          # three missed 5-minute runs


def mirrored_user_ids() -> set[str]:
    """Users the W7 mirror ever sent an order for (their ledgers need exact fills)."""
    out = set()
    for f in (USER_KEY_DIR / "journal").glob("*.jsonl"):
        for line in f.read_text(errors="ignore").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("account") and r.get("status") in ("pending_send", "live_sent"):
                out.add(r["account"])
    return out


def fill_cache_age(path: Path) -> float | None:
    j = _read_json(path, None)
    try:
        from datetime import datetime
        return time.time() - datetime.fromisoformat(str(j["as_of"])).timestamp()
    except (TypeError, KeyError, ValueError):
        return None


def fill_cache_line(path: Path) -> str:
    age = fill_cache_age(path)
    if age is None:
        return "无缓存"
    n = len((_read_json(path, {}) or {}).get("orders") or {})
    return f"{age/60:.0f} 分钟前,{n} 笔订单" + ("  ⚠ 过期" if age > FILL_CACHE_STALE_S else "")


# ── live-trading application review (2026-10-04) — READ-ONLY ────────────────
LIVEWATCH = "com.someopark.crypto.livewatch"
RATIOS = (0.10, 0.25, 0.50, 1.00)    # == config.MIRROR_RATIOS == the web panel's MIRROR_RATIOS


def _tfile(kind: str, uid: str) -> Path:
    return USER_KEY_DIR / "trading" / kind / f"{uid}.json"


def read_trading(uid: str) -> dict:
    """The user's application, the owner's approval and any stop marker
    (None when absent, unreadable, or recorded under another id)."""
    out = {}
    for kind, name in (("requests", "request"), ("approved", "approved"), ("stopped", "stopped")):
        rec = _read_json(_tfile(kind, uid), None)
        out[name] = rec if isinstance(rec, dict) and rec.get("user_id") == uid else None
    return out


def _pct(r) -> str:
    try:
        return f"{float(r) * 100:.0f}%"
    except (TypeError, ValueError):
        return "?"


def trading_line(uid: str, allow: set[str], live: set[str]) -> str:
    t = read_trading(uid)
    req, appr, stop = t["request"], t["approved"], t["stopped"]
    parts = [f"申请 {_pct(req.get('ratio'))} 于 {str(req.get('requested_at', ''))[:16]}" if req else "未申请"]
    if appr:
        parts.append(f"批准 {_pct(appr.get('ratio'))}")
    if stop:
        parts.append(f"已停止({'用户' if stop.get('by') == 'user' else '主账户'} {str(stop.get('stopped_at', ''))[:16]})")
    reg = _read_json(USER_KEY_DIR / "users.json", {})
    on = (uid in live and appr is not None and stop is None
          and not readiness(uid, reg.get(uid) if isinstance(reg, dict) else None))
    parts.append(f".env 名单{'内' if uid in allow else '外'}")
    parts.append(f"正在跟单: {'是' if on else '否'}")
    return " / ".join(parts)


def user_balance(uid: str) -> dict:
    """One signed read-only GET /portfolio/balance with the user's own key, through
    the SAME Python client the live mirror orders with (proves RSA/Ed25519 signing)."""
    from crypto_trading.crypto_common.config import KalshiKey
    from crypto_trading.crypto_common.kalshi.rest_event import KalshiEventOrderClient
    envf = _parse_env_file(USER_KEY_DIR / "users.env")
    key = KalshiKey(envf.get(f"KALSHI_PROD_API_KEY_ID_{uid}", ""),
                    envf.get(f"KALSHI_PROD_PRIVATE_KEY_PATH_{uid}", ""), borrowed=False)
    c = KalshiEventOrderClient(env="prod", key=key)
    r = c._authed("GET", "/portfolio/balance")
    j = r.json() if r.status_code == 200 else {}
    i2 = next((float(x["balance"]) for x in j.get("balance_breakdown", []) if x.get("exchange_index") == 2), 0.0)
    return {"http": r.status_code, "instance2": i2, "key_type": type(c._pk).__name__.replace("PrivateKey", "")}


def risk_preview() -> dict | None:
    """The owner's W7 prod at 100% since the table went live: capital per
    15-minute window and the worst 1-hour / 24-hour stretches."""
    import pandas as pd
    from crypto_trading.crypto_strategies.w7_scenarios import live_vs_t8
    df = live_vs_t8.build()
    s = df[df.kind == "live"]
    if s.empty:
        return None
    spend = (s.n * (s.cost + s.fee)).groupby(s.close_ts).sum()
    pw = s[s.won.notna()].groupby("close_ts").pnl.sum()
    pw.index = pw.index.astype(int)
    pw = pd.Series(0.0, index=range(int(pw.index.min()), int(pw.index.max()) + 1, 900)).add(pw, fill_value=0.0)
    return dict(since=str(live_vs_t8.GO_LIVE)[:16], windows=int(len(spend)), spend_med=float(spend.median()),
                spend_p90=float(spend.quantile(0.9)), spend_max=float(spend.max()),
                worst_1h=float(pw.rolling(4, min_periods=1).sum().min()),
                worst_24h=float(pw.rolling(96, min_periods=1).sum().min()))


def cmd_review(email: str, ratio: float | None = None) -> int:
    """Everything the owner should know before enabling one user, then the manual
    steps. Read-only: one signed GET with the user's key, nothing written."""
    from crypto_trading.crypto_common.execution_events import EventExecutionRouter
    min_i2 = float(EventExecutionRouter.MIN_SHARD2_USD)
    email = email.strip().lower()
    if email == OWNER_EMAIL:
        print("这是主账户:用平台自己的 KALSHI_PROD_* 下单,不需要也不能开通跟单。")
        return 1
    hits = [u for u in supabase_users() if (u.get("email") or "").lower() == email]
    if not hits:
        print(f"Supabase 里没有 {email}")
        return 1
    u = hits[0]
    uid = u["id"]
    registry = _read_json(USER_KEY_DIR / "users.json", {})
    registry = registry if isinstance(registry, dict) else {}
    rec = registry.get(uid)
    t = read_trading(uid)
    req = t["request"]
    checks: list[tuple[bool, str]] = [(bool(u.get("email_confirmed_at")), f"Supabase 账户 {uid},邮箱已验证")]
    problems = readiness(uid, rec)
    checks.append((not problems, "Kalshi 凭证已通过面板检测,私钥文件权限 600,未被停用"
                   + (f":{';'.join(problems)}" if problems else "")))
    if rec and str(rec.get("email") or "").lower() != email:
        checks.append((False, f"登记邮箱 {rec.get('email')} 与 Supabase 不一致(先跑 check)"))
    shared = any(uid in pair for pair in shared_kalshi_accounts(registry, _parse_env_file(USER_KEY_DIR / "users.env")))
    checks.append((not shared, "没有与其他平台账户共用同一个 Kalshi 账户"))
    checks.append((req is not None and bool(req.get("ack_version")),
                   f"用户已在网页面板申请并勾选风险确认,申请于 {str(req.get('requested_at', ''))[:19]}" if req
                   else "用户已在网页面板申请并勾选风险确认:还没有申请"))
    r = ratio if ratio is not None else (req or {}).get("ratio")
    try:
        r = float(r)
    except (TypeError, ValueError):
        r = None
    ok_ratio = r is not None and any(abs(r - x) < 1e-9 for x in RATIOS)
    checks.append((ok_ratio, f"跟单比例 {_pct(r) if r is not None else '未定'}"
                   + ("" if ratio is None else f",你指定;用户申请的是 {_pct((req or {}).get('ratio'))}")
                   + ("" if ok_ratio else ":只能是 10% / 25% / 50% / 100%")))
    i2 = None
    if not problems:
        try:
            b = user_balance(uid)
        except Exception as e:                                      # noqa: BLE001
            b = {"error": f"{type(e).__name__}: {str(e)[:100]}"}
        if b.get("http") == 200:
            i2 = float(b.get("instance2") or 0.0)
            checks.append((True, f"用该用户的 key 经实盘镜像同一客户端读余额成功,{b.get('key_type')} 签名"))
            checks.append((i2 >= min_i2, f"2 号交易实例现金 ${i2:,.2f};加密 15 分钟合约只用这里的钱,至少 ${min_i2:.0f}"))
        else:
            checks.append((False, f"用该用户的 key 读余额失败:{b.get('error') or 'HTTP ' + str(b.get('http'))}"))
    print(f"{email}  ({uid})")
    for ok, text in checks:
        print(f"  {'✓' if ok else '✗'} {text}")
    if ok_ratio:
        try:
            rp, err = risk_preview(), "暂无实盘成交"
        except Exception as e:                                      # noqa: BLE001
            rp, err = None, f"{type(e).__name__}: {str(e)[:100]}"
        if rp:
            print(f"  风险预览:按 {_pct(r)} 跟单,用主账户 {rp['since']} UTC 以来 {rp['windows']} 个有成交的窗口估算")
            print(f"    每个 15 分钟窗口占用资金  中位 ${rp['spend_med'] * r:,.0f}  90% 分位 ${rp['spend_p90'] * r:,.0f}  最大 ${rp['spend_max'] * r:,.0f}")
            print(f"    最差连续 1 小时 ${rp['worst_1h'] * r:+,.0f}  最差连续 24 小时 ${rp['worst_24h'] * r:+,.0f}")
            if i2 is not None and (rp["spend_max"] * r > i2 or -rp["worst_1h"] * r > 0.5 * i2):
                print(f"    ⚠ 相对 2 号实例现金 ${i2:,.0f} 偏大:重仓窗口会有单因余额不足被拒,一个坏的小时可能亏掉一半以上。考虑更低比例(--ratio)。")
        else:
            print(f"  风险预览不可用:{err}")
    if not all(ok for ok, _ in checks):
        print("现在不能开通:先解决上面打 ✗ 的项。")
        return 1
    allow = trading_user_ids()
    print("全部检查通过。开通需要你手动做三步(本工具不写任何文件):")
    print(f"  1. 写批准记录 ~/.kalshi/trading/approved/{uid}.json(目录 700,文件 600),内容:")
    print(f'       {{"user_id": "{uid}", "ratio": {r:g}}}')
    if t["stopped"]:
        print(f"     该用户曾停止过:同时删除 ~/.kalshi/trading/stopped/{uid}.json")
    print("  2. 在 crypto_trading/.env 设:")
    print(f"       KALSHI_PROD_TRADING_USER_IDS={','.join(sorted(allow | {uid}))}")
    print("  3. 在某个 15 分钟收盘前 3.0–4.4 分钟之间(如 xx:10:40–xx:11:50)重启 livewatch:")
    print(f"       launchctl kickstart -k gui/$(id -u)/{LIVEWATCH}")
    print("  然后运行 kalshi_users.sh status,看到「正在跟单: 是」即生效。")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="per-user Kalshi PROD accounts (read-only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("pending")
    sub.add_parser("check")
    lk = sub.add_parser("lookup")
    lk.add_argument("email")
    rv = sub.add_parser("review", help="pre-enable checks + risk preview + the manual steps (read-only)")
    rv.add_argument("email")
    rv.add_argument("--ratio", type=float, help="0.1 / 0.25 / 0.5 / 1 (default: the user's requested ratio)")
    a = ap.parse_args(argv)
    if a.cmd == "lookup":
        return cmd_lookup(a.email)
    if a.cmd == "review":
        return cmd_review(a.email, a.ratio)
    return {"status": cmd_status, "pending": cmd_pending, "check": cmd_check}[a.cmd]()


if __name__ == "__main__":
    sys.exit(main())
