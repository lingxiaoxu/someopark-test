# Someopark AI Chatbot 前端方案

## 一、输出文件结构全景

### 1. Walk-Forward 输出 (`historical_runs/walk_forward/` 和 `walk_forward_mtfs/`)

| 文件 | 格式 | 内容 | 可查询维度 |
|------|------|------|-----------|
| `walk_forward_summary_<ts>.json` | JSON | 6个窗口的完整配置：train/test日期、每窗口选出的pairs+param_set、每窗口OOS Sharpe/PnL | 按窗口、按配对、按参数集 |
| `dsr_selection_log_<ts>.csv` | CSV | 所有15对×32参数集×6窗口的DSR选参过程：pair_pnl, pair_sharpe, run_sharpe, dsr_pvalue, n_trades | 按配对×参数集交叉查询 |
| `oos_equity_curve_<ts>.csv` | CSV | 每日OOS净值曲线：Date, Equity, DailyPnL, Window, Equity_Chained | 时间序列分析/绘图 |
| `oos_pair_summary_<ts>.csv` | CSV | 配对级汇总：OOS_PnL, Sharpe, MaxDD, MaxDD_pct, WinRate, N_Trades, Turnover, N_Days | 配对排名/筛选 |
| `window*_<dates>/` (子目录) | 目录 | 每窗口的回测Excel详细文件（grid search中间结果） | 深入分析单窗口 |

### 2. Daily Signal 输出 (`trading_signals/`)

| 文件 | 格式 | 内容 | 可查询维度 |
|------|------|------|-----------|
| `mrpt_signals_<ts>.json` | JSON | MRPT单策略信号：每对的action(OPEN/CLOSE/HOLD/FLAT)、z_score、shares、prices、regime指标、OOS参考 | 按配对/按action类型 |
| `mtfs_signals_<ts>.json` | JSON | MTFS单策略信号：同上但用momentum_spread | 同上 |
| `combined_signals_<ts>.json` | JSON | 双策略合并信号 + regime完整指标 + component_scores(7类) + 资金分配 | 跨策略对比 |
| `daily_report_<ts>.json` | JSON | 人类可读报告的结构化版本：regime解读(中文)、指标历史对比(30obs/90obs均值)、持仓监测 | 全维度查询 |
| `daily_report_<ts>.txt` | TXT | 格式化文本报告：regime分析表、持仓HOLD列表、OPEN/CLOSE指令、观望配对、OOS淘汰配对、OOS历史表现排名 | 人类阅读 |
| `monitor_history/monitor_<strategy>_<pair>_<ts>.xlsx` | Excel | 每个持仓配对的专属回测Excel（参数与开仓时相同），含recorded_vars、pair_trade_history、stop_loss等sheet | 深入分析单配对持仓健康度 |

### 3. Inventory 状态 (`inventory_mrpt.json` / `inventory_mtfs.json`)

| 字段 | 内容 |
|------|------|
| `as_of` | 信号日期 |
| `capital` | 回测基准资本 |
| `pairs.<key>.direction` | long/short/null |
| `pairs.<key>.s1_shares/s2_shares` | 持仓股数 |
| `pairs.<key>.open_date/open_price` | 开仓信息 |
| `pairs.<key>.wf_source` | 完整walk-forward来源：每个窗口的param_set和OOS表现 |
| `pairs.<key>.monitor_log` | 最近一次监测记录：action、unrealized_pnl、exit_threshold |

### 4. WF Diagnostic 输出 (`historical_runs/wf_diagnostic_<ts>.xlsx`)

| Sheet | 内容 |
|-------|------|
| `Executive_Summary` | 宏观IS→OOS对比表 + 关键结论 |
| `Macro_Regime` | 每窗口宏观指标（VIX/MOVE/HY/YC等） |
| `Cross_Corr_IS` | 42-ticker IS期相关系数矩阵 |
| `Cross_Corr_OOS` | 42-ticker OOS期相关系数矩阵 |
| `Corr_Shift` | IS→OOS相关性变化热力图 |
| `Pair_Cointegration` | Engle-Granger协整p值（每窗口） |
| `MRPT_Pairs` / `MTFS_Pairs` | 逐配对逐窗口统计 |
| `Summary_Diagnosis` | 问题分类（regime shift / cointegration breakdown等） |
| `Ticker_Overlap` | Ticker集中度风险分析 |

### 5. Pair Universe (`pair_universe_mrpt.json` / `pair_universe_mtfs.json`)

当前15对MRPT + 15对MTFS配对定义，含s1/s2/sector/z_col或spread_col。

### 6. Inventory History (`inventory_history/`)

每次信号生成前的inventory快照备份。

---

## 二、Chatbot 需要回答的典型问题

1. **持仓查询**："现在持有哪些仓位？每个的盈亏是多少？"
2. **信号查询**："今天有什么新开仓/平仓信号？"
3. **Regime 查询**："当前市场状态怎样？VIX多少？信用利差在什么位置？MRPT/MTFS权重建议？"
4. **WF 表现查询**："CL/SRE 这对在walk-forward里表现怎么样？6个窗口分别赚了多少？"
5. **参数查询**："F/PG 用的什么参数集？这个参数集的DSR p-value是多少？"
6. **历史对比**："上周和这周的regime评分变化？持仓盈亏趋势？"
7. **诊断查询**："哪些配对的IS→OOS协整性断裂了？相关性shift最大的是哪些？"
8. **排名查询**："OOS Sharpe最高的5对是哪些？哪些配对被淘汰了？淘汰原因是什么？"
9. **风控查询**："当前最大的单对亏损是多少？总体MaxDD是什么水平？"

---

## 三、架构方案

### 方案 A：E2B Sandbox + LLM Agent（推荐，最接近 Manus）

```
用户 ──→ 前端 (Next.js/React)
              ↓
         Agent Orchestrator (你的后端)
              ↓
         LLM (Claude API / OpenAI)
              ↓ tool calls
         E2B Sandbox (Python 容器)
              ↓ 读取文件 / pandas分析 / 绘图
         返回结果 ──→ 前端渲染
```

**具体做法：**

1. **E2B Sandbox 配置**
   - 创建一个 E2B template，预装 pandas, openpyxl, matplotlib, json
   - 把 `trading_signals/`, `historical_runs/`, `inventory_*.json`, `pair_universe_*.json` 同步到 sandbox 的文件系统
   - 每天 DailySignal 跑完后，用脚本 rsync/upload 最新文件到一个持久化存储（S3 或者 Cloudflare R2）

2. **Agent 工具定义**（给 LLM 的 tools）
   - `read_json(path)` — 读取任意 JSON 文件
   - `read_csv(path, query?)` — 读取 CSV，可选 pandas query
   - `read_excel_sheet(path, sheet_name)` — 读取 Excel 指定 sheet
   - `list_files(directory, pattern)` — 列出文件
   - `run_python(code)` — 在 E2B sandbox 里执行任意 Python（pandas 分析、绘图）
   - `get_latest_file(directory, prefix)` — 获取某目录下最新的文件

3. **Agent System Prompt 核心内容**
   - 描述所有文件的结构和字段含义（即上面第一节的内容）
   - 教 agent 怎么找最新文件（按 mtime 排序）
   - 告诉 agent 每种问题该查哪些文件
   - 包含中文金融术语解释

4. **前端**
   - 一个聊天界面，支持渲染 markdown 表格和 matplotlib 图片
   - 可以显示 agent 的 "思考过程"（类似 Manus 的 step-by-step）
   - 侧边栏显示当前持仓概览（从 inventory 读取）

**优点：** 最灵活，agent 可以写任意 Python 做复杂分析。E2B sandbox 安全隔离，按用量计费。
**缺点：** E2B 每次启动 sandbox 有 ~2s 冷启动。需要定期同步文件。

### 方案 B：轻量 OpenClaw（自托管 Agent Runtime）

```
用户 ──→ 前端 (Next.js)
              ↓
         OpenClaw Agent (云端 VPS)
              ↓ tool calls
         本地文件系统 (直接读取 someopark-test/)
              ↓
         Python subprocess (pandas 分析)
```

**具体做法：**

1. **部署一台轻量 VPS**（2C4G 足够，Hetzner/Vultr ~$10/月）
   - 把 someopark-test 仓库 clone 上去
   - DailySignal 的 cron job 也跑在这台机器上
   - 这样文件天然在同一台机器，无需同步

2. **OpenClaw / Open Interpreter / Claude Agent SDK**
   - 用 Claude Agent SDK 搭建一个 agent 服务
   - tools 直接操作本地文件系统（无需 sandbox，因为只读）
   - 暴露一个 HTTP API 给前端调用

3. **前端**
   - 同方案 A，但 API endpoint 指向你的 VPS

**优点：** 文件在本地，零延迟读取。部署简单，一台机器搞定一切。
**缺点：** 安全性不如 E2B（agent 有本地文件系统访问权）。需要自己维护 VPS。

### 方案 C：预处理 + 向量化（最简单但最不灵活）

```
DailySignal 跑完后 ──→ 预处理脚本 ──→ 结构化数据入 DB
                                            ↓
用户 ──→ 前端 ──→ LLM + RAG (检索 DB) ──→ 回答
```

**做法：** 每天跑完 DailySignal 后，用一个脚本把所有 JSON/CSV 解析成结构化数据存入 SQLite 或 Supabase。LLM 用 text-to-SQL 或预定义查询回答问题。

**优点：** 响应最快，成本最低。
**缺点：** 不能做 ad-hoc 分析。每加一种新查询都要改代码。

---

## 四、推荐方案及实施步骤

### 推荐：方案 A（E2B）用于快速原型，方案 B 用于长期

**Phase 1：快速原型（1-2天搭完）**

1. 注册 E2B 账号，创建 sandbox template
2. 用 Claude Agent SDK（`claude_agent_sdk`）或 Anthropic tool_use 写 agent
3. 定义 5-6 个核心 tools（read_json, read_csv, run_python, list_files 等）
4. 写一个 system prompt，把文件结构描述清楚
5. 前端先用 Vercel AI SDK + Next.js 的 chat template
6. 文件上传：手动 upload 到 E2B persistent storage 或 S3

**Phase 2：完善（1周）**

7. 加入文件自动同步（DailySignal 结束后触发 upload）
8. 前端加 chart 渲染（agent 返回 matplotlib base64 图片或 echarts 数据）
9. 加入 "dashboard" 侧边栏（持仓概览、regime 指标卡片）
10. Agent prompt 优化：加入 few-shot examples

**Phase 3：迁移到自托管（可选）**

11. 如果 E2B 成本太高或需要更快响应，迁移到方案 B
12. 用 Claude Agent SDK 的 `computer_use` 或自定义 tool executor
13. 部署到 VPS，DailySignal cron 和 agent 服务共存

---

## 五、关键技术决策

| 决策点 | 建议 | 理由 |
|--------|------|------|
| LLM | Claude Sonnet 4.6 | 性价比最优，tool use 能力强，中文好 |
| Sandbox | E2B (原型) → 自托管 (长期) | E2B 开箱即用，自托管省钱 |
| 前端框架 | Next.js + Vercel AI SDK | 最成熟的 AI chat 前端方案 |
| 文件同步 | S3/R2 + 每日 cron upload | DailySignal 结束后 `aws s3 sync` |
| 图表渲染 | Agent 生成 matplotlib → base64 传前端 | 或者 agent 返回数据 → 前端 echarts 渲染 |
| 认证 | 简单密码保护或 Clerk | 这是私人交易系统，不需要复杂认证 |

---

## 六、Agent System Prompt 骨架

```
你是 Someopark 交易系统的 AI 助手。你可以查询以下数据：

文件位置：/data/someopark-test/

### 可查询的数据源

1. **当日信号** → trading_signals/combined_signals_最新.json
   - regime 评分和7类因子得分
   - 每对配对的 action (OPEN/CLOSE/HOLD/FLAT)
   - 持仓监测 (unrealized PnL)

2. **持仓状态** → inventory_mrpt.json / inventory_mtfs.json
   - 当前所有持仓的 direction, shares, open_date, open_price
   - walk-forward 来源和每窗口表现

3. **Walk-Forward 结果** → historical_runs/walk_forward*/
   - walk_forward_summary_*.json: 6窗口配置和OOS表现
   - oos_pair_summary_*.csv: 配对级Sharpe/PnL/MaxDD排名
   - oos_equity_curve_*.csv: 每日净值曲线
   - dsr_selection_log_*.csv: DSR选参详细日志

4. **WF 诊断** → historical_runs/wf_diagnostic_*.xlsx
   - IS vs OOS 对比、协整断裂检测、相关性shift

### 查询策略
- 找最新文件：按 mtime 排序取最后一个
- 跨文件关联：用 pair_key (如 "F/PG") 作为 join key
- 复杂分析：写 pandas 代码在 sandbox 执行
```

---

## 七、成本估算

| 项目 | 月成本 |
|------|--------|
| Claude Sonnet API（假设每天20次对话×~3K tokens） | ~$15-30 |
| E2B Sandbox（假设每天20次，每次~10s） | ~$5-10 |
| Vercel 前端托管 | 免费 (Hobby) |
| S3/R2 存储（几十MB文件） | < $1 |
| **合计** | **~$20-40/月** |

如果迁移到方案 B（自托管 VPS）：
| VPS (Hetzner CX22) | ~$10 |
| Claude API | ~$15-30 |
| **合计** | **~$25-40/月** |
