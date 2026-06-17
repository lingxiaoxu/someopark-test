#!/usr/bin/env bash
#
# clean_wf_charts.sh — 删除 walk-forward charts 文件夹的内容（保留空文件夹）
#
# 目标：只删除以下 4 类 charts 文件夹 **内部** 的文件/子目录：
#   1. historical_runs/walk_forward/window*/charts/*
#   2. historical_runs/walk_forward/window*/wf_*/charts/*
#   3. historical_runs/walk_forward_mtfs/window*/charts/*
#   4. historical_runs/walk_forward_mtfs/window*/wf_*/charts/*
#
# 安全约束：
#   - charts 文件夹本身保留（空目录）
#   - 绝不碰 charts 以外的任何文件（xlsx / log / json / csv 等）
#   - 绝不碰 sector_rotation / semiconductor_strategy / vix_chronos2
#   - 用 rm -rf（不经回收站）；执行前先 dry-run 确认
#
# 用法：
#   bash conductor/clean_wf_charts.sh --dry-run   # 只统计，不删除
#   bash conductor/clean_wf_charts.sh              # 执行删除
#   bash conductor/clean_wf_charts.sh --dry-run --verbose  # 列出每个文件夹
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HR="$REPO_ROOT/historical_runs"

DRY_RUN=false
VERBOSE=false
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    --verbose) VERBOSE=true ;;
    *) echo "Unknown arg: $arg"; exit 1 ;;
  esac
done

# ── 严格验证：确认目标目录存在且路径正确 ──────────────────────────────────────
for d in "$HR/walk_forward" "$HR/walk_forward_mtfs"; do
  if [[ ! -d "$d" ]]; then
    echo "ERROR: expected directory not found: $d"
    exit 1
  fi
done

# ── 收集目标 charts 文件夹（精确 4 类 glob，不用 find 避免意外匹配）────────
charts_dirs=()

# 类型 1: walk_forward/window*/charts
for d in "$HR"/walk_forward/window*/charts; do
  [[ -d "$d" ]] && charts_dirs+=("$d")
done

# 类型 2: walk_forward/window*/wf_*/charts
for d in "$HR"/walk_forward/window*/wf_*/charts; do
  [[ -d "$d" ]] && charts_dirs+=("$d")
done

# 类型 3: walk_forward_mtfs/window*/charts
for d in "$HR"/walk_forward_mtfs/window*/charts; do
  [[ -d "$d" ]] && charts_dirs+=("$d")
done

# 类型 4: walk_forward_mtfs/window*/wf_*/charts
for d in "$HR"/walk_forward_mtfs/window*/wf_*/charts; do
  [[ -d "$d" ]] && charts_dirs+=("$d")
done

total=${#charts_dirs[@]}
echo "Charts folders found: $total"

if [[ $total -eq 0 ]]; then
  echo "Nothing to clean."
  exit 0
fi

# ── 安全检查：每个路径必须包含 /historical_runs/ 和 /charts ──────────────────
for d in "${charts_dirs[@]}"; do
  if [[ "$d" != */historical_runs/walk_forward*/window*/charts ]] && \
     [[ "$d" != */historical_runs/walk_forward*/window*/wf_*/charts ]]; then
    echo "SAFETY CHECK FAILED: unexpected path pattern: $d"
    echo "Aborting. No files deleted."
    exit 1
  fi
done
echo "Safety check passed: all $total paths match expected patterns."

# ── 统计当前大小 ─────────────────────────────────────────────────────────────
total_kb=0
for d in "${charts_dirs[@]}"; do
  kb=$(du -sk "$d" 2>/dev/null | awk '{print $1}')
  total_kb=$((total_kb + kb))
  if $VERBOSE; then
    echo "  $(du -sh "$d" 2>/dev/null | awk '{print $1}')\t$d"
  fi
done
total_gb=$(echo "scale=1; $total_kb / 1048576" | bc)
echo "Total size to clean: ${total_gb} GB (${total} folders)"

# ── 执行或 dry-run ───────────────────────────────────────────────────────────
if $DRY_RUN; then
  echo ""
  echo "[DRY-RUN] No files deleted. Run without --dry-run to execute."
  exit 0
fi

echo ""
echo "Deleting contents of $total charts folders (folders kept empty)..."
deleted=0
for d in "${charts_dirs[@]}"; do
  # 删除 charts/ 下的所有内容，但保留 charts/ 目录本身
  # rm -rf "$d"/* 在空目录时会报 "No match" error，用 find 更安全
  find "$d" -mindepth 1 -delete 2>/dev/null
  deleted=$((deleted + 1))
  if (( deleted % 100 == 0 )); then
    echo "  ... cleaned $deleted / $total"
  fi
done

echo "Done. Cleaned $deleted charts folders."

# ── 验证 ─────────────────────────────────────────────────────────────────────
remaining_kb=0
for d in "${charts_dirs[@]}"; do
  kb=$(du -sk "$d" 2>/dev/null | awk '{print $1}')
  remaining_kb=$((remaining_kb + kb))
done
remaining_mb=$(echo "scale=1; $remaining_kb / 1024" | bc)
echo "Remaining size in charts folders: ${remaining_mb} MB (should be ~0)"
echo "Freed approximately: ${total_gb} GB"
