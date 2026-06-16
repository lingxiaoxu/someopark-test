<p align="center">
  <img src="../public/SOMEO PARK矢量源文件 Big Square.svg" alt="Someopark" width="160"/>
</p>

<h1 align="center">Private Credit — BDC Look-Through Engine</h1>
<p align="center"><b>把 BDC sleeve 追踪的 5 只 BDC 股票,穿透到它们 SEC 披露的底层私募信贷贷款</b></p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11-blue?logo=python&logoColor=white"/>
  <img src="https://img.shields.io/badge/conda-someopark__run-green?logo=anaconda&logoColor=white"/>
  <img src="https://img.shields.io/badge/source-SEC%20EDGAR%20SOI-orange"/>
  <img src="https://img.shields.io/badge/BDCs-GBDC%20TSLX%20OBDC%20BXSL%20ARCC-purple"/>
  <img src="https://img.shields.io/badge/deals-~4%2C800%2Fquarter-teal"/>
  <img src="https://img.shields.io/badge/rates-FRED%20%7C%20Nelson--Siegel-lightgrey"/>
</p>

---

本模块原是一个固收/私募信贷分析引擎(IRR/久期/现金流/信用评分/Nelson-Siegel 利率)。2026-06 起被**盘活为实盘 look-through 系统**:不再用虚构样例,而是从 **SEC Schedule of Investments(SOI,Inline-XBRL)** 拉取 BDC sleeve 实际持有的 5 只 BDC 股票的**底层逐笔贷款**,每日重估值并产出 sleeve 级穿透敞口。

| 追踪的 BDC | 全称 | sleeve 权重 |
|---|---|---|
| **GBDC** | Golub Capital BDC | 80% |
| **TSLX** | Sixth Street Specialty Lending | 5% |
| **OBDC** | Blue Owl Capital Corp | 5% |
| **BXSL** | Blackstone Secured Lending | 5% |
| **ARCC** | Ares Capital Corp | 5% |

> 权重与 sleeve 配置(50% BDC / 50% cash)来自仓库根 `UpdateBDCPerformance.py`。底层数据为 SEC 季度披露(财季末 +28~57 天到齐),因此 look-through 是"**最新已披露季度 × 当日利率曲线**";新鲜度元数据随每份输出。

---

## 架构

整条链路由仓库根的**整合层脚本**驱动(本模块提供建模引擎):

```
┌─ 每日 production run(conductor/bdc_daily_pipeline.sh,目标窗口 19:00–19:30 ET)──┐
│  A  SyncPrivateCreditRates.py   利率统一:MacroStateStore → fred_rates.csv       │
│  C  RefreshBDCHoldings.py        SOI ingest(仅在有新 10-Q/10-K 时,filing 驱动) │
│       通道A=SEC BDC Data Sets soi.tsv │ 通道B=inline-XBRL instance(兜底)        │
│       → price_data/bdc_holdings/{TICKER}/soi_{date}_{adsh}.parquet (PIT 快照)    │
│  D  RunBDCLookThrough.py         每日重估值(forward-SOFR)+ 持仓 diff 引擎       │
│       → 本模块 bdc_results/ + public/data/bdc_lookthrough_latest.json            │
└──────────────────────────────────────────────────────────────────────────────┘
        ↑ 本模块(portfolio_of_private_credit_deals/)= 建模引擎,被上面调用
        ↓ Someo Agent 只读工具 portfolio_bdc_holdings 服务预计算结果(秒回)
```

> 整合层脚本(`RefreshBDCHoldings.py` / `SyncPrivateCreditRates.py` / `RunBDCLookThrough.py` / `conductor/bdc_daily_pipeline.sh`)在 **someopark-test 仓库根**,不在本模块内。本 README 聚焦本模块的建模引擎。

---

## 环境配置

```bash
# someopark_run conda 环境(与主仓库共用)
conda run -n someopark_run --no-capture-output python <script.py>
```

| 凭证 | 用途 | 来源 |
|---|---|---|
| `FRED_API_KEY` | 利率序列 | 仓库根 `.env`(经 MacroStateStore 统一接入) |
| **EDGAR** | SOI 抓取 | **免 key**,仅需 User-Agent(`admin@someopark.com`) |
| `OPENAI_API_KEY`(`config.py`) | **仅 legacy PDF memo 旁路**,BDC 主路径不需要 | `cp config_template.py config.py` |

> `config.py` / `.env` / `fred_rates.csv` / 生成数据 均已 `.gitignore`,不入版本库。BDC 路径**不**经 OpenAI/PDF。

---

## 核心文件

### BDC Look-Through 层(2026-06 新增)

| 文件 | 说明 |
|---|---|
| `bdc_deal_loader.py` | SEC SOI 快照 → 模块 deal 契约(原 11 列 + 21 个真实 SOI 列:fair_value/cost/spread/pik_rate/pct_nav/deal_uid/…)。`load_bdc_deals()` / `write_bdc_deal_start()` |
| `bdc_credit.py` | **SOI-mode 信用评分**:SOI 不披露 EBITDA/杠杆,改用 mark(FV/cost)、spread 分位、PIK、non-accrual、优先级、sector 乘数评 0–120 分,复用现有 recovery/PD/stress 映射。方向性已验证(PIK/低mark → 低分) |
| `bdc_cashflow.py` | 在现有引擎上**附加**真实 SOI 现金流列:现金/PIK 息拆分、OID 拉平、ExitValue(par vs mark)、non-accrual;浮动腿逐期用 **Nelson-Siegel forward SOFR 曲线**(`build_forward_sofr_curve`,从 live fred_rates 重拟合) |
| `bdc_lookthrough.py` | sleeve 级聚合:top 发行人(跨 BDC)、行业敞口(FV 加权 + 等权 BDC 对照)、加权 spread/all-in/IRR/信用分、PIK 占比、non-accrual、mark 分布、maturity ladder、利率敏感度、早期预警 |
| `bdc_sector.py` + `bdc_sector_map.yaml` | SOI 原始行业(100+ filer 变体)→ canonical sector + 风险乘数(关键词规则,鲁棒于新行业) |
| `bdc_calibration.py` | 适配器:真实 deal → `EnhancedLoanSpec`(供 enriched 分析);run_synthetic 参数从真实组合统计校准 |
| `tests/fixtures/` | 5 笔原 demo deal,作 fundamental-mode 回归基准保留 |

### 底层引擎(沿用,数学基础)

| 文件 | 说明 |
|---|---|
| `bond_utilities.py` | 债券/贷款数学:`LoanSpec`、`generate_loan_schedule`(amort/IO/PIK/fees)、XIRR/久期/凸性 |
| `credit_risk_module.py` | `CreditRiskCashflowIntegrator`:credit_score → recovery/PD/stress 分档 + 风险调整 spread(static/advanced 双模式) |
| `forward_rate_lookup.py` | 利率查询:历史查 `fred_rates.csv`,未来查 Nelson-Siegel forward |
| `forward_rate_projections.py` / `yield_curve_modeling.py` | Nelson-Siegel 曲线拟合 + forward 投影生成 |
| `cashflow_exporter.py` | 现金流表导出 |
| `enriched_bond_portfolio.py` | 组合级分析(OU/有效前沿,用于可市场化标的) |
| `run_deals.py` / `run_synthetic.py` | legacy 编排入口(real deal / 合成情景);`download_fred_data.py` 已 **DEPRECATED**(被 SyncPrivateCreditRates 取代) |

---

## 数据来源:SEC Schedule of Investments

BDC 财报的 SOI 自 SEC Release 33-10771 起被 **Inline-XBRL 逐笔结构化**(typed dimension `InvestmentIdentifierAxis`)。两条免费通道:

| 通道 | 来源 | 用途 |
|---|---|---|
| **A(首选)** | SEC BDC Data Sets 月度包 `soi.tsv` | 逐笔财务字段(FV/cost/principal/spread/PIK) |
| **B(兜底)** | 逐 filing inline-XBRL instance | 月度包滞后时;行业(文档顺序分组)、对账 |

下游对两通道无感(统一列名)。逐笔财务**对账**:companyfacts 净额做锚 + `gross_net_ratio` 透明披露(合并子公司 gross-up,如 ARCC Ivy Hill/SDLP)。

---

## 运行

```bash
# 整条 production pipeline(仓库根,目标窗口 19:00–19:30 ET,调度由外部安排)
bash conductor/bdc_daily_pipeline.sh daily

# 单步(均支持 --sandbox DIR 零生产污染 / --dry-run)
set -a && source .env && set +a
conda run -n someopark_run --no-capture-output python SyncPrivateCreditRates.py     # 利率
conda run -n someopark_run --no-capture-output python RefreshBDCHoldings.py          # SOI ingest
conda run -n someopark_run --no-capture-output python RunBDCLookThrough.py           # 重估值 + diff

# 仅本模块:从快照重建 look-through(调试)
conda run -n someopark_run --no-capture-output python bdc_lookthrough.py --csv <bdc_deal_start.csv>
```

**幂等**:同 `(manifest hash, rates date)` 不重算。**filing 驱动**:无新 10-Q/K 即 skip(一年约 20 次真正 ingest)。

---

## 输出

| 文件 | 内容 |
|---|---|
| `price_data/bdc_holdings/{TICKER}/soi_*.parquet` | PIT 逐笔快照(append-only,永不覆盖) |
| `price_data/bdc_holdings/latest_manifest.json` | 各 BDC adsh/reportDate/对账/coverage/non-accrual |
| `bdc_results/bdc_lookthrough_{asof}.json` | 全量逐笔 + 聚合 |
| `bdc_results/daily_report_{date}.json` | 每日重估值 + diff 摘要 + 股价层并排 |
| `bdc_results/diff_{asof}.json` | **新增/变化/exit deal + 预警**(mark 恶化、PIK 上升) |
| `public/data/bdc_lookthrough_latest.json` | sleeve 聚合(供 agent 工具),<2MB |

**diff 引擎(系统化增量)**:基于稳定 `deal_uid`(=sha1(cik|完整 identifier))跨季三向 diff——新增入库、exit 标记不删除(PIT 留痕)、变化记录向量(加减仓/重定价/mark 迁移/PIK/non-accrual)。

---

## 数据可得性说明(诚实标注,非简化)

SOI 不披露发行人私有信息,以下经调研确认为真实数据墙,均**透明标注**:

| 字段 | 状态 |
|---|---|
| **EBITDA / 杠杆** | SOI 不披露 → SOI-mode 评分改用市场信息(mark/spread/PIK),`credit_mode` 标注,不与 fundamental-mode 横比 |
| **maturity** | XBRL 不逐笔标注 → 从主-HTML SOI 表逐行提取(inline-XBRL 事实与 maturity 日期同 `<tr>`,取最晚日期):**BXSL 98% / TSLX 99% 真实**(`maturity_source='primary_html'`);GBDC/ARCC/OBDC 的表不以 `<tr>` 结构化/日期稀疏 → 回退 instrument-type `imputed_tenor`(标注、占比披露) |
| **non-accrual** | 无标准 XBRL 元素 → **BDC 级**比率从 MD&A 文本提取(5/5);**逐笔**标记从 SOI 行脚注编号提取(5/5,共 ~90 笔),feeds 早期预警 + SOI 评分 |
| **floor / affiliation** | XBRL 未逐笔可靠标注(floor 用通用占位 ID;affiliation 在 bleeding 小计)→ 留 None,代码前向兼容。floor 在当前 SOFR≫典型 floor 环境不触发 |

> maturity 只影响现金流投影期限;核心敞口分析(issuer/sector/spread/mark/PIK/non-accrual)全部为真实披露数据。

---

## 与 someopark-test 的集成

- **利率统一**:本模块利率经 `SyncPrivateCreditRates.py` 走仓库级 `MacroStateStore`(全项目一个 FRED 接入点,新增 SOFR/DGS2/5/10/30 五序列;不影响 MCPS/MRPT/MTFS)。
- **Agent 工具**:`someo-park-investment-management/server/tools/portfolioBdcHoldingsTool.ts` 只读服务 `bdc_lookthrough_latest.json`(绕开 pythonBridge 60s 限制);legacy `portfolioRunExistingTool` 现读 `tests/fixtures/`。
- **股价层对照**:daily_report 并排 BDC 股价 sleeve 市值(`UpdateBDCPerformance.py` 维护的 `private_credit_bdc_performance.json`)与 look-through 层,各标 as-of。

---

*Institutional private-credit look-through: real SEC Schedule-of-Investments holdings, SOI-mode credit analytics, daily forward-rate re-valuation, and a systematic holdings-diff engine — built on the module's existing fixed-income math.*
