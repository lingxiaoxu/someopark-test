"""inventory_history 成本基础回填 — 以 portfolio_ledger 账本为唯一真源。

问题(2026-08-03 用户发现): AISS/SSRS 的 inventory 只在 ENTER 时写 cost_basis,
加仓(INCREASE)时原样继承旧成本 → 加权平均从未计算。KLAC 465→1678 股加仓后
成本仍是 192.17(应为 185.36),前端未实现亏损虚增 3.15 倍(-119,048 vs -38,358)。

真源: account_history/account_{strat}_YYYYMMDD.json 的 positions[tk].avg_cost
—— ledger.trade() 逐笔做加权平均(BUY: (s0·c0+Δ·p)/s1;SELL 不改成本),
今日值与手工 lot 复算逐位一致(KLAC 185.3607 / MU 875.684 / AMD 504.6253)。

做法: 每份 inventory 快照按其 as_of 找同日账本快照,逐票覆写 cost_basis。
- 账本无此票(如已清仓/时序边界) → 保留原值并计入 unmatched 报告,不猜
- 账本无该日(SSRS 2026-04-27 早于账本起点) → 整份跳过并报告
- 干跑(--apply 才落盘);落盘前整目录 tar 备份
用法: python backfill.py [--apply]
"""
import argparse
import glob
import json
import os
import re
import shutil
import sys
from collections import defaultdict

REPO = "/Users/xuling/code/someopark-test"
TARGETS = [
    {"name": "AISS",
     "inv_dir": f"{REPO}/qlib-main/semiconductor_strategy/inventory_history",
     "inv_glob": "inventory_aiss_*.json",
     "acc_dir": f"{REPO}/qlib-main/semiconductor_strategy/account_history",
     "acc_pat": "account_aiss_{d}.json",
     "holdings_keys": ["stock_holdings"],
     "live": f"{REPO}/qlib-main/semiconductor_strategy/inventory_aiss.json",
     "live_acc": f"{REPO}/qlib-main/semiconductor_strategy/account_aiss.json"},
    {"name": "SSRS",
     "inv_dir": f"{REPO}/qlib-main/sector_rotation/inventory_history",
     "inv_glob": "inventory_sector_rotation_*.json",
     "acc_dir": f"{REPO}/qlib-main/sector_rotation/account_history",
     "acc_pat": "account_ssrs_{d}.json",
     "holdings_keys": ["holdings"],
     "live": f"{REPO}/qlib-main/sector_rotation/inventory_sector_rotation.json",
     "live_acc": f"{REPO}/qlib-main/sector_rotation/account_ssrs.json"},
]


def load_acc(path):
    with open(path) as f:
        return json.load(f)


def process(t, apply_changes, backup_dir):
    print(f"\n{'='*70}\n{t['name']}\n{'='*70}")
    inv_files = sorted(glob.glob(os.path.join(t["inv_dir"], t["inv_glob"])))
    stats = defaultdict(int)
    changes = []          # (file, ticker, old, new)
    unmatched = []
    skipped = []

    todo = [(f, None) for f in inv_files] + [(t["live"], t["live_acc"])]
    for fpath, acc_override in todo:
        with open(fpath) as f:
            inv = json.load(f)
        as_of = inv.get("as_of") or ""
        if acc_override:
            acc_path = acc_override
        else:
            acc_path = os.path.join(t["acc_dir"],
                                    t["acc_pat"].format(d=as_of.replace("-", "")))
        if not os.path.exists(acc_path):
            skipped.append((os.path.basename(fpath), as_of, "账本无该日"))
            stats["skipped"] += 1
            continue
        pos = load_acc(acc_path).get("positions", {})
        dirty = False
        for hk in t["holdings_keys"]:
            for tk, h in (inv.get(hk) or {}).items():
                if tk not in pos:
                    unmatched.append((os.path.basename(fpath), tk))
                    stats["unmatched"] += 1
                    continue
                # 口径守卫(2026-08-03): 拆股前快照按当时口径存(KLAC 6/01: 127 股
                # @1921.71),账本是重放出来的当前口径(1270 股 @192.171)。二者金额
                # 相同。不变量 = 总成本相等 → 成本按股数比例换算回快照自身口径,
                # 直接抄账本会造出"拆股前股数 × 拆股后成本"的自相矛盾记录。
                inv_sh = int(h.get("shares", 0) or 0)
                acc_sh = int(pos[tk]["shares"])
                if inv_sh <= 0 or acc_sh <= 0:
                    unmatched.append((os.path.basename(fpath), tk + "(零股)"))
                    stats["unmatched"] += 1
                    continue
                ratio = acc_sh / inv_sh
                if abs(ratio - 1.0) > 0.02:
                    stats["caliber_scaled"] += 1
                    if not (0.05 <= ratio <= 20):
                        unmatched.append((os.path.basename(fpath),
                                          f"{tk}(股数比 {ratio:.3f} 异常)"))
                        stats["unmatched"] += 1
                        continue
                new = round(float(pos[tk]["avg_cost"]) * ratio, 4)
                old = h.get("cost_basis")
                stats["checked"] += 1
                if old is None or abs(float(old) - new) > 1e-4:
                    changes.append((os.path.basename(fpath), tk,
                                    None if old is None else float(old), new))
                    h["cost_basis"] = new
                    dirty = True
                    stats["changed"] += 1
        if dirty and apply_changes:
            tmp = fpath + ".tmp"
            with open(tmp, "w") as f:
                json.dump(inv, f, indent=2, ensure_ascii=False, default=str)
            os.replace(tmp, fpath)
            stats["files_written"] += 1

    print(f"  快照 {len(inv_files)}+1(live) | 校验持仓行 {stats['checked']} | "
          f"需改 {stats['changed']} | 口径换算 {stats['caliber_scaled']} | "
          f"账本缺票 {stats['unmatched']} | 跳过 {stats['skipped']}")
    if skipped:
        for s in skipped[:5]:
            print(f"    跳过: {s[0]} (as_of={s[1]}, {s[2]})")
    if unmatched:
        agg = defaultdict(int)
        for _, tk in unmatched:
            agg[tk] += 1
        print(f"    账本无此票(保留原值): {dict(agg)}")
    print(f"  改动样例(前 12):")
    for c in changes[:12]:
        o = "None" if c[2] is None else f"{c[2]:.4f}"
        print(f"    {c[0][:44]:46s} {c[1]:6s} {o:>10s} → {c[3]:.4f}")
    if apply_changes:
        print(f"  已写回 {stats['files_written']} 份文件")
    return stats, changes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写回(默认干跑)")
    a = ap.parse_args()

    backup_dir = None
    if a.apply:
        backup_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backup")
        os.makedirs(backup_dir, exist_ok=True)
        for t in TARGETS:
            dst = os.path.join(backup_dir, f"{t['name']}_inventory_history")
            if not os.path.exists(dst):
                shutil.copytree(t["inv_dir"], dst)
            shutil.copy2(t["live"], os.path.join(backup_dir, f"{t['name']}_live.json"))
        print(f"备份 → {backup_dir}")

    tot = defaultdict(int)
    for t in TARGETS:
        s, _ = process(t, a.apply, backup_dir)
        for k, v in s.items():
            tot[k] += v
    print(f"\n{'='*70}\n合计: 校验 {tot['checked']} 行, 需改 {tot['changed']} 行, "
          f"缺票 {tot['unmatched']}, 跳过 {tot['skipped']}")
    if not a.apply:
        print("(干跑 — 加 --apply 才写回)")


if __name__ == "__main__":
    main()
