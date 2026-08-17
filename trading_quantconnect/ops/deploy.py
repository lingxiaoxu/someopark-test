"""deploy — 项目上载/编译/live paper 部署(全经 QC API,零本地 LEAN 依赖)。

用法:
  python ops/deploy.py --algo probe          部署 M0 探针
  python ops/deploy.py --algo mirror         部署正式镜像
  python ops/deploy.py --algo mirror --update-only   只更新代码+编译(不部署)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_THIS_DIR))
from qc_api import QcClient, QcApiError  # noqa: E402

ALGOS = {
    "probe": {"project": "SomeoPark_M0_Probe",
              "files": {"main.py": _THIS_DIR / "lean" / "m0_probe.py"}},
    "mirror": {"project": "SomeoPark_Mirror",
               "files": {"main.py": _THIS_DIR / "lean" / "main.py",
                         "mirror_logic.py":
                             _THIS_DIR / "lean" / "mirror_logic.py"}},
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--algo", choices=list(ALGOS), required=True)
    ap.add_argument("--update-only", action="store_true")
    a = ap.parse_args()
    spec = ALGOS[a.algo]
    c = QcClient()

    pid = c.find_project(spec["project"])
    if pid is None:
        pid = c.create_project(spec["project"])
        print(f"[deploy] project created: {spec['project']} id={pid}")
    else:
        print(f"[deploy] project exists: {spec['project']} id={pid}")

    for name, path in spec["files"].items():
        c.upsert_file(pid, name, path.read_text())
        print(f"[deploy] {name} uploaded")
    cid = c.compile_project(pid)
    print(f"[deploy] compile OK: {cid}")
    if a.update_only:
        return 0

    org = c.organization_id()
    node = c.idle_live_node_id(org)
    try:
        d = c.live_create_paper(pid, cid, node)
    except QcApiError as e:
        print(f"[deploy] live/create FAILED(把 payload 报错原文修正后重试,"
              f"绝不静默): {e}")
        return 1
    print(json.dumps({"deployed": True, "projectId": pid,
                      "node": node,
                      "state": (d.get("live") or {}).get("status") or d},
                     ensure_ascii=False, default=str)[:400])
    return 0


if __name__ == "__main__":
    sys.exit(main())
