#!/bin/bash
# qcmirror exporter 常驻入口(launchd 调用;plan §8.5 运维化)
# 防火墙:只读五个持仓文件 + state/,只写 state/ + QC ObjectStore。
echo "[wrapper] start $(date) user=$(whoami) home=$HOME"
cd /Users/xuling/code/someopark-test/trading_quantconnect || { echo "[wrapper] cd FAILED"; exit 1; }
exec /Users/xuling/miniforge3/bin/conda run -n someopark_run --no-capture-output \
    python exporter.py --loop 60
