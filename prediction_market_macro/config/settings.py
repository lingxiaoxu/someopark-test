"""config/settings.py — global settings (PLAN §4).

Loads BOTH env files (repo root .env + prediction_market/.env), fails loudly with the
missing key's NAME (never silent None). All macro state lives inside this module's
directory tree (PLAN §0-bis).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

MACRO_ROOT = Path(__file__).resolve().parent.parent          # prediction_market_macro/
REPO_ROOT = MACRO_ROOT.parent                                # someopark-test/  (READ-ONLY)
FRONTEND_DATA = REPO_ROOT / "someo-park-investment-management" / "public" / "data"


def _load_env_file(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines) — does not override existing os.environ."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


@dataclass(frozen=True)
class Settings:
    macro_root: Path
    db_path: Path
    output_dir: Path
    log_dir: Path
    ckpt_dir: Path
    frontend_data: Path
    fred_api_key: str
    polygon_api_key: str
    kalshi_base: str = "https://api.elections.kalshi.com/trade-api/v2"
    ollama_base: str = "http://100.76.95.43:11434/v1"
    ollama_model: str = "nemotron-3-super:120b"
    kalshi_key_id: str | None = None
    kalshi_pk_path: str | None = None
    trading_enabled: bool = False                    # hard default: paper
    paper_bankroll_usd: float = 1000.0               # PLAN §13


def load_settings(require_keys: bool = True) -> Settings:
    _load_env_file(REPO_ROOT / ".env")
    _load_env_file(REPO_ROOT / "prediction_market" / ".env")

    missing = []
    fred = os.environ.get("FRED_API_KEY") or ""
    poly = os.environ.get("POLYGON_API_KEY") or ""
    if require_keys:
        if not fred:
            missing.append("FRED_API_KEY")
        if not poly:
            missing.append("POLYGON_API_KEY")
    if missing:
        raise RuntimeError(f"missing env keys: {missing} (source repo .env + prediction_market/.env)")

    data_dir = MACRO_ROOT / "data"
    s = Settings(
        macro_root=MACRO_ROOT,
        db_path=data_dir / "macro.db",
        output_dir=data_dir / "output",
        log_dir=data_dir / "logs",
        ckpt_dir=data_dir / "ckpt",
        frontend_data=FRONTEND_DATA,
        fred_api_key=fred,
        polygon_api_key=poly,
        kalshi_key_id=os.environ.get("KALSHI_API_KEY_ID"),
        kalshi_pk_path=os.environ.get("KALSHI_PRIVATE_KEY_PATH"),
        trading_enabled=False,
    )
    for d in (data_dir, s.output_dir, s.log_dir, s.ckpt_dir):
        d.mkdir(parents=True, exist_ok=True)
    return s
