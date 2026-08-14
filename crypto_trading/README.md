<p align="center">
  <img src="../public/SOMEO PARK矢量源文件 Big Square.svg" alt="Someopark" width="160"/>
</p>

<h1 align="center">crypto_trading</h1>
<p align="center"><b>Kalshi Crypto Perpetuals 量化研究与交易框架</b></p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11-blue?logo=python&logoColor=white"/>
  <img src="https://img.shields.io/badge/conda-someopark__run-green?logo=anaconda&logoColor=white"/>
  <img src="https://img.shields.io/badge/strategies-12%20tested-orange"/>
  <img src="https://img.shields.io/badge/data%20streams-5%20live%20recorders-teal"/>
  <img src="https://img.shields.io/badge/tests-318%20green-brightgreen"/>
  <img src="https://img.shields.io/badge/fee%20tiers-T0--T10%20(CFTC)-purple"/>
</p>

---

Kalshi crypto 永续合约(KXBTCPERP 等 13 个市场)的完整研究栈:自建数据录制(5 条实时流,2026-07-07 起不间断)、
PIT 严格的回测/walk-forward 框架、成交感知撮合、以及 **12 个策略 × 3 个费率档位**的系统性验证。
独立于 someopark-test 主项目(配对交易)与 qlib-main(SSRS/AISS),零依赖交叉。

**当前结论(2026-08-10,Tier 4±1 资本规划下):证实可交易策略 = 0;五个观察候选(S1/S8/S9 统计待证 + S3改 结构收入待确认 + **W5 knockdown 回测全过、待 live 捕获率**);
详见[主验证表](#主验证表策略--时间结构--费率档位)。** 本框架的方法论产出已外溢至生产策略
(SSRS/AISS 的同 bar 泄露报告与 DSR 公式修正均源于此)。

---

## 当前状态与实盘就绪度(2026-08-10)

**研究阶段已完结,项目进入观察-等待期。** 12 个策略在 T3/4/5 三档费率下全部完成验证:
证实可交易 = 0;**四个观察候选**(注册日 2026-08-10,配置冻结,只测不选):

| | 冻结内容 | 转正判据 | 到判预估 |
|---|---|---|---|
| W1 / S1 basis | k3.5/abs10/off10/abort20/flowY @T4 | 注册后 n≥30 且 t≥2 | ~10 月中 |
| W2 / S8 Chronos | bolt-base/指数/4h/ctx512/40bps 带 | 注册后 t≥2 **且**注册后均值>0 | ~9 月中起 |
| W3 / S9 24h 动量 | 统一延续/\|z\|≥1/13 市场 | n≥1260 且 t≥2 | ~明年 3 月 |
| **W4 / S3改 同资产 carry** | **常持空 perp+现货多,30d 资金费和止损** | 注册后 ≥60 天 且 t≥2 且 @spot50 净>0 | **~10 月中(或经批准提前小额实盘)** |
| **W5 / knockdown(Poly 逆向复刻)** | **买刚被砸的近ATM二元侧,zone .15-.45/dip5c/tte5-45/深度≥50 张,KXBTC** | **探针捕获率 ≥25% 持续 ≥7 天 且纸面 P&L 达回测 50%**(回测已过全部对抗关:+24.5c/张 t=18.4) | **~8 月下旬(探针跑一周即判)** |

**四个实盘执行模块已建成(`crypto_strategies/live_watch/`),全部默认 DISARMED:**
每次运行都是完整排练——计算真实信号、构造真实订单(含 subaccount/tick/post_only)、写日志,
但**不发送**,除非同时打开两级闸门:①`live_watch/config.yaml` 里该策略 `enabled: true`;
②全局执行闸(prod key + `ALLOW_LIVE_ORDERS=1` + margin)。dry-run 状态自动积累**纸面交易记录**,
与 watchlist 复验互为印证。

```bash
./pipeline.sh watch                    # 四策略跑一轮(dry-run)
./pipeline.sh watch --loop 300         # daemon:W1 每分钟/W2 每5分/W3 每小时/W4 每日(节奏门控)
./pipeline.sh watch --strategy w4 --confirm-spot   # W4 确认外部现货腿成交
./pipeline.sh watchlist                # 月度:四候选冻结配置重测(judgment 输出)
```

安全设计:逐策略 kill-switch(实亏超限自动熄火且不自动复活)、W4 资金费数据陈旧自动拒动、
现货腿永远显式人工确认;安全性质有专门测试锁定(`tests/test_live_watch.py`)。
**五探针体系(每个候选的"回测→实盘"最后一环都有实测仪表)**:W1/W2/W3 = maker 成交率探针
(挂单后用实时 tape + 同一 queue 模型验证,成交才开虚拟仓 → 纸面 P&L 只计 verified fills);
W4 = 资金费/基差 regime 监控;W5 = taker 捕获率探针(信号时刻真实盘口深度)。
**W4 依赖每日 `./pipeline.sh backfill`(资金费新鲜度)。**

等待期动作:录制照常 → 每月 `watchlist` + `tier_study` → 任一候选过线再谈武装。
外生监控不变:kalshi.com/incentives、费率结构、30 天账户量(WC prediction 量计入档位)。

---

## 环境配置

```bash
# 所有 Python 一律用 someopark_run 环境
PYTHONPATH=/Users/xuling/code/someopark-test conda run -n someopark_run --no-capture-output \
  python -m crypto_trading.<module> [args]
```

- **研究/回测不需要任何 API key**(市场数据 keyless;自录数据在本地)
- 实盘需 demo/prod key + `ALLOW_LIVE_ORDERS=1` 双闸(见 `crypto_common/execution.py` 的 gate)
- 守护进程与日常操作统一入口:`./pipeline.sh {poll|strips|record|idxrecord|liqrecord|backup|daily|test|...}`
- 费率档位研究:`CRYPTO_FEE_TIER={0..7}` env(官方档表内置于 `crypto_common/costs.py::FEE_TIERS_BPS`)
- 测试输出隔离:`CRYPTO_SIGNALS_DIR=<dir>` env(严禁测试写生产 `trading_signals/`)

---

## 数据资产

### 自录实时流(2026-07-07 起,5 个 daemon,零缺日)

| 流 | 频率 | 内容 | 路径(price_data/ 下) |
|---|---|---|---|
| poll markets | **10 秒** | 13 perp 的 bid/ask/price/OI/liq_mark/contract_size | `kalshi/perps/poll/prod/markets/` |
| poll orderbook | **10 秒** | 13 perp 的完整 L2 深度 | `kalshi/perps/poll/prod/orderbook/` |
| poll trades | 实时 | 逐笔成交(price/count/taker_side) | `kalshi/perps/poll/prod/trades/` |
| 现货合成指数 | **5 秒** | Coinbase+Kraken+Bitstamp 三所 VWAP(BTC/ETH) | `index_proxy/live/` |
| 事件条快照 | **90 秒** | KXBTC/KXBTCD/KXETH/KXETHD 全行权价阶梯(~188 市场/张)+ spot_est | `kalshi/event_strips/prod/` |
| OKX 清算(**另类数据**) | 事件驱动 | BTC/ETH-USDT 强平逐笔(WS) | `offshore/okx/liquidations/` |

### 回填数据(keyless API,可再生)

| 数据 | 粒度 | 跨度 | 备注 |
|---|---|---|---|
| Kalshi K线 | 1m/1h/1d | 2026-06-03 起 | `end_period_ts` **结束标记**(已实证) |
| Kalshi 资金费 | 8h 周期(04/12/20 UTC) | 全历史 | 结算事件时间,PIT 干净 |
| 现货合成 1 分钟 | 1m | ~2 年 | 交易所**开始标记** → loader 层 +1min 修正 |
| 离岸 K线/资金费(OKX/Kraken) | 1h/1d/8h | ~2 年 | 开始标记(OKX 1d 为香港午夜)→ loader +1h/+24h 修正 |

### 数据卫生(与 PIT 同级的教训,全部在 loader 层强制)

- 哨兵垃圾值 `4.61169e14`(持续 30+ 分钟,局部中位数参照会被污染 → 全局稳健参照)
- `bid=0` 单边盘口(mid=ask/2 伪造 50% 暴跌)→ "两边存在且未交叉"才算可成交报价
- 点差 >500bps 剔除;`_drop_quote_outliers` 见 `crypto_common/loader.py`
- **备份**:`pipeline.sh backup` → `~/crypto_data_backup/`(仓库外,保留 5 份;自录 tape 不可再生,绝不删)

---

## 费率(回测的核心约束)

**永续合约**(CFTC 备案档表,2026-07-08 生效;30 天滚动量 = perps+prediction 合并、maker+taker 都计):

| Tier | 30 天量 | taker | maker | 备注 |
|---|---|---|---|---|
| 0 | $0 | 12.0 bps | 5.0 bps | $1500 散户现实 |
| 3 | ≥$1M | 6.0 | 2.4 | ≈$8.3K 资本(2x,2 legs/天) |
| **4** | **≥$3M** | **5.0** | **2.0** | **资本规划锚点(≈$25K)** |
| 5 | ≥$10M | 4.0 | 1.6 | ≈$83K |

**事件合约**(二元):taker = 0.07×P×(1−P)/张,maker = taker 的 25%。独立于 perp 档表。

**执行现实(自录 tape 实测)**:市场点差 BTC/ETH 1.6-1.8bps、alts 3.6-7.4bps;**maker 恒等式**:
挂 touch 赚半点差 1.58bps − 逆向选择 2.46bps = **费前 −0.9bps**(markout 30s 实测 −2.5~−5.6bps)——
本框架所有 maker 策略死因的定量根源。

---

## 统计方法论

**PIT 纪律**(12+ 处泄露修复的血泪总结,新代码强制):
一切 bar 序列 end-label(`resample(label="right", closed="right")`;交易所 K 线 loader 层 +1 周期);
撮合窗严格 `> post_ts`;确认/过滤只用决策前事件;宇宙/阈值/方向一律 IS 半段选定、OOS 只计量一次;
日频引擎信号 `shift(1)`;禁 bfill;修 loader 后删特征缓存;新回测跑 "±1 bar 泄露对照"。

**显著性栈**:Newey-West HAC t(滞后 ≥ 视界重叠)· purged K-fold CV(embargo ≥ label 视界)·
Deflated Sharpe(BLdP,N·e 修正版)· PBO/CSCV · **日块 bootstrap(块 = 独立单位:市场/结算事件,
按日分块挡不住日内伪重复——p 0.001→0.28 的教训)** · Wilson 区间 · Bonferroni(全网格计数)。

**执行真实性栈**:queue 感知 maker 撮合(排真实展示量之后)· 被动失败后按 **cross 时刻不利侧** taker ·
markout(+30s/2m/5m)逆向选择诊断 · 中价结果一律标注"非可交易主张"。

**验证设计**:walk-forward 逐日推进 · 安慰剂窗口(N4 的邻近 5 窗全负)· 阳性对照(Chronos 预测波动率
IC 0.41-0.59 证明管线健康)· 跨市场广度 · 尾部/集中度(剔最佳日/winsorize)· 前注册(文献窗口、冻结配置)。

---

## 主验证表:策略 × 时间结构 × 费率档位

净额单位:bps/笔(除注明);t = Newey-West;全部 fill-aware/交易级口径;阈值按档在 IS 半段重选。
佐证 = `trading_signals/research/` 下的输出文件(❖ 标注者原始件在 /tmp 已轮转,可由对应模块一键再生)。

| # | 策略 | 决策频率 | 持仓 | **替代视界试过吗** | T3 | T4 | T5 | 判决 @T4±1 | 佐证 |
|---|---|---|---|---|---|---|---|---|---|
| S1 | basis 选择性(45 配置) | 1 分钟 | 信号驱动,**实测 <15 分钟**(超时 15/30/60/120 全等价——从不触发) | ✅ 超时扫描本轮补;更长视界的 basis 信号由图谱 5m-4h 覆盖(全死) | +$0.28 t2.2 n12 | **+$0.48 t3.4 n18** | +$0.58 t4.1 | ⚠️ 转正未证(best-of-45,45-trial DSR 不过) | `tier_study_20260810_204428.json` |
| S2 | 事件 gap → perp 腿 | ~90 秒 | ≤30/60/120 分钟(**已扫**;>2h 不可能,合约到期) | ✅ 全部可行持仓已扫 | — | — | — | ☠️ 恒等式:fill-aware 毛利 −2~−12bps,费≥0 ⇒ 净<0 | `fee_defeat_20260726_102821.json` |
| S3 | funding carry → **同资产现货对冲版**(空 KXBTCPERP+现货多 BTC,常持) | 每日监控(30d 资金费和止损) | **数月常持**(entries=1) | ✅ 调仓 0/7/30 天已扫;v1/v2 规则死于换手(13×/4×,已披露),v3 常持 | +3.49% | **+3.49%/yr**(现货 RT 20bps;50bps 时 +3.19) | +3.50% | ⚠️ **W4 观察中**:毛日 NW-t **+3.00**,残余 3.2% 波动(跨资产版 22.6%),所有档×费景净正;57 天单 regime,需外部现货账户 | `spot_hedged_*.json`·`carry_hold_20260731_115921.json`(旧版对照) |
| S4 | 清算级联 fade | 10 秒 | ≤15 分钟(TP 50% 回补/时间止损) | ✅ 其机制在 2-4h 由 stress-family/episode 测过:合并 −6.05bps,死 | −$0.017 | −$0.019 | −$0.013 | ☠️ 三档全负;live 锚下 ≥15bps 事件≈0 | `tier_study_*.json`·`episode_test_20260728_084337.json`·`stress_family_20260728_084137.json` |
| S5 | perp 轮动 | 每日 00:05 UTC | 数天-周(daily/**weekly 双频在 WF 参数集已测**) | ✅ | −0.5% | −0.4% | −0.3% | ☠️ 费曾是主出血(T0 −3.8%)但选股**费前**毁值(等权篮 +7.1%) | 复跑:`CRYPTO_FEE_TIER=4 python -m …perp_rotation.run_backtest` |
| S6 | ML 方向(手工特征) | 5 分钟 | 恰 15 分钟(60m 变体同死) | ✅ 特征全家在图谱 5m→4h 扫过:1816 格,IS前10% OOS 仅 +0.61bps | −5.0 t−8.0 | −4.1 t−6.5 | −3.2 t−5.0 | ☠️ 毛利≈−1bps | `tier_study_*.json`·`ml_gate3_20260727_*.json`·`horizon_atlas_20260728_083950.json`·`pbo_20260731_113940.json` |
| S7 | 多特征组合 conviction | 5 分钟 | 恰 4 小时 | ✅ 2h(全负)/4h/**8h(本轮补:噪声,最好 t0.44)** | −20 | −27 | −17 | ☠️ 新 10 天毛转负(近期衰减) | `fillaware_4h_20260731_113504.json`·`combination_20260731_112724.json`·`adjudicate_4h_20260731_113101.json` |
| S8 | Chronos(bolt-base,指数输入) | 5 分钟预测(≈4.8 笔/天) | 恰 4 小时 | ✅ 2h(死)/4h(唯一毛利为正)/**8h 毛 −5.6/12h 毛 −16.9/24h 净全负**——预测力边界恰在模型原生 64 步窗(≈5.3h),更长视界更差 | +7.7 t1.2 | **+8.6 t1.4** | +9.5 t1.5 | ⚠️ 转正未证;**新 10 天全档为负(−12~−19)= 衰减**;t=2 需再 ~33 天 | `chronos_horizons_20260810_220325.json`(长视界)·tier 件再生:`CRYPTO_FEE_TIER=4 …research_chronos` |
| S9 | 24h 动量(统一延续方向) | 1 小时 | 恰 24 小时 | ✅ 8h/12h(死)/24h(峰)/**48h/72h(本轮补:动量反转,毛 −26/−69)** | +9.9 t0.7 | +10.7 t0.8 | +11.5 t0.8 | ⚠️ 转正未证;σ=239bps ⇒ t=2 需 n≈1,260(~7 月) | `overnight_20260731_203509.json`·`tier_study_*.json` |
| S10 | 二元 above X | TTE 240/120/60/30/15/5 分钟检查点 | 持至结算(5 分钟-4 小时) | ✅ 全 TTE 检查点即视界扫描 | — | — | — | ☠️ 独立费制;**零费下界仍 −2.0c/张**(伪重复+陈旧报价双杀) | 模块 `event_binary/research_calibration.py` ❖·`crypto-dev/12` §18(41 万结算样本) |
| N2 | 跳变 lead-lag | 事件驱动(5s 流,30s 跳≥15/25bps) | 1 分钟(3/5/10m + **本轮 30/60m:更差**) | ✅ | 最好 −6.4 | −4.4 | **−2.4 t−1.1** | ☠️ 中价漂移 +6~8 真实(t 8.1)但 taker 点差吃掉一半 | `tier_study_20260810_204428.json`(new_candidates) |
| N4 | 21-23 UTC 时段窗口 | 固定日程(每天 1 次) | 恰 2 小时 | ✅ 邻近 5 个安慰剂窗全负(=窗口变体已测);不再 re-mine | −19.8 | −17.8 | −15.8 | ☠️ 效应在 7/7 后样本消失(毛 −7.8;58 天 +17.3 由 6 月扛) | `tier_study_20260810_204428.json`(new_candidates) |

| **W5** | **knockdown 复刻**(逆向 Polymarket 盈利账户) | 90 秒快照 | **5-45 分钟持有至结算** | ✅ tte 5-20/20-60 已扫,9 配置 OOS 全正 | — | **+24.5c/张**(独立费制,硬化后 canonical) | — | 🟢 **回测过全部对抗关**(持续性 94%/独立结算 99.5%/L2 深度门 n=1875/35 天 35 正);待 live 捕获率探针 | `knockdown_*.json`·`crypto-dev/15`(如写) |

**横向工具研究(不是策略,是把上表钉死的证据)**:
L2 盘口失衡(545MB 从未用过的数据,出清:执行过滤 +0.2bps 不显著、20/20 信号家族深负,maker 恒等式由此得出)
→ `book_imbalance_20260810_200144.json`;选参持续性(Spearman IS↔OOS = +0.065,top10 胜 100% 随机组合但仍差
盈亏平衡)→ `wf_selection_20260728_084808.json`·`selection_scaling_20260728_085735.json`;跨所资金费差分
(机制真实、前缀选名后 OOS ≈0、合规阻断)→ `cross_venue_20260726_221541.json`。

### 三个 ⚠️ 候选的证活条件(全部未达,勿上线)

| 候选 | 当前 | 证活需要 | 红旗 |
|---|---|---|---|
| S1 | T4 +$0.48,t 3.4,n=18 | **前注册复验**脱离 best-of-45(冻结该配置,新数据 ≥30 笔 t≥2) | 两天 1 笔,n 增长极慢 |
| S8 | T4 +8.6bps,t 1.4,n=140 | 再 ~33 天且**新时段止跌回正** | 新 10 天 −12~−19(衰减) |
| S9 | T4 +10.7bps,t 0.8,n=192 | n≈1,260(~7 个月) | 6 月-7 月初 regime 依赖 |

**近期衰减模式(三处独立同现)**:Chronos 新时段负、S7 毛转负、N4 消失——市场变效或 6 月-7 月初为异常 regime,
均值外推需谨慎。

---

## 资本 / 档位测算(2 倍杠杆)

| 档 | 30 天量 | 日均名义 | 2 legs/天策略资本 | 自持能力 |
|---|---|---|---|---|
| T3 | ≥$1M | $33K | ≈$8.3K | Chronos 单跑($3.5K/笔)即可 |
| **T4** | **≥$3M** | **$100K** | **≈$25K** | **Chronos+S9 同跑 $5K/笔 ≈$4M/30d**;单策略需超 touch 深度(BTC ~$4.6K)拆单 |
| T5 | ≥$10M | $333K | ≈$83K | 深度不支持,需专门滚量 |

世界杯 prediction 交易量计入同一档位(perps+prediction 合并)。**注意:滚量机器结构成立,但发动机(信号)未证活。**

---

## 监控与复跑

| 周期 | 命令 | 看什么 |
|---|---|---|
| 每月 | `python -m crypto_trading.crypto_strategies.research_watchlist` | **四候选前注册观察(注册日 2026-08-10,配置冻结)**:W1/S1 post n≥30 且 t≥2;W2/S8 post t≥2 且 post 均值>0;W3/S9 n≥1260 且 t≥2;**W4/S3改 post≥60 天且 t≥2 且 @spot50 净>0 且 30d 资金费仍正;**W5 探针捕获率≥25%×7 天+纸面达回测 50%** |
| 每月 | `python -m crypto_trading.crypto_strategies.research_tier_study` | 三档全策略;S8 新时段符号 |
| 每月 | `python -m crypto_trading.crypto_strategies.research_selection_scaling` | Spearman ≥0.15 且 top10 t≥2 → 才进 fill-aware |
| 每月 | `python -m …funding_carry.research_cross_venue` | 跨所差分是否扩大 |
| 外生 | kalshi.com/incentives(需登录) | Liquidity Incentive Program 是否覆盖 crypto perp(按挂单深度付 $1-1000/市场/天 → 改写 maker 恒等式) |
| 退出判据 | 2026-10 月底 | Spearman 仍 <0.1 且 S8 未证活 → 转纯费率监控 |

## 模块地图

```
crypto_common/    loader(PIT 修正+数据卫生) costs(官方档表+env) execution(实弹闸)
                  backtest/{fill_model,daily_engine,intraday_sim} trade_stats(NW/DSR/purged)
                  walk_forward run_wf validate smart_select regime bracket(+watcher)
crypto_strategies/  basis_meanrev/ event_perp/ funding_carry/ liq_reversion/
                    ml_directional/ perp_rotation/ event_binary/
                    research_*.py(horizon_atlas·episode·stress·wf_selection·selection_scaling·
                    combination·adjudicate_4h·fillaware_4h·chronos(+diag)·overnight·
                    book_imbalance·tier_study·conditioning·true_anchor·cross_venue)
ops/              backup_data disk_monitor make_launchd
tests/            318 green
```

## 文档索引(crypto-dev/,按时间)

00-08 建设计划 · `09_ml_directional.md` 三闸 ML · `10_pit_audit_20260728.md` **PIT 大审计(12+ 泄露)** ·
`11_horizon_investigation.md` 视界图谱+数据卫生 · `12_how_to_make_money.md` carry/Chronos/二元+根本诊断 ·
`13_max_effort_audit.md` 尽力度复审(盘口/lead-lag/时段/文献) · `14_tier_study.md` **档位重测(本表出处)**
