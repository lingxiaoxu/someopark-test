#!/usr/bin/env bash
#
# clean_old_mlruns.sh — 删除本机上已备份的旧 MLflow run 目录
# （以 clean_old_wf_windows.sh 为模板的平行脚本，目标换成两个 mlruns/）
#
# 删除范围（严格限定，绝不越界）：
#   mlruns/<实验号>/<32位hex run目录>/            ← 仅 run 目录及其内容
#   qlib-main/mlruns/<实验号>/<32位hex run目录>/  ← 同上
#
# **绝不触碰**：
#   - qlib-main/mlruns/mlflow.db（MLflow 追踪数据库 —— 删了 MLflow 就废了）
#   - 实验目录下任何非 32 位 hex 命名的条目（meta 文件、.DS_Store 等）
#   - mlruns 顶层的非实验条目、任何符号链接
#   - 移动硬盘上的任何内容（本脚本对移动硬盘只读）
#
# 时间判据：
#   run 目录的**创建时间**（birthtime，stat -f %B）≤ 截止时刻 → 候选删除。
#   截止时刻默认 = 上一季度末的前一天 13:00 美东时间。
#   （例：2026 年 Q3 内运行 → 上季末 2026-06-30 → 截止 2026-06-29 13:00 ET）
#
# 删除前逐个校验（任一不过 → 跳过该 run，不删）：
#   1. 移动硬盘备份中存在同名 run 目录
#   2. 备份内文件数与本机完全相同
#   3. 备份内总字节数与本机完全相同
#   （run 数量以万计，校验用「单遍索引 + awk 联接」而非逐个访问 ——
#     逐个访问曾因移动硬盘中途掉线导致全部误判）
#
# 其他安全措施：
#   - 移动硬盘未挂载 → 直接退出，不删任何东西
#   - 写入进程检查：回测/pipeline 在跑则中止（--force 可越过）
#   - 删除过程中周期性复查挂载状态，掉线立即中止
#   - assert_deletable 硬闸门：/Volumes/ 路径或超出允许范围 → FATAL 整体中止
#   - rm -rf 不进回收站，故默认先跑 --dry-run 确认
#   - 全程日志留档到 logs/
#
# 注意：删除 run 目录后，mlflow.db 里对应的 run 记录仍在（MLflow UI 里这些
#       run 的 artifacts 会显示缺失）。数据本体已在移动硬盘，需要时可拷回。
#
# 用法：
#   bash conductor/clean_old_mlruns.sh --dry-run              # 演练（务必先跑）
#   bash conductor/clean_old_mlruns.sh                        # 正式删除
#   bash conductor/clean_old_mlruns.sh --cutoff "2026-06-29 13:00"  # 自定义截止
#   bash conductor/clean_old_mlruns.sh --dry-run --verbose    # 逐个列出跳过原因
#
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXT_VOL="/Volumes/Someo Park PRO-BLADE"
BAK_ROOT="$EXT_VOL/code/someopark-test"
LOG_DIR="$REPO_ROOT/conductor/logs"
TS="$(date '+%Y%m%d_%H%M%S')"
LOG="$LOG_DIR/clean_mlruns_${TS}.log"

# 两个 mlruns 根（相对 REPO_ROOT；备份端路径一一对应）
ROOTS=("mlruns" "qlib-main/mlruns")

DRY_RUN=false
VERBOSE=false
FORCE=false
CUTOFF_STR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --verbose) VERBOSE=true; shift ;;
    --force)   FORCE=true; shift ;;
    --cutoff)  [[ $# -ge 2 ]] || { echo "ERROR: --cutoff 需要参数"; exit 1; }; CUTOFF_STR="$2"; shift 2 ;;
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
  CUTOFF_DATE=$(date -j -v-1d -f "%Y-%m-%d" "$QY-$QEND" "+%Y-%m-%d" 2>/dev/null)
  CUTOFF_STR="$CUTOFF_DATE 13:00"
fi
CUTOFF=$(TZ=America/New_York date -j -f "%Y-%m-%d %H:%M" "$CUTOFF_STR" +%s 2>/dev/null)
if [[ -z "$CUTOFF" ]]; then
  echo "ERROR: 无法解析截止时刻：$CUTOFF_STR （格式应为 \"YYYY-MM-DD HH:MM\"）"; exit 1
fi

log "═══════════════════════════════════════════════════════════"
log "旧 MLflow run 清理  $(date '+%F %T %Z')"
log "  截止时刻: $(TZ=America/New_York date -r "$CUTOFF" '+%F %T %Z')  (epoch $CUTOFF)"
log "  判据: run 目录创建时间(birthtime) ≤ 截止 且 移动硬盘上校验通过"
$DRY_RUN && log "  模式: DRY-RUN（只统计，不删除）"
log "═══════════════════════════════════════════════════════════"

# ── 前置检查 ────────────────────────────────────────────────────────────────
for r in "${ROOTS[@]}"; do
  [[ -d "$REPO_ROOT/$r" ]] || { log "ERROR: 源目录不存在：$REPO_ROOT/$r"; exit 1; }
done
if ! mount | grep -q "$EXT_VOL"; then
  log "ERROR: 移动硬盘未挂载：$EXT_VOL"
  log "       没有备份就绝不删除本机数据。请插上硬盘后重试。"
  exit 1
fi
for r in "${ROOTS[@]}"; do
  [[ -d "$BAK_ROOT/$r" ]] || { log "ERROR: 移动硬盘上找不到备份目录：$BAK_ROOT/$r"; log "       请先运行 backup_mlruns_to_external.sh"; exit 1; }
done
log "✓ 移动硬盘已挂载，两个备份目录可访问"

# 写入进程检查：mlruns 是活跃目录（每天数百个新 run）。新 run 因时间戳不会入选，
# 但「快照校验」与「实际删除」之间若有任务往旧 run 追加文件，会被无备份删掉。
# 有任务在跑就不清理，风险归零。
BUSY_PATTERNS='SectorRotationBatchRun|daily_backtest\.sh|SemiconductorBatchRun|aiss_batch|DailySignal|VolumePrediction|pipeline_runner\.sh'
BUSY=$(ps aux | grep -E "$BUSY_PATTERNS" | grep -v grep \
       | grep -v "prediction_market_macro" | grep -v "$(basename "$0")" || true)
if [[ -n "$BUSY" ]]; then
  log "⚠ 检测到可能写入 mlruns 的进程："
  echo "$BUSY" | awk '{print "    PID "$2"  "$11" "$12" "$13}' | tee -a "$LOG"
  if $FORCE; then
    log "  --force 指定，继续执行"
  else
    log "ERROR: 为避免与写入任务竞争，已中止。等任务跑完再执行，或用 --force 强制。"
    exit 2
  fi
else
  log "✓ 无关键写入进程运行"
fi

check_mount() {
  if ! mount | grep -q "$EXT_VOL"; then
    log "ERROR: 移动硬盘掉线！立即中止，剩余 run 保持原样。"
    exit 3
  fi
}

# ── 铁律：本脚本永远不得删除移动硬盘上的任何数据 ────────────────────────────
# 任何 rm 目标必须先过这个闸门：/Volumes/ 下或不在允许范围内 → 硬退出（不是跳过）。
# 允许范围 = <REPO>/mlruns/<数字实验号>/<32位hex> 或 <REPO>/qlib-main/mlruns/同上。
assert_deletable() {
  local p="$1"
  case "$p" in
    /Volumes/*)
      log "FATAL: 拒绝删除移动硬盘路径（备份数据只可写入不可删除）: $p"
      log "       脚本立即中止。"
      exit 9 ;;
  esac
  if ! echo "$p" | grep -qE "^$REPO_ROOT/(mlruns|qlib-main/mlruns)/[0-9]+/[0-9a-f]{32}$"; then
    log "FATAL: 路径不在允许删除范围内: $p"
    log "       仅允许 mlruns/<实验号>/<32位hex run>，脚本立即中止。"
    exit 9
  fi
}

# ── 单遍索引：目录树 → 「run名|文件数|字节数」──────────────────────────────
# 一次 find 汇总而非逐 run 访问：既快（万级 run），也把移动硬盘暴露时间降到最低。
index_tree() {  # $1=实验目录  → 输出 "名称|文件数|字节数"
  find "$1" -mindepth 2 -type f -exec stat -f '%z %N' {} + 2>/dev/null | awk -v base="$1/" '
    { size=$1; path=substr($0, index($0," ")+1)
      rel=substr(path, length(base)+1)
      slash=index(rel,"/"); if (slash==0) next
      top=substr(rel,1,slash-1)
      cnt[top]++; bytes[top]+=size }
    END { for (t in cnt) printf "%s|%d|%d\n", t, cnt[t], bytes[t] }'
}

TOTAL_DEL=0; TOTAL_BYTES=0; TOTAL_FILES=0; TOTAL_SKIP=0

for R in "${ROOTS[@]}"; do
  SRC="$REPO_ROOT/$R"
  BAK="$BAK_ROOT/$R"
  log ""
  log "══ $R ══"

  # 实验目录 = 数字命名的一级子目录（跳过 mlflow.db / .DS_Store / 符号链接 / 其他一切）
  for EXPD in "$SRC"/*/; do
    [[ -d "$EXPD" ]] || continue
    if [[ -L "${EXPD%/}" ]]; then log "  ⚠ 跳过符号链接: $EXPD"; continue; fi
    EXP=$(basename "$EXPD")
    [[ "$EXP" =~ ^[0-9]+$ ]] || { log "  跳过非实验条目: $R/$EXP"; continue; }
    [[ -d "$BAK/$EXP" ]] || { log "  ⚠ 备份中无实验 $R/$EXP，整个实验跳过不删"; continue; }

    check_mount
    log "── $R/$EXP ──"

    BAK_IDX=$(mktemp); SRC_IDX=$(mktemp); BAK_LS=$(mktemp); BIRTHS=$(mktemp)
    DELLIST=$(mktemp); RAW=$(mktemp)

    # 快照备份侧（一次性采集，降低掉线窗口）
    ls -1 "$BAK/$EXP" > "$BAK_LS" 2>/dev/null
    index_tree "$BAK/$EXP" > "$BAK_IDX"
    check_mount
    # 快照本机侧
    index_tree "$SRC/$EXP" > "$SRC_IDX"
    # run 目录创建时间（一次 find 批量 stat；-type d 天然排除符号链接）
    find "$SRC/$EXP" -mindepth 1 -maxdepth 1 -type d -exec stat -f '%B %N' {} + 2>/dev/null > "$BIRTHS"

    # awk 联接：时间过滤 + 三重备份校验，产出待删清单与统计。
    # 输入按首字符打标（B=备份索引 L=备份目录清单 S=本机索引 T=创建时间）——
    # 序号法在某输入为空时会错位，标记法安全。
    # 输出走【单流前缀协议】：所有行进 stdout，行首 DEL/MSG/STATS 区分用途，
    # bash 侧 grep 分流 —— 之前"清单走 stderr、消息走 stdout"的双流设计在
    # verbose 时会把消息混进统计文件，read 读到垃圾（已修复的真实 bug）。
    { sed 's/^/B /' "$BAK_IDX"; sed 's/^/L /' "$BAK_LS"; sed 's/^/S /' "$SRC_IDX"; sed 's/^/T /' "$BIRTHS"; } | \
    awk -v cutoff="$CUTOFF" -v srcexp="$SRC/$EXP/" '
      { tag=substr($0,1,1); rest=substr($0,3) }
      tag=="B" { split(rest, a, "|"); bcnt[a[1]]=a[2]; bbyt[a[1]]=a[3]; next }
      tag=="L" { bls[rest]=1; next }
      tag=="S" { split(rest, a, "|"); scnt[a[1]]=a[2]; sbyt[a[1]]=a[3]; next }
      tag=="T" {
        sp=index(rest," "); birth=substr(rest,1,sp-1)+0; path=substr(rest,sp+1)
        name=substr(path, length(srcexp)+1)
        # 只处理 32 位 hex 命名的 run 目录（等价模板的 window* 过滤）
        if (name !~ /^[0-9a-f]{32}$/) { next }
        if (birth > cutoff) { next }
        cand++
        sc = (name in scnt) ? scnt[name] : 0
        sb = (name in sbyt) ? sbyt[name] : 0
        if (sc == 0) {
          # 本机空 run 目录：只要备份中存在同名目录即可删
          if (name in bls) { ok++; print "DEL " path }
          else { skip++; printf "MSG ✗ 空run备份缺失，跳过: %s\n", name }
          next
        }
        if (!(name in bcnt)) { skip++; printf "MSG ✗ 备份中不存在，跳过: %s\n", name; next }
        if (bcnt[name] != sc) { skip++; printf "MSG ✗ 文件数不符(%d vs %d)，跳过: %s\n", sc, bcnt[name], name; next }
        if (bbyt[name] != sb) { skip++; printf "MSG ✗ 字节数不符，跳过: %s\n", name; next }
        ok++; files+=sc; bytes+=sb
        print "DEL " path
      }
      END { printf "STATS %d %d %d %d %d\n", cand+0, ok+0, skip+0, files+0, bytes+0 }
    ' > "$RAW"

    grep '^DEL ' "$RAW" | sed 's/^DEL //' > "$DELLIST" || true
    # 跳过原因是关键安全信号：非 verbose 也显示（最多 10 条 + 汇总），verbose 全量
    MSG_N=$(grep -c '^MSG ' "$RAW" || true)
    if [[ "${MSG_N:-0}" -gt 0 ]]; then
      if $VERBOSE; then grep '^MSG ' "$RAW" | sed 's/^MSG /  /' | tee -a "$LOG"
      else
        grep '^MSG ' "$RAW" | head -10 | sed 's/^MSG /  /' | tee -a "$LOG"
        [[ "$MSG_N" -gt 10 ]] && log "  …(共 $MSG_N 条跳过记录，--verbose 查看全部)"
      fi
    fi
    STATS_LINE=$(grep -m1 '^STATS ' "$RAW" | sed 's/^STATS //' || true)
    read -r n_cand n_ok n_skip n_files n_bytes <<< "${STATS_LINE:-0 0 0 0 0}"
    n_cand=${n_cand:-0}; n_ok=${n_ok:-0}; n_skip=${n_skip:-0}; n_files=${n_files:-0}; n_bytes=${n_bytes:-0}
    log "  符合日期条件: $n_cand 个 run"
    log "  备份校验通过、将删除: $n_ok 个（文件 $n_files 个，约 $(echo "scale=1; $n_bytes/1073741824" | bc) GB）"
    [[ $n_skip -gt 0 ]] && log "  ⚠ 校验未过、保留不删: $n_skip 个"

    if ! $DRY_RUN && [[ $n_ok -gt 0 ]]; then
      log "  开始删除…"
      i=0
      while IFS= read -r target; do
        [[ -n "$target" ]] || continue
        assert_deletable "$target"
        rm -rf "$target"
        i=$((i+1))
        if (( i % 2000 == 0 )); then log "    …已删 $i / $n_ok"; check_mount; fi
      done < "$DELLIST"
      log "  ✓ 已删除 $i 个 run"
    fi

    TOTAL_DEL=$((TOTAL_DEL+n_ok)); TOTAL_BYTES=$((TOTAL_BYTES+n_bytes))
    TOTAL_FILES=$((TOTAL_FILES+n_files)); TOTAL_SKIP=$((TOTAL_SKIP+n_skip))
    rm -f "$BAK_IDX" "$SRC_IDX" "$BAK_LS" "$BIRTHS" "$DELLIST" "$RAW"
  done
done

log ""
log "═══════════════════════════════════════════════════════════"
if $DRY_RUN; then
  log "[DRY-RUN] 未删除任何文件"
  log "  将删除 $TOTAL_DEL 个 run（$TOTAL_FILES 个文件，约 $(echo "scale=1; $TOTAL_BYTES/1073741824" | bc) GB）"
  [[ $TOTAL_SKIP -gt 0 ]] && log "  另有 $TOTAL_SKIP 个因备份校验未通过而保留"
  log "  确认无误后，去掉 --dry-run 执行。"
else
  log "★ 清理完成：删除 $TOTAL_DEL 个 run，释放约 $(echo "scale=1; $TOTAL_BYTES/1073741824" | bc) GB"
  [[ $TOTAL_SKIP -gt 0 ]] && log "  $TOTAL_SKIP 个因备份校验未通过而保留（未删）"
  log "  本机剩余空间: $(df -h /System/Volumes/Data | tail -1 | awk '{print $4}')"
fi
log "  日志: $LOG"
log "═══════════════════════════════════════════════════════════"
