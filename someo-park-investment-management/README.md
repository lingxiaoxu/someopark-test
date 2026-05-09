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

Full-stack dashboard for the [someopark](../README.md) quantitative trading system. Supports three strategies — **MRPT** (Mean Reversion Pair Trading), **MTFS** (Multi-Timeframe Momentum), and **SSRS** (Smart Sector Rotation) — with real-time signal monitoring, walk-forward analysis browsing, AI chat with 35+ data tools, and portfolio management. All through a responsive web interface supporting 5 languages.

---

## 功能概览

### AI Chat

| 功能 | 说明 |
|------|------|
| **多模型 Chat** | 支持 Claude / GPT / Gemini，流式输出，可配置 temperature / max tokens |
| **Someo Agent 模式** | 自主多步推理代理，35+ 工具链（数据查询、Python 执行、Web 搜索），支持 MRPT/MTFS/SSRS 三策略，带实时进度面板 |
| **Prompt 模板** | 预置常用查询模板（信号查看、持仓分析、WF 诊断、PnL 报告等），一键触发对应 Artifact |
| **Code Sandbox** | 在浏览器内生成 / 预览自定义工具代码，支持部署至 E2B 沙盒（30m / 1h / 3h / 6h / 1d） |

### 数据视图（Artifacts）

所有 Artifact 均通过 **MRPT | MTFS | SSRS** 三策略 Tab 切换器统一访问。

| Artifact | MRPT / MTFS 数据源 | SSRS 数据源 | 说明 |
|----------|-------------------|-------------|------|
| **Trading Signals** | `trading_signals/signals_*.json` | `sr_daily_report_*.json` | MRPT/MTFS: z-score + 操作指令。SSRS: 11 板块 composite scores + 权重 + regime |
| **WF Structure** | `historical_runs/walk_forward*/` | `historical_runs/sector_rotation/` | 回测文件结构浏览器 + Run Inspector（SSRS: 59 参数集 × V1/V2 portfolio Excel） |
| **Daily Report** | `daily_report_*.txt` | `sr_daily_report_*.txt` | 每日量化报告（Regime + 持仓监测 + 信号汇总） |
| **Regime Dashboard** | `trading_signals/regime_*.json` | `sr_daily_report_*.json` | 宏观状态仪表盘：7 类综合评分 → MRPT/MTFS 资金权重 / SSRS 板块权重调整 |
| **Equity Curve** | `oos_equity_curve_*.csv` | `wf_fold_detail.json` | OOS 权益曲线（SSRS: 73 折合成 OOS） |
| **WF Summary** | `walk_forward_summary_*.json` | `wf_fold_detail.json` | MRPT/MTFS: 6 窗口汇总。SSRS: 73 折 × 59 参数集，合成 OOS Sharpe/CAGR/WFE |
| **OOS Summary** | `oos_pair_summary_*.csv` | `param_oos_by_regime.json` | MRPT/MTFS: 按配对汇总。SSRS: 59 参数集按 Regime 的 OOS Sharpe |
| **DSR / Fold Grid** | `dsr_selection_log_*.csv` | `wf_fold_detail.json` | MRPT/MTFS: DSR 选参三维过滤。SSRS: 73 折选参网格（IS/OOS/WFE/Method） |
| **Current Inventory** | `inventory_mrpt/mtfs.json` | `inventory_sector_rotation.json` | MRPT/MTFS: 配对持仓。SSRS: 板块 ETF 权重 + 成本 + 再平衡历史 |
| **Inventory History** | `inventory_history/*.json` | `inventory_history/*.json` | 历史快照（SSRS: 含板块权重、成本基础、regime、PnL） |
| **Portfolio History** | `portfolio_history_*.xlsx` (35 sheets) | `sr_portfolio_*.xlsx` (26 sheets) | Excel 内联查看器（SSRS: 板块权重/PnL 归因/交易/止损/regime） |
| **PnL Report** | `pnl_reports/pnl_report_*.json` | tearsheet PDF | 盈亏报告（SSRS: 内嵌 PDF 查看器） |
| **Strategy Performance** | `strategy_performance.json` | V1/V2 equity 比较 | 策略整体表现：权益曲线 / 回撤 / 每日 PnL |
| **Pair / Sector Universe** | `pair_universe_mrpt/mtfs.json` | `inventory_sector_rotation.json` | MRPT/MTFS: 配对筛选视图。SSRS: 11 GICS 板块 ETF 持仓表 |
| **WF Diagnostic** | `oos_report_*.txt` | `wf_diagnostic_sr_*.xlsx` | WF 诊断报告（SSRS: 5 sheets — 折汇总/OOS矩阵/regime/合成权益/选参记录） |

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
│   │   ├── sectorRotation.ts        SSRS API 端点（/api/ssrs/*，23 端点）
│   │   └── inventory.ts             MRPT/MTFS 持仓 API
│   ├── tools/                       35+ Agent 工具（全部支持 strategy=ssrs）
│   │   ├── index.ts                 工具注册表
│   │   ├── inventoryTool.ts         持仓查询
│   │   ├── signalsTool.ts           交易信号
│   │   ├── regimeTool.ts            Regime 状态
│   │   ├── runPythonTool.ts         Python 代码执行
│   │   ├── webSearchTool.ts         Web 搜索
│   │   ├── statisticsTool.ts        统计计算
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
│   │       ├── SignalTable.tsx              交易信号表
│   │       ├── WFStructureViewer.tsx        Walk-Forward 文件浏览器 + Run Inspector
│   │       ├── RegimeDashboard.tsx          宏观 Regime 仪表盘
│   │       ├── EquityChart.tsx              OOS 权益曲线
│   │       ├── InventoryViewer.tsx          当前持仓
│   │       ├── InventoryHistoryViewer.tsx   持仓历史快照
│   │       ├── DailyReportViewer.tsx        每日量化报告
│   │       ├── WalkForwardSummaryViewer.tsx WF 汇总
│   │       ├── WFGridViewer.tsx             DSR 选参日志
│   │       ├── OOSPairSummaryViewer.tsx     OOS 配对汇总
│   │       ├── PairUniverseViewer.tsx       配对筛选视图
│   │       ├── PnlReportViewer.tsx          盈亏报告
│   │       ├── PortfolioHistoryViewer.tsx   组合历史 Excel 查看器
│   │       ├── StrategyPerformanceViewer.tsx 策略表现仪表盘
│   │       └── WFDiagnosticViewer.tsx       WF 诊断报告
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
├── public/                          静态资源
├── firebase.json                    Firebase Hosting 配置
├── package.json
├── vite.config.ts
└── tsconfig.json
```

---

## Agent 工具列表

Agent 模式下可调用 35+ 工具自主完成复杂查询。所有 strategy 参数均支持 `mrpt`、`mtfs`、`ssrs` 三值：

| 分类 | 工具 | strategy 参数 | 说明 |
|------|------|:---:|------|
| **持仓** | `get_inventory` | mrpt / mtfs / ssrs | 当前持仓（SSRS: 板块 ETF 权重/股数/成本/再平衡历史） |
| | `get_inventory_history` | mrpt / mtfs / ssrs | 历史持仓快照 |
| **信号** | `get_signals` | mrpt / mtfs / combined / ssrs | 最新信号（SSRS: 11 板块 composite scores + regime + smart_select） |
| | `get_daily_report` | — | 每日量化报告 JSON |
| | `get_daily_report_text` | — | 每日量化报告 TXT |
| **Regime** | `get_regime` | — | 宏观 Regime + MRPT/MTFS 权重 |
| **Walk-Forward** | `get_wf_summary` | mrpt / mtfs / ssrs | WF 汇总（SSRS: 73 折合成 OOS Sharpe/CAGR/WFE/per-param） |
| | `get_equity_curve` | mrpt / mtfs / ssrs | OOS 权益曲线（SSRS: 合成 OOS metrics） |
| | `get_oos_pair_summary` | mrpt / mtfs / ssrs | OOS 汇总（SSRS: 59 参数集按 Regime 的 OOS Sharpe） |
| | `get_dsr_log` | mrpt / mtfs / ssrs | 选参日志（SSRS: 73 折选参详情 — 选中参数/方法） |
| | `get_wf_structure` | mrpt / mtfs / ssrs | WF 文件结构（SSRS: `historical_runs/sector_rotation/` Excel） |
| | `get_wf_diagnostic` | — | WF 诊断 XLSX 数据 |
| **配对/板块** | `get_pair_universe` | mrpt / mtfs / ssrs | 配对/板块筛选（SSRS: 11 GICS ETF 持仓状态） |
| | `get_pair_stats` | mrpt / mtfs / ssrs | 配对/板块详情（SSRS: 板块 holding + composite score） |
| **组合** | `get_monitor_history` | mrpt / mtfs / ssrs | 监测历史 XLSX（SSRS: `monitor_sr_*.xlsx`） |
| | `get_pnl_reports` | — | PnL 报告 PDF 列表 |
| | `get_strategy_performance` | — | 策略表现时序 |
| | `compare_strategies` | — | 三策略对比（MRPT / MTFS / SSRS） |
| **文件操作** | `read_file` | — | 读取任意文本文件 |
| | `list_files` | — | 列出目录内容 |
| | `query_json` | — | 读取并查询 JSON（含 SSRS 快捷路径） |
| | `parse_data_file` | — | 解析 XLSX / CSV |
| | `get_set_config` | — | Agent 配置（default_strategy 含 ssrs） |
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
