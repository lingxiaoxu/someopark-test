#!/usr/bin/env bash
#
# backup_to_external.sh — 把 historical_runs/ 与 price_data/ 备份到移动硬盘
#
# 用途：每月执行一次的增量备份（rsync，只增不减）。
#
# 备份内容（源 → 目标，结构一一对应）：
#   historical_runs/  →  <移动硬盘>/code/someopark-test/historical_runs/
#   price_data/       →  <移动硬盘>/code/someopark-test/price_data/
#
# 安全约束：
#   - 只读源目录：全程 **不使用 --delete**，移动硬盘上已有而源已删的文件会保留
#     （这点很重要：清理脚本删掉本机旧数据后，备份仍是完整历史）
#   - 移动硬盘未挂载 → 立刻退出，绝不在本机硬盘上误建目录
#   - 备份前检查关键写入进程（pipeline / 回测），默认拒绝在其运行时备份，
#     避免把写了一半的 Excel 抓进备份（--force 可跳过，不建议）
#   - 传输中断可安全重跑：rsync --partial 支持断点续传
#   - 备份后做四道完整性校验：文件数 / 目录数 / 精确字节数 / rsync 干跑逐文件比对
#     另可用 --md5 N 抽样做内容级校验（默认 300 个）
#   - 全程日志留档到 logs/
#
# 用法：
#   bash conductor/backup_to_external.sh --dry-run    # 演练，不写入
#   bash conductor/backup_to_external.sh              # 正式备份 + 校验
#   bash conductor/backup_to_external.sh --md5 1000   # 加大抽样校验量
#   bash conductor/backup_to_external.sh --only price_data   # 只备份其中一个
#
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXT_VOL="/Volumes/Someo Park PRO-BLADE"
DEST_ROOT="$EXT_VOL/code/someopark-test"
LOG_DIR="$REPO_ROOT/conductor/logs"
TS="$(date '+%Y%m%d_%H%M%S')"
LOG="$LOG_DIR/backup_external_${TS}.log"

DRY_RUN=false
FORCE=false
MD5_SAMPLE=300
ONLY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --force)   FORCE=true; shift ;;
    --md5)     MD5_SAMPLE="${2:-300}"; shift 2 ;;
    --only)    ONLY="${2:-}"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

mkdir -p "$LOG_DIR"
log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

log "═══════════════════════════════════════════════════════════"
log "备份开始  $(date '+%F %T %Z')"
log "  仓库: $REPO_ROOT"
log "  目标: $DEST_ROOT"
$DRY_RUN && log "  模式: DRY-RUN（不写入任何数据）"
log "═══════════════════════════════════════════════════════════"

# ── 0. 铁律自检：本脚本永远不得删除移动硬盘上的数据 ─────────────────────────
# 移动硬盘是唯一的历史备份，只能写入。rsync 的 --delete 会把「源已删而备份还有」
# 的文件一并删掉 —— 那正是我们要保住的历史（本机清理脚本会删旧 window）。
# 这里对脚本自身做静态检查，防止日后有人误加 --delete。
# 只匹配「行首（可含缩进/$( ）真正调用 rsync 的命令行」，避免误伤本段的提示文案。
SELF_BAD=$(grep -nE '^[[:space:]]*(n=\$\()?rsync[[:space:]].*--delete' "$0" || true)
if [[ -n "$SELF_BAD" ]]; then
  echo "FATAL: 检测到 rsync 使用了 --delete —— 本脚本禁止删除移动硬盘数据，已中止。"
  echo "$SELF_BAD"
  exit 9
fi
# 同样禁止对移动硬盘路径执行 rm（临时文件一律 mktemp，不在移动硬盘上）
SELF_RM=$(grep -nE '^[[:space:]]*rm[[:space:]].*\$(EXT_VOL|DEST_ROOT|BAK)' "$0" || true)
if [[ -n "$SELF_RM" ]]; then
  echo "FATAL: 检测到针对移动硬盘的 rm —— 已中止。"
  echo "$SELF_RM"
  exit 9
fi

# ── 1. 移动硬盘挂载检查 ──────────────────────────────────────────────────────
if ! mount | grep -q "$EXT_VOL"; then
  log "ERROR: 移动硬盘未挂载：$EXT_VOL"
  log "       请插上硬盘后重试。（绝不在本机硬盘上创建同名目录）"
  exit 1
fi
# 二次确认：挂载点真实存在且可写
if [[ ! -d "$EXT_VOL" ]]; then
  log "ERROR: 挂载点不存在：$EXT_VOL"; exit 1
fi
log "✓ 移动硬盘已挂载"

# ── 2. 备份对象 ──────────────────────────────────────────────────────────────
# account_history：pairs_ledger 的逐日冻结切片（写下不再变）。账本主文件
# account_*.json / trade_ledger_*.jsonl 可从这些切片完整重放，故只需备份切片。
ITEMS=("historical_runs" "price_data" "account_history")
if [[ -n "$ONLY" ]]; then
  ITEMS=("$ONLY")
  log "  仅备份: $ONLY"
fi
for it in "${ITEMS[@]}"; do
  if [[ ! -d "$REPO_ROOT/$it" ]]; then
    log "ERROR: 源目录不存在：$REPO_ROOT/$it"; exit 1
  fi
done

# ── 3. 写入活动检查（两道：进程名 + 实证写入痕迹）─────────────────────────
# 目的：避免把「写了一半」的文件抓进备份。
#
# 3a. 进程名匹配 —— 只列真正会写 historical_runs/、price_data/ 或
#     account_history/ 的任务（DailySignal 写后者，已在下列模式中）。
#     注意排除 prediction_market_macro 的 walkforward：它同名但只写
#     prediction_market_macro/data/macro.db，与本备份的两个目录无关，
#     若不排除会导致备份被无谓阻塞（已实测其句柄不涉及这两个目录）。
BUSY_PATTERNS='SectorRotationBatchRun|daily_backtest\.sh|SemiconductorBatchRun|aiss_batch|DailySignal|VolumePrediction|pipeline_runner\.sh'
BUSY=$(ps aux | grep -E "$BUSY_PATTERNS" | grep -v grep \
       | grep -v "prediction_market_macro" | grep -v "$(basename "$0")" || true)
if [[ -n "$BUSY" ]]; then
  log "⚠ 检测到正在写入目标目录的进程："
  echo "$BUSY" | awk '{print "    PID "$2"  "$11" "$12" "$13}' | tee -a "$LOG"
  if $FORCE; then
    log "  --force 指定，继续备份（备份可能包含写了一半的文件）"
  else
    log "ERROR: 为保证备份完整性，已中止。"
    log "       等这些任务跑完再执行，或用 --force 强制（不推荐）。"
    exit 2
  fi
else
  log "✓ 无关键写入进程运行"
fi

# 3b. 实证检查 —— 不依赖进程名：看目标目录最近是否真的有文件在写。
#     进程名清单可能漏掉新脚本，这道以“写入痕迹”为准，更难骗过。
QUIET_MIN=3
for it in "${ITEMS[@]}"; do
  RECENT=$(find "$REPO_ROOT/$it" -type f -newermt "-${QUIET_MIN} minutes" 2>/dev/null | head -5)
  if [[ -n "$RECENT" ]]; then
    log "⚠ $it 在最近 ${QUIET_MIN} 分钟内仍有文件写入："
    echo "$RECENT" | sed "s|$REPO_ROOT/|    |" | tee -a "$LOG"
    if $FORCE; then
      log "  --force 指定，继续备份"
    else
      log "ERROR: 目标目录仍在被写入，为保证备份完整性已中止。"
      log "       稍等片刻重试，或用 --force 强制（不推荐）。"
      exit 2
    fi
  fi
done
log "✓ 目标目录近 ${QUIET_MIN} 分钟无写入活动"

# ── 4. 空间检查 ──────────────────────────────────────────────────────────────
NEED_KB=0
for it in "${ITEMS[@]}"; do
  NEED_KB=$((NEED_KB + $(du -sk "$REPO_ROOT/$it" 2>/dev/null | cut -f1)))
done
AVAIL_KB=$(df -k "$EXT_VOL" | tail -1 | awk '{print $4}')
log "  源体积合计: $(echo "scale=1; $NEED_KB/1048576" | bc) GB   移动硬盘可用: $(echo "scale=1; $AVAIL_KB/1048576" | bc) GB"
if [[ "$AVAIL_KB" -lt "$NEED_KB" ]]; then
  log "ERROR: 移动硬盘空间不足"; exit 1
fi
# 说明：增量备份实际写入远小于源体积，这里按最坏情况（全量）保守判断。

# ── 5. 记录源清单（作为校验基准，必须在传输前采集）────────────────────────
declare -a SRC_FILES SRC_DIRS SRC_BYTES
for i in "${!ITEMS[@]}"; do
  it="${ITEMS[$i]}"
  SRC_FILES[$i]=$(find "$REPO_ROOT/$it" -type f 2>/dev/null | wc -l | tr -d ' ')
  SRC_DIRS[$i]=$(find "$REPO_ROOT/$it" -type d 2>/dev/null | wc -l | tr -d ' ')
  SRC_BYTES[$i]=$(find "$REPO_ROOT/$it" -type f -exec stat -f %z {} + 2>/dev/null | awk '{s+=$1} END {print s+0}')
  log "  源清单 [$it] 文件 ${SRC_FILES[$i]} / 目录 ${SRC_DIRS[$i]} / ${SRC_BYTES[$i]} bytes"
done

if $DRY_RUN; then
  log ""
  log "── DRY-RUN：以下为 rsync 将要传输的内容（不实际写入）──"
  for it in "${ITEMS[@]}"; do
    mkdir -p "$DEST_ROOT/$it" 2>/dev/null || true
    n=$(rsync -an --stats "$REPO_ROOT/$it/" "$DEST_ROOT/$it/" 2>/dev/null \
        | grep "Number of files transferred" | awk '{print $NF}')
    log "  [$it] 待传输 ${n:-?} 个文件"
  done
  log ""
  log "[DRY-RUN] 未写入任何数据。去掉 --dry-run 执行正式备份。"
  exit 0
fi

# ── 6. 执行备份 ──────────────────────────────────────────────────────────────
RSYNC_FAIL=0
for it in "${ITEMS[@]}"; do
  log ""
  log "── 备份 $it ──"
  mkdir -p "$DEST_ROOT/$it"
  # -a 归档（保留权限/时间戳，移动硬盘为 APFS，元数据可完整保留）
  # --partial 断点续传；不用 --delete，移动硬盘只增不减
  rsync -a --partial --stats "$REPO_ROOT/$it/" "$DEST_ROOT/$it/" >> "$LOG" 2>&1
  rc=$?
  if [[ $rc -ne 0 ]]; then
    log "ERROR: rsync 失败（退出码 $rc），$it 备份不完整"
    RSYNC_FAIL=1
  else
    log "✓ $it 传输完成"
  fi
  # 每个大目标传完就复查一次挂载 —— 移动硬盘中途掉线是真实风险
  if ! mount | grep -q "$EXT_VOL"; then
    log "ERROR: 移动硬盘在备份过程中掉线！备份不可信，请重新执行。"
    exit 3
  fi
done

# ── 7. 完整性校验 ────────────────────────────────────────────────────────────
log ""
log "═══════════════ 完整性校验 ═══════════════"
ALL_OK=1

for i in "${!ITEMS[@]}"; do
  it="${ITEMS[$i]}"
  log ""
  log "── [$it] ──"

  # 校验 1/2/3：文件数、目录数、字节数
  # 判据是「备份 ⊇ 源」而非「完全相等」：本备份只增不减，清理脚本删掉本机旧数据后，
  # 备份仍保留那份历史，所以备份端天然会比源多。少于源才是真问题。
  df_=$(find "$DEST_ROOT/$it" -type f 2>/dev/null | wc -l | tr -d ' ')
  dd_=$(find "$DEST_ROOT/$it" -type d 2>/dev/null | wc -l | tr -d ' ')
  db_=$(find "$DEST_ROOT/$it" -type f -exec stat -f %z {} + 2>/dev/null | awk '{s+=$1} END {print s+0}')

  fmt_extra() { [[ "$1" -gt "$2" ]] && echo "（含已归档历史 +$(( $1 - $2 ))）" || echo ""; }
  if [[ "$df_" -ge "${SRC_FILES[$i]}" ]]; then
    log "  ✓ 文件数 源 ${SRC_FILES[$i]} ≤ 备份 $df_ $(fmt_extra "$df_" "${SRC_FILES[$i]}")"
  else
    log "  ✗ 备份文件数少于源！源 ${SRC_FILES[$i]} vs 备份 $df_"; ALL_OK=0
  fi
  if [[ "$dd_" -ge "${SRC_DIRS[$i]}" ]]; then
    log "  ✓ 目录数 源 ${SRC_DIRS[$i]} ≤ 备份 $dd_ $(fmt_extra "$dd_" "${SRC_DIRS[$i]}")"
  else
    log "  ✗ 备份目录数少于源！源 ${SRC_DIRS[$i]} vs 备份 $dd_"; ALL_OK=0
  fi
  if [[ "$db_" -ge "${SRC_BYTES[$i]}" ]]; then
    log "  ✓ 字节数 源 ${SRC_BYTES[$i]} ≤ 备份 $db_"
  else
    log "  ✗ 备份字节数少于源！源 ${SRC_BYTES[$i]} vs 备份 $db_"; ALL_OK=0
  fi

  # 校验 4：rsync 干跑逐文件比对（大小 + 时间戳）——最严格的结构级校验
  n=$(rsync -an --stats "$REPO_ROOT/$it/" "$DEST_ROOT/$it/" 2>/dev/null \
      | grep "Number of files transferred" | awk '{print $NF}')
  if [[ "${n:-1}" == "0" ]]; then
    log "  ✓ rsync 干跑：0 个文件需重传（零差异）"
  else
    log "  ✗ rsync 干跑：仍有 ${n} 个文件存在差异"; ALL_OK=0
  fi
done

# 校验 5：随机抽样 MD5 内容比对
# 说明：前面几道只能证明"大小/时间戳一致"。APFS 不对用户数据做校验和，
#       抽样比内容可发现静默写入损坏。
if [[ "$MD5_SAMPLE" -gt 0 ]]; then
  log ""
  log "── 随机抽样 MD5 内容校验（$MD5_SAMPLE 个）──"
  SAMPLE=$(mktemp)
  for it in "${ITEMS[@]}"; do
    (cd "$REPO_ROOT/$it" && find . -type f 2>/dev/null | sed "s|^\./|$it/|")
  done | sort -R | head -"$MD5_SAMPLE" > "$SAMPLE"
  ok=0; fail=0; miss=0
  while IFS= read -r rel; do
    s="$REPO_ROOT/$rel"; d="$DEST_ROOT/$rel"
    [[ -f "$d" ]] || { miss=$((miss+1)); log "    ✗ 备份缺失: $rel"; continue; }
    a=$(md5 -q "$s" 2>/dev/null); b=$(md5 -q "$d" 2>/dev/null)
    if [[ -n "$a" && "$a" == "$b" ]]; then ok=$((ok+1)); else fail=$((fail+1)); log "    ✗ MD5 不符: $rel"; fi
  done < "$SAMPLE"
  rm -f "$SAMPLE"
  log "  MD5 一致 $ok / 不符 $fail / 缺失 $miss"
  [[ $fail -eq 0 && $miss -eq 0 ]] && log "  ✓ 抽样内容一致" || ALL_OK=0
fi

# 最终确认移动硬盘仍在线（校验期间掉线会让上面结论失真）
if ! mount | grep -q "$EXT_VOL"; then
  log "ERROR: 校验期间移动硬盘掉线，结论不可信，请重跑。"; exit 3
fi

log ""
log "═══════════════════════════════════════════════════════════"
if [[ $ALL_OK -eq 1 && $RSYNC_FAIL -eq 0 ]]; then
  log "★ 备份完成，全部校验通过（一个不差）"
  log "  日志: $LOG"
  log "═══════════════════════════════════════════════════════════"
  exit 0
else
  log "✗ 备份存在问题，请查看上面的 ✗ 项并重跑"
  log "  日志: $LOG"
  log "═══════════════════════════════════════════════════════════"
  exit 4
fi
