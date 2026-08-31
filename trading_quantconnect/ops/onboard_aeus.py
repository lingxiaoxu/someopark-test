"""ops/onboard_aeus.py — AEUS 单策略 QC 挂载(2026-09-01 go-live 当天跑一次)。

与 2026-08-17 全书 golive 的关系(QUANTCONNECT_MIRROR_PLAN §9 同一套算术,单策略版):
  scalar_aeus = 官方equity_aeus / 账本equity_aeus     (official = master json
                aeus_equity 末行 = NAV 面板头条锚;ledger = account_aeus.json)
  deposit K   = 官方equity_aeus                        (QC CashBook 显式入金)
  target      = account_aeus 股数 × scalar_aeus        (exporter 常驻循环自动接手)

不变量(M4 对账依赖):
  Σ(QC aeus 持仓市值 + aeus 现金份额) ≡ 官方口径 aeus equity ≡ NAV 面板头条的
  aeus 数字 —— 挂载后两边日度变化 1:1,只剩执行价差(对账单列)。

纪律(逐字承自镜像 plan):
  * 既有五策略 scalars **一个字节不动**(冻结常数永不重算);本脚本只 append "aeus"。
  * 官方 perf json 仅本次构造时读一次;此后常驻循环只读持仓文件。
  * 幂等:state 里已有 aeus scalar → 拒绝重跑(--force 才覆盖,留痕)。

用法(go-live 当天,AEUS 建仓落盘 account_aeus.json 之后):
    conda run -n someopark_run python trading_quantconnect/ops/onboard_aeus.py [--dry-run]
    → 打印 deposit K 精确值(QC 侧 CashBook 手工入金依据)+ 写 scalars state
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parents[1]))          # trading_quantconnect/

from inventory_source import (LEDGER_ACCOUNT_FILES, OFFICIAL_ANCHORS,  # noqa: E402
                              REPO, stable_read)

STATE = _THIS.parents[1] / "state" / "exporter_state.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="覆盖已存在的 aeus scalar(留痕;正常永不需要)")
    args = ap.parse_args()

    # 1) 账本(必须已建仓)
    led_p = REPO / LEDGER_ACCOUNT_FILES["aeus"]
    if not led_p.exists():
        print(f"✗ {led_p} 不存在 — 先跑 aeus_pipeline.sh monthly 建仓,再挂载 QC")
        return 1
    led_doc = stable_read(led_p)
    ledger_eq = float(led_doc.get("equity") or 0)
    if ledger_eq <= 0:
        print(f"✗ account_aeus equity={ledger_eq!r} 无效")
        return 1

    # 2) 官方(master json aeus_equity 末行 = NAV 面板头条锚;仅此一次读取)
    rel, col = OFFICIAL_ANCHORS["aeus"]
    rows = stable_read(REPO / rel)
    nn = [r[col] for r in rows if isinstance(r, dict) and r.get(col) is not None]
    if not nn:
        print(f"✗ {rel} 无 {col} 行 — 先跑 UpdateMasterPerformance")
        return 1
    official_eq = float(nn[-1])

    scalar = round(official_eq / ledger_eq, 8)
    n_pos = len(led_doc.get("positions") or {})
    print("═" * 64)
    print("AEUS → QC 挂载参数(scalar = 官方/账本,AISS 同款一次性构造)")
    print("═" * 64)
    print(f"  账本 equity (account_aeus) : ${ledger_eq:,.2f}  ({n_pos} 票)")
    print(f"  官方 equity (aeus_equity)  : ${official_eq:,.2f}")
    print(f"  scalar_aeus(冻结,永不重算): {scalar}")
    print(f"  ➜ QC CashBook DEPOSIT K   : ${official_eq:,.2f}")
    print(f"  ➜ 之后 exporter 常驻循环自动: target = 账本股数 × {scalar}")
    print(f"  不变量: QC aeus 市值+现金 ≡ 官方口径 ≡ NAV 面板头条(M4 对账基准)")

    st = json.loads(STATE.read_text())
    scalars = st.get("scalars") or {}
    if "aeus" in scalars and not args.force:
        print(f"✗ scalars 已含 aeus={scalars['aeus']} — 幂等拒绝(--force 覆盖)")
        return 1
    if args.dry_run:
        print("  [dry-run] 未写 state")
        return 0
    scalars["aeus"] = scalar
    st["scalars"] = scalars
    st.setdefault("onboard_log", []).append({
        "strategy": "aeus", "at": datetime.now(timezone.utc).isoformat(),
        "ledger_equity": ledger_eq, "official_equity": official_eq,
        "scalar": scalar, "deposit_K": official_eq,
    })
    STATE.write_text(json.dumps(st, indent=1, ensure_ascii=False))
    print(f"  ✓ scalars['aeus']={scalar} 已写入 {STATE.name}(五策略常数未动)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
