# prediction_market_<类别> 家族开仓模板(M8 定稿,2026-07-28)

macro 分支已按 §17 九条约定落地并验证。nba / soccer 开仓时逐条照抄,右列是 macro 的实证锚点。

| # | 约定 | macro 实证(已验证可抄) |
|---|---|---|
| 1 | 目录契约 config/ingest/venues/model/strategy/exec/jobs/ops/research/data/docs/tests/util | 全部就位;README 职责表 |
| 2 | 五问接口:fair/edge/decision/size/复盘,`decide()` 单入口纯函数 | `strategy/decision.py::decide()` — 生产(decide_all)/回测(replay)/报表同一入口 |
| 3 | 统一抽象 Pred/Dist/grid_pmf/survival | `model/common.py`;阶梯/categorical/binary 三型全覆盖 |
| 4 | 台账契约 decisions append-only + inputs_json + model_version + gate_snapshot | `ops/ledger.py`;kind: open/exit/pass/cancel/settle_note |
| 5 | 导出契约 `<类别>_*.json` 前缀进 public/data | 9 个 macro_*.json,`ops/frontend_export.py` 单点写 |
| 6 | 调度契约 launchd 三件套 + runs/coverage 看门狗 | com.someopark.macro{refresh,tick,watchdog};MISSED 不补跑决策 |
| 7 | 闸门契约 paper→OOS 闸门→real 三级 + circuit breaker | 回测已证明闸门有效(claims Brier 输市场→留 paper) |
| 8 | 文档契约 PLAN + 目录 + PLAN_AUDIT | docs/ 三件 + FAMILY_TEMPLATE(本文件) |
| 9 | 隔离契约 零 import、独立 db、独立调度 | 边界审计通过;唯二例外写入点白名单化 |

## 前端家族接入七步(M7 实证流程,照抄)

1. `appMode` 联合类型加 `'<类别>'` + `toggle<类别>Mode`(App.tsx)
2. Sidebar 加第 N 个模式按钮(同款反色样式)
3. ChatArea 欢迎页加分支:`<类别>Upcoming` + `<类别>ArtifactGrid`
4. `src/components/<类别>/` 整目录新建(copy prediction/macro 任一为骨架,primitives 自带不共享)
5. i18n 五语言加 `<类别>.*` namespace + sidebar 两键
6. server: `tools/<类别>MarketTool.ts`(grounding 注入)+ `routes/<类别>Analyze.ts`(nemotron 串行)+ artifactDetector 加模式分支
7. 清洁审计:grep 别族关键词 0 命中 + tsc + build:wc

## 已知工程坑(macro 实付学费,后续分支免费)

- Kalshi `/markets` 列表无价格 → 必须逐 ticker orderbook;429 按 Retry-After 退避
- 阈值梯 devig 必须 isotonic 单调化;strict(>) vs ≥ 是一阶定价输入
- ALFRED vintage 只有日粒度 → join 发布时刻表才能日内 PIT
- 标签必须用首发 vintage(y_first),不是今天的修订序列
- launchd plist 内 `&&` 必须 XML 转义(用 plistlib 生成,别手写)
- period 键两套并存(Kalshi token vs ISO)→ 全部经 util/periods 换算,直连比较必错
- 浮点费用要先 round 再 ceil;materialize 不创建"生成即过期"的决策任务
