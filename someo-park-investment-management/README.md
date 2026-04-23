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

Full-stack dashboard for the [someopark](../README.md) quantitative pair-trading system. Provides real-time signal monitoring, walk-forward analysis browsing, AI chat with 30+ data tools, and portfolio management — all through a responsive web interface supporting 5 languages.

---

## 功能概览

### AI Chat

| 功能 | 说明 |
|------|------|
| **多模型 Chat** | 支持 Claude / GPT / Gemini，流式输出，可配置 temperature / max tokens |
| **Someo Agent 模式** | 自主多步推理代理，30+ 工具链（数据查询、Python 执行、Web 搜索），带实时进度面板 |
| **Prompt 模板** | 预置常用查询模板（信号查看、持仓分析、WF 诊断、PnL 报告等），一键触发对应 Artifact |
| **Code Sandbox** | 在浏览器内生成 / 预览自定义工具代码，支持部署至 E2B 沙盒（30m / 1h / 3h / 6h / 1d） |

### 数据视图（Artifacts）

| Artifact | 数据源 | 说明 |
|----------|--------|------|
| **Trading Signals** | `trading_signals/signals_*.json` | MRPT / MTFS 每日交易信号表，含 z-score、操作指令、股数分配。点击任意 Pair Badge 弹出持仓详情 + 快速导航 |
| **Walk-Forward Structure** | `historical_runs/walk_forward*/` | 回测文件结构浏览器 + Run Inspector。可切换策略 / 窗口 / IS Grid Search vs OOS Test 阶段，内联查看 Excel Sheet 数据 |
| **Daily Report** | `trading_signals/daily_report_*.txt` | 每日量化报告（Regime 分析 + 持仓监测 + 信号汇总） |
| **Regime Dashboard** | `trading_signals/regime_*.json` | 宏观状态仪表盘：7 类指标综合评分（波动率 / 信用 / 利率 / 动量 / 宏观压力 / 地缘 / 策略 vol），驱动 MRPT-MTFS 资金权重 |
| **Equity Curve** | `historical_runs/walk_forward*/oos_equity_curve_*.csv` | OOS 权益曲线：总回报 / Sharpe / 最大回撤 |
| **WF Summary** | `historical_runs/walk_forward*/walk_forward_summary_*.json` | Walk-Forward 汇总：6 窗口 × OOS PnL / Sharpe / 选中配对 |
| **OOS Pair Summary** | `historical_runs/walk_forward*/oos_pair_summary_*.csv` | 按配对汇总的 OOS 表现（PnL / Sharpe / MaxDD% / 胜率） |
| **DSR Selection Grid** | `historical_runs/walk_forward*/dsr_selection_log_*.csv` | DSR 选参日志：pair × param_set × window 三维过滤 |
| **Current Inventory** | `inventory_mrpt.json` / `inventory_mtfs.json` | 当前持仓状态：开仓日期、价格、param_set、对冲比率、WF 来源 |
| **Inventory History** | `inventory_history/*.json` | 历史持仓快照浏览（含监测日志、PnL 追踪） |
| **Portfolio History** | `historical_runs/*/portfolio_history_*.xlsx` | 组合历史 Excel 内联查看器（35 个 Sheet，按日期 / 配对 / 交易分解） |
| **PnL Report** | `pnl_reports/pnl_report_*.json` | 盈亏报告：交易明细、杠杆分析、系统价 vs 执行价（次日开盘） |
| **Strategy Performance** | `public/data/strategy_performance.json` | 策略整体表现：权益曲线（% / $）、回撤、每日 PnL，支持日期范围选择 |
| **Pair Universe** | `pair_universe_mrpt.json` / `pair_universe_mtfs.json` | 配对筛选视图：已选 / 协整 / 相似 / PCA 候选 |
| **WF Diagnostic** | `historical_runs/walk_forward*/oos_report_*.txt` | Walk-Forward 诊断文本报告 |

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
│   │   └── agent.ts                 Agent 模式（多步工具链编排）
│   ├── tools/                       30+ Agent 工具
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

Agent 模式下可调用 30+ 工具自主完成复杂查询：

| 分类 | 工具 | 说明 |
|------|------|------|
| **数据查询** | `read_inventory` | 读取当前 MRPT / MTFS 持仓 |
| | `read_signals` | 读取最新交易信号 |
| | `read_regime` | 读取宏观 Regime 状态 |
| | `read_daily_report` | 读取每日量化报告 |
| | `read_equity_curve` | 读取 OOS 权益曲线 |
| | `read_wf_summary` | 读取 Walk-Forward 汇总 |
| | `read_oos_pair_summary` | 读取 OOS 配对表现 |
| | `read_dsr_log` | 读取 DSR 选参日志 |
| | `read_inventory_history` | 读取持仓历史快照 |
| | `read_monitor_history` | 读取持仓监测历史 |
| | `read_pair_universe` | 读取配对筛选结果 |
| | `read_pnl_report` | 读取 PnL 报告 |
| | `read_strategy_performance` | 读取策略表现数据 |
| | `read_wf_structure` | 读取 WF 文件结构 |
| | `read_diagnostic` | 读取 WF 诊断报告 |
| **文件操作** | `read_file` | 读取任意文本文件 |
| | `list_files` | 列出目录内容 |
| | `read_json` | 读取并解析 JSON |
| | `read_csv` | 读取 CSV（支持过滤 / 排序 / 聚合） |
| | `read_config` | 读取运行配置 |
| | `search_content` | 在文件中搜索关键词 |
| **计算** | `calculator` | 数学表达式计算 |
| | `statistics` | 统计分析（均值 / 中位数 / 标准差 / 相关性） |
| | `pair_stats` | 配对统计（协整检验 / z-score / 对冲比率） |
| | `compare_strategies` | MRPT vs MTFS 策略对比 |
| **执行** | `run_python` | 执行 Python 代码（conda 环境） |
| | `web_search` | Web 搜索 |
| | `http_request` | HTTP 请求 |
| | `datetime` | 日期时间计算 |
| **流程控制** | `send_message` | 向用户发送中间消息 |
| | `sleep` | 等待指定时间 |
| | `stop_task` | 停止当前任务 |

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
