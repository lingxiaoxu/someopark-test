# Someo Park 前后端对接开发计划

**Version:** v1.0
**Date:** 2026-03-21
**Scope:** 将现有前端 UI 组件接入后端真实数据，不修改后端任何 Python 代码
**Frontend Root:** `/someo-park-investment-management/`
**Backend Root:** `/someopark-test/`（只读引用，不修改）

---

## 一、整体架构

### 1.1 当前状态

前端有 13 个 Artifact Viewer 组件，全部使用 hardcoded mock 数据。需要逐个替换为真实后端数据。

### 1.2 目标架构

```
React Frontend (Vite dev server :3000)
    ↓ fetch /api/*
Express API Server (:3001, 运行在 someo-park-investment-management/ 内)
    ↓ 读文件 / 查 MongoDB
后端文件系统 (../someopark-test/) + MongoDB (someopark / someo_stra)
```

### 1.3 核心原则

1. **前端代码只改数据获取层**，不重写 UI 组件的渲染逻辑
2. **API Server 完全在 `someo-park-investment-management/` 内**，用 TypeScript 编写
3. **后端 Python 文件零修改**，API Server 只读取后端的输出文件和 MongoDB
4. **Excel 解析用 Node.js 库**（xlsx / exceljs），不调用 Python pandas
5. **CSV 解析用 Node.js 库**（csv-parse / papaparse）

---

## 二、API Server 设计

### 2.1 技术选型

```
Runtime:    Node.js (已有 package.json)
Framework:  Express (已在 dependencies 中)
XLSX 解析:  exceljs (需新增)
CSV 解析:   csv-parse (需新增)
MongoDB:    mongodb (需新增)
CORS:       cors (需新增)
```

### 2.2 文件结构

```
someo-park-investment-management/
├── server/
│   ├── index.ts                    ← API Server 入口
│   ├── config.ts                   ← 路径常量 + MongoDB URI
│   ├── routes/
│   │   ├── inventory.ts            ← /api/inventory/*
│   │   ├── signals.ts              ← /api/signals/*
│   │   ├── dailyReport.ts          ← /api/daily-report/*
│   │   ├── walkForward.ts          ← /api/wf/*
│   │   ├── pairUniverse.ts         ← /api/pairs/*
│   │   ├── diagnostic.ts           ← /api/diagnostic/*
│   │   ├── regime.ts               ← /api/regime/*
│   │   ├── portfolioHistory.ts     ← /api/portfolio-history/*
│   │   └── monitorHistory.ts       ← /api/monitor-history/*
│   └── utils/
│       ├── fileUtils.ts            ← 通用文件查找（按时间戳排序取最新）
│       ├── csvParser.ts            ← CSV→JSON 转换
│       ├── xlsxParser.ts           ← XLSX→JSON 转换（按 sheet 名）
│       └── mongoClient.ts          ← MongoDB 连接池
├── src/
│   ├── hooks/
│   │   └── useApi.ts               ← 通用 fetch hook
│   ├── lib/
│   │   └── api.ts                  ← API 调用函数集合
│   └── components/
│       └── artifacts/              ← 现有 Viewer 组件（改为接入真实数据）
└── .env                            ← MONGO_URI, BACKEND_ROOT 等
```

### 2.3 环境变量（`.env`）

```bash
# API Server
API_PORT=3001
BACKEND_ROOT=..                      # 相对于 someo-park-investment-management/

# MongoDB（从后端 .env 复制，只读连接）
MONGO_URI=mongodb://...
MONGO_VEC_URI=mongodb+srv://...

# Vite 前端
VITE_API_BASE=http://localhost:3001
```

### 2.4 package.json 新增

```json
{
  "scripts": {
    "dev": "vite --port=3000 --host=0.0.0.0",
    "server": "tsx watch server/index.ts",
    "dev:all": "concurrently \"npm run dev\" \"npm run server\""
  },
  "dependencies": {
    "exceljs": "^4.4.0",
    "csv-parse": "^5.5.0",
    "mongodb": "^6.0.0",
    "cors": "^2.8.5",
    "concurrently": "^8.2.0",
    "dotenv": "^17.2.3"
  },
  "devDependencies": {
    "@types/cors": "^2.8.17"
  }
}
```

---

## 三、API 端点设计（详细）

### 3.1 Inventory（当前持仓）

| Endpoint | Method | 说明 |
|----------|--------|------|
| `/api/inventory/:strategy` | GET | 读取 `inventory_mrpt.json` 或 `inventory_mtfs.json` |

**路由实现 `server/routes/inventory.ts`：**

```typescript
import { Router } from 'express';
import { readJsonFile } from '../utils/fileUtils';
import { getBackendPath } from '../config';

const router = Router();

// GET /api/inventory/mrpt  或  /api/inventory/mtfs
router.get('/:strategy', async (req, res) => {
  const { strategy } = req.params;
  if (!['mrpt', 'mtfs'].includes(strategy)) {
    return res.status(400).json({ error: 'Invalid strategy' });
  }
  const filePath = getBackendPath(`inventory_${strategy}.json`);
  const data = await readJsonFile(filePath);
  res.json(data);
});

export default router;
```

**对接前端：** `InventoryViewer.tsx`
- 当前 mock 数据是 2 个假的 AAPL/MSFT、GOOGL/META 持仓
- 替换为从 API 获取真实 `inventory_mrpt.json` 的 `pairs` 对象
- 遍历 `pairs` 键值对，渲染每个真实持仓（direction、shares、open_date、open_price 等）
- 新增 strategy 切换（MRPT / MTFS tabs）

**数据映射：**
```
API Response                    →  UI 字段
pairs[key].direction            →  dir (long/short)
pairs[key].s1_shares            →  s1 shares
pairs[key].s2_shares            →  s2 shares
pairs[key].open_date            →  openDate
pairs[key].open_s1_price        →  openPrice (s1)
pairs[key].open_s2_price        →  openPrice (s2)
pairs[key].wf_source            →  WF Source
pairs[key].monitor_log          →  最近监测记录
pairs[key].days_held            →  持仓天数
as_of                           →  As Of Date
capital                         →  Base Capital
```

---

### 3.2 Inventory History（历史快照）

| Endpoint | Method | 说明 |
|----------|--------|------|
| `/api/inventory-history/:strategy` | GET | 列出 `inventory_history/` 中对应策略的所有快照 |
| `/api/inventory-history/:strategy/:filename` | GET | 读取单个快照文件内容 |

**路由实现 `server/routes/inventory.ts`（续）：**

```typescript
// GET /api/inventory-history/mrpt
router.get('/history/:strategy', async (req, res) => {
  const { strategy } = req.params;
  const dir = getBackendPath('inventory_history');
  const files = await listFiles(dir, `inventory_${strategy}_*.json`);
  // 返回文件列表：文件名、时间戳、大小
  const result = files.map(f => ({
    filename: path.basename(f),
    timestamp: extractTimestamp(f),  // 从文件名提取 YYYYMMDD_HHMMSS
    size: fs.statSync(f).size
  }));
  res.json(result.sort((a, b) => b.timestamp.localeCompare(a.timestamp)));
});

// GET /api/inventory-history/mrpt/inventory_mrpt_20260321_123653.json
router.get('/history/:strategy/:filename', async (req, res) => {
  const filePath = getBackendPath(`inventory_history/${req.params.filename}`);
  const data = await readJsonFile(filePath);
  // 计算 active pairs 数量
  const activePairs = Object.values(data.pairs || {})
    .filter((p: any) => p.direction !== null).length;
  res.json({ ...data, _activePairs: activePairs });
});
```

**对接前端：** `InventoryHistoryViewer.tsx`
- 当前 mock 是 4 条假的历史记录
- 替换为从 API 获取 `inventory_history/` 目录列表
- 每条显示：snapshot 时间、active pairs 数量、文件名
- 点击某条记录时，请求该文件内容，展示完整仓位快照
- Download 按钮：直接 `window.open(apiUrl)` 下载原始 JSON

---

### 3.3 Trading Signals（交易信号）

| Endpoint | Method | 说明 |
|----------|--------|------|
| `/api/signals/latest/:strategy` | GET | 读取最新的 `mrpt_signals_*.json` / `mtfs_signals_*.json` |
| `/api/signals/combined/latest` | GET | 读取最新的 `combined_signals_*.json` |
| `/api/signals/list` | GET | 列出所有 signal 文件及时间戳 |

**路由实现 `server/routes/signals.ts`：**

```typescript
// GET /api/signals/latest/mrpt
router.get('/latest/:strategy', async (req, res) => {
  const { strategy } = req.params;
  const dir = getBackendPath('trading_signals');
  const pattern = `${strategy}_signals_*.json`;
  const latestFile = await findLatestFile(dir, pattern);
  const data = await readJsonFile(latestFile);
  res.json(data);
});
```

**对接前端：** `SignalTable.tsx`
- 当前 mock 是 4 条假信号（AAPL/MSFT 等）
- 替换为从 API 获取真实的 `active_signals` + `flat_signals` + `excluded_pairs`
- 表格渲染：pair、action、z_score/momentum_spread、shares (s1/s2)、strategy、oos_sharpe

**数据映射：**
```
API Response                           →  UI 字段
active_signals[].pair                  →  pair
active_signals[].action                →  action (OPEN_LONG/OPEN_SHORT/HOLD/CLOSE/CLOSE_STOP)
active_signals[].z_score               →  zScore (MRPT) 或 momentum_spread (MTFS)
active_signals[].s1.shares + s2.shares →  shares 展示
active_signals[].oos_sharpe            →  OOS 参考 Sharpe
flat_signals[].action                  →  FLAT
excluded_pairs[].exclusion_reason      →  排除原因
```

---

### 3.4 Daily Report（每日报告）

| Endpoint | Method | 说明 |
|----------|--------|------|
| `/api/daily-report/latest` | GET | 读取最新的 `daily_report_*.json` |
| `/api/daily-report/latest/txt` | GET | 读取最新的 `daily_report_*.txt`（纯文本） |
| `/api/daily-report/list` | GET | 列出所有 report 文件 |

**路由实现 `server/routes/dailyReport.ts`：**

```typescript
// GET /api/daily-report/latest
router.get('/latest', async (req, res) => {
  const dir = getBackendPath('trading_signals');
  // 只匹配 daily_report_[0-9]*.json（避免匹配 daily_report_mrpt_* 等）
  const latestFile = await findLatestFile(dir, 'daily_report_[0-9]*.json');
  const data = await readJsonFile(latestFile);
  res.json(data);
});

// GET /api/daily-report/latest/txt
router.get('/latest/txt', async (req, res) => {
  const dir = getBackendPath('trading_signals');
  const latestFile = await findLatestFile(dir, 'daily_report_[0-9]*.txt');
  const text = await fs.promises.readFile(latestFile, 'utf-8');
  res.type('text/plain').send(text);
});
```

**对接前端：** `DailyReportViewer.tsx`
- **UI 视图**：从 `daily_report_*.json` 提取 regime、position_monitor、portfolio 等
  - regime.regime_score → 显示分数
  - regime.regime_label → Risk-Off / Neutral / Risk-On badge
  - regime.indicators → VIX、Credit Spread、Yield Curve 等卡片
  - regime.component_scores → 7 类评分展示
  - position_monitor.mrpt / mtfs → OPEN/CLOSE/HOLD 动作列表
- **TXT 视图**：直接渲染后端生成的纯文本报告（完整保留）
- **JSON 视图**：JSON.stringify 展示原始数据

**数据映射（UI 视图 Regime Analysis 部分）：**
```
regime.indicators.vix_level.raw_value          → VIX 数值
regime.indicators.vix_level.history.avg90      → 90 天均值
regime.indicators.hy_spread.raw_value          → HY Spread 数值
regime.regime_score                            → 总分 (0-100)
regime.mrpt_weight / mtfs_weight               → 权重分配建议
regime.interpretation                          → 中文解读
```

**数据映射（Action Required 部分）：**
```
position_monitor.mrpt[] + mtfs[]  中 action 为 OPEN*/CLOSE* 的条目
→ 渲染为 Action Required 卡片
→ pair, action, direction, z_score/momentum
→ s1.symbol, s1.shares, s1.price
→ s2.symbol, s2.shares, s2.price
```

---

### 3.5 Regime Dashboard

| Endpoint | Method | 说明 |
|----------|--------|------|
| `/api/regime/latest` | GET | 从最新 `daily_report_*.json` 提取 regime 字段 |

**路由实现 `server/routes/regime.ts`：**

```typescript
// GET /api/regime/latest
router.get('/latest', async (req, res) => {
  const dir = getBackendPath('trading_signals');
  const latestFile = await findLatestFile(dir, 'daily_report_[0-9]*.json');
  const data = await readJsonFile(latestFile);
  res.json(data.regime);
});
```

**对接前端：** `RegimeDashboard.tsx`
- 替换 hardcoded VIX 24.5 等数据
- 从 `regime.indicators` 提取所有宏观指标
- 从 `regime.component_scores` 提取 7 类评分
- 动态渲染颜色：score > 0.6 用 error，< 0.4 用 success，中间用 text-primary

**数据映射：**
```
regime.indicators.vix_level.raw_value          → VIX Level 卡片
regime.indicators.vix_level.history.change_abs → 变化量
regime.indicators.hy_spread.raw_value          → Credit Spread (HY) 卡片
regime.indicators.yield_curve.raw_value        → Yield Curve 卡片
regime.indicators.move_index.raw_value         → MOVE Index 卡片
regime.mrpt_weight / mtfs_weight               → MRPT / MTFS Weight 卡片
regime.regime_label                            → 如果是 "risk_off" 则显示 Regime Shift warning
```

---

### 3.6 Walk-Forward Summary

| Endpoint | Method | 说明 |
|----------|--------|------|
| `/api/wf/summary/:strategy` | GET | 读取最新 `walk_forward_summary_*.json` |
| `/api/wf/summary/list/:strategy` | GET | 列出所有 summary 文件 |

**路由实现 `server/routes/walkForward.ts`：**

```typescript
// GET /api/wf/summary/mrpt
router.get('/summary/:strategy', async (req, res) => {
  const { strategy } = req.params;
  const dir = strategy === 'mtfs'
    ? getBackendPath('historical_runs/walk_forward_mtfs')
    : getBackendPath('historical_runs/walk_forward');
  const latestFile = await findLatestFile(dir, 'walk_forward_summary_*.json');
  const data = await readJsonFile(latestFile);
  res.json(data);
});
```

**对接前端：** `WalkForwardSummaryViewer.tsx`
- 当前 mock 是 6 个假窗口（2020-2024 日期范围）
- 替换为从 API 获取真实 `walk_forward_summary` 的 `windows` 数组
- 每个 window 渲染：train_start/end、test_start/end、oos_sharpe、oos_pnl、n_selected_pairs
- 新增 MRPT/MTFS 策略切换 tabs
- 底部显示 `oos_stats` 聚合指标

**数据映射：**
```
windows[i].window_idx          → Window 编号
windows[i].train_start/end     → Train Period
windows[i].test_start/end      → Test (OOS) Period
windows[i].oos_sharpe          → Sharpe
windows[i].oos_pnl             → PnL (格式化为 $ 或 %)
windows[i].n_selected_pairs    → Selected Pairs 数
windows[i].selected_pairs      → 点击展开 → 显示 pair + param_set
oos_stats.oos_total_pnl        → 总计 PnL
oos_stats.oos_sharpe           → 综合 Sharpe
oos_stats.oos_max_dd_pct       → 最大回撤 %
```

---

### 3.7 OOS Equity Curve

| Endpoint | Method | 说明 |
|----------|--------|------|
| `/api/wf/equity-curve/:strategy` | GET | 读取最新 `oos_equity_curve_*.csv` 并转为 JSON |

**路由实现 `server/routes/walkForward.ts`（续）：**

```typescript
// GET /api/wf/equity-curve/mrpt
router.get('/equity-curve/:strategy', async (req, res) => {
  const { strategy } = req.params;
  const dir = strategy === 'mtfs'
    ? getBackendPath('historical_runs/walk_forward_mtfs')
    : getBackendPath('historical_runs/walk_forward');
  const latestFile = await findLatestFile(dir, 'oos_equity_curve_*.csv');
  const data = await parseCsvFile(latestFile);
  res.json(data);
});
```

**对接前端：** `EquityChart.tsx`
- 当前 mock 是 7 个月度假数据
- 替换为真实日度 OOS 净值曲线
- X 轴：Date
- Y 轴：OOS_Equity_Chained（如有）或 OOS_Equity
- 可选第二条线：benchmark（=500000 起始的水平线）
- 可添加 Window 分隔虚线
- 更新顶部统计卡片：Total Return、Sharpe Ratio、Max Drawdown（从 wf_summary oos_stats 获取）

**CSV 列映射：**
```
Date                  → x 轴
OOS_Equity_Chained    → equity 线（主线）
OOS_DailyPnL          → 可选日度 PnL bar chart
Window                → 用于画分隔线
```

---

### 3.8 OOS Pair Summary

| Endpoint | Method | 说明 |
|----------|--------|------|
| `/api/wf/pair-summary/:strategy` | GET | 读取最新 `oos_pair_summary_*.csv` 并转为 JSON |

**路由实现 `server/routes/walkForward.ts`（续）：**

```typescript
// GET /api/wf/pair-summary/mrpt
router.get('/pair-summary/:strategy', async (req, res) => {
  const { strategy } = req.params;
  const dir = strategy === 'mtfs'
    ? getBackendPath('historical_runs/walk_forward_mtfs')
    : getBackendPath('historical_runs/walk_forward');
  const latestFile = await findLatestFile(dir, 'oos_pair_summary_*.csv');
  const data = await parseCsvFile(latestFile);
  res.json(data);
});
```

**对接前端：** `OOSPairSummaryViewer.tsx`
- 当前 mock 是 4 对假数据
- 替换为真实 15 对 OOS 聚合表现
- 支持按 PnL / Sharpe / MaxDD 排序
- 新增 MRPT/MTFS 切换 tabs

**CSV 列映射：**
```
Pair       → pair
OOS_PnL    → pnl（格式化为 $xxx 或 +xxx%）
Sharpe     → sharpe
MaxDD      → maxDd（绝对值 $）
MaxDD_pct  → maxDd %
WinRate    → winRate
N_Trades   → trades
Turnover   → turnover
N_Days     → days
```

---

### 3.9 DSR Selection Log（Grid Search）

| Endpoint | Method | 说明 |
|----------|--------|------|
| `/api/wf/dsr-log/:strategy` | GET | 读取最新 `dsr_selection_log_*.csv` 并转为 JSON |

**路由实现 `server/routes/walkForward.ts`（续）：**

```typescript
// GET /api/wf/dsr-log/mrpt
router.get('/dsr-log/:strategy', async (req, res) => {
  const { strategy } = req.params;
  const dir = strategy === 'mtfs'
    ? getBackendPath('historical_runs/walk_forward_mtfs')
    : getBackendPath('historical_runs/walk_forward');
  const latestFile = await findLatestFile(dir, 'dsr_selection_log_*.csv');
  const data = await parseCsvFile(latestFile);
  res.json(data);
});
```

**对接前端：** `WFGridViewer.tsx`
- 当前 mock 是 6 条假的 grid search 结果（P01-P06）
- 替换为真实的 DSR 选择日志（15 对 × 32 param_set × 6 window = ~2880 行）
- Pair 筛选下拉框：从数据动态提取唯一 pair_key
- 表格列改为真实列名：pair_key、param_set、pair_pnl、pair_sharpe、run_sharpe、dsr_pvalue、n_trades、window_idx

**CSV 列映射：**
```
pair_key        → pair（显示为 S1/S2 格式）
param_set       → Param Set 名称
pair_pnl        → 训练期 PnL
pair_sharpe     → 训练期 Sharpe
run_sharpe      → 全组合 Sharpe
dsr_pvalue      → DSR P-Value（越小越显著）
n_trades        → 交易次数
window_idx      → Window 编号
```

---

### 3.10 Pair Universe

| Endpoint | Method | 说明 |
|----------|--------|------|
| `/api/pairs/:strategy` | GET | 读取 `pair_universe_mrpt.json` / `pair_universe_mtfs.json` |
| `/api/pairs/db/:collection` | GET | 从 MongoDB `pairs_day_select` 读取 coint/similar/pca 候选对 |

**路由实现 `server/routes/pairUniverse.ts`：**

```typescript
// GET /api/pairs/mrpt — 已选 15 对
router.get('/:strategy', async (req, res) => {
  const { strategy } = req.params;
  const filePath = getBackendPath(`pair_universe_${strategy}.json`);
  const data = await readJsonFile(filePath);
  res.json(data);
});

// GET /api/pairs/db/coint — MongoDB 中的候选对数据库
router.get('/db/:collection', async (req, res) => {
  const { collection } = req.params; // coint, similar, pca
  const fieldMap: Record<string, string> = {
    coint: 'coint_pairs',
    similar: 'similar_pairs',
    pca: 'pca_pairs'
  };
  const field = fieldMap[collection];
  if (!field) return res.status(400).json({ error: 'Invalid collection' });

  const db = await getMongoDb('someopark');
  const doc = await db.collection('pairs_day_select')
    .find({}, { projection: { day: 1, [field]: 1 } })
    .sort({ day: -1 })
    .limit(1)
    .toArray();

  if (doc.length === 0) return res.json([]);

  const pairs = doc[0][field] || [];
  // 标记哪些已被选为当前 pair universe
  const mrptPairs = await readJsonFile(getBackendPath('pair_universe_mrpt.json'));
  const mtfsPairs = await readJsonFile(getBackendPath('pair_universe_mtfs.json'));
  const selectedSet = new Set([
    ...mrptPairs.map((p: any) => `${p.s1}/${p.s2}`),
    ...mtfsPairs.map((p: any) => `${p.s1}/${p.s2}`)
  ]);

  const result = pairs.map((p: string[]) => ({
    s1: p[0],
    s2: p[1],
    pair: `${p[0]}/${p[1]}`,
    selected: selectedSet.has(`${p[0]}/${p[1]}`) || selectedSet.has(`${p[1]}/${p[0]}`)
  }));

  res.json({ day: doc[0].day, pairs: result, total: result.length });
});
```

**对接前端：** `PairUniverseViewer.tsx`
- 当前 mock 是 3 个 tab 各有 2-5 对假数据
- 替换为：
  - **coint tab**：从 `/api/pairs/db/coint` 获取，高亮已选的
  - **similar tab**：从 `/api/pairs/db/similar` 获取
  - **pca tab**：从 `/api/pairs/db/pca` 获取
- 每对显示 s1、s2、是否 selected（在当前 pair_universe 中）
- sector 信息：如果在 pair_universe JSON 中则取 sector 字段；否则显示 "—"

---

### 3.11 WF Diagnostic

| Endpoint | Method | 说明 |
|----------|--------|------|
| `/api/diagnostic/latest` | GET | 列出 diagnostic XLSX 的所有 sheet 名 |
| `/api/diagnostic/latest/:sheet` | GET | 读取指定 sheet 的数据，转为 JSON |
| `/api/diagnostic/list` | GET | 列出所有 diagnostic XLSX 文件 |

**路由实现 `server/routes/diagnostic.ts`：**

```typescript
import ExcelJS from 'exceljs';

// GET /api/diagnostic/latest — 列出所有 sheet 名
router.get('/latest', async (req, res) => {
  const dir = getBackendPath('historical_runs');
  const latestFile = await findLatestFile(dir, 'wf_diagnostic_*.xlsx');
  const workbook = new ExcelJS.Workbook();
  await workbook.xlsx.readFile(latestFile);
  const sheets = workbook.worksheets.map(ws => ({
    name: ws.name,
    rowCount: ws.rowCount,
    columnCount: ws.columnCount
  }));
  res.json({ file: path.basename(latestFile), sheets });
});

// GET /api/diagnostic/latest/Executive_Summary
router.get('/latest/:sheet', async (req, res) => {
  const { sheet } = req.params;
  const dir = getBackendPath('historical_runs');
  const latestFile = await findLatestFile(dir, 'wf_diagnostic_*.xlsx');
  const workbook = new ExcelJS.Workbook();
  await workbook.xlsx.readFile(latestFile);
  const worksheet = workbook.getWorksheet(sheet);
  if (!worksheet) return res.status(404).json({ error: `Sheet "${sheet}" not found` });

  // 转为 JSON：第一行作为 header，后续行作为数据
  const headers: string[] = [];
  const rows: any[] = [];
  worksheet.eachRow((row, rowNumber) => {
    if (rowNumber === 1) {
      row.eachCell((cell) => {
        headers.push(String(cell.value ?? ''));
      });
    } else {
      const obj: Record<string, any> = {};
      row.eachCell((cell, colNumber) => {
        const key = headers[colNumber - 1] || `col_${colNumber}`;
        obj[key] = cell.value;
      });
      rows.push(obj);
    }
  });

  res.json({ sheet, headers, rows, rowCount: rows.length });
});
```

**对接前端：** `WFDiagnosticViewer.tsx`
- 当前 10 个 tab 全部是 hardcoded mock
- 替换为：
  1. 组件 mount 时请求 `/api/diagnostic/latest` 获取 sheet 列表
  2. 点击某个 tab 时请求 `/api/diagnostic/latest/{sheetName}` 获取该 sheet 数据
  3. 用 `headers` + `rows` 动态渲染表格
  4. 特殊 sheet 的特殊渲染逻辑保留：
     - **Executive_Summary**：保留卡片式布局，从 rows 提取关键指标
     - **Cross_Corr_IS / OOS**：用数值矩阵渲染热力图（将 rows 转为 N×N 矩阵，颜色映射 -1 到 1）
     - **Corr_Shift**：保留表格，按 shift 绝对值着色
     - **Summary_Diagnosis**：渲染为问题卡片（每行一个 issue）
     - **其他**：通用表格渲染

**Cross Correlation 热力图实现方案：**
```
1. 从 API 获取 Cross_Corr_IS / Cross_Corr_OOS 的 rows 数据
2. rows 结构为：{ticker: "AAPL", AAPL: 1.0, MSFT: 0.85, GOOGL: 0.72, ...}
3. 提取 ticker 列表作为 labels
4. 构建 N×N 数值矩阵
5. 用 CSS Grid + 背景色渐变（红=负相关、绿=正相关）渲染热力图
6. 无需额外图表库，纯 div + 颜色映射即可
```

---

### 3.12 Portfolio History / Monitor History（Excel Sheet 查看器）

| Endpoint | Method | 说明 |
|----------|--------|------|
| `/api/monitor-history/list` | GET | 列出 `monitor_history/` 下所有 xlsx 文件 |
| `/api/monitor-history/:filename/sheets` | GET | 列出指定 xlsx 的所有 sheet |
| `/api/monitor-history/:filename/:sheet` | GET | 读取指定 sheet 数据 |
| `/api/portfolio-history/:windowDir/:filename/sheets` | GET | WF window 内的 portfolio history |
| `/api/portfolio-history/:windowDir/:filename/:sheet` | GET | 读取 WF 内的 portfolio sheet |

**路由实现 `server/routes/monitorHistory.ts`：**

```typescript
// GET /api/monitor-history/list
router.get('/list', async (req, res) => {
  const dir = getBackendPath('trading_signals/monitor_history');
  const files = await listFiles(dir, 'monitor_*.xlsx');
  const result = files.map(f => {
    const basename = path.basename(f);
    // 解析文件名：monitor_mrpt_AWK_FOX_20260321_123649.xlsx
    const parts = basename.replace('.xlsx', '').split('_');
    const strategy = parts[1]; // mrpt or mtfs
    const s1 = parts[2];
    const s2 = parts[3];
    const timestamp = `${parts[4]}_${parts[5]}`;
    return { filename: basename, strategy, pair: `${s1}/${s2}`, timestamp };
  });
  res.json(result.sort((a, b) => b.timestamp.localeCompare(a.timestamp)));
});

// GET /api/monitor-history/:filename/sheets
router.get('/:filename/sheets', async (req, res) => {
  const filePath = getBackendPath(`trading_signals/monitor_history/${req.params.filename}`);
  const workbook = new ExcelJS.Workbook();
  await workbook.xlsx.readFile(filePath);
  const sheets = workbook.worksheets.map(ws => ws.name);
  res.json(sheets);
});

// GET /api/monitor-history/:filename/:sheet
router.get('/:filename/:sheet', async (req, res) => {
  const filePath = getBackendPath(`trading_signals/monitor_history/${req.params.filename}`);
  const data = await parseXlsxSheet(filePath, req.params.sheet);
  res.json(data);
});
```

**对接前端：** `PortfolioHistoryViewer.tsx`
- 当前 5 个 sheet tab 全部用 mock 数据（假的 sin 曲线 PnL、假的交易记录）
- 替换为：
  1. 组件接收 `filename` prop（从 artifact data 传入）
  2. mount 时请求 `/api/monitor-history/{filename}/sheets` 获取所有 sheet 名
  3. 显示 5 个核心 sheet 作为 tab + "查看全部 35 sheets" 折叠
  4. 切换 tab 时请求 `/api/monitor-history/{filename}/{sheet}` 获取数据
  5. 特殊渲染：
     - `acc_pair_trade_pnl_history` → Recharts LineChart（date 列做 x 轴，数值列做 y 轴）
     - `recorded_vars` → 双 Y 轴 LineChart（z-score + spread）
     - `pair_trade_history` → 表格（entry/exit、price、PnL）
     - `stop_loss_history` → 表格 + 红色高亮
     - `statistical_test_history` → 表格（ADF p-value 着色）

---

### 3.13 WF Structure Viewer

**无需 API 变更。**

`WFStructureViewer.tsx` 已经是纯前端组件，hardcoded 的文件结构树和 35 sheet 列表是正确的。该组件是教育性质的，不需要从后端读取真实文件树。

保持不变。

---

## 四、前端改造详细方案

### 4.1 新增公共模块

#### `src/lib/api.ts` — API 调用函数集合

```typescript
const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:3001';

async function fetchApi<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) throw new Error(`API error: ${res.status} ${res.statusText}`);
  return res.json();
}

async function fetchText(path: string): Promise<string> {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.text();
}

// Inventory
export const getInventory = (strategy: string) =>
  fetchApi(`/api/inventory/${strategy}`);
export const getInventoryHistory = (strategy: string) =>
  fetchApi(`/api/inventory-history/${strategy}`);
export const getInventorySnapshot = (strategy: string, filename: string) =>
  fetchApi(`/api/inventory-history/${strategy}/${filename}`);

// Signals
export const getLatestSignals = (strategy: string) =>
  fetchApi(`/api/signals/latest/${strategy}`);
export const getLatestCombinedSignals = () =>
  fetchApi(`/api/signals/combined/latest`);

// Daily Report
export const getLatestDailyReport = () =>
  fetchApi(`/api/daily-report/latest`);
export const getLatestDailyReportTxt = () =>
  fetchText(`/api/daily-report/latest/txt`);

// Regime
export const getLatestRegime = () =>
  fetchApi(`/api/regime/latest`);

// Walk-Forward
export const getWFSummary = (strategy: string) =>
  fetchApi(`/api/wf/summary/${strategy}`);
export const getOOSEquityCurve = (strategy: string) =>
  fetchApi(`/api/wf/equity-curve/${strategy}`);
export const getOOSPairSummary = (strategy: string) =>
  fetchApi(`/api/wf/pair-summary/${strategy}`);
export const getDSRLog = (strategy: string) =>
  fetchApi(`/api/wf/dsr-log/${strategy}`);

// Pair Universe
export const getPairUniverse = (strategy: string) =>
  fetchApi(`/api/pairs/${strategy}`);
export const getPairDb = (collection: string) =>
  fetchApi(`/api/pairs/db/${collection}`);

// Diagnostic
export const getDiagnosticSheets = () =>
  fetchApi(`/api/diagnostic/latest`);
export const getDiagnosticSheet = (sheet: string) =>
  fetchApi(`/api/diagnostic/latest/${sheet}`);

// Monitor / Portfolio History
export const getMonitorHistoryList = () =>
  fetchApi(`/api/monitor-history/list`);
export const getMonitorHistorySheets = (filename: string) =>
  fetchApi(`/api/monitor-history/${filename}/sheets`);
export const getMonitorHistorySheet = (filename: string, sheet: string) =>
  fetchApi(`/api/monitor-history/${filename}/${sheet}`);
```

#### `src/hooks/useApi.ts` — 通用 fetch hook

```typescript
import { useState, useEffect } from 'react';

export function useApi<T>(fetchFn: () => Promise<T>, deps: any[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    fetchFn()
      .then(result => { if (!cancelled) setData(result); })
      .catch(err => { if (!cancelled) setError(err.message); })
      .finally(() => { if (!cancelled) setLoading(false); });

    return () => { cancelled = true; };
  }, deps);

  return { data, loading, error };
}
```

#### Loading / Error 状态组件

每个 Viewer 的模板：

```typescript
// 在每个 Viewer 组件中
const { data, loading, error } = useApi(() => getInventory('mrpt'), []);

if (loading) return <LoadingState />;
if (error) return <ErrorState message={error} />;
if (!data) return null;

// ... 渲染真实数据
```

`LoadingState` 使用已有的 shimmer 动画。`ErrorState` 显示红色错误信息 + 重试按钮。

---

### 4.2 Artifact Data Flow 改造

当前流程：
```
ChatArea button click → setActiveArtifact({ type: 'inventory', title: '...' })
→ RightPanel 根据 type 渲染对应 Viewer
→ Viewer 使用 hardcoded mock 数据
```

改造后流程：
```
ChatArea button click → setActiveArtifact({ type: 'inventory', title: '...', params: { strategy: 'mrpt' } })
→ RightPanel 根据 type 渲染对应 Viewer，传入 params
→ Viewer 在 useEffect 中调用 API 获取真实数据
→ 显示 loading → 渲染真实数据
```

**RightPanel 传参改造：**

```typescript
// 现有
{artifact.type === 'inventory' && <InventoryViewer data={artifact.data} />}

// 改为
{artifact.type === 'inventory' && <InventoryViewer params={artifact.params} />}
```

**ChatArea 按钮传参改造：**

```typescript
// 现有
onClick={() => setActiveArtifact({ type: 'inventory', title: 'inventory_mrpt.json' })}

// 改为
onClick={() => setActiveArtifact({
  type: 'inventory',
  title: 'inventory_mrpt.json',
  params: { strategy: 'mrpt' }
})}
```

---

### 4.3 Vite 代理配置

在 `vite.config.ts` 中添加 API 代理（开发环境避免 CORS 问题）：

```typescript
server: {
  hmr: process.env.DISABLE_HMR !== 'true',
  proxy: {
    '/api': {
      target: 'http://localhost:3001',
      changeOrigin: true
    }
  }
}
```

这样前端可以直接 `fetch('/api/inventory/mrpt')` 而不需要完整 URL，生产环境也方便部署。

---

## 五、Server 工具函数详细实现

### 5.1 `server/utils/fileUtils.ts`

```typescript
import fs from 'fs';
import path from 'path';
import { glob } from 'glob';

/**
 * 查找目录中匹配 pattern 的所有文件，按修改时间排序
 */
export async function listFiles(dir: string, pattern: string): Promise<string[]> {
  const fullPattern = path.join(dir, pattern);
  const files = await glob(fullPattern);
  return files.sort((a, b) => {
    // 按文件名中的时间戳排序（文件名包含 YYYYMMDD_HHMMSS）
    return b.localeCompare(a);
  });
}

/**
 * 查找目录中匹配 pattern 的最新文件
 */
export async function findLatestFile(dir: string, pattern: string): Promise<string> {
  const files = await listFiles(dir, pattern);
  if (files.length === 0) {
    throw new Error(`No files matching ${pattern} in ${dir}`);
  }
  return files[0]; // 按字母逆序排列，时间戳最大的排第一
}

/**
 * 读取 JSON 文件
 */
export async function readJsonFile(filePath: string): Promise<any> {
  const content = await fs.promises.readFile(filePath, 'utf-8');
  return JSON.parse(content);
}

/**
 * 从文件名中提取时间戳部分 (YYYYMMDD_HHMMSS)
 */
export function extractTimestamp(filename: string): string {
  const match = path.basename(filename).match(/(\d{8}_\d{6})/);
  return match ? match[1] : '';
}
```

### 5.2 `server/utils/csvParser.ts`

```typescript
import fs from 'fs';
import { parse } from 'csv-parse/sync';

export async function parseCsvFile(filePath: string): Promise<any[]> {
  const content = await fs.promises.readFile(filePath, 'utf-8');
  const records = parse(content, {
    columns: true,          // 第一行作为 header
    skip_empty_lines: true,
    trim: true,
    cast: true              // 自动转换数值
  });
  return records;
}
```

### 5.3 `server/utils/xlsxParser.ts`

```typescript
import ExcelJS from 'exceljs';

export async function parseXlsxSheet(
  filePath: string,
  sheetName: string
): Promise<{ headers: string[]; rows: any[]; rowCount: number }> {
  const workbook = new ExcelJS.Workbook();
  await workbook.xlsx.readFile(filePath);

  const worksheet = workbook.getWorksheet(sheetName);
  if (!worksheet) {
    throw new Error(`Sheet "${sheetName}" not found in ${filePath}`);
  }

  const headers: string[] = [];
  const rows: any[] = [];

  worksheet.eachRow((row, rowNumber) => {
    if (rowNumber === 1) {
      row.eachCell((cell, colNumber) => {
        headers[colNumber - 1] = String(cell.value ?? `col_${colNumber}`);
      });
    } else {
      const obj: Record<string, any> = {};
      row.eachCell((cell, colNumber) => {
        const key = headers[colNumber - 1] || `col_${colNumber}`;
        // 处理日期类型
        if (cell.value instanceof Date) {
          obj[key] = cell.value.toISOString().split('T')[0];
        } else {
          obj[key] = cell.value;
        }
      });
      rows.push(obj);
    }
  });

  return { headers, rows, rowCount: rows.length };
}

export async function listXlsxSheets(filePath: string): Promise<string[]> {
  const workbook = new ExcelJS.Workbook();
  await workbook.xlsx.readFile(filePath);
  return workbook.worksheets.map(ws => ws.name);
}
```

### 5.4 `server/utils/mongoClient.ts`

```typescript
import { MongoClient, Db } from 'mongodb';

let client: MongoClient | null = null;

export async function getMongoClient(): Promise<MongoClient> {
  if (!client) {
    const uri = process.env.MONGO_URI;
    if (!uri) throw new Error('MONGO_URI not set');
    client = new MongoClient(uri);
    await client.connect();
  }
  return client;
}

export async function getMongoDb(dbName: string): Promise<Db> {
  const c = await getMongoClient();
  return c.db(dbName);
}

// 优雅关闭
process.on('SIGTERM', async () => {
  if (client) await client.close();
});
```

---

## 六、开发实施顺序

### Phase 1：基础设施（Day 1）

| 步骤 | 内容 | 涉及文件 |
|------|------|---------|
| 1.1 | 安装依赖 | `package.json` |
| 1.2 | 创建 server/ 目录结构 | `server/index.ts`, `server/config.ts` |
| 1.3 | 实现工具函数 | `server/utils/*.ts` |
| 1.4 | Vite 代理配置 | `vite.config.ts` |
| 1.5 | 前端 API 调用层 | `src/lib/api.ts`, `src/hooks/useApi.ts` |
| 1.6 | Loading/Error 组件 | `src/components/LoadingState.tsx`, `src/components/ErrorState.tsx` |

### Phase 2：JSON 直读类接口（Day 1-2）

这些接口只需要读 JSON 文件，最简单。

| 步骤 | API | 前端组件 |
|------|-----|---------|
| 2.1 | `/api/inventory/:strategy` | `InventoryViewer.tsx` |
| 2.2 | `/api/inventory-history/:strategy` | `InventoryHistoryViewer.tsx` |
| 2.3 | `/api/pairs/:strategy` | `PairUniverseViewer.tsx`（已选对部分） |
| 2.4 | `/api/daily-report/latest` + `/txt` | `DailyReportViewer.tsx` |
| 2.5 | `/api/regime/latest` | `RegimeDashboard.tsx` |
| 2.6 | `/api/signals/latest/:strategy` | `SignalTable.tsx` |
| 2.7 | `/api/wf/summary/:strategy` | `WalkForwardSummaryViewer.tsx` |

### Phase 3：CSV 解析类接口（Day 2-3）

需要 CSV→JSON 转换。

| 步骤 | API | 前端组件 |
|------|-----|---------|
| 3.1 | `/api/wf/equity-curve/:strategy` | `EquityChart.tsx` |
| 3.2 | `/api/wf/pair-summary/:strategy` | `OOSPairSummaryViewer.tsx` |
| 3.3 | `/api/wf/dsr-log/:strategy` | `WFGridViewer.tsx` |

### Phase 4：Excel 解析类接口（Day 3-4）

需要 ExcelJS 解析，最复杂。

| 步骤 | API | 前端组件 |
|------|-----|---------|
| 4.1 | `/api/diagnostic/latest` + `/:sheet` | `WFDiagnosticViewer.tsx` |
| 4.2 | `/api/monitor-history/*` | `PortfolioHistoryViewer.tsx` |

### Phase 5：MongoDB 类接口（Day 4）

需要 MongoDB 连接。

| 步骤 | API | 前端组件 |
|------|-----|---------|
| 5.1 | `/api/pairs/db/:collection` | `PairUniverseViewer.tsx`（3 个 DB tab） |

### Phase 6：集成测试 & 调优（Day 5）

| 步骤 | 内容 |
|------|------|
| 6.1 | 全流程手动测试：每个 Artifact Viewer 点击 → 数据加载 → 渲染检查 |
| 6.2 | 边界情况处理：文件不存在、MongoDB 连不上、空数据 |
| 6.3 | 性能优化：大 CSV 文件分页、Excel 延迟加载、API 结果缓存 |
| 6.4 | ChatArea 中的 mock 对话内容更新为真实数据展示 |

---

## 七、难点与解决方案

### 7.1 Excel 文件解析性能

**问题：** Portfolio History Excel 文件含 35 个 sheet，每个 sheet 可能有几千行日度数据。完整读取一个文件可能需要几秒。

**解决方案：**
1. Sheet 列表请求（`/sheets`）只读元数据，不读内容 — 秒级响应
2. Sheet 数据请求（`/:sheet`）按需加载单个 sheet — 限制在 1-2 秒
3. 服务端添加简单的内存缓存：同一文件的 workbook 对象缓存 5 分钟
4. 对于超大 sheet（>5000 行），API 支持 `?limit=500&offset=0` 分页参数

```typescript
// server/utils/xlsxCache.ts
const cache = new Map<string, { workbook: ExcelJS.Workbook; expiry: number }>();

export async function getCachedWorkbook(filePath: string): Promise<ExcelJS.Workbook> {
  const cached = cache.get(filePath);
  if (cached && cached.expiry > Date.now()) return cached.workbook;

  const workbook = new ExcelJS.Workbook();
  await workbook.xlsx.readFile(filePath);
  cache.set(filePath, { workbook, expiry: Date.now() + 5 * 60 * 1000 });
  return workbook;
}
```

### 7.2 Cross Correlation 热力图

**问题：** `Cross_Corr_IS` 和 `Cross_Corr_OOS` 是 42×42 的相关系数矩阵。前端需要渲染热力图，但当前用的是 random 色块 mock。

**解决方案：**
1. API 返回矩阵数据：`{ labels: string[], matrix: number[][] }`
2. 前端用 CSS Grid 渲染：每个单元格用 `background-color` 映射数值
3. 颜色映射函数：

```typescript
function correlationColor(value: number): string {
  // -1 (红) → 0 (灰) → +1 (绿)
  if (value >= 0) {
    const g = Math.round(180 * value);
    return `rgb(${30}, ${g + 30}, ${30})`;
  } else {
    const r = Math.round(180 * Math.abs(value));
    return `rgb(${r + 30}, ${30}, ${30})`;
  }
}
```

4. 支持 hover 显示精确数值
5. 42×42 = 1764 个 div，渲染性能完全没问题

### 7.3 Monitor History Excel 的 Recharts 适配

**问题：** `recorded_vars` sheet 的 z-score 和 spread 数据需要渲染为双 Y 轴图表。数据从 Excel 解析后是通用的 `{headers, rows}` 格式，需要转换为 Recharts 需要的 `{date, zscore, spread}[]` 格式。

**解决方案：**
1. API 返回原始表格数据
2. 前端在 Viewer 组件内做列名匹配和数据转换：

```typescript
// 在 PortfolioHistoryViewer 内
function transformForChart(apiData: { headers: string[]; rows: any[] }) {
  // 查找日期列（可能叫 Date, date, 日期 等）
  const dateCol = apiData.headers.find(h => /date/i.test(h)) || apiData.headers[0];

  // 对于 recorded_vars，查找 z_score 和 spread 列
  const zCol = apiData.headers.find(h => /z.?score/i.test(h));
  const spreadCol = apiData.headers.find(h => /spread/i.test(h));

  return apiData.rows.map(row => ({
    date: row[dateCol],
    zscore: zCol ? Number(row[zCol]) : null,
    spread: spreadCol ? Number(row[spreadCol]) : null,
    ...row  // 保留其他列
  }));
}
```

### 7.4 大 CSV 文件处理

**问题：** `dsr_selection_log_*.csv` 可能有 ~2880 行（15 对 × 32 参数 × 6 窗口），`oos_equity_curve_*.csv` 可能有 ~150 行。前者较大。

**解决方案：**
1. 2880 行对于 JSON API 来说完全可以（约 500KB），无需分页
2. 前端表格组件用 `overflow-y-auto` 虚拟滚动（当前已有）
3. 筛选逻辑（按 pair_key 过滤）在前端做，减少 API 调用

### 7.5 MongoDB 连接管理

**问题：** 前端 API Server 需要连接 MongoDB 读取 `pairs_day_select` 集合。但 MongoDB URI 存在后端的 `.env` 中。

**解决方案：**
1. 在 `someo-park-investment-management/.env` 中单独配置 `MONGO_URI`（复制后端 .env 中的值）
2. 使用连接池（MongoClient 单例），不在每个请求中新建连接
3. MongoDB 读操作天然只读，不影响后端数据
4. 如果 MongoDB 不可达，该 API 返回 503 + 友好错误信息，前端对应 tab 显示 "数据库不可用"

### 7.6 文件路径安全

**问题：** API 接收的参数（strategy、filename、sheet）可能被注入恶意路径。

**解决方案：**
1. strategy 参数白名单：只接受 `mrpt` / `mtfs`
2. filename 参数：用 `path.basename()` 去除路径分隔符
3. sheet 名参数：从实际 xlsx worksheets 中验证存在性
4. 所有文件访问都基于 `BACKEND_ROOT` 基准路径，不允许 `..` 逃逸

```typescript
// server/config.ts
import path from 'path';

const BACKEND_ROOT = path.resolve(__dirname, '..', process.env.BACKEND_ROOT || '..');

export function getBackendPath(relativePath: string): string {
  const resolved = path.resolve(BACKEND_ROOT, relativePath);
  // 确保不超出 BACKEND_ROOT
  if (!resolved.startsWith(BACKEND_ROOT)) {
    throw new Error('Path traversal attempt detected');
  }
  return resolved;
}
```

---

## 八、前端组件改造要点速查表

| 组件 | API 源 | 改动范围 | 难度 |
|------|--------|---------|------|
| `InventoryViewer` | `/api/inventory/{s}` | 替换 mock 数组 → API 数据，新增 MRPT/MTFS tab | 低 |
| `InventoryHistoryViewer` | `/api/inventory-history/{s}` | 替换 mock 列表 → API 目录列表，点击展开详情 | 低 |
| `SignalTable` | `/api/signals/latest/{s}` | 替换 mock signals 数组 → active_signals + flat + excluded | 低 |
| `DailyReportViewer` | `/api/daily-report/latest` + `/txt` | UI 视图用 JSON 数据重新绑定，TXT 视图直接渲染文本 | 中 |
| `RegimeDashboard` | `/api/regime/latest` | 替换 hardcoded VIX/Spread → indicators 对象 | 低 |
| `WalkForwardSummaryViewer` | `/api/wf/summary/{s}` | 替换 mock windows → windows 数组 + oos_stats | 低 |
| `EquityChart` | `/api/wf/equity-curve/{s}` | 替换 mock 月度数据 → 日度 OOS 净值 CSV 数据 | 中 |
| `OOSPairSummaryViewer` | `/api/wf/pair-summary/{s}` | 替换 mock 4 对 → 真实 15 对 CSV 数据 | 低 |
| `WFGridViewer` | `/api/wf/dsr-log/{s}` | 替换 mock P01-P06 → 真实 DSR 选择日志 CSV | 中 |
| `PairUniverseViewer` | `/api/pairs/{s}` + `/api/pairs/db/{c}` | Mock → JSON + MongoDB 数据，高亮已选 | 中 |
| `WFDiagnosticViewer` | `/api/diagnostic/latest/{sheet}` | 10 个 tab 全部替换为真实 Excel sheet 数据 | 高 |
| `PortfolioHistoryViewer` | `/api/monitor-history/*` | Excel sheet → 图表/表格，需要数据格式转换 | 高 |
| `WFStructureViewer` | 无（纯前端） | 不改 | — |

---

## 九、运行命令

### 开发环境

```bash
cd someo-park-investment-management

# 安装新增依赖
npm install

# 启动 API Server + Vite 前端（并行）
npm run dev:all

# 或分开启动
npm run server    # Express API on :3001
npm run dev       # Vite frontend on :3000
```

### 生产环境（未来）

```bash
# 构建前端
npm run build

# 启动 API Server（同时 serve 前端静态文件）
npm run server:prod
```

---

## 十、验收标准

每个 Artifact Viewer 的验收标准：

1. **点击按钮后**，右侧面板显示 loading shimmer 动画
2. **数据加载完成后**，显示真实后端数据（非 mock）
3. **数据格式正确**：数值正确格式化、日期可读、颜色着色正确
4. **错误处理**：API 不可达时显示友好错误 + 重试按钮
5. **策略切换**：MRPT / MTFS 切换后重新加载对应策略数据
6. **无 console 报错**：开发工具无 TypeScript 错误、无 uncaught promise rejection

**不改变的部分：**
- 整体布局（三栏）不变
- CSS Design System 不变
- WFStructureViewer 不变（纯教育性前端组件）
- 左侧 Sidebar 不变（chat history 等仍为 mock，后续再接）
- Chat 输入框和发送逻辑不变（AI 对话功能后续再接）
- Connection Modal 不变（OpenClaw 接入后续再接）
