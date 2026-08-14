#!/usr/bin/env bash
#
# clean_old_wf_windows.sh — 删除本机上已备份的旧 walk-forward window 文件夹
#
# 用途：定期（每月）清理本机硬盘空间。只删除移动硬盘上**已确认存在且内容吻合**
#       的旧 window 文件夹；本机删掉后，完整历史仍保留在移动硬盘。
#
# 删除范围（严格限定，绝不越界）：
#   historical_runs/walk_forward/window*/          ← 仅 window 开头的目录及其内容
#   historical_runs/walk_forward_mtfs/window*/     ← 同上
#
# **绝不触碰**：
#   - 两个目录下所有非 window 开头的条目（dsr_selection_log_*.csv、.DS_Store 等，
#     实测各有约 615 个，一个都不动）
#   - historical_runs 下的其他目录（sector_rotation / semiconductor_strategy /
#     vix_chronos2 / audit）与顶层散落文件
#   - 移动硬盘上的任何内容（本脚本对移动硬盘只读）
#
# 时间判据：
#   文件夹的**创建时间**（birthtime，stat -f %B）≤ 截止时刻 → 候选删除。
#   截止时刻默认 = 上一季度末的前一天 13:00 美东时间。
#   （例：现在是 2026 Q3 → 上季末 2026-06-30 → 截止 2026-06-29 13:00 ET）
#   注意：文件夹名里的日期（如 window01_2024-02-01_2025-07-31）是**数据窗口期**，
#         不是创建日期，脚本一律以文件系统时间戳为准。
#
# 删除前逐个校验（任一不过 → 跳过该文件夹，不删）：
#   1. 移动硬盘上存在同名 window 文件夹
#   2. 备份内文件数与本机完全相同
#   3. 备份内总字节数与本机完全相同
#
# 其他安全措施：
#   - 移动硬盘未挂载 → 直接退出，不删任何东西
#   - 删除过程中周期性复查挂载状态，掉线立即中止（曾实际发生过掉线）
#   - 每个待删路径做模式校验，不符合 .../walk_forward*/window* 一律拒绝
#   - 用 rm -rf（不进回收站），故默认先跑 --dry-run 确认
#   - 全程日志留档到 logs/
#
# 用法：
#   bash conductor/clean_old_wf_windows.sh --dry-run              # 演练（务必先跑）
#   bash conductor/clean_old_wf_windows.sh                        # 正式删除
#   bash conductor/clean_old_wf_windows.sh --cutoff "2026-06-29 13:00"   # 自定义截止
#   bash conductor/clean_old_wf_windows.sh --dry-run --verbose    # 逐个列出
#
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HR="$REPO_ROOT/historical_runs"
EXT_VOL="/Volumes/Someo Park PRO-BLADE"
BAK="$EXT_VOL/code/someopark-test/historical_runs"
LOG_DIR="$REPO_ROOT/conductor/logs"
TS="$(date '+%Y%m%d_%H%M%S')"
LOG="$LOG_DIR/clean_wf_windows_${TS}.log"

DRY_RUN=false
VERBOSE=false
CUTOFF_STR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --verbose) VERBOSE=true; shift ;;
    --cutoff)  CUTOFF_STR="${2:-}"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

mkdir -p "$LOG_DIR"
log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

# ── 截止时刻：默认 = 上一季度末的前一天 13:00 美东 ──────────────────────────
if [[ -z "$CUTOFF_STR" ]]; then
  Y=$(date '+%Y'); M=$(date '+%m'); M=$((10#$M))
  if   [[ $M -le 3  ]]; then QY=$((Y-1)); QEND="12-31"
  elif [[ $M -le 6  ]]; then QY=$Y;       QEND="03-31"
  elif [[ $M -le 9  ]]; then QY=$Y;       QEND="06-30"
  else                       QY=$Y;       QEND="09-30"
  fi
  # 上季末的前一天
  CUTOFF_DATE=$(date -j -v-1d -f "%Y-%m-%d" "$QY-$QEND" "+%Y-%m-%d" 2>/dev/null)
  CUTOFF_STR="$CUTOFF_DATE 13:00"
fi
CUTOFF=$(TZ=America/New_York date -j -f "%Y-%m-%d %H:%M" "$CUTOFF_STR" +%s 2>/dev/null)
if [[ -z "$CUTOFF" ]]; then
  echo "ERROR: 无法解析截止时刻：$CUTOFF_STR （格式应为 \"YYYY-MM-DD HH:MM\"）"; exit 1
fi

log "═══════════════════════════════════════════════════════════"
log "旧 window 清理  $(date '+%F %T %Z')"
log "  截止时刻: $(TZ=America/New_York date -r "$CUTOFF" '+%F %T %Z')  (epoch $CUTOFF)"
log "  判据: 文件夹创建时间(birthtime) ≤ 截止 且 移动硬盘上校验通过"
$DRY_RUN && log "  模式: DRY-RUN（只统计，不删除）"
log "═══════════════════════════════════════════════════════════"

# ── 前置检查 ────────────────────────────────────────────────────────────────
for d in "$HR/walk_forward" "$HR/walk_forward_mtfs"; do
  [[ -d "$d" ]] || { log "ERROR: 源目录不存在：$d"; exit 1; }
done
if ! mount | grep -q "$EXT_VOL"; then
  log "ERROR: 移动硬盘未挂载：$EXT_VOL"
  log "       没有备份就绝不删除本机数据。请插上硬盘后重试。"
  exit 1
fi
[[ -d "$BAK" ]] || { log "ERROR: 移动硬盘上找不到备份目录：$BAK"; exit 1; }
log "✓ 移动硬盘已挂载，备份目录可访问"

check_mount() {
  if ! mount | grep -q "$EXT_VOL"; then
    log "ERROR: 移动硬盘掉线！立即中止，剩余文件夹保持原样。"
    exit 3
  fi
}

# ── 铁律：本脚本永远不得删除移动硬盘上的任何数据 ────────────────────────────
# 移动硬盘是唯一的历史备份，只能写入、绝不能删。任何 rm 目标必须先过这个闸门：
# 一旦发现路径落在 /Volumes/ 下（或不在本机允许范围内），直接硬退出，不是跳过。
assert_deletable() {
  local p="$1"
  case "$p" in
    /Volumes/*)
      log "FATAL: 拒绝删除移动硬盘路径（备份数据只可写入不可删除）: $p"
      log "       脚本立即中止。"
      exit 9 ;;
  esac
  case "$p" in
    "$HR/walk_forward/window"*|"$HR/walk_forward_mtfs/window"*) return 0 ;;
    *)
      log "FATAL: 路径不在允许删除范围内: $p"
      log "       仅允许 historical_runs/walk_forward{,_mtfs}/window* ，脚本立即中止。"
      exit 9 ;;
  esac
}

# ── 单次遍历采集「备份树」每个 window 的文件数与字节数 ──────────────────────
# 用一次 find 汇总，而不是逐个目录访问：既快，也把移动硬盘暴露时间降到最低
# （逐个访问 789 次曾因中途掉线导致全部误判为“缺失”）。
index_tree() {  # $1=根目录  → 输出 "名称|文件数|字节数"
  find "$1" -mindepth 2 -type f -exec stat -f '%z %N' {} + 2>/dev/null | awk -v base="$1/" '
    { size=$1; path=substr($0, index($0," ")+1)
      rel=substr(path, length(base)+1)
      slash=index(rel,"/"); if (slash==0) next
      top=substr(rel,1,slash-1)
      cnt[top]++; bytes[top]+=size }
    END { for (t in cnt) printf "%s|%d|%d\n", t, cnt[t], bytes[t] }'
}

TOTAL_DEL=0; TOTAL_KB=0; TOTAL_FILES=0; TOTAL_SKIP=0

for W in walk_forward walk_forward_mtfs; do
  log ""
  log "── $W ──"
  check_mount

  BAK_IDX=$(mktemp); SRC_IDX=$(mktemp)
  index_tree "$BAK/$W"   > "$BAK_IDX"
  index_tree "$HR/$W"    > "$SRC_IDX"
  check_mount

  n_cand=0; n_ok=0; n_skip=0; kb=0; files=0
  DELLIST=$(mktemp)

  for d in "$HR/$W"/window*/; do
    [[ -d "$d" ]] || continue
    # 安全闸1：必须是真目录，不能是符号链接
    [[ -L "${d%/}" ]] && { log "  ⚠ 跳过符号链接: $d"; continue; }
    bn=$(basename "$d")
    # 安全闸2：名称必须以 window 开头（双保险，glob 之外再校验一次）
    [[ "$bn" == window* ]] || { log "  ⚠ 跳过非 window 条目: $bn"; continue; }
    # 安全闸3：完整路径必须落在预期模式内
    case "${d%/}" in
      "$HR/walk_forward/$bn"|"$HR/walk_forward_mtfs/$bn") ;;
      *) log "  ⚠ 路径模式异常，跳过: $d"; continue ;;
    esac

    # 时间判据：创建时间
    b=$(stat -f %B "${d%/}" 2>/dev/null)
    [[ -n "$b" ]] || { log "  ⚠ 无法读取创建时间，跳过: $bn"; continue; }
    [[ "$b" -le "$CUTOFF" ]] || continue
    n_cand=$((n_cand+1))

    # 备份校验：存在 + 文件数一致 + 字节数一致
    src_line=$(grep -m1 "^${bn}|" "$SRC_IDX" || true)
    bak_line=$(grep -m1 "^${bn}|" "$BAK_IDX" || true)
    if [[ -z "$bak_line" ]]; then
      log "  ✗ 备份中不存在，跳过不删: $bn"; n_skip=$((n_skip+1)); continue
    fi
    s_cnt="${src_line#*|}"; s_cnt="${s_cnt%%|*}"; s_byt="${src_line##*|}"
    b_cnt="${bak_line#*|}"; b_cnt="${b_cnt%%|*}"; b_byt="${bak_line##*|}"
    if [[ "$s_cnt" != "$b_cnt" ]]; then
      log "  ✗ 备份文件数不符($s_cnt vs $b_cnt)，跳过不删: $bn"; n_skip=$((n_skip+1)); continue
    fi
    if [[ "$s_byt" != "$b_byt" ]]; then
      log "  ✗ 备份字节数不符($s_byt vs $b_byt)，跳过不删: $bn"; n_skip=$((n_skip+1)); continue
    fi

    n_ok=$((n_ok+1))
    kb=$((kb + $(du -sk "${d%/}" 2>/dev/null | cut -f1)))
    files=$((files + s_cnt))
    echo "${d%/}" >> "$DELLIST"
    $VERBOSE && log "  ✓ 待删(已验证备份): $bn  文件 $s_cnt"
  done

  log "  符合日期条件: $n_cand 个"
  log "  备份校验通过、将删除: $n_ok 个（文件 $files 个，约 $(echo "scale=1; $kb/1048576" | bc) GB）"
  [[ $n_skip -gt 0 ]] && log "  ⚠ 校验未过、保留不删: $n_skip 个"

  if ! $DRY_RUN && [[ $n_ok -gt 0 ]]; then
    log "  开始删除…"
    i=0
    while IFS= read -r target; do
      # 删除前最后一道闸门：非本机 window 路径 / 任何 /Volumes 路径 → 硬退出
      assert_deletable "$target"
      rm -rf "$target"
      i=$((i+1))
      # 每 50 个复查一次移动硬盘在线状态
      if (( i % 50 == 0 )); then log "    …已删 $i / $n_ok"; check_mount; fi
    done < "$DELLIST"
    log "  ✓ 已删除 $i 个文件夹"
  fi

  TOTAL_DEL=$((TOTAL_DEL+n_ok)); TOTAL_KB=$((TOTAL_KB+kb))
  TOTAL_FILES=$((TOTAL_FILES+files)); TOTAL_SKIP=$((TOTAL_SKIP+n_skip))
  rm -f "$BAK_IDX" "$SRC_IDX" "$DELLIST"
done

log ""
log "═══════════════════════════════════════════════════════════"
if $DRY_RUN; then
  log "[DRY-RUN] 未删除任何文件"
  log "  将删除 $TOTAL_DEL 个 window 文件夹（$TOTAL_FILES 个文件，约 $(echo "scale=1; $TOTAL_KB/1048576" | bc) GB）"
  [[ $TOTAL_SKIP -gt 0 ]] && log "  另有 $TOTAL_SKIP 个因备份校验未通过而保留"
  log "  确认无误后，去掉 --dry-run 执行。"
else
  log "★ 清理完成：删除 $TOTAL_DEL 个 window 文件夹，释放约 $(echo "scale=1; $TOTAL_KB/1048576" | bc) GB"
  [[ $TOTAL_SKIP -gt 0 ]] && log "  $TOTAL_SKIP 个因备份校验未通过而保留（未删）"
  log "  本机剩余空间: $(df -h /System/Volumes/Data | tail -1 | awk '{print $4}')"
fi
log "  日志: $LOG"
log "═══════════════════════════════════════════════════════════"
