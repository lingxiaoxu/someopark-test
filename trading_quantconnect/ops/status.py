"""status — live 状态/持仓/订单/日志一屏(只读)。
用法: python ops/status.py --algo mirror|probe [--logs N]"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_THIS_DIR))
from qc_api import QcClient, QcApiError  # noqa: E402
from ops.deploy import ALGOS  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--algo", choices=list(ALGOS), required=True)
    ap.add_argument("--logs", type=int, default=30)
    a = ap.parse_args()
    c = QcClient()
    pid = c.find_project(ALGOS[a.algo]["project"])
    if pid is None:
        print("project not found")
        return 1
    try:
        live = c.live_read(pid)
        print("== live/read ==")
        print(json.dumps({k: live.get(k) for k in
                          ("status", "deployId", "launched", "stopped",
                           "brokerage", "error")}, default=str,
                         ensure_ascii=False))
    except QcApiError as e:
        print(f"live/read: {e}")
    for name, fn in (("portfolio", c.live_portfolio),
                     ("orders", c.live_orders)):
        try:
            d = fn(pid)
            print(f"== live/{name} ==")
            print(json.dumps(d, default=str, ensure_ascii=False)[:1500])
        except QcApiError as e:
            print(f"live/{name}: {e}")
    try:
        d = c.live_logs(pid, 0, a.logs)
        print("== logs(tail) ==")
        for line in (d.get("logs") or [])[-a.logs:]:
            print(" ", line if isinstance(line, str) else json.dumps(line))
    except QcApiError as e:
        print(f"live/logs: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
