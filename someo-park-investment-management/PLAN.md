# SomeoClaw Frontend Architecture & Implementation Plan

## 1. 整体架构 (Overall Architecture)

基于你提供的要求，前端将采用 **Next.js (或 React + Vite) + Tailwind CSS** 构建，作为一个 AI-native 的 Chatbot 界面。后端将依赖于部署在轻量级 VPS 上的 **SomeoClaw Agent**，该 Agent 拥有对 `someopark-test` 仓库文件的读取权限，并通过 API 与前端进行交互。

### 核心组件
- **前端 (Frontend)**: 负责 UI 渲染、状态管理、与 Agent 的 WebSocket/SSE 通信。
- **Agent 接入层 (SomeoClaw Bridge)**: 负责管理用户的 OpenClaw 实例，处理鉴权、心跳和任务分发。
- **策略执行层 (Strategy Services)**: 运行在 VPS 上的 Python 环境，执行 `DailySignal.py`、`SelectPairs.py` 等脚本，并读取 JSON/CSV/Excel 输出。

## 2. 数据流与交互 (Data Flow & Interaction)

1. **用户查询**: 用户在前端 Chat 窗口输入自然语言（例如：“现在持有哪些仓位？每个的盈亏是多少？”）。
2. **请求转发**: 前端将请求发送给 SomeoClaw Agent。
3. **Agent 规划与工具调用**: Agent 解析意图，决定调用哪些 Tools（例如 `read_inventory`、`query_daily_signal`）。
4. **本地文件读取**: Agent 在 VPS 上读取 `inventory_mrpt.json` 或 `trading_signals/daily_report_<ts>.json`。
5. **数据处理与返回**: Agent 将数据整理为结构化 JSON 或 Markdown 格式，通过流式接口 (SSE) 返回给前端。
6. **前端渲染**: 前端根据返回的数据类型（文本、表格、特定卡片），使用对应的 UI 组件进行渲染。

## 3. 前端 UI 结构 (Frontend UI Structure)

界面采用三栏布局（Stanse-style AI Terminal UI）：

### 3.1 左侧边栏 (Sidebar - 260px)
- **Agent 状态**: 显示当前连接的 OpenClaw 状态（在线/离线）。
- **历史对话 (Chat History)**: 用户历史会话列表，支持点击切换。
- **接入向导入口**: "Connect Your OpenClaw" 按钮，点击弹出三步接入向导 Modal。

### 3.2 中间主对话区 (Main Chat Area - flex)
- **消息流 (Message Stream)**: 
  - 用户消息：右侧对齐，深色气泡。
  - AI 消息：左侧对齐，无明显气泡，原生数据展示（Data-native UI）。
- **状态指示器 (State Indicator)**: 显示 Agent 当前状态（`thinking` 闪烁效果、`running tool` 提示）。
- **输入框 (Chat Input)**: 底部固定，支持自动扩展高度，支持 `/command` 快捷指令。

### 3.3 右侧信息面板 (Right Panel - 320px)
- **上下文信息 (Contextual Info)**: 根据当前对话内容动态显示。
- **持仓概览 (Inventory Summary)**: 提取当前 `inventory_mrpt.json` 的核心数据。
- **配对详情 (Pair Details)**: 当对话涉及特定配对（如 AAPL/MSFT）时，显示其 Z-score、PnL 等。

## 4. 核心 UI 组件设计 (Core UI Components)

根据你提供的 Design System，前端将实现以下专用组件：

1. **`MessageAI`**: AI 回复组件，支持 Markdown 渲染、代码块高亮 (`.code-block`) 和表格渲染 (`.table`)。
2. **`PairCard`**: 配对卡片组件，用于展示 `coint_pairs` 或 `similar_pairs` 的信息（高亮已选配对）。
3. **`InventoryBox`**: 持仓状态组件，带有左侧强调色边框 (`.inventory-box`)，展示方向、股数、开仓价等。
4. **`WFBox`**: Walk-Forward 窗口组件，采用 Grid 布局 (`.wf-box`)，展示 6 个窗口的 OOS Sharpe / PnL。
5. **`StatusIndicator`**: 状态指示组件，处理 `idle`, `thinking` (shimmer 动画), `running`, `streaming`, `success`, `error` 等状态。

## 5. SomeoClaw 接入向导 (Onboarding Modal)

实现一个三步向导 Modal：
1. **Step 1**: 填写 Agent Name、选择模式（Research/Backtest/Daily run）和风险级别。
2. **Step 2**: 生成专属配置（`agent_token`, `bridge_url`, `workspace_id`）。
3. **Step 3**: 验证连接（调用 `/api/someoclaw/verify`），成功后更新 Sidebar 状态。

## 6. 针对典型问题的处理策略

- **持仓/信号查询**: Agent 直接读取 `inventory_mrpt.json` 和 `mrpt_signals_<ts>.json`，前端使用 `InventoryBox` 和 `Table` 渲染。
- **Regime 查询**: Agent 读取 `daily_report_<ts>.json`，前端提取 `regime` 字段并用图表或高亮文本展示。
- **WF 表现查询**: Agent 跨文件查询 `walk_forward_summary_<ts>.json` 和 `oos_pair_summary_<ts>.csv`，前端使用 `WFBox` 渲染 6 个窗口的数据。
- **诊断查询**: Agent 读取 `wf_diagnostic_<ts>.xlsx`（通过 Python pandas 转换为 JSON），前端渲染 `Summary_Diagnosis` 表格。

## 7. 下一步行动建议 (Next Steps)

1. **确认 UI 基础**: 我已经将你提供的 Design Tokens 和 CSS 样式写入了 `src/index.css`。你可以查看样式是否符合预期。
2. **搭建 React 组件**: 接下来可以开始编写基础的 Layout (Sidebar, Chat, RightPanel) 和核心组件 (PairCard, InventoryBox)。
3. **Mock 数据对接**: 在 Agent 后端就绪前，先使用你提供的 JSON/CSV 结构 Mock 数据，测试前端渲染效果。
4. **Agent API 联调**: 等待你的轻量 VPS 和 OpenClaw 服务端 API 就绪后，替换 Mock 数据进行真实联调。
