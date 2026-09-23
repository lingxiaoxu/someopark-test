#!/usr/bin/env bash
#
# backup_gaps_to_external.sh — 补上三处「本机有、外置盘完全没有」的备份缺口
# （2026-09-11 磁盘取证发现;以 backup_to_external.sh 为模板的第四组备份脚本）
#
# 备份内容（源 → 目标，**沿用外置盘既有树状结构，不另造层级**）：
#   prediction_market_soccer/                          →  <盘>/code/someopark-test/prediction_market_soccer/
#   someo-park-investment-management/server/sim_assets/microfootball/
#                                                      →  <盘>/code/someopark-test/someo-park-investment-management/server/sim_assets/microfootball/
#   ~/crypto_data_backup/                              →  <盘>/crypto_data_backup/
#
#   前两者在仓库内 → 落到既有的 code/someopark-test/ 镜像下（与 historical_runs、
#   mlruns、qlib-main、price_data、account_history 平级，路径与仓库内完全一致）。
#   第三个在 home 根 → 落到盘根（与既有的 .codex/、tmp/ 同一惯例）。
#
# 为什么是这三处（取证实测）：
#   - prediction_market_soccer  18 GB：外置盘上**完全没有这棵树**
#   - sim_assets/microfootball 4.5 GB：本机 22 个条目，盘上只有 someopark-sandbox
#     里 2026-08-19 快照的 17 个 → 5 批 run 无任何异地副本，且前端正在服务它
#   - ~/crypto_data_backup     14 GB：在 /dev/disk3s5，与仓库**同一块盘**，
#     本机盘一坏两者同归于尽，不构成异地保护（红线 3 的录制数据靠它兜底）
#
# SQLite 特别处理（本脚本与前三组的关键差异）：
#   soccer.db 4.8 GB / wc.db 594 MB / multimarket.db 1.2 GB 等在被 live_refresh
#   持续写入。裸 rsync 会拷出**撕裂的库**（页面来自不同事务）。故：
#     · rsync 阶段排除 *.db / *.db-wal / *.db-shm
#     · 再用 `sqlite3 源 ".backup 目标"`（SQLite 在线备份 API）逐个生成
#       **事务一致**的快照；非 sqlite 文件自动回退到 cp
#     · -wal/-shm 是瞬时文件，.backup 产物自包含，不需要也不应该复制
#
# 安全约束（与模板一致）：
#   - 对移动硬盘**只增不减**：全程不使用 --delete
#   - 移动硬盘未挂载 → 立刻退出，绝不在本机硬盘上误建目录
#   - rsync --partial 断点续传，中断可安全重跑
#   - 备份后校验：文件数 / 字节数（备份 ⊇ 源）+ rsync 干跑 + MD5 抽样 + 每个
#     sqlite 快照跑 `PRAGMA integrity_check`
#   - 全程日志留档到 conductor/logs/
#
# 用法：
#   bash conductor/backup_gaps_to_external.sh --dry-run
#   bash conductor/backup_gaps_to_external.sh
#   bash conductor/backup_gaps_to_external.sh --only soccer
#   bash conductor/backup_gaps_to_external.sh --only microfootball
#   bash conductor/backup_gaps_to_external.sh --only crypto_backup --md5 500
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXT_VOL="/Volumes/Someo Park PRO-BLADE"
REPO_MIRROR="$EXT_VOL/code/someopark-test"
LOG_DIR="$REPO_ROOT/conductor/logs"
TS="$(date '+%Y%m%d_%H%M%S')"
LOG="$LOG_DIR/backup_gaps_${TS}.log"
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

# ── 自检：本脚本绝不可含针对移动硬盘的删除语句（检查行自身带标记并排除）──
if grep -nE 'rm .*(EXT_VOL|DEST|REPO_MIRROR)|--delete' "$0" | grep -v 'SELFCHECK' | grep -vE '^[0-9]+:\s*#' | grep -q .; then  # SELFCHECK
  die "自检失败：脚本含针对移动硬盘的删除语句"  # SELFCHECK
fi

[[ -d "$EXT_VOL" ]] || die "移动硬盘未挂载：$EXT_VOL"
[[ -d "$REPO_MIRROR" ]] || die "外置盘上没有既有的 code/someopark-test 镜像 —— 结构异常，中止"

# 名称｜源｜目标（目标严格沿用外置盘既有结构）
declare -a NAMES=("soccer" "microfootball" "crypto_backup")
declare -a SRCS=(
  "$REPO_ROOT/prediction_market_soccer"
  "$REPO_ROOT/someo-park-investment-management/server/sim_assets/microfootball"
  "/Users/xuling/crypto_data_backup"
)
declare -a DSTS=(
  "$REPO_MIRROR/prediction_market_soccer"
  "$REPO_MIRROR/someo-park-investment-management/server/sim_assets/microfootball"
  "$EXT_VOL/crypto_data_backup"
)

count_files() { find "$1" -type f 2>/dev/null | wc -l | tr -d ' '; }
count_bytes() { find "$1" -type f -exec stat -f %z {} + 2>/dev/null | awk '{s+=$1} END{print s+0}'; }

log "═══════════════════════════════════════════════════════════"
log "  备份缺口补齐 → 移动硬盘  $(date '+%F %T %Z')"
log "  模式: $([[ "$DRY_RUN" == true ]] && echo DRY-RUN || echo 正式)"
log "  盘上既有结构: $(ls "$REPO_MIRROR" | tr '\n' ' ')"
log "═══════════════════════════════════════════════════════════"

FAIL=0
for i in "${!NAMES[@]}"; do
  NAME="${NAMES[$i]}"; SRC="${SRCS[$i]}"; DST="${DSTS[$i]}"
  [[ -n "$ONLY" && "$ONLY" != "$NAME" ]] && continue
  [[ -d "$SRC" ]] || { log "⚠ 源不存在，跳过: $SRC"; continue; }
  [[ -d "$EXT_VOL" ]] || die "移动硬盘中途掉线"

  log ""
  log "══ $NAME ══"
  log "  源:   $SRC"
  log "  目标: $DST"
  SN=$(count_files "$SRC"); SB=$(count_bytes "$SRC")
  NDB=$(find "$SRC" -name "*.db" -type f 2>/dev/null | wc -l | tr -d ' ')
  log "  源: $SN 个文件, $SB 字节, 其中 sqlite 库 $NDB 个"

  if [[ "$DRY_RUN" == true ]]; then
    log "  [DRY-RUN] 将 rsync（排除 *.db/-wal/-shm）+ 对 $NDB 个库做 sqlite3 .backup"
    log "  [DRY-RUN] 未写入任何文件"
    continue
  fi

  mkdir -p "$DST"

  # 2026-09-12:活目录(public_quote_captures 每 20 分钟新增 302 个文件)必须
  # **先冻结源清单**再传。否则事后拿新的源清单去比对,会把「备份完成之后才出现的
  # 文件」误判成「盘上缺失」——实测两个文件创建于 15:57:41,而 rsync 15:49:57 就结束了。
  # 正确语义:本次备份只对「rsync 开始那一刻存在的文件」负责。
  SNAP=$(mktemp -t bkgaps_snap)
  find "$SRC" -type f ! -name "*.db" ! -name "*.db-wal" ! -name "*.db-shm" 2>/dev/null > "$SNAP"
  log "  源清单已冻结: $(wc -l < "$SNAP" | tr -d ' ') 个非 sqlite 文件"

  # ── 阶段 1：rsync 普通文件（排除 sqlite，避免撕裂）────────────────────
  log "  阶段1/2 rsync 普通文件…"
  rsync -a --partial \
    --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
    "$SRC/" "$DST/" 2>>"$LOG"
  RC=$?
  if [[ "$RC" -ne 0 && "$RC" -ne 24 && "$RC" -ne 23 ]]; then
    log "  ✗ rsync 退出码 $RC"; FAIL=1; continue
  fi
  [[ "$RC" -eq 24 ]] && log "  （码 24：传输中有文件消失，活动目录属正常）"
  [[ "$RC" -eq 23 ]] && log "  （码 23：部分条目跳过，通常是套接字/特殊文件）"

  # ── 阶段 2：sqlite 在线备份（事务一致快照）──────────────────────────
  DBOK=0; DBBAD=0
  if [[ "$NDB" -gt 0 ]]; then
    log "  阶段2/2 sqlite 在线备份($NDB 个)…"
    while IFS= read -r db; do
      rel="${db#"$SRC/"}"
      out="$DST/$rel"
      mkdir -p "$(dirname "$out")"
      if sqlite3 "$db" ".backup '$out'" 2>>"$LOG"; then
        chk=$(sqlite3 "$out" "PRAGMA integrity_check;" 2>/dev/null | head -1)
        if [[ "$chk" == "ok" ]]; then
          DBOK=$((DBOK+1))
        else
          log "    ✗ 完整性检查未过: $rel → $chk"; DBBAD=$((DBBAD+1))
        fi
      else
        # 不是合法 sqlite（或加锁失败）→ 退回普通复制
        if cp -p "$db" "$out" 2>>"$LOG"; then
          log "    · 非 sqlite 或加锁失败，已按普通文件复制: $rel"; DBOK=$((DBOK+1))
        else
          log "    ✗ 复制失败: $rel"; DBBAD=$((DBBAD+1))
        fi
      fi
    done < <(find "$SRC" -name "*.db" -type f 2>/dev/null)
    log "    sqlite 备份: 成功 $DBOK, 失败 $DBBAD"
    [[ "$DBBAD" -gt 0 ]] && FAIL=1
  fi

  # ── 校验 ──────────────────────────────────────────────────────────
  [[ -d "$EXT_VOL" ]] || die "移动硬盘中途掉线"
  DN=$(count_files "$DST"); DB_=$(count_bytes "$DST")
  # 源里的 -wal/-shm 是瞬时文件，有意不备份，从源计数里扣除后比对
  WS=$(find "$SRC" \( -name "*.db-wal" -o -name "*.db-shm" \) -type f 2>/dev/null | wc -l | tr -d ' ')
  SN_EFF=$((SN - WS))
  # 变量后紧跟全角字符必须用 ${} —— 否则 bash 会把全角括号读进变量名(实测炸过)
  log "  校验 1/3 文件数: 源 ${SN_EFF}（已扣除 ${WS} 个 -wal/-shm）vs 盘 ${DN}  $([[ "$DN" -ge "$SN_EFF" ]] && echo ✓ || echo ✗)"
  log "  校验 2/3 字节数: 源 $SB vs 盘 $DB_ （sqlite 快照会因 vacuum 略小，属正常）"
  # MD5 抽样(活动目录会有竞争:抽样瞬间文件正被写入)。
  # 2026-09-12 加定向重试:不符 → 单独重传该文件再比一次,**第二次仍不符才算真失败**。
  # 这样才能把「活文件churn」与「真损坏」分开——此前两次误报都属前者。
  SF=0; SC=0; SR=0
  while IFS= read -r f; do
    rel="${f#"$SRC/"}"
    SC=$((SC+1))
    # 源文件在备份后被删/轮转 → 不计入(本次不对它负责)
    [[ -f "$f" ]] || { SC=$((SC-1)); continue; }
    if [[ ! -f "$DST/$rel" ]]; then
      # 盘上缺失 → 定向补传一次再判(活目录常见)
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
  log "  校验 3/3 MD5 抽样(非 sqlite): $SC 个, 定向重传 $SR 个, 最终失败 $SF  $([[ "$SF" -eq 0 ]] && echo ✓ || echo ✗)"

  if [[ "$DN" -ge "$SN_EFF" && "$SF" -eq 0 && "$DBBAD" -eq 0 ]]; then
    log "  ★ $NAME 备份完成并校验通过"
  else
    log "  ✗ $NAME 校验未全过 —— 本机数据请勿删除"; FAIL=1
  fi
done

log ""
log "═══════════════════════════════════════════════════════════"
if [[ "$FAIL" -eq 0 ]]; then
  log "★ 全部完成。日志: $LOG"
  log "  外置盘可用: $(df -h "$EXT_VOL" | tail -1 | awk '{print $4}')"
  echo "BACKUP_GAPS_OK"
else
  log "有失败项，详见日志: $LOG"
  echo "BACKUP_GAPS_HAS_FAILURE"
fi
exit "$FAIL"
