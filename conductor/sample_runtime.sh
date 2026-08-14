#!/usr/bin/env bash
#
# sample_runtime.sh — 跑批期间的轻量运行时采样(诊断 walk-forward 变慢)
#
# 背景: 2026-08-11 起 WF 逐晚变慢(STEP3 5h57m → 8h45m)。已排除:
#   - 计算量: 同一 param 同样 397 行输入,8/08 用 63s、8/13 用 81s(+29%)
#   - pair universe: 恒定 15 pairs
#   - 窗口故障: 九窗产物数一致(各 75 文件),param 耗时均匀无卡顿
#   - charts 堆积: 逐窗耗时震荡而非单调递增(且 8/13 已清理 58GB)
# 事后取证不足以定位 → 需运行中采样。
#
# 只读采样,绝不干预管道。每 N 秒记一行到 conductor/logs/runtime_sample_<ts>.log
#
# 用法:
#   bash conductor/sample_runtime.sh &            # 默认 300s 一次,随管道跑
#   bash conductor/sample_runtime.sh 60 &         # 自定义间隔
#   pkill -f sample_runtime.sh                    # 停止
set -uo pipefail

INTERVAL="${1:-300}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$REPO_ROOT/conductor/logs/runtime_sample_$(date '+%Y%m%d_%H%M%S').log"

echo "# sample_runtime start $(date '+%F %T'), interval=${INTERVAL}s" >> "$LOG"
echo "# ts | load1 load5 | mem_free% | swap_used | cpu_user% cpu_sys% cpu_idle% | thermal | wf_pid_cpu%" >> "$LOG"

while true; do
    ts=$(date '+%H:%M:%S')

    # 负载
    load=$(sysctl -n vm.loadavg 2>/dev/null | awk '{print $2, $3}')

    # 内存空闲百分比(memory_pressure 比 vm_stat 直观)
    memfree=$(memory_pressure -Q 2>/dev/null | awk '/percentage/{print $NF}')
    [ -z "$memfree" ] && memfree="?"

    # swap 使用(内存压力的硬指标: 非 0 说明在换页,严重拖慢)
    swap=$(sysctl -n vm.swapusage 2>/dev/null | awk '{print $6}')

    # CPU 分布
    cpu=$(top -l 1 -n 0 2>/dev/null | awk '/CPU usage/{print $3, $5, $7}')

    # 散热/降频: pmset 有则用(Apple Silicon 上 CPU_Speed_Limit < 100 = 降频)
    therm=$(pmset -g therm 2>/dev/null | awk -F': *' '/CPU_Speed_Limit/{print $2}')
    [ -z "$therm" ] && therm="na"

    # walk-forward 进程自身的 CPU 占用(是它没吃满 CPU,还是被别人抢)
    wfcpu=$(ps -A -o %cpu,command 2>/dev/null \
            | grep -E "MRPTWalkForward|MTFSWalkForward|PortfolioMRPTRun|PortfolioMTFSRun" \
            | grep -v grep | awk '{s+=$1} END{printf "%.1f", s+0}')

    # 同时在跑的其他重进程(抢占嫌疑犯) — 取 CPU 前 3 且 >20%
    others=$(ps -A -o %cpu,comm 2>/dev/null | sort -rn | awk '$1>20{print $2":"$1}' \
             | grep -vE "WalkForward|PortfolioM" | head -3 | tr '\n' ' ')

    echo "$ts | $load | $memfree | $swap | $cpu | $therm | wf=${wfcpu}% | others: ${others:-none}" >> "$LOG"

    sleep "$INTERVAL"
done
