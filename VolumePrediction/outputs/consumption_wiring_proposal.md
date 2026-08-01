# ③ 四策略消费接线方案（PROPOSAL ONLY — 按 plan §5.4/§5.5 需单独批准）

日期: 2026-08-01 · 前置: ②学习模型已晋升（lgbm_prodv6_20260731，daily 混合工件在产）
· 状态: **第一批已实施(2026-08-01)** — W3/W4 影子双算上线(`wiring_shadow.py`,
零接触策略代码,挂在 daily_update,每日记录 outputs/wiring_shadow/);W2 由 adapters
advice 文件承载(已在产)。首日读数: W3 flips 7/12859(全为边缘小票)、
W4 腿 ratio 中位 1.43(lgbm 预测量高于后视,与 η̂ 均值+0.35 一致)、双向 0 dtl 超限。
**W3/W4 真切换仍未实施** — 两周影子数据(≈8/15)后按数据决定。

## 总原则（plan 原文约束）

- **红线不变**: `ADV_PARTICIPATION = 0.20`、清仓天数逻辑、SelectPairs 流动性阈值的**数值**
  一律不动——只把"后视 20d ADV"换成"前瞻 ADV"，参与率纪律保持。
- 服务已提供零改动兼容层: `svc.adv.get_adv_forecast(ticker, window=20)`
  （签名语义 = RiskManager.adv；缺工件/缺票自动回退 trailing，source 标注）。
- 每条接线先跑 **2 周影子对比**（新旧 ADV 双算、只记录不切换），差异报告后再切。

## 接线点清单（按风险从低到高）

### W1. PnL/报表只读消费（零风险，可先行）
- 位置: PnLReport 等量统计处
- 改法: 无需改——只读消费，现状即可。**跳过**。

### W2. AISS/SSRS 执行窗口预算（低风险）
- 位置: `qlib-main/*/AISSdailySignal.py` 调仓单生成后的执行提示
- 改法: 调仓日输出里附 `market_liquidity_outlook` + 每标的 `get_adv_forecast`
  的参考行（纯展示字段，不改任何决策）。
- 回滚: 删展示字段即可。

### W3. SelectPairs 流动性过滤（中风险——影响选对宇宙）
- 位置: `SelectPairs.py` 流动性过滤（plan §5.5 第2行）
- 改法: 后视 ADV → `svc.adv.batch(tickers, 20)`；阈值数值不变。
- 影子: 2 周内每日记录两种口径的过滤名单 diff；名单变动率 <5% 且无系统性
  剔除大市值票 → 切换。
- 回滚: 单行开关（`USE_FORECAST_ADV=False`）。

### W4. RiskManager.adv 替换（最高风险——直接进下单约束）
- 位置: `RiskManager.py:335 adv()` 与 `:1127 dtl = shares/(adv×0.20)`
- 改法: `adv()` 内部改调 `get_adv_forecast`（服务不可用自动回退 trailing——
  服务方 §5.3-① 已保证不断流）；`ADV_PARTICIPATION` 与 dtl 公式**逐字不动**。
- 影子: 2 周内每日记录新旧 adv 之比的分布 + dtl 超限名单 diff。
- 回滚: 同款单行开关。

## 建议批次

1. **第一批（建议先批）**: W2（纯展示）+ W3/W4 的**影子记录**（双算只记不切）
2. **第二批**: 影子报告后按数据决定 W3/W4 是否切换

每批实施照旧: 测试全进 tmp、生产零污染、上线后首日专项核验。
