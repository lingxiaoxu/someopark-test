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


def sanitize_for_json(obj):
    """→ (clean, paths)。非有限 float(NaN/±inf)一律替换为 None,并收集出现
    路径。裸 NaN 是**非法 JSON**——json.dumps 默认放行,但 jq/JS 等严格解析器
    直接 parse 失败(2026-08-15 advice 文件 BK 改名票实证)。调用方应把 paths
    写进 warnings,让"无数据"可见而不是让下游解析炸掉。"""
    import math
    paths: list[str] = []

    def walk(o, path):
        if isinstance(o, dict):
            return {k: walk(v, f"{path}.{k}") for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [walk(v, f"{path}[{i}]") for i, v in enumerate(o)]
        if isinstance(o, float) and not math.isfinite(o):
            paths.append(path)
            return None
        return o

    return walk(obj, ""), paths
