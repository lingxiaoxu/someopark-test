#!/usr/bin/env bash
#
# backup_codex_tmp_to_external.sh — 把 ~/.codex 与 /private/tmp 备份到移动硬盘
# （以 backup_to_external.sh 为模板的平行脚本；2026-09-11 新增）
#
# 备份内容（源 → 目标，**按源的目录结构**一一对应）：
#   ~/.codex       →  <移动硬盘>/.codex        （源在 home 根 → 盘根，与 code/ 同一惯例）
#   /private/tmp   →  <移动硬盘>/tmp           （源在文件系统根 → 盘根）
#
# 安全约束（与模板一致）：
#   - 对移动硬盘**只增不减**：全程不使用 --delete；已有内容一律保留
#   - 移动硬盘未挂载 → 立刻退出，绝不在本机硬盘上误建目录
#   - rsync --partial 断点续传，中断可安全重跑
#   - 源是活动目录（Codex 扩展在写、Claude 会话草稿在写），rsync 会报
#     "file has vanished"（码 24）——对临时数据属正常，不计为失败
#   - 备份后校验：文件数 / 字节数（备份 ⊇ 源）+ rsync 干跑零差异 + MD5 抽样
#   - 全程日志留档到 conductor/logs/
#
# 用法：
#   bash conductor/backup_codex_tmp_to_external.sh --dry-run
#   bash conductor/backup_codex_tmp_to_external.sh
#   bash conductor/backup_codex_tmp_to_external.sh --only .codex
#   bash conductor/backup_codex_tmp_to_external.sh --only tmp --md5 500
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXT_VOL="/Volumes/Someo Park PRO-BLADE"
LOG_DIR="$REPO_ROOT/conductor/logs"
TS="$(date '+%Y%m%d_%H%M%S')"
LOG="$LOG_DIR/backup_codex_tmp_${TS}.log"
DRY_RUN=false
MD5_SAMPLE=300
ONLY=""

mkdir -p "$LOG_DIR"
log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }
die() { log "FATAL: $*"; exit 9; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --md5)  [[ $# -ge 2 && "$2" =~ ^[0-9]+$ ]] || die "--md5 需要一个数字"; MD5_SAMPLE="$2"; shift 2 ;;
    --only) [[ $# -ge 2 ]] || die "--only 需要一个参数"; ONLY="$2"; shift 2 ;;
    *) die "未知参数: $1" ;;
  esac
done

# ── 自检：本脚本绝不可含针对移动硬盘的删除语句 ───────────────────────────
# 注意：检查行自身带 SELFCHECK 标记并被排除，否则它写的模式串会误伤自己。
if grep -nE 'rm .*(EXT_VOL|DEST)|--delete' "$0" | grep -v 'SELFCHECK' | grep -vE '^[0-9]+:\s*#' | grep -q .; then  # SELFCHECK
  die "自检失败：脚本含针对移动硬盘的删除语句"  # SELFCHECK
fi

[[ -d "$EXT_VOL" ]] || die "移动硬盘未挂载：$EXT_VOL"

# 源 → 目标（目标按源的路径结构；home 根与文件系统根都落到盘根）
declare -a NAMES=(".codex" "tmp")
declare -a SRCS=("/Users/xuling/.codex" "/private/tmp")
declare -a DSTS=("$EXT_VOL/.codex" "$EXT_VOL/tmp")

count_files() { find "$1" -type f 2>/dev/null | wc -l | tr -d ' '; }
count_bytes() { find "$1" -type f -exec stat -f %z {} + 2>/dev/null | awk '{s+=$1} END{print s+0}'; }

log "═══════════════════════════════════════════════════════════"
log "  ~/.codex 与 /private/tmp → 移动硬盘备份  $(date '+%F %T %Z')"
log "  模式: $([[ "$DRY_RUN" == true ]] && echo DRY-RUN || echo 正式)"
log "═══════════════════════════════════════════════════════════"

FAIL=0
for i in "${!NAMES[@]}"; do
  NAME="${NAMES[$i]}"; SRC="${SRCS[$i]}"; DST="${DSTS[$i]}"
  [[ -n "$ONLY" && "$ONLY" != "$NAME" ]] && continue
  [[ -d "$SRC" ]] || { log "⚠ 源不存在，跳过: $SRC"; continue; }
  [[ -d "$EXT_VOL" ]] || die "移动硬盘中途掉线"

  log ""
  log "══ $NAME ══"
  log "  源: $SRC  →  目标: $DST"
  SN=$(count_files "$SRC"); SB=$(count_bytes "$SRC")
  log "  源: $SN 个文件, $SB 字节"

  if [[ "$DRY_RUN" == true ]]; then
    NEED=$(rsync -an --partial "$SRC/" "$DST/" 2>/dev/null | grep -cv '^\.\?/\?$' || true)
    log "  [DRY-RUN] 待传条目约 $NEED —— 未写入任何文件"
    continue
  fi

  mkdir -p "$DST"
  # 与 backup_gaps 同:活目录必须先冻结源清单,只对「开始那一刻存在的文件」负责
  SNAP=$(mktemp -t bkcodex_snap)
  find "$SRC" -type f 2>/dev/null > "$SNAP"
  log "  源清单已冻结: $(wc -l < "$SNAP" | tr -d ' ') 个文件"
  log "  复制中…（活动目录会有 vanished 提示，属正常）"
  rsync -a --partial "$SRC/" "$DST/" 2>>"$LOG"
  RC=$?
  # 24 = some files vanished during transfer（源是活动目录，正常）
  # 码 24 = 传输中文件消失（活动目录正常）；码 23 = 部分条目跳过，在 /private/tmp
  # 上恒定发生——那里有 883 个 Unix 套接字，rsync 报 mkstempsock: Invalid argument。
  # 套接字是 IPC 端点不是数据，跳过是**正确行为**。
  # 2026-09-12 修:原来 23 走 `continue` → **整段校验被跳过**，tmp 分支因此从未被
  # 脚本校验过（当时是人工另跑命令才确认完好）。现在 23/24 都只记录、继续校验。
  if [[ "$RC" -ne 0 && "$RC" -ne 24 && "$RC" -ne 23 ]]; then
    log "  ✗ rsync 退出码 $RC"; FAIL=1; continue
  fi
  [[ "$RC" -eq 24 ]] && log "  （rsync 码 24：传输中有文件消失，活动目录属正常）"
  [[ "$RC" -eq 23 ]] && log "  （rsync 码 23：部分条目跳过，通常是套接字等特殊文件；继续校验）"

  [[ -d "$EXT_VOL" ]] || die "移动硬盘中途掉线"
  DN=$(count_files "$DST"); DB=$(count_bytes "$DST")
  log "  校验 1/3 文件数: 源 $SN vs 盘 $DN  $([[ "$DN" -ge "$SN" ]] && echo ✓ || echo ✗)"
  log "  校验 2/3 字节数: 源 $SB vs 盘 $DB  $([[ "$DB" -ge "$SB" ]] && echo '✓（⊇ 语义）' || echo ✗)"
  DIFF=$(rsync -an "$SRC/" "$DST/" 2>/dev/null | grep -vE '^(sending|sent|total|building|created )' | grep -cv '^\.\?/\?$' || true)
  log "  校验 3/3 rsync 零差异: 待传 $DIFF  $([[ "$DIFF" -le 5 ]] && echo '✓（≤5 为活动文件抖动）' || echo ✗)"

  # MD5 抽样(活动目录会有竞争:抽样瞬间文件正被写入)。
  # 2026-09-12 加定向重试:不符 → 单独重传该文件再比一次,**第二次仍不符才算真失败**。
  # 这样才能把「活文件churn」与「真损坏」分开——此前两次误报都属前者。
  SF=0; SC=0; SR=0
  while IFS= read -r f; do
    rel="${f#"$SRC/"}"
    SC=$((SC+1))
    [[ -f "$f" ]] || { SC=$((SC-1)); continue; }
    if [[ ! -f "$DST/$rel" ]]; then
      SR=$((SR+1)); mkdir -p "$(dirname "$DST/$rel")"
      rsync -a --partial "$f" "$DST/$rel" 2>>"$LOG"
      if [[ -f "$DST/$rel" ]]; then log "    · 盘上原缺失,已定向补传: $rel"
      else log "    ✗ 补传后仍缺失: $rel"; SF=$((SF+1)); fi
      continue
    fi
    if [[ "$(md5 -q "$f" 2>/dev/null)" == "$(md5 -q "$DST/$rel" 2>/dev/null)" ]]; then continue; fi
    # 第一次不符 → 定向重传后复比
    SR=$((SR+1))
    rsync -a --partial "$f" "$DST/$rel" 2>>"$LOG"
    if [[ "$(md5 -q "$f" 2>/dev/null)" == "$(md5 -q "$DST/$rel" 2>/dev/null)" ]]; then
      log "    · 活文件竞争,已定向重传并复比通过: $rel"
    else
      log "    ✗ 重传后仍不符(疑似真损坏): $rel"; SF=$((SF+1))
    fi
  done < <(sort -R "$SNAP" | head -"$MD5_SAMPLE")
  rm -f "$SNAP"
  log "  校验 MD5 抽样: $SC 个, 定向重传 $SR 个, 最终失败 $SF  $([[ "$SF" -eq 0 ]] && echo ✓ || echo ✗)"

  if [[ "$DN" -ge "$SN" && "$DB" -ge "$SB" && "$SF" -eq 0 ]]; then
    log "  ★ $NAME 备份完成并校验通过"
  else
    log "  ✗ $NAME 校验未全过 —— 本机数据请勿删除"; FAIL=1
  fi
done

log ""
log "═══════════════════════════════════════════════════════════"
[[ "$FAIL" -eq 0 ]] && log "全部完成。日志: $LOG" || log "有失败项，详见日志: $LOG"
[[ "$FAIL" -eq 0 ]] && echo "BACKUP_ALL_OK" || echo "BACKUP_HAS_FAILURE"
exit "$FAIL"
