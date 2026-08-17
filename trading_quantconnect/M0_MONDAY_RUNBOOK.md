# M0/Go-Live 周一执行手册(2026-08-17,全部命令在 trading_quantconnect/ 下)

前提: `.env` 已有 QC_USER_ID/QC_API_TOKEN;L-MICRO 节点 idle;
单测 11/11 全绿;ObjectStore `mirror/` 已清空(周日核实)。
环境: `conda run -n someopark_run python ...`(下略)。

## T-0 前置(09:00 ET 前)

```
python exporter.py --dry            # 预览: targets=AISS9+SSRS6+BDC6, 零 pairs 腿
                                    # 缩放镜像: scalars≈{mrpt .59, mtfs .38,
                                    # aiss 2.68, ssrs 1.0, bdc 1.0}, C0≈$6.00M
python qc_api.py                    # 节点应 idle
```
核对 dry 输出的 `would_freeze`(= 当日凌晨管道后的实仓对数;周日晚为
{mrpt:1, mtfs:9},若凌晨有开/平仓数字会变 —— **变了也照走**,golive 冻结的
就是"当下"的既有仓,正是用户规格)。

## T-1 Go-Live(09:05)

```
python exporter.py --golive         # 冻结 legacy + C0=Σequity + 推 v1
cat state/legacy_positions.json     # 人工过目冻结清单
```

## T-2 M0 探针(09:10 部署,09:32 开盘后读结果)

```
python ops/deploy.py --algo probe
# 等 09:32+
python ops/status.py --algo probe --logs 80 | grep "M0-RESULT"
```
判读:
- `R2 objectstore_read=OK version=1` → 传输面成立,继续;
  `KEY_MISSING` → **停**: 换备用通道(live/commands 推送,当天实现),不硬闯;
- `R6 fill ... fee=0.0` → 零费覆盖生效;fee>0 → 对账加精确费用项(plan A-1 预案);
- `R1 fractional status=Invalid` → 残差账为主方案(已实现,无需改);
  非 Invalid → 记录为 upside,BDC 可切原生小数(M4 再切)。

收尾: `python ops/stop.py --algo probe --liquidate`

## T-3 镜像上线(09:45)

```
python ops/deploy.py --algo mirror
# ~1-2 分钟后
python ops/status.py --algo mirror --logs 40
```
预期: 日志 `[MIRROR] cash init → C0≈$6.0M` + `applied v1: ~21 orders`;
portfolio 与 `state/target_portfolio.json` 逐票一致(全票整数股,缩放镜像:
AISS 股数≈inventory×2.68,如 KLAC 1678→4500)。

## T-4 全天值守

```
python exporter.py --loop 60        # 前台常驻(周一先人工跑,稳定后再 launchd)
```
- 若盘中 inventory 变化(如 10:00 新对开仓): ~1-2 分钟内 QC 应出对应订单
  (§3 状态机 RTH 即时分支的首次真实验证);
- legacy 对盘中平仓: exporter 日志 `legacy_alive` 数字下降、版本**不变**、
  QC 零订单 —— 这是"无仓可平不交易"的正确表现,不是故障。

## 回退(任何异常)

```
python ops/stop.py --algo mirror              # 停(保留持仓)
python ops/stop.py --algo mirror --liquidate  # 停+清仓(paper 零成本)
```
exporter 只读持仓文件,停它零影响(防火墙: 反向永不存在)。

## 收盘后(16:10)

- `python ops/status.py --algo mirror > state/eod_snapshot_$(date +%Y%m%d).txt`
- 与 state/target_portfolio.json 人工 eyeball 对账(M4 自动对账前的手工桥)
- 把 M0 三项实测结论写回 plan 附录 A。
