"""crypto_trading configuration (Plan 00 §2, Plan 08 §2).

Self-contained env loading — no python-dotenv. Precedence per variable:
  1. real process environment (highest)
  2. crypto_trading/.env
  3. repo-root .env                      (POLYGON_API_KEY / FRED_API_KEY live here)
  4. prediction_market/.env FALLBACK     (borrowed demo key — READ-ONLY recording only)

The prediction_market fallback exists so demo WS recording works before the
dedicated crypto keys are created (Plan 08 §2 item 2/3). Any order path must
refuse to run on a borrowed key — see ``kalshi_key(borrowed_ok=False)``.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

CRYPTO_ROOT = Path(__file__).resolve().parents[1]          # crypto_trading/
REPO_ROOT = CRYPTO_ROOT.parent                             # someopark-test/
PRICE_DATA = CRYPTO_ROOT / "price_data"
# Outputs (reports, WF artifacts, backtests, inventories, bracket/halt state) go
# here. Redirectable via CRYPTO_SIGNALS_DIR so a test/dry run can write to a temp
# dir instead of polluting the real production output tree. Reads (PRICE_DATA)
# are NOT redirectable — tests read the real recorded data, write elsewhere.
SIGNALS_DIR = Path(os.environ.get("CRYPTO_SIGNALS_DIR")
                   or (CRYPTO_ROOT / "trading_signals"))


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser (comments/blank lines skipped, no quoting games)."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


_CRYPTO_ENV = _parse_env_file(CRYPTO_ROOT / ".env")
_ROOT_ENV = _parse_env_file(REPO_ROOT / ".env")
_PM_ENV = _parse_env_file(REPO_ROOT / "prediction_market" / ".env")


def env(key: str, default: str = "") -> str:
    """Layered lookup WITHOUT the prediction_market fallback (that is key-only)."""
    for source in (os.environ, _CRYPTO_ENV, _ROOT_ENV):
        if key in source and str(source[key]).strip() != "":
            return str(source[key]).strip()
    return default


@dataclass(frozen=True)
class KalshiKey:
    key_id: str
    private_key_path: str
    borrowed: bool          # True ⇒ prediction_market's key — read-only use ONLY

    def expanded_path(self) -> str:
        return os.path.expanduser(self.private_key_path)


# ── Per-user PROD accounts (2026-10-01) ────────────────────────────────────
# Written by the web server's /api/kalshi-keys flow ONLY after a signed live
# check passes; stored OUTSIDE the public repo. Owner orders always go first
# (execution_events.submit); these accounts mirror the identical intent after.
USER_KEY_DIR = Path(os.environ.get("KALSHI_USER_KEY_DIR")
                    or (Path.home() / ".kalshi"))
_USER_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass(frozen=True)
class UserAccount:
    user_id: str            # Supabase auth.users.id
    key: KalshiKey
    ratio: float = 1.0      # owner-approved copy ratio: user contracts = floor(owner contracts x ratio)


def trading_user_ids() -> set[str]:
    """Owner-controlled live-trading allowlist (2026-10-01, user directive).

    A validated key alone only lets that user SEE his own account; it never
    starts trading. Mirroring W7 orders into a user's account requires the
    owner, BY HAND, to record an approval with the copy ratio
    (~/.kalshi/trading/approved/<uid>.json, see below), add the user id here
    (crypto_trading/.env, gitignored: KALSHI_PROD_TRADING_USER_IDS=<uid>,<uid>)
    and restart the runner at a safe point. Review first with the read-only
    `ops/kalshi_users.sh review <email>`. Empty / missing = nobody but the
    owner trades.
    """
    raw = env("KALSHI_PROD_TRADING_USER_IDS")
    return {u.strip() for u in raw.split(",") if _USER_ID_RE.fullmatch(u.strip())}


# ── Standard live-trading enable flow (2026-10-04, owner directive) ─────────
# The user APPLIES on the web panel (risk acknowledgement + requested ratio):
#   ~/.kalshi/trading/requests/<uid>.json   written by the web server; never trades by itself
# the owner APPROVES by hand after `ops/kalshi_users.sh review <email>`:
#   ~/.kalshi/trading/approved/<uid>.json   {"user_id": ..., "ratio": ...} written by the owner
#   + the id in KALSHI_PROD_TRADING_USER_IDS + a runner restart at a safe point
# the user (panel) or the owner (by hand) can STOP at any time:
#   ~/.kalshi/trading/stopped/<uid>.json    honoured from the very next order
# Starting to trade therefore always needs the owner's own hand and a restart;
# stopping needs neither.
MIRROR_RATIOS = (0.10, 0.25, 0.50, 1.00)


def trading_dir() -> Path:
    return USER_KEY_DIR / "trading"


def approved_ratio(user_id: str) -> float | None:
    """The owner-approved copy ratio (0 < r <= 1) for this user, else None."""
    try:
        rec = json.loads((trading_dir() / "approved" / f"{user_id}.json").read_text())
        ratio = float(rec.get("ratio"))
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if rec.get("user_id") != user_id or not (0.0 < ratio <= 1.0):
        return None
    return ratio


def trading_stopped(user_id: str) -> bool:
    return (trading_dir() / "stopped" / f"{user_id}.json").exists()


def kalshi_user_accounts() -> list[UserAccount]:
    """Allowlisted, validated, not-disabled per-user PROD accounts.

    Requires all of: the owner's allowlist entry, registry status 'active',
    both suffixed env entries, the key file present, no disabled/<uid> marker,
    an owner approval record with a valid ratio, no stop marker, and a key id
    different from the owner's (an account can never be double-ordered). The
    key files, approvals and stop markers are re-read on every call; the
    allowlist is fixed for the life of the process (restart to add).
    """
    allowed = trading_user_ids()
    if not allowed:
        return []
    try:
        registry = json.loads((USER_KEY_DIR / "users.json").read_text())
    except (OSError, ValueError):
        return []
    envf = _parse_env_file(USER_KEY_DIR / "users.env")
    owner_kids, owner_pems = _owner_prod_identity()
    out = []
    for uid, rec in sorted(registry.items()):
        if uid not in allowed or not _USER_ID_RE.fullmatch(uid) \
                or not isinstance(rec, dict) or rec.get("status") != "active":
            continue
        if (USER_KEY_DIR / "disabled" / uid).exists():
            continue
        if trading_stopped(uid):
            continue        # stopped by the user or the owner: no order from the next one on
        ratio = approved_ratio(uid)
        if ratio is None:
            continue        # no owner approval on record (written by the owner by hand)
        kid = envf.get(f"KALSHI_PROD_API_KEY_ID_{uid}", "")
        kpath = envf.get(f"KALSHI_PROD_PRIVATE_KEY_PATH_{uid}", "")
        if not kid or not kpath or kid in owner_kids or not Path(kpath).is_file():
            continue
        if _key_fingerprint(kpath) in owner_pems:
            continue        # the owner's own private key re-uploaded under another login
        out.append(UserAccount(uid, KalshiKey(kid, kpath, borrowed=False), ratio))
    return _one_platform_account_per_kalshi_account(out, registry, owner_kids)


def _one_platform_account_per_kalshi_account(accounts, registry, owner_kids):
    """Drop EVERY user whose Kalshi account is also reachable through another
    user id or the owner (same key id, or overlapping account key-id hashes
    recorded at verification) - one Kalshi account is never ordered twice."""
    import hashlib
    owner_h = {hashlib.sha256(k.encode()).hexdigest() for k in owner_kids}
    sets = {}
    for a in accounts:
        rec = registry.get(a.user_id) or {}
        h = set(rec.get("account_key_hashes") or [])
        h.add(hashlib.sha256(a.key.key_id.encode()).hexdigest())
        sets[a.user_id] = h
    keep = []
    for a in accounts:
        mine = sets[a.user_id]
        shared = any(mine & other for uid, other in sets.items() if uid != a.user_id)
        if not shared and not (mine & owner_h):
            keep.append(a)
    return keep


def _key_fingerprint(path: str) -> str | None:
    try:
        import hashlib
        return hashlib.sha256(Path(os.path.expanduser(path)).read_bytes().strip()).hexdigest()
    except OSError:
        return None


def _owner_prod_identity() -> tuple[set[str], set[str]]:
    """Every key id / key file the OWNER's orders can sign with - never a user.

    Includes the key the order path actually resolves (kalshi_key('margin',
    borrowed_ok=False) prefers KALSHI_MARGIN_* when set), not only
    KALSHI_PROD_API_KEY_ID.
    """
    kids, pems = set(), set()
    for k in ("KALSHI_PROD_API_KEY_ID", "KALSHI_MARGIN_KEY_ID"):
        v = env(k) or _PM_ENV.get(k, "")
        if v:
            kids.add(v)
    paths = [env(k) or _PM_ENV.get(k, "") for k in
             ("KALSHI_PROD_PRIVATE_KEY_PATH", "KALSHI_MARGIN_PRIVATE_KEY_PATH")]
    try:
        live = kalshi_key("margin", borrowed_ok=False)
        kids.add(live.key_id)
        paths.append(live.private_key_path)
    except Exception:
        pass
    for p in paths:
        fp = _key_fingerprint(p) if p else None
        if fp:
            pems.add(fp)
    return kids, pems


def disable_user_account(user_id: str, reason: str) -> None:
    """Stop routing to one user (e.g. 401/403); the web flow re-verifies."""
    if not _USER_ID_RE.fullmatch(user_id or ""):
        return
    d = USER_KEY_DIR / "disabled"
    d.mkdir(parents=True, exist_ok=True)
    (d / user_id).write_text(json.dumps({"reason": reason[:200],
                                         "at": time.time()}))


def kalshi_key(namespace: str = "margin", *, borrowed_ok: bool = True) -> KalshiKey:
    """Resolve the API key for a namespace ('margin' | 'event').

    Prefers the dedicated crypto key; falls back to the prediction_market demo
    key when ``borrowed_ok`` (recording/reads). Raises if no usable key, or if
    only a borrowed key exists and ``borrowed_ok=False`` (order paths).
    """
    prefix = "KALSHI_MARGIN" if namespace == "margin" else "KALSHI_EVENT"
    kid, kpath = env(f"{prefix}_KEY_ID"), env(f"{prefix}_PRIVATE_KEY_PATH")
    if kid and kpath:
        return KalshiKey(kid, kpath, borrowed=False)
    if not borrowed_ok:
        # ORDER paths only (borrowed_ok=False == the prod-orders path): the
        # user's own PROD account key, named KALSHI_PROD_* in the
        # prediction_market env, is a dedicated credential for this account —
        # designated for crypto live use by the user on 2026-09-12 ("env 里
        # 已有所有 prod 所需的 key"). It must NOT be returned for demo/read
        # paths (borrowed_ok=True): a prod key does not authenticate against
        # the demo host, and the demo mirror must keep using the demo key.
        pr_kid = env("KALSHI_PROD_API_KEY_ID") or _PM_ENV.get(
            "KALSHI_PROD_API_KEY_ID", "")
        pr_path = env("KALSHI_PROD_PRIVATE_KEY_PATH") or _PM_ENV.get(
            "KALSHI_PROD_PRIVATE_KEY_PATH", "")
        if pr_kid and pr_path:
            return KalshiKey(pr_kid, pr_path, borrowed=False)
    pm_kid = _PM_ENV.get("KALSHI_API_KEY_ID", "")
    pm_path = _PM_ENV.get("KALSHI_PRIVATE_KEY_PATH", "")
    if pm_kid and pm_path:
        if not borrowed_ok:
            raise RuntimeError(
                f"no dedicated {prefix}_* key configured and the borrowed "
                "prediction_market key is not allowed for this operation "
                "(orders/authed writes). Create the crypto key — Plan 08 §2.")
        if kalshi_env() != "demo":
            raise RuntimeError(
                "borrowed prediction_market key may only be used on DEMO "
                f"(KALSHI_ENV={kalshi_env()!r}). Create a dedicated prod key.")
        return KalshiKey(pm_kid, pm_path, borrowed=True)
    raise RuntimeError(f"no Kalshi key available for namespace {namespace!r}")


def kalshi_env() -> str:
    return env("KALSHI_ENV", "demo").lower()


def allow_live_orders() -> bool:
    return env("ALLOW_LIVE_ORDERS", "0").strip() in ("1", "true", "yes", "on")


# ── Universe (probe snapshot 2026-07-07; refreshed live via /margin/markets) ──
ACTIVE_PERPS_SNAPSHOT: tuple[str, ...] = (
    "KXBTCPERP", "KXETHPERP", "KXSOLPERP", "KXXRPPERP", "KXDOGEPERP",
    "KXKSHIBPERP", "KXBCHPERP", "KXLTCPERP", "KXLINKPERP", "KXNEARPERP",
    "KXSUIPERP", "KXHYPEPERP", "KXZECPERP",
)

# Perp listing epoch — backfills start here (BTC perp live 2026-06-03).
LISTING_EPOCH_TS = 1_780_444_800   # 2026-06-03 00:00:00 UTC
