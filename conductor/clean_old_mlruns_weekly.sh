#!/usr/bin/env bash
# clean_old_mlruns_weekly.sh — launchd 入口:滚动 14 天保留期的 mlruns 清理
#
# 为什么存在:2026-09-11 磁盘写满打死四个录制器、丢了 12.9 小时不可再取的
# 15 分钟 tape,根因是 mlruns 无人管地涨到 113.8 GB(默认季度截止等于一个
# 季度不清)。本包装把截止改为"14 天前",与 9/11 人工抢救(12 天)同一口径
# 但更保守;底层脚本自带全部安全闸门——移动硬盘未挂载即退出、备份逐 run
# 校验(文件数+字节数全等)才删、回测在跑即中止、/Volumes 路径硬禁删。
# 即:这里只删「本机上已在移动硬盘验证存在完整副本」且 14 天以上的 run。
set -uo pipefail
CUTOFF="$(date -v-14d '+%Y-%m-%d 13:00')"
exec bash /Users/xuling/code/someopark-test/conductor/clean_old_mlruns.sh --cutoff "$CUTOFF"
