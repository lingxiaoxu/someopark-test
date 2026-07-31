# PLAN_AUDIT.md — 实施状态总账(PLAN §17 契约文档;2026-07-31 定稿)

> 本文档回答一个问题:**PLAN.md v3 + PLAN_EXTENSION.md 里的每一项,现在到底做没做。**
> 依据:三轮深度审计(含盲测交叉验证)+ 2026-07-31 的 11 步集中实施(git 提交
> 83d5e5d…HEAD,每步独立 commit)。测试基线:81/81 绿,全部 tmp/内存库。

## 一、本轮(2026-07-31)完成清单

| 步 | 交付 | 关键文件 |
|---|---|---|
| 0 | 整仓纳入 git(71 文件)+ 前端 PnL 字段 bug + Coverage 色映射 + 5 处虚标注释/模型卡 | .gitignore, MacroArtifact.tsx |
| 1 | **§9.5/§7-bis 闸门评估器**:DM 检验(HLN)+bootstrap CI+十分位校准表+drift_check+**决策/PnL 全链路重放**(过生产 decide())+`series_gate` 行首次有了写入者 | research/eval.py, backtest.py |
| 2 | **安全网**:circuit_breaker 真函数+滚动20熔断;health 5 种 break 探测器(Brier双窗/CRPS+2σ/熵收敛/特征z=4/Chronos NaN)+台账3条随机自检;红灯自动触发熔断(铁律10);exits 红灯强平真代码;decide_all 熔断阻断+staleness 硬门(§8.2-5);size≤20%depth(铁律5前半);**tests/test_pit.py 四件套×8模型=25测试** | risk.py, health.py, exits.py, decision.py, test_pit.py |
| 3 | **三源 ensemble**(log-pool,Bates-Granger 逆MSPE动态权重+trimmed守卫,shadow)+**isotonic 校准层**(Kelly 前,OOS pairs 拟合,identity 回退)+**MIDAS bridge nowcast**(GASREGW→CPI/ICSA→NFP/ICSA→U3,shadow)+source_scores 表 | ensemble.py, calibration.py, bridge.py |
| 4 | 熵门(平坦分布→PASS)+**逐 strike edge_capture 记忆**(<0.4 且 n≥8 剔除,铁律5后半)+分歧门改用 devig 概率(§11 原口径) | decision.py, capture.py |
| 5 | **Fed 声明抓取**(5 份真实声明入库)+statement_risk/news_flags 接电(event_flags 表→家族级闸门收紧)+BEA_GDP/EIA 日历+**releases.actual_ts 复活**(23条回填)+postponed 侦测+BLS 排期联网漂移检查 | fed_text.py, llm.py, calendars.py |
| 6 | **事件窗加密轮询**:tick linger ≤840s,窗内 5 分钟快照、±10分钟 1 分钟,T+3m reassess 准时执行(§19-9 补上处理器) | tick.py |
| 7 | **微观结构**:spread 宽→市场权重减半+sanity 回退 cost;favorite-longshot 修正(isotonic on (市场概率,结果));**ACI conformal 仓位节流** | conformal.py, ensemble.py |
| 8 | **model/gdp.py**(GDPNow 锚+历史误差σ,实测 2026-Q3 4.95±1.30)+**nowcast_vintages 复活**(800 条真实 GDPNow vintage)+quarterly lane | gdp.py, nowcast.py |
| 9 | 结算 **z 归因**(luck/gray/model_miss)+误差归因周聚类;**发现并修复 settle 重复结算 bug**(open_positions 未排除 settle_note,KXFED 重复7次;台账保持 append-only,读取端去重);pricetrack(tick 每次盯市+导出);**PDF 导出前端**(macro_reports/) | pnl.py, attribution.py, ledger.py |
| 10 | **前端 5 组 IA 重构**(13 方块信息不删,组内二级 tab)+系统健康灯条(macro_health.json 首次被消费)+component_gates 渲染+能源过时文案删除+一致性告警独立+CRPS/截断提示/时间戳+Reports 真列表+盯市曲线;i18n 18键×5语言;tsc+build 绿 | MacroArtifactGrid/MacroArtifact 等 |
| 11 | **单一数据口全迁移**(claims/u3/payrolls/fed 直连 SQL 清零,5/5 字节级重放验证——过程中发现并修复 SQLite 双聚合裸列陷阱);结算↔标签对账保险丝(铁律2);per_release_day $30 限额;2宽桶;Chronos/bridge/ensemble **晋升计数器**(shadow_gate);install_launchd.sh+macro_health.sh;周报十分位校准表;本文档 | features.py, eval.py, install_launchd.sh |

## 二、审计勘误(此前审计报告的错误,以此为准)

1. **#35 "run_extended 死代码" 是误报**:`frontend_export.run()` 第 83 行一直调用
   `run_extended`,`risk.scenario_var` 每天在跑。DFM 升级链是活的(闸门未过所以停在基线,
   这是设计)。
2. **#6 初判"只有 canary"不准**:单调性测试当时已存在(test_m0);现在四件套完整。
3. Sidebar 三按钮直达是用户主动要求的重设计,**有意偏离** §16.1,不是缺口。

## 三、有意偏离(决策记录,非缺口)

1. **per_cluster $8(计划 $40)**:更保守,在任何系列过闸门前维持。
2. **venues/kalshi import 母版 auth**:保留(RSA 签名代码复制反而引入风险);
   FAMILY_TEMPLATE 已白名单化,视为对 §15/§20.8 的正式修订。
3. **macro_inflation/labor/energy/overview 无专属 JSON**:5 组 IA 重构后系列视图是
   board 数据的二级 tab,专属文件不再必要;§16.3 契约按此修订。
4. **期货源 yfinance 而非 Polygon**:已有注记,数据等价,保留。
5. **calibration/FL 修正当前为 identity 回退**:样本 <200 pairs 前不启用(坏图比没图
   更糟);随结算样本积累自动激活,无需人工。

## 四、剩余未做(诚实清单,含理由)

| 项 | 状态 | 理由/条件 |
|---|---|---|
| §7 模型深化(fed 51次史判别式/有序logit、claims 状态空间、payrolls ADP、energy AAA 传导回归) | ⏸ 未做 | **被计划自身的纪律挡住**:§7-bis/铁律13 要求模型改动 shadow 起步、live-forward 过门。现在 eval/shadow_gate 基建已齐,正确路径=逐个做成 shadow 变体走闸门,而非今天盲改生产模型作废全部重放历史。建议每次只动一个模型。 |
| daily_snapshot / annual_watch lane(美债/FX/WTI日频、年度极值系列) | ⏸ 未做 | 铁律2:每个新系列需先人工核实 Kalshi 结算细则(strict/rounding/首印口径)。квarterly lane 已通;日频系列待逐个实测 rulebook 后注册即可复用全部现有管线。 |
| M7 扩展视图(claims_history/fomc_history/params/venues/llm 5个) | ⏸ P2 | pricetrack/consistency/reports 三个已并入本轮;其余按需。 |
| exec 实盘接线 | ⏸ 按设计 | series_gate 全部 real=false(13/13 诚实落后市场)。链路已全通:eval 写门 → exec 读门 + 熔断查询,过门即可开(另需 KALSHI_TRADING_ENABLED=1 + settings.trading_enabled)。 |
| BLS/BEA 2027 精确日程 | ⏸ 官方未发布 | refresh_from_web 周度漂移检查已接;官方 2027 排期公布后回填 _CPI/_JOBS/_PCE。 |
| macroweekly 首次真实触发验证 | ⏸ 等周日 06:30 | 已装载 exit 0;下个周日看 macroweekly.log。 |
| Kalshi KXGDP 合约结构实测 | ⏸ | 模型/日历/lane 已备;合约上市后实测 rulebook 再开 paper。 |

## 五、当前系统状态(2026-07-31)

- 13 系列全部 paper;`series_gate` 13/13 real=false(模型落后市场,DM 不显著)——
  **这正是闸门该说的话**。决策重放显示当前策略在历史上会亏钱(claims ROI −105%),
  所以准确率路线 v2(ensemble/校准/微观结构)的价值将由每周 eval 的 Brier/ROI 变化检验。
- 测试 81/81;replay 金丝雀 5/5 字节一致;launchd 4 job 载入;上次 refresh 零失败。

## 六、2026-07-31 追加:WF 实验室三线 + 生产/chat 接线

- **research/selector.py(新)**:ML 选注器(扩窗逻辑回归)。数据集=每个已结算带
  K 线事件的全部候选结构(PIT 入场 asof,特征只用 kt≤asof 读数;训练集只含入场日
  之前已结算的事件)。修过两个致命 bug:①bucket 结构 cost=有效价但真实支出 1+eff、
  错桶仍有一腿赔付 → 首跑 39W-1L/+3686% 是记账假象(settle_cash() 修正,
  test_selector.py 钉死);②class_weight=balanced 使 p̂ 系统虚高(p̂0.8 实胜 29%)+
  无便士下限(60d"盈利"=两张 <0.10 彩票)→ 去平衡、每事件等权、下注限 0.10-0.90。
- **三线对比(同数据集走前)**:baseline=大热顺市复刻;ml=上述;blend=智能切换
  (逐事件比较两线入场前已结算滚动战绩 TRAIL=10,ML 需 MIN_SWITCH=5 注结算才可领先,
  被选线弃权落到另一线)。**采纳规矩:挑战者须两窗(last30+last60)同时胜基线**。
  blend 当前形式上过线(30d 7W-1L / 60d 17W-5L +25.1%)但样本薄、盈利集中于单笔
  等风险注 → 仅前端展示为候选,不接实盘。
- **生产接线**:refresh --weekly 依次跑 sweep → 标准 30d run(必须最后,sweep 分
  lead 跑会覆盖 daily_walkforward 行)→ selector.walkforward_eval → 周报 →
  **frontend_export_postweekly**(主导出在 weekly 块之前,不补跑则新数字要等次日);
  macroweekly launchd 原样触发,无需新 job。顺手修 sweep() 的 fair_mode NameError。
- **前端**:WF 实验室 = 唯一入口(标题"30 天前上线 · 实盘口径(严格 PIT)",按用户
  指令不用"假装"字样):入场提前对比/曲线/覆盖表 + 三线对比表(线路×窗口×W-L/胜率/
  PnL/ROI)+ Bet 历史五标签(混合/边际/大热/ML/智能切换)。
- **chat 接入**:macro_walkforward 注册为第 14 个 artifact 类型(macroMarketTool
  类型表/关键词/文件映射/ABOUT/slim——ml 块置前防截断)。SomeoAgent 路线:
  agentPrompt.ts 新增 macro 段 + macro_market_data 工具描述更新;非 agent 宏观模式
  经 chat.ts 既有 mode==='macro' 分支自动获得(chat.ts/toPrompt/templates.ts 零改动,
  coding 路线纯净);WF 实验室 Ai 按钮(macroAnalyze VALID_VIEWS 自动含新类型,修复
  此前 400 拒答)。
