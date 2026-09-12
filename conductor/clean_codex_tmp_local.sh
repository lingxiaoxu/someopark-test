#!/usr/bin/env bash
#
# clean_codex_tmp_local.sh — 删除本机上已备份的 Codex 快照与 /tmp 临时产物
# （与 clean_old_wf_windows.sh / clean_old_mlruns.sh 同一模板的第三组；2026-09-11 新增）
# 配对的备份脚本：conductor/backup_codex_tmp_to_external.sh
#
# 删除范围（严格限定，绝不越界）：
#   ~/.codex/backups/<子目录>        ← Codex CLI 的整仓自动快照，纯历史副本
#   /private/tmp/<顶层条目>          ← 临时工作目录（需 --tmp 显式开启）
#
# **绝不触碰**：
#   - ~/.codex 下除 backups/ 以外的一切：sessions/、thread_history_*.sqlite、
#     plugins/、config —— 这些是**正在使用的会话历史与配置**，删了 Cursor 的
#     Codex 扩展会丢对话记录（实测有 5 个进程持有这些文件的句柄）
#   - /private/tmp/claude-501 下**当前会话**的目录（删了本工具链立刻失效）
#   - 任何有进程持有文件句柄的目录（lsof 判定）
#   - 移动硬盘上的任何内容（本脚本对移动硬盘只读）
#
# 删除前逐个校验（任一不过 → 跳过该条目，不删）：
#   1. 移动硬盘上存在同名目录
#   2. 备份文件数 ≥ 本机（⊇ 语义：备份是只增不减的归档，可以更多，不能更少）
#   3. 备份字节数 ≥ 本机
#   4. 无进程占用（lsof +D）
#   ※ 套接字/命名管道不计入比对 —— rsync 无法复制它们（mkstempsock），
#     它们是 IPC 端点不是数据，实测 /private/tmp 下有 883 个。
#
# 其他安全措施：
#   - 移动硬盘未挂载 → 直接退出，不删任何东西
#   - 删除过程中复查挂载状态，掉线立即中止
#   - assert_deletable 硬闸门：路径必须匹配允许的前缀，/Volumes/ 一律 FATAL
#   - rm -rf 不进回收站，故默认先跑 --dry-run 确认
#   - 全程日志留档到 conductor/logs/
#
# 用法：
#   bash conductor/clean_codex_tmp_local.sh --dry-run          # 演练（务必先跑）
#   bash conductor/clean_codex_tmp_local.sh                    # 只删 ~/.codex/backups
#   bash conductor/clean_codex_tmp_local.sh --tmp              # 同时清 /private/tmp
#   bash conductor/clean_codex_tmp_local.sh --dry-run --tmp --verbose
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXT_VOL="/Volumes/Someo Park PRO-BLADE"
CODEX_SRC="/Users/xuling/.codex/backups"
CODEX_BAK="$EXT_VOL/.codex/backups"
TMP_SRC="/private/tmp"
TMP_BAK="$EXT_VOL/tmp"
LOG_DIR="$REPO_ROOT/conductor/logs"
TS="$(date '+%Y%m%d_%H%M%S')"
LOG="$LOG_DIR/clean_codex_tmp_${TS}.log"
DRY_RUN=false
VERBOSE=false
DO_TMP=false

# 当前 Claude Code 会话目录名：删掉它本工具链立刻失效，硬编码为保护项。
CURRENT_SESSION="59510526-28d9-4a88-92e0-81aab1434f15"

mkdir -p "$LOG_DIR"
log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }
die() { log "FATAL: $*"; exit 9; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --verbose) VERBOSE=true; shift ;;
    --tmp)     DO_TMP=true; shift ;;
    *) die "未知参数: $1" ;;
  esac
done

# ── 硬闸门：只允许删这两个前缀下的东西，移动硬盘一律拒绝 ──────────────────
assert_deletable() {
  local p="$1"
  [[ "$p" == /Volumes/* ]] && die "闸门拦截：企图删除移动硬盘路径 $p"
  case "$p" in
    "$CODEX_SRC"/*) ;;
    "$TMP_SRC"/*)   ;;
    *) die "闸门拦截：路径超出允许范围 $p" ;;
  esac
  [[ "$p" == "$CODEX_SRC" || "$p" == "$TMP_SRC" ]] && die "闸门拦截：不允许删除根容器 $p"
  return 0
}

count_files() { find "$1" -type f 2>/dev/null | wc -l | tr -d ' '; }
count_bytes() { find "$1" -type f -exec stat -f %z {} + 2>/dev/null | awk '{s+=$1} END{print s+0}'; }
busy() { [[ "$(lsof +D "$1" 2>/dev/null | tail -n +2 | wc -l | tr -d ' ')" != "0" ]]; }

[[ -d "$EXT_VOL" ]] || die "移动硬盘未挂载：$EXT_VOL —— 不删任何东西"

log "═══════════════════════════════════════════════════════════"
log "  Codex 快照 / tmp 清理  $(date '+%F %T %Z')"
log "  判据: 移动硬盘上有同名备份且 文件数与字节数 ≥ 本机（⊇）且 无进程占用"
log "  模式: $([[ "$DRY_RUN" == true ]] && echo 'DRY-RUN（只统计，不删除）' || echo 正式删除)"
log "  /private/tmp: $([[ "$DO_TMP" == true ]] && echo '纳入清理' || echo '不动（需 --tmp 开启）')"
log "═══════════════════════════════════════════════════════════"

TOTAL_KB=0; N_DEL=0; N_SKIP=0
declare -a DELLIST=()

scan_group() {
  local GNAME="$1" SRC="$2" BAK="$3" PROTECT_SESSION="$4"
  [[ -d "$SRC" ]] || { log "  源不存在，跳过: $SRC"; return; }
  [[ -d "$BAK" ]] || { log "  ✗ 备份目录不存在，整组跳过: $BAK"; return; }
  log ""
  log "── $GNAME ──"
  local d bn sc bc sb bb kb
  for d in "$SRC"/*; do
    [[ -e "$d" ]] || continue
    bn="$(basename "$d")"
    # 符号链接不跟随、不删
    if [[ -L "$d" ]]; then log "  ⚠ 跳过符号链接: $bn"; N_SKIP=$((N_SKIP+1)); continue; fi
    # 只处理目录；顶层散落文件保持不动（体积小、可能是别的程序在用）
    if [[ ! -d "$d" ]]; then $VERBOSE && log "  · 跳过非目录: $bn"; continue; fi
    # 当前会话目录：硬保护
    if [[ "$PROTECT_SESSION" == "yes" && "$bn" == *"$CURRENT_SESSION"* ]]; then
      log "  🔒 保护(当前会话): $bn"; N_SKIP=$((N_SKIP+1)); continue
    fi
    if [[ ! -d "$BAK/$bn" ]]; then
      log "  ✗ 备份中不存在，跳过: $bn"; N_SKIP=$((N_SKIP+1)); continue
    fi
    sc=$(count_files "$d");     bc=$(count_files "$BAK/$bn")
    sb=$(count_bytes "$d");     bb=$(count_bytes "$BAK/$bn")
    if [[ "$bc" -lt "$sc" ]]; then
      log "  ✗ 备份文件数少于本机($sc vs $bc)，跳过: $bn"; N_SKIP=$((N_SKIP+1)); continue
    fi
    if [[ "$bb" -lt "$sb" ]]; then
      log "  ✗ 备份字节数少于本机($sb vs $bb)，跳过: $bn"; N_SKIP=$((N_SKIP+1)); continue
    fi
    if busy "$d"; then
      log "  ⚠ 有进程占用，跳过: $bn"; N_SKIP=$((N_SKIP+1)); continue
    fi
    assert_deletable "$d"
    kb=$(du -sk "$d" 2>/dev/null | cut -f1)
    TOTAL_KB=$((TOTAL_KB + kb)); N_DEL=$((N_DEL+1))
    DELLIST+=("$d")
    $VERBOSE && log "  ✓ 待删(已验证备份): $bn  文件 $sc  $(echo "$kb" | awk '{printf "%.2f GB", $1/1048576}')"
  done
}

scan_group "~/.codex/backups" "$CODEX_SRC" "$CODEX_BAK" "no"
[[ "$DO_TMP" == true ]] && scan_group "/private/tmp" "$TMP_SRC" "$TMP_BAK" "yes"

log ""
log "  待删 $N_DEL 个目录，约 $(echo "$TOTAL_KB" | awk '{printf "%.1f", $1/1048576}') GB"
log "  校验未过/受保护而跳过: $N_SKIP 个"

if [[ "$DRY_RUN" == true ]]; then
  log ""
  log "[DRY-RUN] 未删除任何文件。确认无误后去掉 --dry-run 执行。"
  log "  日志: $LOG"
  echo "DRY_RUN_DONE"
  exit 0
fi

log ""
log "  开始删除…"
DONE=0
for p in "${DELLIST[@]:-}"; do
  [[ -n "$p" ]] || continue
  [[ -d "$EXT_VOL" ]] || die "移动硬盘中途掉线 —— 已删 $DONE 个后中止"
  assert_deletable "$p"
  rm -rf "$p" && DONE=$((DONE+1))
  [[ $((DONE % 20)) -eq 0 ]] && log "    …已删 $DONE / $N_DEL"
done

log ""
log "═══════════════════════════════════════════════════════════"
log "★ 清理完成：删除 $DONE 个目录，释放约 $(echo "$TOTAL_KB" | awk '{printf "%.1f", $1/1048576}') GB"
log "  本机剩余空间: $(df -h /System/Volumes/Data | tail -1 | awk '{print $4}')"
log "  日志: $LOG"
log "═══════════════════════════════════════════════════════════"
echo "CLEAN_DONE"
