<p align="center">
  <img src="public/favicon.ico" alt="Someo Park" width="48"/>
</p>

<h1 align="center">Someo Park Investment Management</h1>
<p align="center"><b>AI-Powered Quantitative Trading Dashboard</b></p>

<p align="center">
  <img src="https://img.shields.io/badge/React-19-blue?logo=react&logoColor=white"/>
  <img src="https://img.shields.io/badge/TypeScript-5-blue?logo=typescript&logoColor=white"/>
  <img src="https://img.shields.io/badge/Tailwind_CSS-4-38B2AC?logo=tailwindcss&logoColor=white"/>
  <img src="https://img.shields.io/badge/Vite-6-646CFF?logo=vite&logoColor=white"/>
  <img src="https://img.shields.io/badge/i18n-5_languages-orange"/>
  <img src="https://img.shields.io/badge/LLM-Claude%20%7C%20GPT%20%7C%20Gemini-purple"/>
  <img src="https://img.shields.io/badge/hosting-Firebase-FFCA28?logo=firebase&logoColor=black"/>
</p>

<p align="center">
  <b>Live:</b> <a href="https://someopark.web.app">someopark.web.app</a>
</p>

---

Full-stack dashboard for the [someopark](../README.md) quantitative trading system. Supports four strategies — **MRPT** (Mean Reversion Pair Trading), **MTFS** (Multi-Timeframe Momentum), **SSRS** (Smart Sector Rotation), and **AISS** (AI Semiconductor Strategy) — plus a **Private Credit BDC** sleeve, with real-time signal monitoring, walk-forward analysis browsing, AI chat with 53+ data tools, risk management reports, and portfolio management. All through a responsive web interface supporting 5 languages.

---

## 功能概览

### AI Chat

| 功能 | 说明 |
|------|------|
| **多模型 Chat** | 支持 Claude / GPT / Gemini，流式输出，可配置 temperature / max tokens |
| **Someo Agent 模式** | 自主多步推理代理，53+ 工具链（数据查询、Python 执行、Web 搜索、Private Credit 建模、Knowledge Base），支持 MRPT/MTFS/SSRS/AISS 四策略，带实时进度面板 |
| **Prompt 模板** | 预置常用查询模板（信号查看、持仓分析、WF 诊断、PnL 报告、风险报告等），一键触发对应 Artifact |
| **Code Sandbox** | 在浏览器内生成 / 预览自定义工具代码，支持部署至 E2B 沙盒（30m / 1h / 3h / 6h / 1d） |

### 数据视图（Artifacts）

所有 Artifact 均通过 **MRPT | MTFS | SSRS | AISS** 四策略 Tab 切换器统一访问。

| Artifact | MRPT / MTFS 数据源 | SSRS 数据源 | AISS 数据源 | 说明 |
|----------|-------------------|-------------|-------------|------|
| **Trading Signals** | `signals_*.json` | `sr_daily_report_*.json` | `/api/aiss/signals/latest` | MRPT/MTFS: z-score + 操作。SSRS: 11 板块 composite + regime。AISS: 8 子板块信号 + 个股分解 |
| **WF Structure** | `walk_forward*/` | `sector_rotation/` | `/api/aiss/wf/` | 回测文件浏览器 + Run Inspector |
| **Daily Report** | `daily_report_*.txt` | `sr_daily_report_*.txt` | `/api/aiss/daily-report/latest` | 每日量化报告（Regime + 持仓监测 + 信号汇总） |
| **Regime Dashboard** | `regime_*.json` | `sr_daily_report_*.json` | `/api/aiss/regime/latest` | 宏观状态仪表盘 |
| **Equity Curve** | `oos_equity_curve_*.csv` | `wf_fold_detail.json` | `/api/aiss/equity-curve` | OOS 权益曲线 |
| **WF Summary** | `walk_forward_summary_*.json` | `wf_fold_detail.json` | `/api/aiss/wf/summary` | 窗口/折汇总 |
| **OOS Summary** | `oos_pair_summary_*.csv` | `param_oos_by_regime.json` | `/api/aiss/wf/param-oos` | OOS 汇总（按配对/参数/Regime） |
| **DSR / Fold Grid** | `dsr_selection_log_*.csv` | `wf_fold_detail.json` | `/api/aiss/wf/fold-grid` | 选参网格 |
| **Current Inventory** | `inventory_mrpt/mtfs.json` | `inventory_sector_rotation.json` | `/api/aiss/inventory` | 持仓（AISS: 个股级持仓 + 子板块权重） |
| **Inventory History** | `inventory_history/*.json` | `inventory_history/*.json` | `/api/aiss/inventory/history` | 历史快照 |
| **Portfolio History** | `portfolio_history_*.xlsx` | `sr_portfolio_*.xlsx` | `/api/aiss/portfolio-history/` | Excel 内联查看器 |
| **PnL Report** | `pnl_report_*.pdf` | tearsheet PDF | `/api/aiss/tearsheet/` | 盈亏报告 |
| **Risk Report** | `risk_report_*.pdf/json` | — | — | 机构级风险管理报告（敞口/杠杆/VaR/CVaR/集中度/因子β/压力测试/资产负债表/现金流/Kelly） |
| **Strategy Performance** | `strategy_performance.json` | V1/V2 equity | `master_portfolio_performance.json` | 策略权益曲线 / 回撤 / 每日 PnL / **Master 模式**（全 4+1 策略 + SPY/SMH/SOXX/MAGS 4 基准对照） |
| **Pair / Sector Universe** | `pair_universe_*.json` | `inventory_sector_rotation.json` | `/api/aiss/stock-universe` | 配对/板块/个股筛选视图 |
| **WF Diagnostic** | `oos_report_*.txt` | `wf_diagnostic_sr_*.xlsx` | `/api/aiss/diagnostic/latest` | WF 诊断报告 |
| **Knowledge Base** | — | — | — | 文档知识库搜索 / 阅读（支持 Agent RAG 工具链） |
| **Private Credit Model** | — | — | — | BDC 信用模型：模板输入/输出 + Merton PD/LGD + 敏感度热力图 + 远期利率曲线 + 现金流排期 |

### UI 特性

| 特性 | 说明 |
|------|------|
| **5 语言 i18n** | English / 中文 / 日本語 / Français / Español，一键切换。技术术语和 Ticker 保持英文 |
| **可调面板** | 左侧栏 ±18% 拖拽调宽，右侧 Artifact 面板自由拖拽。支持触屏拖拽（16px 热区） |
| **自动隐藏滚动条** | 滚动 / 触摸时显示，静止 1.2s 后淡出 |
| **Pair Badge** | 点击任意配对弹出浮层（React Portal），含持仓详情 + 4 个快捷导航，不受父容器 overflow 裁剪 |
| **Supabase Auth** | 邮箱登录 / 注册 / 密码重置 |
| **暗色适配** | CSS 变量驱动，可扩展主题 |

---

## 技术栈

| 层级 | 技术 |
|------|------|
| **前端** | React 19, TypeScript, Tailwind CSS 4, Vite 6 |
| **后端** | Express, tsx (TypeScript runner) |
| **AI** | Anthropic SDK, OpenAI SDK, Google GenAI, Vercel AI SDK |
| **数据** | xlsx 解析 (xlsx), CSV 解析 (csv-parse), JSON 文件存储 |
| **认证** | Supabase Auth |
| **沙盒** | E2B Code Interpreter |
| **部署** | Firebase Hosting (前端), Cloud VPS (API 服务器) |
| **国际化** | react-i18next |

---

## 环境配置

### 1. 安装依赖

```bash
cd someo-park-investment-management
npm install
```

### 2. 配置环境变量

创建 `.env` 文件：

```env
# LLM API Key（至少配置一个）
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GOOGLE_GENERATIVE_AI_API_KEY=...

# 可选
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your-anon-key
E2B_API_KEY=your-e2b-key
API_PORT=3001
```

> `.env` 已加入 `.gitignore`，不会提交到版本库。

### 3. 运行

```bash
# 同时启动前端（端口 3000）和 API 服务器（端口 3001）
npm run dev:all
```

分别启动：

```bash
npm run dev       # 前端 Vite dev server（端口 3000）
npm run server    # API 服务器（端口 3001，tsx watch 热重载）
```

### 4. 构建 & 部署

```bash
npm run build                    # 构建至 dist/
firebase deploy --only hosting   # 部署至 Firebase Hosting
```

---

## 项目结构

```
someo-park-investment-management/
├── server/
│   ├── index.ts                     Express API 入口
│   ├── config.ts                    端口 / 路径配置
│   ├── routes/
│   │   ├── chat.ts                  LLM Chat 端点（流式输出）
│   │   ├── morphChat.ts             Morph Apply 端点
│   │   ├── agent.ts                 Agent 模式（多步工具链编排）
│   │   ├── sectorRotation.ts        SSRS API 端点（/api/ssrs/*）
│   │   ├── semiconductor.ts         AISS API 端点（/api/aiss/*，23+ 端点）
│   │   ├── riskReport.ts            风险管理报告 API（/api/risk-report/*）
│   │   ├── monitorHistory.ts        监测历史 XLSX API
│   │   └── inventory.ts             MRPT/MTFS 持仓 API
│   ├── tools/                       53+ Agent 工具（支持 MRPT/MTFS/SSRS/AISS 四策略）
│   │   ├── index.ts                 工具注册表
│   │   ├── inventoryTool.ts         持仓查询
│   │   ├── signalsTool.ts           交易信号
│   │   ├── regimeTool.ts            Regime 状态
│   │   ├── riskReportTool.ts        风险管理报告
│   │   ├── runPythonTool.ts         Python 代码执行
│   │   ├── webSearchTool.ts         Web 搜索
│   │   ├── statisticsTool.ts        统计计算
│   │   ├── privateCreditTools.ts    Private Credit 建模（7 工具）
│   │   ├── knowledgeBaseTool.ts     知识库 RAG 搜索（3 工具）
│   │   └── ...                      （完整列表见 tools/index.ts）
│   └── utils/
│       ├── prompt.ts                系统提示词构建器
│       ├── agentPrompt.ts           Agent 专用提示词
│       └── taskManager.ts           后台任务执行管理
├── src/
│   ├── App.tsx                      主布局（侧栏 + Chat + Artifact 面板）
│   ├── index.css                    全局样式（CSS 变量 / 像素风格 / 滚动条）
│   ├── components/
│   │   ├── ChatArea.tsx             Chat 消息区 / 欢迎页 / 流式渲染
│   │   ├── ChatInput.tsx            输入框 + 浮动工具栏
│   │   ├── Sidebar.tsx              导航 / Runtime 选择 / Auth / 语言切换
│   │   ├── PairBadge.tsx            交互式配对徽章（Portal 浮层）
│   │   ├── AgentModeToggle.tsx      Agent 开关
│   │   ├── AgentProgress.tsx        Agent 多步执行进度
│   │   ├── CodePreview.tsx          代码编辑器 + 实时预览 + E2B 部署
│   │   ├── ChatPicker.tsx           模型 / Persona 选择器
│   │   ├── ChatSettings.tsx         LLM 参数设置
│   │   └── artifacts/
│   │       ├── SignalTable.tsx              交易信号表（MRPT/MTFS/SSRS/AISS 四策略）
│   │       ├── WFStructureViewer.tsx        Walk-Forward 文件浏览器 + Run Inspector
│   │       ├── RegimeDashboard.tsx          宏观 Regime 仪表盘
│   │       ├── EquityChart.tsx              OOS 权益曲线
│   │       ├── InventoryViewer.tsx          当前持仓（AISS: 个股级）
│   │       ├── InventoryHistoryViewer.tsx   持仓历史快照
│   │       ├── DailyReportViewer.tsx        每日量化报告
│   │       ├── WalkForwardSummaryViewer.tsx WF 汇总
│   │       ├── WFGridViewer.tsx             DSR 选参日志
│   │       ├── OOSPairSummaryViewer.tsx     OOS 配对汇总
│   │       ├── PairUniverseViewer.tsx       配对/个股筛选视图
│   │       ├── PnlReportViewer.tsx          盈亏报告
│   │       ├── PortfolioHistoryViewer.tsx   组合历史 Excel 查看器
│   │       ├── StrategyPerformanceViewer.tsx 策略表现（Strategies + Master + 4 基准）
│   │       ├── RiskReportViewer.tsx         机构级风险管理报告（PDF）
│   │       ├── WFDiagnosticViewer.tsx       WF 诊断报告
│   │       ├── KnowledgeBaseViewer.tsx      知识库搜索 / 阅读
│   │       ├── PrivateCreditModelViewer.tsx Private Credit 模板模型
│   │       ├── CreditRiskDashboard.tsx     信用风险 Merton PD/LGD
│   │       ├── SensitivityHeatmap.tsx      敏感度热力图
│   │       ├── ForwardRateCurve.tsx        远期利率曲线
│   │       └── CashflowScheduleViewer.tsx  现金流排期
│   ├── i18n/
│   │   └── locales/                 en.json / zh.json / ja.json / fr.json / es.json
│   ├── lib/
│   │   ├── api.ts                   前端 API 客户端
│   │   ├── messages.ts              消息类型 + Artifact 触发器
│   │   ├── templates.ts             Chat 提示模板
│   │   ├── models.ts                LLM 模型配置
│   │   └── types.ts                 通用类型定义
│   └── contexts/
│       └── ArtifactContext.tsx       Artifact 导航上下文
├── public/                          静态资源 + 运行时数据（strategy/master performance JSON、inventory）
├── firebase.json                    Firebase Hosting 配置
├── package.json
├── vite.config.ts
└── tsconfig.json
```

---

## Agent 工具列表

Agent 模式下可调用 53+ 工具自主完成复杂查询。strategy 参数支持 `mrpt`、`mtfs`、`ssrs`、`aiss` 四值（部分工具无 strategy 参数）：

| 分类 | 工具 | strategy | 说明 |
|------|------|:---:|------|
| **持仓** | `get_inventory` | mrpt / mtfs / ssrs / aiss | 当前持仓（AISS: 个股级 + 子板块权重） |
| | `get_inventory_history` | mrpt / mtfs / ssrs / aiss | 历史持仓快照 |
| **信号** | `get_signals` | mrpt / mtfs / combined / ssrs / aiss | 最新信号 |
| | `get_daily_report` | — | 每日量化报告 JSON |
| | `get_daily_report_text` | — | 每日量化报告 TXT |
| **Regime** | `get_regime` | — | 宏观 Regime + 策略权重 |
| **Walk-Forward** | `get_wf_summary` | mrpt / mtfs / ssrs / aiss | WF 汇总 |
| | `get_equity_curve` | mrpt / mtfs / ssrs / aiss | OOS 权益曲线 |
| | `get_oos_pair_summary` | mrpt / mtfs / ssrs / aiss | OOS 汇总 |
| | `get_dsr_log` | mrpt / mtfs / ssrs / aiss | 选参日志 |
| | `get_wf_structure` | mrpt / mtfs / ssrs / aiss | WF 文件结构 |
| | `get_wf_diagnostic` | — | WF 诊断 XLSX |
| **配对/板块** | `get_pair_universe` | mrpt / mtfs / ssrs / aiss | 配对/板块/个股筛选 |
| | `get_pair_stats` | mrpt / mtfs / ssrs / aiss | 配对/板块详情 |
| **组合** | `get_monitor_history` | mrpt / mtfs / ssrs / aiss | 监测历史 XLSX |
| | `get_pnl_reports` | — | PnL 报告 PDF 列表 |
| | `get_risk_reports` | — | 风险管理报告 PDF/JSON/XLSX 列表 |
| | `get_strategy_performance` | — | 策略表现时序 |
| | `compare_strategies` | — | 四策略 + BDC 对比 |
| **Private Credit** | `pc_list_models` | — | PC 模板模型列表 |
| | `pc_read_model` | — | 读取模板输入/输出 |
| | `pc_compute` | — | 运行 PC 模型（IRR/MOIC/现金流） |
| | `pc_sensitivity` | — | 敏感度分析（双变量） |
| | `pc_compare_scenarios` | — | 情景对比 |
| | `pc_custom_cashflow` | — | 自定义现金流排期 |
| | `pc_excel_raw` | — | 读取原始 Excel 数据 |
| **PC Portfolio** | `portfolio_generate_cashflows` | — | 组合现金流生成 |
| | `portfolio_credit_risk` | — | 信用风险评分（Merton PD/LGD） |
| | `portfolio_forward_rates` | — | 远期利率曲线 |
| | `portfolio_stress_test` | — | 组合压力测试 |
| | `portfolio_analyze_deal` | — | 单笔交易分析 |
| | `portfolio_run_existing` | — | 已有模型批量运行 |
| | `portfolio_bdc_holdings` | — | BDC 持仓查询 |
| **知识库** | `kb_search` | — | 知识库语义搜索 |
| | `kb_read` | — | 读取知识库文档 |
| | `kb_list` | — | 列出知识库条目 |
| **文件操作** | `read_file` | — | 读取任意文本文件 |
| | `list_files` | — | 列出目录内容 |
| | `query_json` | — | 读取并查询 JSON |
| | `parse_data_file` | — | 解析 XLSX / CSV |
| | `get_set_config` | — | Agent 配置 |
| | `search_content` | — | ripgrep 文件搜索 |
| **计算** | `calculate` | — | 数学表达式计算 |
| | `calculate_statistics` | — | 统计分析 |
| **执行** | `run_python` | — | Python 代码执行（E2B 沙盒） |
| | `web_search` | — | Web 搜索 |
| | `http_request` | — | HTTP 请求 |
| | `datetime` | — | 日期时间计算 |
| **流程控制** | `send_message` | — | 向用户发送中间消息 |
| | `manage_tasks` | — | 任务管理 |
| | `ask_user` | — | 向用户提问 |
| | `sleep` | — | 等待指定时间 |

---

## 脚本命令

| 命令 | 说明 |
|------|------|
| `npm run dev` | 启动 Vite dev server（端口 3000） |
| `npm run server` | 启动 API 服务器（端口 3001，tsx watch 热重载） |
| `npm run dev:all` | 同时启动前端 + API（推荐开发使用） |
| `npm run build` | 生产构建至 `dist/` |
| `npm run preview` | 预览生产构建 |
| `npm run clean` | 清理 `dist/` |
| `npm run lint` | TypeScript 类型检查（`tsc --noEmit`） |

---

## License

Proprietary. All rights reserved.
