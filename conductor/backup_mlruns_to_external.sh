#!/usr/bin/env bash
#
# backup_mlruns_to_external.sh — 把两个 mlruns/ 备份到移动硬盘
# （以 backup_to_external.sh 为模板的平行脚本，目标换成 MLflow 实验数据）
#
# 备份内容（源 → 目标，结构一一对应）：
#   mlruns/            →  <移动硬盘>/code/someopark-test/mlruns/
#   qlib-main/mlruns/  →  <移动硬盘>/code/someopark-test/qlib-main/mlruns/
#
# 注意：qlib-main/mlruns/ 顶层有 mlflow.db（1.5GB 的 SQLite 追踪库），
#       会随目录一并备份；写入静默检查（3 分钟）保证不会抓到写一半的库。
#
# 安全约束（与模板一致）：
#   - 只读源目录：全程 **不使用 --delete**，移动硬盘只增不减
#     （本机清理旧 run 后，备份仍保留完整历史）
#   - 移动硬盘未挂载 → 立刻退出，绝不在本机误建目录
#   - 备份前检查关键写入进程 + 目录 3 分钟写入静默，避免抓到半成品
#   - rsync --partial 断点续传，中断可安全重跑
#   - 备份后校验：文件数 / 目录数 / 字节数（备份 ⊇ 源）+ rsync 干跑 + MD5 抽样
#   - 全程日志留档到 logs/
#
# 用法：
#   bash conductor/backup_mlruns_to_external.sh --dry-run    # 演练
#   bash conductor/backup_mlruns_to_external.sh              # 正式备份 + 校验
#   bash conductor/backup_mlruns_to_external.sh --md5 1000   # 加大抽样
#   bash conductor/backup_mlruns_to_external.sh --only mlruns          # 只备其一
#   bash conductor/backup_mlruns_to_external.sh --only qlib-main/mlruns
#
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXT_VOL="/Volumes/Someo Park PRO-BLADE"
DEST_ROOT="$EXT_VOL/code/someopark-test"
LOG_DIR="$REPO_ROOT/conductor/logs"
TS="$(date '+%Y%m%d_%H%M%S')"
LOG="$LOG_DIR/backup_mlruns_${TS}.log"

DRY_RUN=false
FORCE=false
MD5_SAMPLE=300
ONLY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --force)   FORCE=true; shift ;;
    --md5)     [[ $# -ge 2 && "$2" =~ ^[0-9]+$ ]] || { echo "ERROR: --md5 需要数值参数"; exit 1; }; MD5_SAMPLE="$2"; shift 2 ;;
    --only)    [[ $# -ge 2 ]] || { echo "ERROR: --only 需要参数"; exit 1; }; ONLY="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

mkdir -p "$LOG_DIR"
log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

log "═══════════════════════════════════════════════════════════"
log "mlruns 备份开始  $(date '+%F %T %Z')"
log "  仓库: $REPO_ROOT"
log "  目标: $DEST_ROOT"
$DRY_RUN && log "  模式: DRY-RUN（不写入任何数据）"
log "═══════════════════════════════════════════════════════════"

# ── 0. 铁律自检：本脚本永远不得删除移动硬盘上的数据 ─────────────────────────
# 只匹配「行首真正调用 rsync 的命令行」，避免误伤本段提示文案。
SELF_BAD=$(grep -nE '^[[:space:]]*(n=\$\()?rsync[[:space:]].*--delete' "$0" || true)
if [[ -n "$SELF_BAD" ]]; then
  echo "FATAL: 检测到 rsync 使用了 --delete —— 本脚本禁止删除移动硬盘数据，已中止。"
  echo "$SELF_BAD"
  exit 9
fi
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
if [[ ! -d "$EXT_VOL" ]]; then
  log "ERROR: 挂载点不存在：$EXT_VOL"; exit 1
fi
log "✓ 移动硬盘已挂载"

# ── 2. 备份对象 ──────────────────────────────────────────────────────────────
ITEMS=("mlruns" "qlib-main/mlruns")
if [[ -n "$ONLY" ]]; then
  # 白名单校验：防止 --only ../xxx 之类路径逃出预期范围
  case "$ONLY" in
    mlruns|qlib-main/mlruns) ITEMS=("$ONLY"); log "  仅备份: $ONLY" ;;
    *) log "ERROR: --only 仅接受 mlruns 或 qlib-main/mlruns，收到: $ONLY"; exit 1 ;;
  esac
fi
for it in "${ITEMS[@]}"; do
  if [[ ! -d "$REPO_ROOT/$it" ]]; then
    log "ERROR: 源目录不存在：$REPO_ROOT/$it"; exit 1
  fi
done

# ── 3. 写入活动检查（两道：进程名 + 实证写入痕迹）─────────────────────────
# mlruns 由 qlib 回测 / pipeline / VolumePrediction 等任务写入，模式与模板一致。
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

# 3b. 实证检查 —— 目标目录近 3 分钟是否真的有文件在写（mlflow.db 尤其重要）。
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

# ── 5. 记录源清单（校验基准，传输前采集）───────────────────────────────────
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
  rsync -a --partial --stats "$REPO_ROOT/$it/" "$DEST_ROOT/$it/" >> "$LOG" 2>&1
  rc=$?
  if [[ $rc -ne 0 ]]; then
    log "ERROR: rsync 失败（退出码 $rc），$it 备份不完整"
    RSYNC_FAIL=1
  else
    log "✓ $it 传输完成"
  fi
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

  # 判据「备份 ⊇ 源」：本备份只增不减，清理脚本删掉本机旧 run 后备份仍保留历史，
  # 备份端天然会比源多；少于源才是真问题。
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

  n=$(rsync -an --stats "$REPO_ROOT/$it/" "$DEST_ROOT/$it/" 2>/dev/null \
      | grep "Number of files transferred" | awk '{print $NF}')
  if [[ "${n:-1}" == "0" ]]; then
    log "  ✓ rsync 干跑：0 个文件需重传（零差异）"
  else
    log "  ✗ rsync 干跑：仍有 ${n} 个文件存在差异"; ALL_OK=0
  fi
done

# 随机抽样 MD5 内容比对（APFS 不校验用户数据，抽样内容比对兜底静默损坏）
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

if ! mount | grep -q "$EXT_VOL"; then
  log "ERROR: 校验期间移动硬盘掉线，结论不可信，请重跑。"; exit 3
fi

log ""
log "═══════════════════════════════════════════════════════════"
if [[ $ALL_OK -eq 1 && $RSYNC_FAIL -eq 0 ]]; then
  log "★ mlruns 备份完成，全部校验通过（一个不差）"
  log "  日志: $LOG"
  log "═══════════════════════════════════════════════════════════"
  exit 0
else
  log "✗ 备份存在问题，请查看上面的 ✗ 项并重跑"
  log "  日志: $LOG"
  log "═══════════════════════════════════════════════════════════"
  exit 4
fi
