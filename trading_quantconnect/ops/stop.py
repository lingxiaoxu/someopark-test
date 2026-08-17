"""stop — 停止 live 部署(可选清仓)。
用法: python ops/stop.py --algo probe [--liquidate]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_THIS_DIR))
from qc_api import QcClient  # noqa: E402
from ops.deploy import ALGOS  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--algo", choices=list(ALGOS), required=True)
    ap.add_argument("--liquidate", action="store_true")
    a = ap.parse_args()
    c = QcClient()
    pid = c.find_project(ALGOS[a.algo]["project"])
    if pid is None:
        print("project not found")
        return 1
    from qc_api import QcApiError
    if a.liquidate:
        print(c.live_liquidate(pid))
    try:
        print(c.live_stop(pid))
    except QcApiError as e:
        if "No running deployment" in str(e):
            print("(liquidate 已自停,stop 无需执行)")
        else:
            raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
