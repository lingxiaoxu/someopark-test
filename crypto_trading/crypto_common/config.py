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

import os
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
