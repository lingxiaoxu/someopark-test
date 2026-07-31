# prediction_market_macro — Kalshi 宏观经济市场系统

宏观经济预测市场（Fed/通胀/就业/GDP/油气/利率/汇率等）的系统化交易模块。
与世界杯系统 `prediction_market/` 平行、**完全隔离**：不 import 它的代码，但沿用同一套
架构约定与纪律（venue 封装、devig、$1-cap Kelly、PIT 决策台账、只读审计导出）。

## 目录约定（与 prediction_market/ 对齐）

| 目录 | 职责 |
|---|---|
| `config/` | 全局配置、**系列注册表**（每个 Kalshi series 的 ticker/频率/结算源/发布日历） |
| `ingest/` | 数据源接入：FRED、BLS/BEA/EIA 发布日历、AAA 油价、Kalshi 行情落库 |
| `venues/kalshi/` | Kalshi 发现/行情/订单簿（参考 `prediction_market/venues/kalshi/` 的模式重写，不复用代码） |
| `model/` | 逐系列预测模型：CPI nowcast、初请、NFP、FOMC 反应函数、EIA 库存 → 每个系列一个模块 |
| `strategy/` | devig、edge、仓位（Kelly/$1-cap）、跨场所对比 |
| `exec/` | 下单执行（后期，参考 WC 的 executor 纪律） |
| `jobs/` | 定时任务：数据发布日轮询、逐小时刷新 |
| `ops/` | 导出/报表/PDF（复用 `prediction_market.ops.pdf_style` 的排版风格约定） |
| `research/` | 探索脚本与一次性分析；`discover_series.py` 是市场目录的生成器 |
| `data/output/` | 导出产物（git 可忽略） |
| `docs/` | `SERIES_CATALOG.md`：可交易系列总目录（生成+人工筛注） |
| `tests/` | pytest |

## 运行环境

- conda env：`someopark_run`（与 WC 系统一致）
- 密钥：仓库根 `.env`（FRED_API_KEY 等）+ `prediction_market/.env`（KALSHI keys，后续迁移副本至本模块 `.env`）
- Kalshi 公开行情无需鉴权：`https://api.elections.kalshi.com/trade-api/v2`

## 起步

```bash
conda run -n someopark_run python -m prediction_market_macro.research.discover_series
# → data/output/kalshi_macro_catalog.json + docs/SERIES_CATALOG.md 的数据基础
```
