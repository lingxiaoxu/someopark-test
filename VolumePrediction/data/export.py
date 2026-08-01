"""
data/export — A15: 面板落盘/读回(附录 A 映射)
==============================================
- 写入仅 VolumePrediction/outputs/panel/;tmp+rename 原子(common.atomic_write_df)
- "latest 链接语义"用指针文件 latest.json(跨平台稳妥,不依赖 symlink),
  记录 path/generated_at/rows/cols/date_range/fill_policy 元数据三戳纪律的面板版
- 测试时经 out_dir 参数重定向到 /tmp(生产零污染纪律)
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from VolumePrediction.common import OUT, atomic_write_df

PANEL_DIR = OUT / "panel"


def save_panel(
    panel: pd.DataFrame,
    fill_policy: str = "paper",
    tag: str = "",
    out_dir: Optional[Path] = None,
) -> Path:
    """面板落盘 + 更新 latest.json 指针。返回 parquet 路径。"""
    out_dir = Path(out_dir) if out_dir is not None else PANEL_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"panel_{ts}{('_' + tag) if tag else ''}.parquet"
    path = out_dir / name
    atomic_write_df(panel, path)

    dates = panel.index.get_level_values("date")
    meta = {
        "path": str(path),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "rows": int(len(panel)),
        "cols": list(map(str, panel.columns)),
        "n_tickers": int(panel.index.get_level_values("ticker").nunique()),
        "date_range": [str(dates.min().date()), str(dates.max().date())] if len(panel) else None,
        "fill_policy": fill_policy,
        "tag": tag,
    }
    tmp = out_dir / "latest.json.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    tmp.rename(out_dir / "latest.json")
    return path


def load_panel(path: Optional[Path] = None, out_dir: Optional[Path] = None) -> pd.DataFrame:
    """读面板;path 缺省时按 latest.json 指针取最新。"""
    if path is None:
        out_dir = Path(out_dir) if out_dir is not None else PANEL_DIR
        ptr = out_dir / "latest.json"
        if not ptr.exists():
            raise FileNotFoundError(f"no panel pointer at {ptr}; run save_panel first")
        path = Path(json.loads(ptr.read_text())["path"])
    df = pd.read_parquet(path)
    if list(df.index.names) != ["date", "ticker"]:
        raise ValueError("loaded panel lost MultiIndex(date,ticker) — corrupt file?")
    return df.sort_index()


def latest_meta(out_dir: Optional[Path] = None) -> dict:
    out_dir = Path(out_dir) if out_dir is not None else PANEL_DIR
    return json.loads((out_dir / "latest.json").read_text())
