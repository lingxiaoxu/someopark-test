"""common — 包级公共工具(路径/配置/日志;DEV_CONTRACTS 公共接口的实现)。"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parent               # VolumePrediction/
REPO = PKG.parent                                    # someopark-test/
OUT = PKG / "outputs"
LOG_DIR = PKG / "logs"
DATA_ROOT = REPO / "price_data" / "volume_prediction"
TMP_TEST_DIR = Path("/tmp/vp_tests")

_cfg_cache: dict | None = None


def load_config(force: bool = False) -> dict:
    global _cfg_cache
    if _cfg_cache is None or force:
        with open(PKG / "config.yaml") as f:
            _cfg_cache = yaml.safe_load(f)
    return _cfg_cache


def get_logger(name: str) -> logging.Logger:
    lg = logging.getLogger(f"VolumePrediction.{name}")
    if not lg.handlers:
        LOG_DIR.mkdir(exist_ok=True)
        h = logging.FileHandler(LOG_DIR / "vp.log")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
    return lg


def atomic_write_df(df, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp)
    tmp.rename(path)
