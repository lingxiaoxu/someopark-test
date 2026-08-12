#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# online_microfootball.sh — 微足球模拟上线标准流程（box B /games → 前端 + DFM 扩散）
#
# 用途:box B 上有新的比赛模拟系列(10 或 15 场)完成后,在本机 Mac 一键执行:
#   [0] 预检   — box B 连通性、/games 逐对阵场数清点
#   [1] 同步   — 双跳 ssh 拉取全部对阵到本地镜像 + 重建 microfootball_index.json
#   [2] DFM    — extract(语料张量+审计) → production(每对阵3种子训练+5000场引导生成
#                + 事件层 + 双视图,官方带指纹快照) → dfm_index.json
#   [3] 校验   — 镜像场数=box B 场数;manifest 对阵数、dfm_index 对阵数一致
#   [4] 构建   — npm run build:wc
#   [5] 部署   — npx firebase deploy --only hosting  →  https://someopark.web.app
#
# 网络拓扑(重要,勿改):
#   Mac(本机) --Tailscale--> box A ed9f (someoparkdgx25@100.76.95.43, key=nvsync.key)
#   box A     --cable------> box B ec7f (someoparkdgx25@169.254.229.3, box A 上免密)
#   生产模拟只在 box B 的 ~/mirofootball/games/ 下;box B 只读,绝不写入。
#
# 环境:
#   * 同步/索引: node (scripts/sync_microfootball.mjs, 必须在 someo-park 目录下运行)
#   * DFM:      conda env `someopark_run`(由 sync 脚本内部调 conda run 执行
#               dfm/football/extract.py + production.py;唯一写 production_runs/ 的入口)
#   * 部署:     firebase CLI(npx, 已登录)
#
# 用法:
#   prediction_market/ops/online_microfootball.sh            # 完整流程
#   prediction_market/ops/online_microfootball.sh --check    # 只做预检+现状核对,不执行
#   prediction_market/ops/online_microfootball.sh --skip-deploy   # 跑完不部署
#   prediction_market/ops/online_microfootball.sh --skip-dfm      # 只同步资产,不跑 DFM
# ═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

REPO="/Users/xuling/code/someopark-test"
FRONT="$REPO/someo-park-investment-management"
DFM_RUNS="$REPO/dfm/football/production_runs"
DFM_INDEX="$FRONT/public/data/dfm_index.json"
MIRROR="$FRONT/server/sim_assets/microfootball"
KEY="$HOME/Library/Application Support/NVIDIA/Sync/config/nvsync.key"
BOXA="someoparkdgx25@100.76.95.43"
BOXB="someoparkdgx25@169.254.229.3"
GAMES='~/mirofootball/games'
TS="$(date +%Y%m%d_%H%M%S)"
LOG="/tmp/mf_online_${TS}.log"

CHECK_ONLY=0; SKIP_DEPLOY=0; SKIP_DFM=0; MIN_GAMES=10
for arg in "$@"; do
  case "$arg" in
    --check)       CHECK_ONLY=1 ;;
    --skip-deploy) SKIP_DEPLOY=1 ;;
    --skip-dfm)    SKIP_DFM=1 ;;
    # 显式豁免:系列本来就设计得短(如 2026-08-12 的两场 5 场系列)时由人declare
    # "这就是完整的"。默认 10 的半成品守卫对未来一切照旧。
    --min-games=*) MIN_GAMES="${arg#*=}" ;;
    *) echo "未知参数: $arg (可用: --check --skip-deploy --skip-dfm --min-games=N)"; exit 2 ;;
  esac
done

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\033[31m✗ %s\033[0m\n' "$*"; exit 1; }
ok()   { printf '\033[32m✓ %s\033[0m\n' "$*"; }

# 双跳只读执行(box B)。注意:任何写操作都不允许出现在这里。
boxb() { ssh -i "$KEY" -o ConnectTimeout=15 "$BOXA" "ssh -o ConnectTimeout=15 $BOXB '$1'"; }

# ── [0] 预检 ──────────────────────────────────────────────────────────────────
say "[0] 预检"
[ -f "$KEY" ]            || fail "找不到 Tailscale ssh key: $KEY"
command -v node  >/dev/null || fail "缺 node"
command -v conda >/dev/null || fail "缺 conda(DFM 需要 someopark_run env)"
conda env list | grep -q "someopark_run" || fail "conda env someopark_run 不存在"
[ -f "$FRONT/scripts/sync_microfootball.mjs" ] || fail "sync 脚本不存在"

BOXB_TOTAL="$(boxb "find $GAMES -mindepth 2 -maxdepth 2 -type d | wc -l" | tr -d '[:space:]')"
[ -n "$BOXB_TOTAL" ] && [ "$BOXB_TOTAL" -gt 0 ] || fail "box B 连不上或 /games 为空"
ok "box B 可达,/games 生产模拟共 $BOXB_TOTAL 场"

echo "── box B 逐对阵场数:"
boxb "cd $GAMES && for d in */; do echo \"  \$(find \"\$d\" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d \" \")  \$(basename \"\$d\")\"; done"
LOW="$(boxb "cd $GAMES && for d in */; do n=\$(find \"\$d\" -mindepth 1 -maxdepth 1 -type d | wc -l); [ \"\$n\" -lt $MIN_GAMES ] && echo \"\$d:\$n\"; done; true")"
[ -z "$LOW" ] || fail "有对阵场数<$MIN_GAMES(可能还没跑完,先别上线;若系列本就短,用 --min-games=N 显式豁免): $LOW"
NEWEST="$(boxb "ls -dt $GAMES/*/*/ | head -1")"
echo "── 最新一场: $NEWEST"

LOCAL_TOTAL="$(find "$MIRROR" -mindepth 2 -maxdepth 2 -type d 2>/dev/null | wc -l | tr -d '[:space:]')"
echo "── 本地镜像现有 $LOCAL_TOTAL 场(同步后应为 $BOXB_TOTAL)"
if [ "$CHECK_ONLY" = 1 ]; then
  echo "── 当前 dfm_index:"
  python3 -c "
import json
d=json.load(open('$DFM_INDEX'))
print('   snapshot', d['ts'], '·', len(d['matchups']), '个对阵 ·', d['corpus_sha1'])" 2>/dev/null || echo "   (无)"
  ok "--check 完成,未执行任何操作"; exit 0
fi

# ── [1]+[2] 同步 + DFM(sync 脚本一体完成;必须在 FRONT 目录下运行)────────────
say "[1/2] 同步 + DFM 放大(约 15-20 分钟,日志: $LOG)"
SYNC_ARGS=""; [ "$SKIP_DFM" = 1 ] && SYNC_ARGS="--skip-dfm"
( cd "$FRONT" && node scripts/sync_microfootball.mjs $SYNC_ARGS ) 2>&1 | tee "$LOG"

# ── [3] 校验(三道关口,任一失败即停,不部署)──────────────────────────────────
say "[3] 校验"
LOCAL_TOTAL="$(find "$MIRROR" -mindepth 2 -maxdepth 2 -type d | wc -l | tr -d '[:space:]')"
[ "$LOCAL_TOTAL" = "$BOXB_TOTAL" ] || fail "镜像场数 $LOCAL_TOTAL ≠ box B $BOXB_TOTAL(同步不完整)"
ok "镜像完整: $LOCAL_TOTAL 场"

if [ "$SKIP_DFM" = 0 ]; then
  grep -q "^\[extract\] $BOXB_TOTAL sims" "$LOG" || fail "extract 语料场数与 box B 不符(见日志)"
  MANIFEST="$(ls "$DFM_RUNS"/manifest_*.json | sort | tail -1)"
  python3 - "$MANIFEST" "$DFM_INDEX" <<'EOF'
import json, sys
mf = json.load(open(sys.argv[1])); idx = json.load(open(sys.argv[2]))
n_mf, n_idx = len(mf["files"]), len(idx["matchups"])
assert idx["ts"] == mf["ts"], f"dfm_index 快照 {idx['ts']} != manifest {mf['ts']}"
assert n_mf == n_idx, f"manifest {n_mf} 对阵 != dfm_index {n_idx}"
print(f"✓ DFM 快照 {mf['ts']} · {n_mf} 个对阵 · 语料 {mf['corpus']['sha1']}")
EOF
  echo "── 各对阵 DFM 定价(engine / anchored W-D-L):"
  grep -E "src=[0-9]+ sims" "$LOG" | sed 's/^/  /'
fi

# ── [4] 构建 ──────────────────────────────────────────────────────────────────
say "[4] 构建前端"
( cd "$FRONT" && npm run build:wc ) 2>&1 | tail -2

# ── [5] 部署 ──────────────────────────────────────────────────────────────────
if [ "$SKIP_DEPLOY" = 1 ]; then
  say "[5] 已按 --skip-deploy 跳过部署。手动部署: cd $FRONT && npx firebase deploy --only hosting"
else
  say "[5] 部署 Firebase hosting"
  ( cd "$FRONT" && npx firebase deploy --only hosting ) 2>&1 | tail -3
fi

say "完成 ✅  $BOXB_TOTAL 场 / $(ls "$DFM_RUNS"/manifest_*.json | wc -l | tr -d ' ') 份快照在库 · https://someopark.web.app"
echo "日志: $LOG"
