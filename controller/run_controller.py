"""
controller/run_controller.py — 入口(plan §4.1)。

    python -m controller.run_controller --interval 1m           # 常驻循环
    python -m controller.run_controller --once [--force]        # 单轮(测试/盘后)
5/15/60m 为 1m 主循环的整分对齐子采样(消费方按 ts 过滤 nav_stream 即得,
controller 不为每档开进程)。调度由外部安排(openclaw/cron),本脚本不自装。
"""
from __future__ import annotations

import argparse
import json

from controller.scheduler import Controller

_INTERVALS = {"1m": 60, "5m": 300, "15m": 900, "60m": 3600}


def main() -> int:
    ap = argparse.ArgumentParser(description="central valuation controller")
    ap.add_argument("--interval", default="1m", choices=sorted(_INTERVALS))
    ap.add_argument("--once", action="store_true", help="单轮 tick 后退出")
    ap.add_argument("--force", action="store_true", help="闭市也跑(用 last/close 价)")
    a = ap.parse_args()
    c = Controller()
    if a.once:
        out = c.tick(force=a.force)
        print(json.dumps(out, indent=1, default=str))
        return 0
    c.run(interval_s=_INTERVALS[a.interval])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
