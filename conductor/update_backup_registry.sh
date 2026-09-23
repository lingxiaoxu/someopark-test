#!/usr/bin/env bash
#
# update_backup_registry.sh — 全面重测并更新 conductor/backup_and_cleanup_registry.json
# （2026-09-12 新增;配套那份由 32 个 agent 审计产出的中央记录）
#
# 定位：这是**确定性测量脚本**（纯 shell + python,零 LLM、零网络）。
#       它只刷新「能用命令量出来」的字段,不碰需要判断力的字段。
#
# 会被重新测量并覆盖的字段（machine_measured）：
#   disk_snapshot                本机 / 外置盘水位
#   backup_scripts[].last_real_run / last_run_result / run_count / evidence_log
#   cleanup_scripts[].last_real_run / last_cutoff / last_result / history
#   local_coverage[]             各目录体积 / 文件数 / 最早最新日期
#   external_coverage[]          同上（外置盘）
#   gaps[].size                  缺口体积（存在性也会重判）
#   daily_growth[]               各目录最近 7 天的逐日新增字节
#   totals                       工作日/周末总日增、多少天满盘
#   alternative_data[].current_size / daily_bytes   （按已登记的 directory 重测）
#
# **不会**被覆盖的字段（需重跑审计工作流才能更新,脚本原样保留并标 stale）：
#   alternative_data[].status / consumers / source / evidence
#   gaps[].severity / note、red_lines、open_items、uncertainties
#   script_defects_found_and_fixed、critic_notes、purpose、path_convention
#   strategies[].pulls_from / writes_to、external_data_sources[].used_by
#   —— 这些是「判断」不是「测量」,脚本没资格改。每个被保留的块会带上
#      last_audited 与 stale_days,超过 30 天会在结尾提示重跑审计。
#
# 安全约束：
#   - 全程只读源数据,唯一写入的是 registry JSON 本身（先写 .tmp 再原子替换）
#   - 外置盘未挂载 → 仍然运行,但把外置盘相关字段标记为 unmeasured,不清零
#   - 不调用任何外部 API
#
# 用法：
#   bash conductor/update_backup_registry.sh              # 全量重测并覆盖
#   bash conductor/update_backup_registry.sh --dry-run    # 只打印将写入的差异,不落盘
#   bash conductor/update_backup_registry.sh --days 14    # 日增统计窗口改 14 天（默认 7）
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REGISTRY="$REPO_ROOT/conductor/backup_and_cleanup_registry.json"
EXT_VOL="/Volumes/Someo Park PRO-BLADE"
LOG_DIR="$REPO_ROOT/conductor/logs"
TS="$(date '+%Y%m%d_%H%M%S')"
LOG="$LOG_DIR/update_registry_${TS}.log"
DRY_RUN=false
DAYS=7

mkdir -p "$LOG_DIR"
log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }
die() { log "FATAL: $*"; exit 9; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --days) [[ $# -ge 2 && "$2" =~ ^[0-9]+$ ]] || die "--days 需要一个数字"; DAYS="$2"; shift 2 ;;
    *) die "未知参数: $1" ;;
  esac
done

[[ -f "$REGISTRY" ]] || die "找不到 $REGISTRY —— 首次生成需先跑审计工作流"

log "═══════════════════════════════════════════════════════════"
log "  更新备份/清理中央记录  $(date '+%F %T %Z')"
log "  目标: $REGISTRY"
log "  日增统计窗口: 最近 $DAYS 天"
log "  模式: $([[ "$DRY_RUN" == true ]] && echo 'DRY-RUN（不落盘）' || echo 正式)"
log "═══════════════════════════════════════════════════════════"

[[ -d "$EXT_VOL" ]] && log "  ✓ 外置盘在线" || log "  ⚠ 外置盘未挂载 —— 相关字段将标记 unmeasured,不会清零"

REPO_ROOT="$REPO_ROOT" REGISTRY="$REGISTRY" EXT_VOL="$EXT_VOL" \
DAYS="$DAYS" DRY_RUN="$DRY_RUN" LOG="$LOG" python3 <<'PY' 2>&1 | tee -a "$LOG"
import json, os, re, subprocess, sys, time
from pathlib import Path
from datetime import datetime, timedelta

REPO   = Path(os.environ["REPO_ROOT"])
REG    = Path(os.environ["REGISTRY"])
EXT    = Path(os.environ["EXT_VOL"])
DAYS   = int(os.environ["DAYS"])
DRY    = os.environ["DRY_RUN"] == "true"
EXT_OK = EXT.is_dir()

def sh(cmd, timeout=900):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""

def clean_path(p):
    """把记录里的 path 字段还原成真实路径。
    审计 agent 常在路径后面附说明,如
      '/Users/.../mlruns(根,SSRS 回测)'、'~/crypto_data_backup(仓外,同卷)'
    还有整条不是路径的行(如 '无归因残差(2026-09-10 当日)')。
    2026-09-12 实测:不做清洗会让 13 个目录只有 2 个解析成功,
    全仓日增算出 451 MiB(真值约 14 GiB),必须先净化。"""
    if not p: return None
    p = re.split(r"[(（]", p, 1)[0].strip()      # 砍掉中英文括号后的注解
    p = re.split(r"\s*[:：]\s*", p, 1)[0].strip()  # 砍掉 '仓外同卷其它:...' 这类前缀式条目
    if not p or p.startswith("无"): return None
    # 审计记录里用 <盘> 指代外置盘卷根,展开成真实挂载点
    p = p.replace("<盘>", str(EXT)).replace("＜盘＞", str(EXT))
    if p.startswith("~"): p = os.path.expanduser(p)
    if not p.startswith("/"): p = str(REPO / p)
    return p

def multi_paths(raw):
    """把 'A + B + C' 这类多路径条目拆开;单路径返回单元素列表。
    2026-09-12:gaps 里有 4 条是 '+' 连接的多目录,不拆则整条测不到。"""
    if not raw: return []
    head = re.split(r"[(（]", raw, 1)[0]
    parts = [x.strip() for x in head.split("+") if x.strip()]
    out = []
    for x in parts:
        c = clean_path(x)
        if c and Path(c).exists(): out.append(c)
    return out

def human(n):
    try: n = float(n)
    except Exception: return ""
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024: return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PiB"

def dir_stats(p):
    """体积/文件数/最早最新 mtime。用 stat 求和而非 du(避开 4KB block 取整)。"""
    p = str(p)
    if not Path(p).is_dir(): return None
    out = sh(f'find "{p}" -type f -exec stat -f "%m %z" {{}} + 2>/dev/null '
             '| awk \'{n++; s+=$2; if(mn==""||$1<mn)mn=$1; if($1>mx)mx=$1} '
             'END{printf "%d %d %s %s", n+0, s+0, mn, mx}\'')
    f = out.split()
    if len(f) < 4 or f[2] in ("", "0"): return None
    n, b, mn, mx = int(f[0]), int(f[1]), int(f[2]), int(f[3])
    fmt = lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")
    return {"files": n, "bytes": b, "size": human(b),
            "earliest": fmt(mn), "latest": fmt(mx), "date_basis": "mtime"}

def daily_growth(p, days):
    """按 birthtime 分组的逐日新增字节,最近 days 天。"""
    p = str(p)
    if not Path(p).is_dir(): return {}
    out = sh(f'find "{p}" -type f -exec stat -f "%SB %z" -t "%Y-%m-%d" {{}} + 2>/dev/null '
             '| awk \'{d[$1]+=$2} END{for(k in d) print k, d[k]}\' | sort')
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    res = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] >= cutoff:
            res[parts[0]] = int(parts[1])
    return res

reg = json.loads(REG.read_text())
now = datetime.now()
stamp = now.strftime("%Y-%m-%d %H:%M:%S")
print(f"  读入旧记录: schema {reg.get('schema_version')}, 上次生成 {reg.get('generated_at')}")

# ── 1. 磁盘水位 ───────────────────────────────────────────────────────
reg["disk_snapshot"] = {
    "local": sh("df -h /System/Volumes/Data | tail -1 | "
                "awk '{print $4\" 可用 / \"$2\" 共 (\"$5\" 已用)\"}'"),
    "external": (sh(f'df -h "{EXT}" | tail -1 | '
                    "awk '{print $4\" 可用 / \"$2\" 共 (\"$5\" 已用)\"}'")
                 if EXT_OK else "外置盘未挂载(unmeasured)"),
    "measured_at": stamp,
}
print(f"  磁盘: 本机 {reg['disk_snapshot']['local']}")

# ── 2. 脚本运行史(从日志反推,区分 dry-run 与正式)──────────────────
LOGD = REPO / "conductor" / "logs"
def scan_logs(prefix):
    """返回 (正式运行列表, dry-run 次数)。正式 = 日志里没有 DRY-RUN 字样。"""
    real, dry = [], 0
    for f in sorted(LOGD.glob(f"{prefix}_*.log")):
        try: txt = f.read_text(errors="replace")
        except Exception: continue
        is_dry = ("DRY-RUN" in txt) or ("dry_run" in txt) or ("DRY_RUN_DONE" in txt)
        if is_dry:
            dry += 1; continue
        m = re.search(r"截止时刻:\s*(\S+\s+\S+)", txt)
        ok = ("★" in txt)
        m2 = re.search(r"(\d{8})_(\d{6})\.log$", f.name)
        when = (f"{m2.group(1)[:4]}-{m2.group(1)[4:6]}-{m2.group(1)[6:]} "
                f"{m2.group(2)[:2]}:{m2.group(2)[2:4]}") if m2 else ""
        summ = ""
        for pat in (r"★[^\n]*", r"清理完成[^\n]*", r"有失败项[^\n]*"):
            mm = re.search(pat, txt)
            if mm: summ = mm.group(0).strip(); break
        real.append({"when": when, "ok": ok, "cutoff": m.group(1) if m else "",
                     "summary": summ[:160], "log": f.name})
    return real, dry

PREFIX = {
    "conductor/backup_to_external.sh": "backup_external",
    "conductor/backup_mlruns_to_external.sh": "backup_mlruns",
    "conductor/backup_codex_tmp_to_external.sh": "backup_codex_tmp",
    "conductor/backup_gaps_to_external.sh": "backup_gaps",
    "conductor/clean_old_wf_windows.sh": "clean_wf_windows",
    "conductor/clean_old_mlruns.sh": "clean_mlruns",
    "conductor/clean_codex_tmp_local.sh": "clean_codex_tmp",
}
for group in ("backup_scripts", "cleanup_scripts"):
    for e in reg.get(group, []):
        pref = PREFIX.get(e.get("script", ""))
        if not pref: continue
        real, dry = scan_logs(pref)
        if not real:
            e["machine_measured_at"] = stamp
            e["_note_no_real_run"] = f"仅见 {dry} 次 dry-run,无正式运行日志"
            continue
        last = real[-1]
        e["last_real_run"]  = last["when"]
        e["run_count"]      = len(real) + dry
        e["evidence_log"]   = f"conductor/logs/{last['log']}（正式 {len(real)} 次 + dry-run {dry} 次）"
        if group == "backup_scripts":
            e["last_run_result"] = last["summary"] or ("★ 通过" if last["ok"] else "无 ★ 标记,疑似未全过")
        else:
            e["last_cutoff"] = last["cutoff"]
            e["last_result"] = last["summary"] or ("★ 通过" if last["ok"] else "无 ★ 标记")
            e["history"] = [f"{r['when']} cutoff={r['cutoff'] or '默认'} {r['summary'][:80]}" for r in real]
        e["machine_measured_at"] = stamp
        print(f"  {e['script']}: 最后正式 {last['when']}"
              + (f" cutoff={last['cutoff']}" if group == "cleanup_scripts" else ""))

# ── 3. 本机 / 外置盘覆盖 ──────────────────────────────────────────────
for e in reg.get("local_coverage", []):
    full = clean_path(e.get("path", ""))
    s = dir_stats(full) if full else None
    if s:
        e.update({k: s[k] for k in ("size", "files", "earliest", "latest", "date_basis")})
        e["machine_measured_at"] = stamp
    else:
        e["_unmeasured"] = "路径不存在或非路径条目"

for e in reg.get("external_coverage", []):
    if not EXT_OK:
        e["_unmeasured"] = "外置盘未挂载"; continue
    # 走 clean_path 才认得 <盘> 占位符;不以 / 开头的相对路径挂到盘根
    full = clean_path(e.get("path", ""))
    if full and not full.startswith(str(EXT)) and not Path(full).exists():
        rel = re.split(r"[(（]", e.get("path", ""), 1)[0].strip().lstrip("/")
        full = str(EXT / rel)
    s = dir_stats(full)
    if s:
        e.update({k: s[k] for k in ("size", "files", "earliest", "latest", "date_basis")})
        e["machine_measured_at"] = stamp
    else:
        e["_unmeasured"] = "路径不存在或为空"

# ── 4. alternative_data 的体积与日增(只更新可量的两个字段)────────────
for e in reg.get("alternative_data", []):
    d = clean_path(e.get("directory", ""))
    if not d: continue
    s = dir_stats(d) if Path(d).is_dir() else None
    if s:
        e["current_size"] = s["size"]
        g = daily_growth(d, DAYS)
        vals = sorted(g.values())
        if vals:
            med = vals[len(vals)//2]
            e["daily_bytes"] = f"{human(med)}/天（最近 {len(vals)} 天中位;窗口 {DAYS} 天）"
            e["daily_series"] = g
        e["machine_measured_at"] = stamp
    elif Path(d).is_file():
        b = Path(d).stat().st_size
        e["current_size"] = human(b); e["machine_measured_at"] = stamp
    else:
        e["_unmeasured"] = "目录/文件不存在(可能已停用或路径已变)"

# ── 5. 日增台账与总量 ────────────────────────────────────────────────
ledger = []
for e in reg.get("daily_growth", []):
    full = clean_path(e.get("path", ""))
    g = daily_growth(full, DAYS) if full else {}
    if not g: e["_unmeasured"] = "路径不存在或非路径条目"; continue
    wd = {k: v for k, v in g.items()
          if datetime.strptime(k, "%Y-%m-%d").weekday() < 5}
    we = {k: v for k, v in g.items()
          if datetime.strptime(k, "%Y-%m-%d").weekday() >= 5}
    med = lambda d: sorted(d.values())[len(d)//2] if d else 0
    e["weekday_bytes"] = f"{human(med(wd))}/工作日" if wd else ""
    e["weekend_bytes"] = f"{human(med(we))}/周末日" if we else ""
    e["daily_series"] = g
    e["machine_measured_at"] = stamp
    ledger.append((full, med(wd)))

tot_wd = sum(v for _, v in ledger)
for e in reg.get("daily_growth", []):
    if tot_wd and "weekday_bytes" in e and e["weekday_bytes"]:
        p = e["path"]
        mine = dict(ledger).get(clean_path(p) or p, 0)
        e["share_pct"] = f"{100*mine/tot_wd:.1f}%"

free = sh("df -k /System/Volumes/Data | tail -1 | awk '{print $4}'")
free_b = int(free) * 1024 if free.isdigit() else 0
reg.setdefault("totals", {})
reg["totals"]["weekday_total"] = f"{human(tot_wd)}/工作日（本脚本实测,{len(ledger)} 个目录合计）"
reg["totals"]["days_until_full"] = (f"{free_b/tot_wd:.1f} 天" if tot_wd else "无法计算")
reg["totals"]["machine_measured_at"] = stamp
print(f"  全仓工作日日增: {human(tot_wd)}  → 约 {free_b/tot_wd:.1f} 天后满盘" if tot_wd else "  日增无法计算")

# ── 6. 缺口重判(本机有 / 外置盘无)────────────────────────────────────
CONV = [("", "code/someopark-test/")]   # 仓库内路径 → 盘上镜像
for e in reg.get("gaps", []):
    paths = multi_paths(e.get("path", ""))
    if not paths:
        e["_unmeasured"] = "非单一路径条目(概念性/多路径/已不存在)"; continue
    tot_b, tot_f = 0, 0
    for one in paths:
        st = dir_stats(one) if Path(one).is_dir() else (
             {"bytes": Path(one).stat().st_size, "files": 1} if Path(one).is_file() else None)
        if st: tot_b += st["bytes"]; tot_f += st["files"]
    if not tot_f:
        e["_unmeasured"] = "本机路径已不存在"; continue
    s = {"files": tot_f, "bytes": tot_b}
    e["size"] = human(tot_b)
    e["files"] = tot_f
    e["_measured_paths"] = paths
    if EXT_OK:
        rel = p if not p.startswith("/") else None
        cand = (EXT / "code/someopark-test" / rel) if rel else None
        if cand and cand.is_dir():
            es = dir_stats(cand)
            e["_external_now"] = (f"盘上 {es['files']} 文件 / {es['size']}"
                                  if es else "盘上为空")
            e["_still_a_gap"] = bool(es and es["files"] < s["files"])
        else:
            e["_external_now"] = "盘上仍不存在"; e["_still_a_gap"] = True
    e["machine_measured_at"] = stamp

# ── 6.5 未测量条目:把 LLM 断言的数字挪走,绝不让它冒充实测值 ──────────
# 2026-09-12 发现:路径字段带多路径/说明文字时 clean_path 解析失败,
# 该条目的 size/daily_bytes 等仍留着 LLM 写的原文,读者无法分辨机器实测与模型断言。
NUMERIC = ("size", "files", "bytes", "daily_bytes", "current_size",
           "weekday_bytes", "weekend_bytes", "share_pct", "earliest", "latest")
moved = 0
for sec in ("alternative_data", "local_coverage", "external_coverage",
            "daily_growth", "gaps"):
    for e in reg.get(sec, []):
        if e.get("machine_measured_at") == stamp:
            e["measurement_status"] = "machine_measured"
            continue
        e["measurement_status"] = "unmeasured_llm_asserted"
        for k in NUMERIC:
            if k in e and e[k] not in ("", None):
                e[f"_llm_asserted_{k}"] = e.pop(k)
                moved += 1
print(f"  未测量条目清洗: {moved} 个数字字段已移入 _llm_asserted_*(不再冒充实测)")

# ── 6.6 自检:实测覆盖率 ──────────────────────────────────────────────
cover = {}
for sec in ("alternative_data", "local_coverage", "external_coverage",
            "daily_growth", "gaps"):
    items = reg.get(sec, [])
    ok = sum(1 for e in items if e.get("measurement_status") == "machine_measured")
    cover[sec] = f"{ok}/{len(items)}"
reg["measurement_coverage"] = {**cover, "measured_at": stamp}
print("  实测覆盖率: " + "  ".join(f"{k} {v}" for k, v in cover.items()))

# ── 7. 判断类字段的陈旧度提示 ────────────────────────────────────────
prev = reg.get("generated_at", "")
try:
    audited = datetime.strptime(prev.split(" ")[0], "%Y-%m-%d")
    stale = (now - audited).days
except Exception:
    stale = -1
reg["judgment_fields_last_audited"] = prev
reg["judgment_fields_stale_days"] = stale
reg["last_machine_update"] = stamp

if DRY:
    print("\n  [DRY-RUN] 未落盘。上面是将要写入的测量结果。")
else:
    tmp = REG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(REG)
    print(f"\n  ★ 已更新 {REG}（{REG.stat().st_size:,} 字节）")

if stale > 30:
    print(f"  ⚠ 判断类字段(status/consumers/severity 等)已 {stale} 天未审计,"
          f"建议重跑审计工作流刷新。")
PY

RC=$?
log ""
log "═══════════════════════════════════════════════════════════"
[[ "$RC" -eq 0 ]] && log "★ 完成。日志: $LOG" || log "✗ 退出码 $RC,详见 $LOG"
[[ "$RC" -eq 0 ]] && echo "REGISTRY_UPDATED" || echo "REGISTRY_UPDATE_FAILED"
exit "$RC"
