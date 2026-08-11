# PLAN — 暴力参数扫描(全组合模拟版,已备妥未运行)

登记日 2026-08-11。用户指示:计划先落档,过几天再跑(时长过大)。

## 目标

对 12 个可搜市场的**每一组非默认参数**,跑一次**完整 75 天全组合 walkforward**
(逐日、全系列、全闸门、其他系列照常走日选器,仅目标系列注入该组参数),
以组合级真值取代 `/tmp/cross75.py` 逐事件近似的两处已测偏差:

- 逐事件重放忽略组合层效应(同日风险上限、跨周期 `already_open`、churn 守卫、bankroll 演化)。
  实测:KXWTIW 默认格 −13.9%(逐事件) vs −27.7%(全模拟) —— 能源周频事件密,偏差为真;
  KXJOBLESSCLAIMS 两法精确一致(事件稀疏,组合效应为零)。
- 三流在全模拟中共享一个钱包与风险额度,逐事件版不共享。

## 规模与机制(已就绪)

| 项 | 值 |
|---|---|
| 单元数 | **716**(12 市场全部非默认参数组;默认组=正典 `d75:model:end2026-08-04`,不重跑) |
| 单次成本 | ~13 分钟(完整 75 天模拟) |
| 总时长 | 串行 ≈155h;**3 并行 ≈52h** |
| 机制 | `walkforward.run(param_override={series: params}, hash_tag="po-<series>-<idx>")` — commit d3xxxx 已实现:override 只作用于目标系列、优先于日选器;hash_tag 进 cfg_hash,716 行 experiments 各自可寻址,不碰正典行;无标签的 override 直接拒跑 |
| 驱动 | `/tmp/brute75.py <worker_idx> <n_workers>` — 按事件数多的市场优先(NATGASW/WTIW→claims→…→月频最后);断点续跑(按 hash 标签查已完成);逐单元追加 `/tmp/brute75_w<i>.jsonl`,每行含目标系列三流 n/staked/realized/roi + 组合 hybrid ROI |
| 启动 | `for w in 0 1 2; do PYTHONPATH=$PWD conda run -n someopark_run --no-capture-output python /tmp/brute75.py $w 3 & done`(建议避开凌晨 MRPT/MTFS 管线时段起跑) |
| 清理 | 结束后 `DELETE FROM experiments WHERE name='daily_walkforward' AND config_hash LIKE '%:po-%'`(可选;716 行无害但杂) |

## 判读协议(先写死,免得看到结果再挑)

1. 主判据:目标系列 **hybrid 流**(prod 规则)逐事件配对 PnL,`dsr.select` 折减,
   搜索宽度=该市场全部单元数。n<12 的市场(全部月频)**预期依旧无法进入检验**——
   本扫描对它们的价值仅是把逐事件近似的数字换成组合真值,不产生采纳判决。
2. 逐事件近似 vs 全模拟的**逐单元差异分布**单独报告(它本身是"何时可以用便宜方法"的度量)。
3. 任何"最优参数"仍只能**提名**进 PREREGISTER 前向检验;本扫描不直接改 prod。
   (2026-08-11 用户已按叉积近似结果先行采纳一批参数,见 `manual_params` 机制——
   本扫描完成后应回头核对那批采纳在组合真值下是否仍是各自市场的 argmax。)

## 依赖与风险

- `/tmp/grid75.py` 定义参数空间(SPACES/CAP/build);若正式化应迁入 `research/param_space.py`。
- 52 小时内 db 会被 716 次模拟反复读 + 每日管线正常写;WAL + busy_timeout 已验证共存,
  但凌晨管线时段单次模拟可能拖长至 ~20 分钟。
- 期间若日选器采纳了新参数(每日 `param_select`),不影响本扫描(override 优先),
  但会使"正典 run"与扫描单元的对照基线漂移——完成后如需重对照,重跑正典 run 一次即可。
