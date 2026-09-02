<p align="center">
  <img src="../../public/SOMEO PARK矢量源文件 Big Square.svg" alt="Someopark" width="120"/>
</p>

<h1 align="center">AI Electric Utilities Strategy (AEUS)</h1>
<p align="center"><b>Institutional-grade AI-power value-chain rotation that aims to beat XLU &amp; GRID — powered by qlib</b></p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11-blue?logo=python&logoColor=white"/>
  <img src="https://img.shields.io/badge/conda-qlib__run-green?logo=anaconda&logoColor=white"/>
  <img src="https://img.shields.io/badge/universe-10%20subsectors%20%C3%97%2041%20stocks%20(%2B10%20reserves)-orange"/>
  <img src="https://img.shields.io/badge/rebalance-monthly%20(V1)%20%7C%20semi--monthly%20(V2)-purple"/>
  <img src="https://img.shields.io/badge/direction-long--only-teal"/>
  <img src="https://img.shields.io/badge/benchmarks-XLU%20%7C%20GRID%20%7C%20SPY-lightgrey"/>
</p>

---

> **隔离原则**：本策略只使用 `qlib_run` conda 环境，**绝不调用 `someopark_run`**。
> 它**只写入本目录**，外加 `price_data/elec_strategy/` 下的*增量*文件（回测 Excel 落 `historical_runs/electric_utilities_strategy/`）；从不修改 `semiconductor_strategy/`、根脚本或任何已有数据文件。
> `price_data/macro/` parquets 为**只读**（someopark 主 pipeline 写入）；macro 模块的 EIA 镜像（`price_data/eia/`）与 ERCOT dashboard sqlite 同为**只读复用**。

---

> **AEUS 存在的唯一理由：跑赢简单持有 XLU 或 GRID。**
> 已验证的 default 配置（校准 v2 图谱）在 Sharpe **和** CAGR **和** 回撤上**同时**优于两者：
>
> | (2019-01-02 → 2026-08-27, IS) | CAGR | Vol | Sharpe | Calmar | MaxDD |
> |---|---|---|---|---|---|
> | **AEUS (default)** | **36.5%** | 24.4% | **1.40** | **1.37** | **−26.7%** |
> | XLU | 13.0% | 20.8% | 0.69 | 0.37 | −35.4% |
> | GRID | 23.7% | 24.2% | 1.00 | 0.58 | −40.6% |
> | SPY | 17.6% | 19.3% | 0.94 | 0.52 | −33.7% |
>
> `bash aeus_pipeline.sh validate` 可复现该裁决（exit 0 = PASS）。
>
> **诚实口径（walk-forward 可实现 OOS，70 折，无前视）**：Sharpe **1.27** / CAGR **30.6%** / MaxDD **−22.1%**，WFE 1.76，oracle 天花板 2.39。上表是全样本 IS；做预期管理请看这一行。

---

## 策略概览

跨 10 个 AI-power 产业链**子板块**（上游燃料 → 下游数据中心供电）进行多因子轮动。每个子板块是一个固定 **base_w 向量**的 4–5 只股票合成篮子（总收益篮子），可交易资产是子板块本身——引擎按月在子板块**之间**轮动，篮子**内部**保持先验比例（κ>0 时叠加纯度倾斜，见下）。共 41 只独立加权股票 + 每板块 1 只 0% 储备股（共 10 只入库）+ XLU / GRID / SPY 基准。

> **比 SSRS 多一层（个股执行层，承自 AISS）**：AEUS 的"ETF 层"是 subsector（合成篮子，不可直接下单），所以日度信号**多一步**——把 subsector 目标权重按 base_w（PIT 正确，晚期 IPO 自动剔除并重归一；AISS 的 80/15/5 是 3 档特例，AEUS 泛化为 N 档）分解到**真实个股持仓/订单**，并**按 ticker 跨子板块聚合**。回测端对应 Excel 的 7 个 `*_stock_decomp` sheet，实盘端对应日报的股票层 + inventory 的 `stock_holdings`（见"输出"与 `stock_decompose.py`）。

| 维度 | 设计 |
|---|---|
| 标的池 | 10 子板块：nuclear_fuel · gas_midstream · grid_equipment · grid_epc · ipp_wholesale · regulated_mega · regional_utility · dc_power_cooling · renewables_storage · water_cooling |
| 基准 | SPY（beta / 信息比率）；**XLU & GRID = 胜负门槛（必须同时跑赢）**；50/50 XLU+GRID 日度再平衡混合 = active-return 基准 |
| 回测起点 | 2019-01-01（晚期 IPO 地板：GEV/CEG/VRT/OKLO/SMR/NXT/FLNC/TLN/ARRY/BE/TT/CARR/WTRG 上市后 24 个月才进篮） |
| 调仓频率 | V1 每月首个交易日（生产默认） / V2 半月度（1 日 + ~月中） |
| 方向 | 纯多头，无做空，基础配置无杠杆 |
| 胜负门槛 | 在 Sharpe **且** CAGR 上同时跑赢 XLU **和** GRID |

### 子板块篮子（base_w 向量 + 0% 储备）

| 子板块 | 加权成员（base_w） | reserve 0% | 链位/周期角色 |
|---|---|---|---|
| 核能与铀燃料 nuclear_fuel | BWXT .45 · LEU .20 · UUUU .15 · OKLO .10 · SMR .10 | NXE | 最上游，核电复兴（滞后 ~6 月） |
| 天然气中游 gas_midstream | KMI .35 · WMB .30 · OKE .20 · TRGP .15 | LNG | 最上游燃料链，DC 基载气 |
| 输配电设备 grid_equipment | ETN .40 · EMR .25 · GEV .20 · POWL .15 | VMI | 上游设备，变压器瓶颈 |
| 电网施工 EPC grid_epc | PWR .45 · FIX .25 · STRL .15 · MYRG .15 | DY | 中游服务，接网/变电站 |
| 独立发电 ipp_wholesale | VST .40 · CEG .30 · NRG .20 · TLN .10 | ORA | AI-PPA 主战场（0 滞后） |
| 受监管龙头 regulated_mega | NEE .35 · SO .25 · DUK .25 · AEP .15 | D | rate base 增长（最慢，防御位） |
| 区域公用 regional_utility | AEE .35 · LNT .25 · OGE .20 · ATO .20 | BKH | 下游配电（防御位） |
| DC 电力冷却 dc_power_cooling | VRT .45 · TT .25 · CARR .20 · BE .10 | AOS | 最下游，DC 建设同步 |
| 绿电储能 renewables_storage | NXT .40 · FSLR .25 · FLNC .20 · ARRY .15 | SHLS | 中游绿电，绿色算力承诺 |
| 水务冷却 water_cooling | AWK .40 · WTRG .30 · AWR .15 · CWT .15 | YORW | 辅助基础设施（防御位） |

> **储备股（0% 配比）**：经筛选的"数据就绪备胎"，平时**不买**。仅当加权成员**不能用了**（停牌/退市/数据断流）——其权重才**自动级联给储备股**（若储备股当时可用），否则回退到剩余存活档按比例分。机制与 AISS 逐字相同：`universe.effective_weights(unavailable=)` + `loader.build_subsector_prices`。water_cooling 的 reserve 原为 SJW，2025-05 被收购退市后换 YORW（价格数据实证）。
>
> 晚期 IPO / 分拆（GEV 2024-04、CEG 2022-02、VRT 2020-02、OKLO 2024-05、SMR 2022-05、NXT 2023-02、FLNC 2021-10、TLN 2023-07、ARRY 2020-10、BE 2018-07、TT 2020-03、CARR 2020-04、WTRG 2020-02）只有在累计 24 个月历史后才并入篮子（PIT 正确；在此之前权重按比例重归一到存活成员）。IPO 缺口**不是**"意外"，不触发储备股。
>
> **纯度倾斜（AEUS 对 AISS 的机制扩展，`signals.purity_tilt`）**：板块内权重按 `w_i ∝ eff_w_i × (1 + κ × purity_i × g_s(t))` 再归一——purity 是个股对"AI 电力"主题的静态纯度分（universe.py 单一真源，GEV=1.0、EMR=0.35…），g_s 是该板块当月图谱分。**κ=0 为 config 默认 = 逐 bit 等价 AISS 静态行为（回归锚）**；Group N 参数组探 0.3/0.5，由 walk-forward 裁决生产值，不拍脑袋。

### 信号架构（4 因子，月度，Regime 条件）

| 信号 | 权重 | 方法 |
|---|---|---|
| 横截面动量（`cs_momentum`） | 0.30 | 子板块篮子 12-1 月相对回报，横截面 z-score |
| **供应链（`supply_chain`）** | **0.35** | **知识图谱传播** —— 每个子板块由滞后的上游驱动因子打分（**AEUS 核心 alpha**；`signals/supply_chain.py`） |
| CapEx 脉冲（`capex_pulse`） | 0.25 | 超大规模云厂商 AI-CapEx 脉冲（MSFT/GOOGL/META/AMZN 3 月动量 z-score，**与 AISS 共享同一最上游驱动**）按子板块 beta 倾斜 |
| 周期 Regime（`cycle_regime`） | 0.10 | VIX + CapEx regime 倾斜（防御 vs AI 周期） |

> 知识图谱是电力版：**5 个宏观节点** —— `ai_capex_proxy`（与 AISS 共享）、`power_demand_proxy`（**去天气** EIA 需求：实际需求减 STEO CDD/HDD 回归拟合的残差 = 结构性 AI 负荷）、`power_price_proxy`（Henry Hub + 天然气库存 blend）、`rate_env_proxy`（10Y **取负**，公用事业债券代理通道）、`industrial_demand_proxy`（IPUTIL）。**23 条先验边（V1 硬编码）+ 28 条校准 v2 边（config）**：滞后由 `graph_calibration.py` 在**因子残差收益**上取 IC-argmax 得到；首轮校准 5 条候选边全部保留（最高 IC +0.209）。
>
> 外部**确认 tilt（每板块 ≤2 条 × 0.30）**：EIA 燃料结构（gas/nuclear/solar+wind）、XBRL 在手订单 RPO（GEV/PWR/EMR/ETN，$219B，YoY +40%，**成分匹配 YoY**）、变压器 PPI（PCU335311335311）、utility capex（NEE/DUK/SO）、water capex（AWK）、DC 重镇州电价溢价（VA/GA/TX/AZ/OH）、建筑用工、ERCOT DAM 枢纽电价 + AS 稀缺度、PJM 西枢纽（等 key，门控中）。

### Regime 四态定义

4 态（risk_on / transition_up / transition_down / risk_off），VIX 阈值（25 / 32）承自 AISS（Group D/H 参数组 + walk-forward 实证再调），动态修正四个因子权重并在 risk-off 时给三个防御板块加分。

| 状态 | `cs_mom` | `supply_chain` | `capex_pulse` | `cycle_regime` | 说明 |
|---|---|---|---|---|---|
| `risk_on` | 1.1 | 1.1 | 1.2 | 0.8 | 强化进攻因子 |
| `transition_up` | 1.1 | 1.1 | 1.1 | 0.9 | 温和进攻 |
| `transition_down` | 0.8 | 0.9 | 0.7 | 1.3 | 压制 capex，提升周期防御 |
| `risk_off` | 0.5 | 0.5 | 0.3 | 1.8 | 大幅压制进攻；**regulated_mega / regional_utility / water_cooling 三防御位各 +0.40** |

### Portfolio & Risk（高信念卫星仓）

top-3 子板块（10 选 3，截面比 AISS 更厚），max-weight 0.55，逆波动率优化器，**30% 波动目标**（AISS 起点值，Group C 扫 0.18–0.40），beta 0.40–3.00（允许防御低 beta 倾斜，也不把高 beta 强拉回 1.0），月度调仓，阶梯式 VIX 去风险（28→10% / 32→25% 现金），−25% 回撤减半，3 个极端止损。

---

## 环境配置

### 1. 创建 qlib_run 环境

```bash
conda create -n qlib_run python=3.11
conda run -n qlib_run pip install qlib yfinance polygon-api-client fredapi \
    pandas numpy scipy statsmodels matplotlib pyportfolioopt pytest pytz \
    pandas_market_calendars openpyxl
```

> AEUS 与 AISS / SSRS 共用同一个 `qlib_run` 环境。**绝不使用 `someopark_run` 或系统 Python。**

### 2. 配置 API Key

```bash
# 在项目根目录 someopark-test/.env 中：
POLYGON_API_KEY=your_polygon_api_key_here   # 股票价格 + SEC 数据
FRED_API_KEY=your_fred_api_key_here         # 宏观指标
EIA_API_KEY=your_eia_api_key_here           # EIA v2（与 macro 模块共享）
ERCOT_API_...                               # ERCOT Public API 四变量（2026-08-30 已验证）
PJM_API_KEY=...                             # 2026-09-01 到手（Data Miner 2 非会员 key）；config 已翻 true
```

> `.env` 已在 `.gitignore`。Pipeline 会自动从 `.env` grep 出所需 key（不调用 `source .env`，避免 job-control 噪音）。SEC 无需 key（`AEUS_SEC_USER_AGENT` 可选覆盖）。
> ⚠️ **PJM / ERCOT 数据绝不 commit**（仓库 PUBLIC，数据条款 internal-use-only）。

### 3. 正确运行方式

**所有命令从项目根目录（`someopark-test/`）运行：**

```bash
# 统一入口（推荐 —— 自动加载 key + 选择 qlib_run）
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh [MODE] [OPTIONS]

# 直接调用 Python（仅开发调试）
set -a && source .env && set +a
conda run -n qlib_run --no-capture-output python -m electric_utilities_strategy.<module>
```

> 直接 `python` 或 `conda activate` 均不可靠——`conda run -n qlib_run --no-capture-output` 是确保环境正确的唯一方式。

---

## Pipeline 快速参考

```bash
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh [MODE] [OPTIONS]
```

| MODE | 说明 | 典型耗时 |
|---|---|---|
| `update_data` | 增量刷新全部数据源（**9 步**：prices / capex 脉冲 / utility+water capex / hyperscaler capex / EIA 月度 / EIA 日频需求+STEO 度日 / 装机+分州电价+FRED / ERCOT / PJM-门控） | 1–5 min |
| `daily` | 生成当日信号（subsector + **个股订单**）+ 更新 `inventory_aeus.json`（**NYSE 节假日感知**；先跑非致命 update_data） | 2–5 min |
| `weekly` | 全宇宙价格刷新 + 数据/PIT 健康检查（STALE 显式 FAILED 横幅）+ **Weekly Review** + dry-run | 5–15 min |
| `monthly` | `daily_backtest`（V1+V2 选参刷新 P0，恢复 V1）+ force-rebalance（**节假日感知**） | 20–40 min |
| `dry-run` | 只读每日信号，不写 inventory，随时可运行 | 1–2 min |
| `backtest` | 用 active/selected 参数集跑全期回测 | ~2 s（数据已缓存） |
| `batch` | 批量运行全部 **42** 个参数集 → 排名 CSV/Excel | ~2–3 min |
| `select` | batch + WF OOS 过滤 + MCPS 选参 → `selected_param_set.json` | 5–15 min |
| `daily_backtest` | **V1+V2 全套**：batch IS + WF IS-OOS + diagnostic + PDF + select + validate（刷新 smart_select 的 P0 缓存，结束恢复 V1）（**节假日感知**） | 20–40 min |
| `walk-forward` / `wf` | 独立 Walk-Forward IS/OOS（anchored + rolling） | 3–8 min |
| `validate` | 回测 + 对比 XLU/GRID/SPY → PASS/FAIL（胜负门槛） | ~3 s |
| `tearsheet` | 多页 PDF 绩效报告（含 XLU/GRID 叠加页） | 视参数集而定 |
| `test` | pytest 套件（173 测试，纯合成数据，无网络） | ~10 s |
| `status` | 打印当前持仓 + 最新信号摘要 | < 5 s |
| `help` | 打印帮助 | — |

### 常用 OPTIONS

| 选项 | 说明 | 默认值 |
|---|---|---|
| `--signal-version v1\|v2` | 信号版本（V1=月度, V2=半月度），用于 daily/backtest/batch/select/wf/validate/tearsheet | smart_select 自动选；config 默认 v1 |
| `--param-set NAME` | 指定参数集（不依赖 selected_param_set.json） | selected / default |
| `--date YYYY-MM-DD` | 覆盖信号日期 | 最近交易日 |
| `--force-rebalance` | 强制全额再平衡：绕过月度调度 + zscore 阈值过滤 + 同日幂等护栏 | 关 |
| `--skip-holiday` | 跳过 NYSE 节假日检查（回填 / 手动运行；shell 层唯一自解析的 flag） | 关 |
| `--dry-run` | 不写 inventory | 关 |

> **NYSE 节假日检查**：`daily` / `monthly` / `daily_backtest` 在周末/节假日会跳过工作并 `exit 0`（正常成功，非失败），由 `pandas_market_calendars`（qlib_run）判定，失败时降级为工作日检查。`weekly` / `dry-run` 始终运行。除 `--skip-holiday` 外，所有 flag 原样转发给 Python 入口自行解析（对 SSRS parser 的一处有意偏离）。

---

## 快速开始

```bash
# ── 首次：初始化数据层（见下方"数据层维护"）
# ── 每日运行（NYSE 节假日自动跳过）
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh daily

# ── 安全测试（不写 inventory）
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh dry-run

# ── 当前持仓
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh status

# ── 胜负门槛裁决（vs XLU / GRID / SPY）
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh validate

# ── 全量重套：V1+V2 batch + WF IS-OOS + PDF + select（刷新 P0，恢复 V1）
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh daily_backtest

# ── 参数选优（月度）：batch + WF OOS 过滤 + MCPS → selected_param_set.json
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh select

# ── 独立 Walk-Forward IS/OOS
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh walk-forward

# ── 指定参数集回测
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh backtest --param-set pure_supply_chain

# ── V2 半月度版本
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh validate --signal-version v2

# ── 测试套件
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh test
```

---

## 事件风险降险 overlay（phase-1 关闭，机制完整保留）

AISS 已验证的事件降险层（NFP 前高 beta + 龙头财报传染 → 砍半到 cash）在 AEUS **完整保留但 phase-1 关闭**：`config.yaml` → `risk.event_derisk.enabled: false`。

原因（AEUS_PLAN §6C）：根目录共享的 `EventRiskDetector.py` / `RefreshEventRiskData.py` 读的是**半导体** universe 文件（`price_data/semiconductor_universe.json` 域）；电力事件 universe 文件 + 检测器参数化属于要改共享文件的 **C 级接线**，需单独请示批准。daily 模式里对应的 event-risk 刷新步已注释（留 TODO），避免 AEUS 去刷别家数据。

- 参数原封保留待接线：`beta_threshold` 2.5 · `nfp_window_days` 2 · `bellwether_drop` −0.045 · `sell_frac` 0.5 · `beta_mode` bottomup
- 接线获批后：恢复 pipeline daily 的刷新步 + config 翻 `enabled: true`，执行机器（`reduce_next_open` → `event_derisk` 调仓分支）与 AISS 同一套，无需新代码

---

## 数据层维护（PIT，可回填，生产增量）

AEUS 的"另类数据"层全部隔离在 `price_data/elec_strategy/` 下（`prices/industry/company/altdata/cache` 五子目录），采用**仅追加的 PIT 冻结**存储（数据刷新不会改写历史 → 可复现）。**外部数据只在月度采样点进决策**（图谱节点 / 确认 tilt / 敞口放大器 / purity tilt 四通路，无日内触发）。

### 数据结构（按模块）

| 模块 | 内容 | 数据源 | 日常更新 | PIT 字段 |
|---|---|---|---|---|
| `aeus_fetch_prices` | 51+3 票价格（41 加权 + 10 reserve + XLU/GRID/SPY），股息回调 | Polygon（yfinance 回退），store `price_data/elec_strategy/prices` | `--update` | 交易日 |
| `aeus_fetch_sec_data` | SEC EDGAR HTTP 层（CIK 映射、companyfacts、限速） | SEC XBRL | （被 company/industry 调用） | filed |
| `aeus_pit` | PIT 工具：`merge_frozen` 仅追加 + 分频率 staleness（5td/45d/120d） | — | — | 可得日期 |
| `company_signals` | capex_pulse（与 AISS 共享 4 hyperscaler）+ utility / water / hyperscaler 组 capex（XBRL **YTD 去累计引擎**） | yfinance + SEC XBRL | `--update-capex` / `--update-utility-capex` / `--update-water-capex` / `--update-hyperscaler-capex` | 同日 / filed |
| `industry_signals` | EIA 售电 + 燃料结构（EPM 滞后 56d）、backlog RPO（成分匹配 YoY）、gas 价格 proxy（z252 + 库存 blend，库存**只读** `price_data/eia/` macro 镜像）、IPUTIL | EIA v2 + SEC XBRL + FRED | `--update-elec-gen` / `--update-fuel-mix` / `--update-backlog` / `--update-gas` / `--update-pmi` | EPM 发布 / filed / release |
| `altdata_signals` | 日频需求（2015-07+，滞后 3d，STEO CDD/HDD **去天气**）、860M 装机（滞后 60d）、DC 州电价溢价、变压器 PPI/CPI/建筑用工、缺电度、ai_demand_cycle 敞口放大器、GPU（forward-only） | EIA v2 + FRED | `--update-demand` / `--update-dd` / `--update-capacity` / `--update-state-price` / `--update-fred` / `--snapshot-gpu` | 各系列发布日 |
| `ercot_signals` | 凭证回填 **991 天** DAM SPP 枢纽电价 + AS（受 2023-12 档案下限约束）；macro 模块 dashboard 增量**只读** | ERCOT Public API | `--update` | 日 |
| `pjm_signals` | **已接线（2026-09-01）+ 扩展（09-02）**：西枢纽 DA LMP（2016+）+ 五个扩展 feed —— DOM 区基差 / DOM+PEPCO+BGE+AEP 计量负荷 YoY / 日备用裕度 / 日 0 强迫停机 / DA 负荷预报误差 → `shortage_east`；全部 PIT 冻结 append-only；扩展 feed 受 ~731 天非会员存档墙限制，自 2024-09-15 起；数据 internal-use-only，**绝不 commit** | PJM API | `--init` 回填全部 / `--update` 增量全部 / `--verify` 7 序列时效 | `external_sources.pjm.extended=false` 可退回仅西枢纽（逐字节等价） |

### 首次初始化（一次）

```bash
set -a && source .env && set +a
ENV="conda run -n qlib_run --no-capture-output python -m electric_utilities_strategy"

# 价格：51 股 + XLU/GRID/SPY 全历史（Polygon → 2016-01 warm-up）
$ENV.data.aeus_fetch_prices --init --start 2016-01-01
# Company 层：CapEx 脉冲 + utility/water/hyperscaler capex（SEC XBRL）
$ENV.data.company_signals  --init
# Industry 层：EIA 售电/燃料、backlog RPO、gas proxy、IPUTIL
$ENV.data.industry_signals --init
# Altdata 层：日频需求、度日、装机、州电价、FRED altdata
$ENV.data.altdata_signals  --init
# ERCOT 凭证回填（2023-12 档案下限起）
$ENV.data.ercot_signals    --init

# 覆盖度校验（2019+ 回测窗口必须 OK）
$ENV.data.aeus_fetch_prices --verify
$ENV.data.company_signals   --verify
$ENV.data.industry_signals  --verify
$ENV.data.altdata_signals   --verify
```

### 增量更新（生产，`update_data` 模式封装 = 9 步）

```bash
ENV="conda run -n qlib_run --no-capture-output python -m electric_utilities_strategy"
$ENV.data.aeus_fetch_prices --update                                        # 1 增量价格
$ENV.data.company_signals   --update-capex                                  # 2 CapEx 脉冲
$ENV.data.company_signals   --update-utility-capex --update-water-capex    # 3 utility+water capex
$ENV.data.company_signals   --update-hyperscaler-capex                     # 4 hyperscaler 真实 capex
$ENV.data.industry_signals  --update-elec-gen --update-fuel-mix --update-backlog --update-gas --update-pmi   # 5 EIA 月度
$ENV.data.altdata_signals   --update-demand --update-dd                    # 6 日频需求 + STEO 度日
$ENV.data.altdata_signals   --update-capacity --update-state-price --update-fred  # 7 装机 + 州电价 + FRED
$ENV.data.ercot_signals     --update                                       # 8 ERCOT DAM SPP + AS
$ENV.data.pjm_signals       --update                                       # 9 PJM（门控）
```

> **PIT 规则**（`data/aeus_pit.py`）：每个慢源存其**可得日期**（`filed_date` / `release_date` / EPM 发布日历），只在 `as_of >= 该日期` 时读回；`merge_frozen()` 仅追加 `> max(existing)` 的新日期 → 历史不被改写 → 可复现，无前视。每步独立失败不阻断（WARN-and-continue），失败记账反映到返回值，weekly `--verify` 会把过期序列标 `← STALE` 并打显式 **FAILED** 横幅。
>
> **季节性纪律**（电力数据与半导体最大的不同）：月度量类序列一律先 YoY 再 z（天然消灭季节周期）；日频需求做 52 周同期对比；**去天气** = 实际需求 − f(CDD,HDD) 滚动 5 年拟合（只用 as-of 前数据）的残差 —— AI 负荷信号的本体。

### Corporate actions（拆股/合股，phase-1 响亮降级）

价格源 split 后全历史回溯调整；`inventory_aeus.json` 的 `stock_holdings` 是建仓口径，由根目录共享模块 `CorporateActions.py` 统一处理。**phase-1 现状**：`run_for('aeus')` 的一行注册属 C 级接线（改共享文件），尚未获批——daily 入口的调用**已是 try/except 失败降级不阻断**（AISS 既有行为），接线前每天一条 WARNING，拆股窗口人工盯。价格 store 侧的自愈三件套（7 天 overlap 偏差 >2% 全量重拉、`_persist` truncation guard、weekly 全宇宙含 XLU/GRID benchmark 刷新）已随代码继承生效。portfolio_ledger 的 `'aeus'` 注册同属 C 级（接线前账本调用自动退名义 capital + WARNING，属预期）。

---

## 文件结构

```
qlib-main/electric_utilities_strategy/
├── README.md                       本文件
├── AEUS_PLAN.md                    设计文档（10 节 + 逐行核验记录）
├── RUNBOOK.md                      运维操作手册
├── config.yaml                     所有可调参数（AEUS-tuned，含 external_sources / purity_tilt）
├── aeus_pipeline.sh                主 Pipeline 控制器（15 个模式；update_data 9 步）
├── daily_backtest.sh               V1+V2 全套回测/选参（幂等 gate："AEUS DAILY BACKTEST COMPLETE"）
├── validate.py                     胜负门槛裁决（vs XLU/GRID，另报 50/50 混合 active return/IR）
├── AEUSdailySignal.py              每日信号生成 + inventory（含 smart_select + 个股分解）
├── stock_decompose.py              subsector 权重 → 个股持仓/订单（PIT，N 档 base_w + purity tilt）
├── AEUSStrategyRuns.py             42 个命名参数集（组 A–H, M + Group N purity tilt）
├── AEUSBatchRun.py                 批量参数扫描 + P0 持久化 + --signal-version
├── smart_select.py                 每日宏观条件选参引擎（MCPS P2/P3/P5 + version_selector，防抖）
├── macro_clusters.py               23 维→AE latent→KMeans 簇（encoder 持久化，serving 不重训）
├── walk_forward.py                 Walk-Forward IS/OOS 分析器（70 折 / DSR / WFE / oracle）
├── multi_horizon_backtest.py       多 horizon 复合回测
├── weekly_review.py                周报（漂移 + regime + 多视角 + P0 健康）
├── portfolio_record.py             33-sheet 回测 Excel + 5-sheet monitor + WF diagnostic 导出
│
├── data/
│   ├── universe.py                 10 子板块 + N 档 base_w 篮子 + purity 分 + STOCK_TIER + PIT 入篮
│   ├── loader.py                   价格（隔离 parquet store）+ 50/50 混合基准合成 + FRED macro
│   ├── aeus_fetch_prices.py        Polygon 价格抓取（--init / --update / --verify）
│   ├── aeus_fetch_sec_data.py      SEC EDGAR HTTP 层（CIK / companyfacts / 限速）
│   ├── aeus_pit.py                 PIT 可得日期 + merge_frozen（仅追加冻结）
│   ├── company_signals.py          CapEx 脉冲 + utility/water/hyperscaler capex（YTD 去累计）
│   ├── industry_signals.py         EIA 售电/燃料 + backlog RPO + gas proxy + IPUTIL
│   ├── altdata_signals.py          日频需求/度日/装机/州电价/FRED altdata/GPU
│   ├── ercot_signals.py            ERCOT DAM SPP + AS（凭证回填 + dashboard 只读增量）
│   └── pjm_signals.py              PJM 西枢纽 + DOM 基差 / 分区负荷 / 备用裕度 / 停机 / 预报误差（已接线）
│
├── signals/
│   ├── momentum.py                 横截面 12-1m 动量
│   ├── supply_chain.py             知识图谱传播（AEUS 核心 alpha；V1 硬编码 + V2 config 双轨）
│   ├── graph_calibration.py        因子残差 IC 校准（0.05 门槛，候选边池）
│   ├── regime.py                   4 态 Regime + CapEx regime 倾斜
│   ├── composite.py                4 因子聚合 + Regime 条件权重 + 确认 tilt / 敞口放大器
│   └── risk_overlay.py             V2 风险叠加（AEUS 默认 OFF，同 AISS 裁决）
│
├── portfolio/
│   ├── optimizer.py                逆波动率 / 风险平价 / GMV / 等权 + Ledoit-Wolf
│   ├── risk.py                     波动率缩放（可选下行半波动口径）+ VIX 阶梯去风险 + 回撤断路器 + beta
│   ├── rebalance.py                月度/半月度调度 + 阈值过滤 + 换手率上限
│   ├── stop_loss.py                极端止损（circuit breaker + sector collapse + trailing）
│   └── strategy.py                 AEUSWeightStrategy（qlib WeightStrategyBase 适配）
│
├── backtest/
│   ├── engine.py                   事件驱动回测（native loop 为生产引擎）+ walk-forward
│   ├── qlib_adapter.py             qlib Exchange / Executor 适配（休眠脚手架，见下）
│   ├── costs.py                    按 3 tier 的点差成本模型（3/5/8 bps；真源 universe.STOCK_TIER）
│   ├── metrics.py                  Sharpe / Calmar / IR / CVaR
│   ├── trade_audit.py              逐笔交易审计 CSV
│   ├── dd_analysis.py · sensitivity.py · robustness.py   回撤/敏感度/稳健性分析
│
├── report/
│   ├── plots.py                    matplotlib 可视化
│   └── tearsheet.py                多页 PDF 报告（aeus_tearsheet.pdf）
│
├── tests/                          pytest（173 测试，合成数据，无网络）
│   ├── aeus_matrix.py              42 参数集 × V1/V2 全网格回测
│   ├── aeus_verify_excel.py        逐 sheet Excel 审计
│   └── test_pipeline_integration.sh  6 阶段集成 QA（--quick ≈ 5 min）
└── logs/                           运行日志（gitignore）
```

### 输出文件完整参考

#### 目录结构总览

```
someopark-test/                                            项目根目录
├── price_data/elec_strategy/                              AEUS 隔离数据（仅追加）
│   ├── prices/*.parquet                                   51 股 + XLU/GRID/SPY 价格
│   ├── company/capex_pulse.json · *_capex*.json           CapEx 脉冲 / utility·water·hyperscaler capex（PIT 冻结）
│   ├── industry/*.json                                    EIA 售电·燃料 / backlog RPO / gas proxy / IPUTIL（PIT 冻结）
│   ├── altdata/*                                          日频需求 / 度日 / 装机 / 州电价 / FRED / ERCOT / GPU
│   └── cache/macro_frozen.parquet                         macro 仅追加冻结快照
│
├── price_data/macro/*.parquet                             宏观（只读，主 pipeline 写入）
├── price_data/eia/                                        macro 模块 EIA 镜像（只读复用）
│
├── historical_runs/electric_utilities_strategy/           回测输出 Excel（gitignored）
│   ├── aeus_portfolio_{set}_{v}_{span}_{mode}_{ts}.xlsx   33-sheet 完整回测记录
│   ├── wf_diagnostic_aeus_{v}_IS-OOS_{wfmode}_{mode}_{ts}.xlsx  5-sheet WF 诊断
│   └── trade_audit.csv                                    逐笔交易审计
│
└── qlib-main/electric_utilities_strategy/
    ├── selected_param_set.json                            生产参数（含 signal_version）
    ├── inventory_aeus.json                                当前持仓（subsector `holdings` + 个股 `stock_holdings` + param_set/signal_version）
    ├── inventory_history/inventory_aeus_{ts}.json         持仓变更快照（ENTER/CLOSE 各一次/日）
    ├── trading_signals/aeus_daily_report_{date}_{ts}.{json,txt}   日报（subsector 层 + 个股层 stock_holdings/stock_breakdown/stock_trades）
    ├── backtest_results/
    │   ├── aeus_batch_summary_{ts}.csv                    42 集 batch 汇总
    │   ├── aeus_batch_equity_{ts}.csv                     batch equity（splice-freeze 的 vintage 来源）
    │   ├── param_oos_by_regime{,_v1,_v2}.json             P0: smart_select 缓存（含版本标记）
    │   ├── graph_calibration_report.json                  图谱校准 IC 报告
    │   └── weekly_review*.json · …                        周报输出 / 其它 P0 缓存
    ├── report/output/*.pdf                                Tearsheet PDF
    └── logs/aeus_{mode}_{YYYYMMDD_HHMMSS}.log             运行日志（带时间戳后缀）
```

> **注意日志命名**：AEUS 日志带 `_HHMMSS` 时间戳后缀（同 AISS，不同于 SSRS 仅日期），定位最新用 `ls -t logs/aeus_daily_*.log | head -1`。

#### 命名规则

| 占位符 | 格式 | 示例 |
|---|---|---|
| `{ts}` | `YYYYMMDD_HHMMSS` | `20260830_215619` |
| `{date}` | `YYYYMMDD` | `20260830` |
| `{set}` | 参数集名（lowercase_snake） | `pure_supply_chain` |
| `{v}` | 信号版本 | `v1` / `v2` |
| `{span}` | 数据范围 | `IS`（纯样本内） / `IS-OOS`（walk-forward 验证） |
| `{mode}` | 入口 | `batch` / `select` / `tearsheet` / `wf` |
| `{wfmode}` | WF 模式 | `anchored` / `rolling` |

**文件名模板**：
```
aeus_portfolio_{set}_{v1|v2}_{IS|IS-OOS}_{batch|select|tearsheet}_{ts}.xlsx
wf_diagnostic_aeus_{v1|v2}_IS-OOS_{anchored|rolling}_{select|wf|tearsheet}_{ts}.xlsx
```

前端通过文件名区分所有维度：**版本** `_v1_`/`_v2_`、**范围** `_IS_`/`_IS-OOS_`、**入口** `_batch_`/`_select_`/`_tearsheet_`、**参数** 文件名含完整参数集名。

#### 各 Pipeline Mode 输出文件矩阵

| Mode | 信号/报告 | 回测/分析 | Excel 记录 | 备注 |
|---|---|---|---|---|
| **daily** | `trading_signals/` JSON+TXT, `inventory_aeus.json` | — | monitor（调仓日） | 含 param_set/signal_version |
| **dry-run** | `trading_signals/` JSON+TXT | — | — | 不写 inventory |
| **weekly** | dry-run 报告 | data/PIT verify + weekly_review | — | STALE → FAILED 横幅（首尾各一次） |
| **monthly** | = daily 调仓 | = daily_backtest 全部 | = daily_backtest | 两步合一，结束恢复 V1 |
| **batch** | — | `aeus_batch_summary_*.csv` | （`--save-equity` 时 ×42） | 42 集汇总 |
| **select** | — | P0 缓存, `selected_param_set.json` | 最优集 + `wf_diagnostic_*` | 生产选参 |
| **daily_backtest** | — | V1+V2 各 42 batch IS + WF IS-OOS + select + validate | 42×2 IS + 42×2 IS-OOS + WF diag | 全套；生产恢复 V1（幂等 gate：当日已 COMPLETE 秒退，`--force` 重跑） |
| **walk-forward** | — | fold 汇总 | `wf_diagnostic_*` | IS/OOS 分析 |
| **validate** | console PASS/FAIL | — | — | 胜负门槛 |
| **tearsheet** | — | IS-OOS Excel + PDF | `wf_diagnostic_*` | 含 XLU/GRID 叠加页 |
| **test / status / help** | — | — | — | 只读/显示 |

#### Portfolio History Excel（33 Sheets = 26 主 + 7 stock_decomp）

`historical_runs/electric_utilities_strategy/aeus_portfolio_{set}_{v}_{span}_{mode}_{ts}.xlsx`

**26 个主 sheet**（"sector" 在 AEUS 中指子板块）：

| # | Sheet | 频率 | 内容 |
|---|---|---|---|
| 1 | summary | 单行 | Sharpe, Calmar, MaxDD, CAGR, param_set, signal_version |
| 2 | portfolio_history | 日频 | date, equity, asset, liability, daily_pnl, cum_pnl, drawdown_pct |
| 3–6 | asset/liability/equity/asset_cash_history | 日频 | 资产/负债/净值/现金 |
| 7 | sector_prices | 日频 | 10 子板块篮子价格 |
| 8 | share_history | 日频 | 10 子板块持有"份额" |
| 9 | sector_weights | 调仓日 | 10 子板块目标权重 + cash |
| 10 | sector_weight_pct | 日频 | 漂移后实际占比 |
| 11 | cost_basis | 日频 | 进入价格（加权均价） |
| 12 | sector_ratio_matrix | 调仓日 | 子板块权重互比矩阵 |
| 13–14 | sector_pnl_acc / sector_pnl_daily | 日频 | 子板块累计/日度 PnL |
| 15 | sector_contribution | 日频 | 子板块对总收益贡献 |
| 16 | daily_pnl | 日频 | 总 PnL + 累计 |
| 17–18 | interest_expense / acc_interest | 日频 | 利息（无杠杆=0） |
| 19–20 | realized_pnl / total_notional | 按子板块 | 已实现 PnL / 累计名义 |
| 21 | drawdown_history | 日频 | dd_dollar, dd_pct |
| 22 | rebalance_trades | 按交易 | date, sector, direction, old/new weight, shares, price, cost |
| 23 | regime_indicators | 日频 | VIX, 利差, capex regime + regime 标签 |
| 24 | strategy_vars | 调仓日 | config 参数 + composite scores + risk flags |
| 25 | stop_loss_history | 按事件 | date, sector, type, reason, entry/current price, pnl |
| 26 | config | 参数表 | 完整 config.yaml 快照 |

**7 个 stock_decomp sheet**（凡含子板块列的 sheet 均分解到个股，列名 `{subsector}/{stock}`，N 档 base_w × 10 板块）：

`sector_prices_stock_decomp` · `sector_weights_stock_decomp` · `sector_wt_pct_stock_decomp` · `share_hist_stock_decomp` · `cost_basis_stock_decomp` · `sector_pnl_stock_decomp` · `sector_contrib_stock_decomp`

> 校验不变量（`tests/aeus_verify_excel.py` 逐 sheet 审计）：权重每行求和 = 1（含 cash），stock_decomp 各列回拼到子板块误差 < 0.0001，equity 全正。V1 月度调仓，V2 半月度约两倍调仓次数。

#### WF Diagnostic Excel（5 Sheets）

`wf_diagnostic_aeus_{v}_IS-OOS_{wfmode}_{mode}_{ts}.xlsx`

| # | Sheet | 内容 |
|---|---|---|
| 1 | fold_summary | 每折 × (IS/OOS dates, Selected, Method) |
| 2 | param_oos_matrix | 42 param × fold 的 OOS Sharpe 矩阵 |
| 3 | param_by_regime | 42 param × regime 的 mean OOS Sharpe |
| 4 | synthetic_equity | 合成 OOS 净值曲线 |
| 5 | selection_log | 每折选参决策记录 |

#### Monitor Excel（5 Sheets，调仓日）

| # | Sheet | 内容 |
|---|---|---|
| 1 | snapshot | equity, regime, VIX, cash, n_positions, param_set, signal_version |
| 2 | holdings | 10 子板块 × (weight, shares, price, cost_basis, pnl, composite_score) |
| 3 | signals | 10 子板块 × 4 因子分量 |
| 4 | smart_select | MCPS score, rank, candidates, version_selector |
| 5 | risk_flags | vol_scaling, vix_emergency, dd_circuit, beta_adj + 阈值 |

---

## 配置参考

所有参数在 `config.yaml` 中管理：

| 节 | 键 | 默认值 | 说明 |
|---|---|---|---|
| `signals.weights` | cs_momentum / supply_chain / capex_pulse / cycle_regime | 0.30 / 0.35 / 0.25 / 0.10 | 四因子权重，和必须 = 1.0 |
| `signals` | signal_version | `"v1"` | V1 月度 / V2 半月度 |
| `signals.supply_chain` | graph_version | `"v2"` | v2=校准图谱(`graph_config.edges`) / v1=硬编码图谱 |
| `signals.supply_chain` | use_external_macro | `true` | EIA/ERCOT/XBRL 确认 tilt PIT 可得时用，否则价格代理 |
| `signals.purity_tilt` | kappa / clip | `0.0` / `0.40` | 板块内纯度倾斜强度（κ=0 = AISS 静态行为回归锚） |
| `portfolio` | optimizer | `"inv_vol"` | inv_vol / risk_parity / gmv / equal_weight |
| `portfolio` | top_n_sectors | `3` | 高信念集中（10 选 3） |
| `portfolio.constraints` | max_weight | `0.55` | 单子板块上限 |
| `portfolio.constraints` | beta_min / beta_max | `0.40` / `3.00` | 允许防御低 beta，也不强拉高 beta 回 1.0 |
| `risk.vol_scaling` | target_vol_annual | `0.30` | AISS 起点值，Group C 扫 0.18–0.40 |
| `risk.drawdown` | cumulative_dd_halve | `−0.25` | 累计回撤减半线 |
| `risk.event_derisk` | enabled | `false` | **phase-1 关闭**（C 级接线 TODO） |
| `rebalance` | emergency_derisk_vix | `36.0` | 仅真危机 |
| `signals.regime` | vix_high / vix_extreme | `25` / `32` | regime 倾斜阈值 |
| `external_sources` | eia / ercot / pjm / sec | true / true / **true** / true | PJM 2026-09-01 接线；`pjm.extended: true` 打开五个扩展 feed |
| `backtest` | start_date | `"2019-01-01"` | 晚期 IPO 地板 |
| `backtest` | initial_capital | `1_000_000` | 初始资金 USD |

---

## 全参数完整参考

> 所有 `config.yaml` 参数均可在不改代码的情况下调整。

### 一、数据 `data` 与外部源 `external_sources`

| 键 | 默认值 | 说明 |
|---|---|---|
| `price_dir` | `"../../price_data/elec_strategy/prices"` | 隔离 parquet 价格 store |
| `industry_dir` / `company_dir` / `altdata_dir` | `…/industry` · `…/company` · `…/altdata` | 另类数据存储 |
| `cache_dir` | `…/cache` | macro pickle 缓存 |
| `macro_dir` | `"../../price_data/macro"` | 共享宏观（**只读**） |
| `price_source` | `"store"` | AEUS 隔离 store（Polygon-backed） |
| `price_start` | `"2016-01-01"` | 早于回测起点以 warm-up |
| `macro_source` | `"fred"` | 宏观来源 |
| `external_sources.eia.enabled` | `true` | EIA_API_KEY（根 .env，与 macro 模块共享） |
| `external_sources.ercot.enabled` | `true` | ERCOT_API_*（2026-08-30 已验证） |
| `external_sources.pjm.enabled` | `true` | 2026-09-01 接线（key 已落 .env）|
| `external_sources.pjm.extended` | `true` | 2026-09-02：DOM 基差进 `price_pulse` z 均值、分区负荷 YoY 进 power_demand 节点 z 均值、`shortage_east` 进 shortage_score z 均值；**不新增 tilt**（ipp_wholesale 已满 2 条）；`false` → 仅西枢纽腿，历史逐字节不变 |
| `external_sources.sec.enabled` | `true` | 无需 key；`AEUS_SEC_USER_AGENT` 可选覆盖 |

> enabled 源缺 key 时 fetcher 响亮硬失败；disabled 源的信号降级为 graceful-0 tilt（AISS 惯例）。

### 二、标的 `universe`

| 键 | 默认值 | 说明 |
|---|---|---|
| `etfs` | 10 子板块名 | 可交易资产（沿用引擎兼容 key 名 `etfs`） |
| `benchmark` | `"SPY"` | beta / 信息比率基准 |
| `benchmarks` | `["XLU","GRID","SPY"]` | XLU & GRID = 胜负门槛 |
| `benchmark_blend` | XLU 0.50 + GRID 0.50 | 50/50 日度再平衡混合 = active-return 基准 |
| `subsectors` | 10 × {members, reserve} | members = [ticker, base_w] 列表（首名为篮子锚，不受历史门控）；**单一真源 = `data/universe.py`**，config 仅镜像 |
| `min_history_months` | `24` | 非锚成员入篮所需历史 |
| `basket_base_value` | `100.0` | 篮子基值 |
| `universe_start` | `"2019-01-01"` | 完整宇宙起点 |

### 三、信号 `signals`

#### 3.1 权重（和 = 1.0）
`cs_momentum` 0.30 · `supply_chain` 0.35 · `capex_pulse` 0.25 · `cycle_regime` 0.10

#### 3.2 横截面动量 `cs_momentum`
`lookback_months` 12 · `skip_months` 1 · `zscore_window` 36（12-1 动量）

#### 3.3 供应链 `supply_chain`（核心 alpha，知识图谱传播）
`graph_version` `"v2"` · `use_external_macro` `true`（PIT 可得时用 EIA/ERCOT/XBRL 确认 tilt，否则价格代理）· `lag_decay` 0.0（0=硬滞后，>0=指数衰减 e^{-λk}，半衰期≈ln2/λ 月）

**V2 知识图谱**（`graph_config`，可在 config 直接编辑；2026-08-30 D5 校准）：
- **节点**：`ai_capex_proxy`（与 AISS 共享的 capex 脉冲 + hyperscaler 真实 capex）+ 4 个电力宏观节点 —— `power_demand_proxy`（EIA_RTO_DEMAND，weather_adj_yoy_z）、`power_price_proxy`（DHHNGSP，z252 + 库存 blend）、`rate_env_proxy`（DGS10 **取负** yoy_z）、`industrial_demand_proxy`（IPUTIL yoy_z）。
- **边**：23 条先验边（V1 硬编码于 `supply_chain.py`）+ **28 条校准 v2 边**（= 23 先验 + 5 候选全保留）。滞后由 `signals/graph_calibration.py` 在**因子残差收益**（剔除电力共同 beta）上取 IC-argmax 得到，`KEEP_IC_THRESHOLD=0.05`，最高候选 IC **+0.209**（power_demand→gas_midstream）；报告 `backtest_results/graph_calibration_report.json`。
- 传导主链：ai_capex → ipp_wholesale（PPA 最快兑现）→ dc_power_cooling → grid_equipment（变压器订单）→ grid_epc → regulated_mega（rate base 最慢），加上 power_price→ipp/gas_midstream、rate_env→三防御位、power_demand→ipp/regulated/regional 等宏观边。
- 切回 V1：`graph_version: "v1"` 即用回硬编码先验图谱（代码逐位兼容）。

#### 3.4 CapEx 脉冲 `capex_pulse`
`tickers` `[MSFT, GOOGL, META, AMZN]`（**与 AISS 共享同一最上游驱动** —— 同 4 家 hyperscaler 同时驱动芯片需求与数据中心电力需求）· `lookback_months` 3 · `zscore_window` 24

#### 3.5 纯度倾斜 `purity_tilt`（AEUS 独有扩展）
`kappa` 0.0 · `clip` 0.40。`w_i ∝ eff_w_i × (1 + κ × purity_i × g_s(t))` 再归一，单票倾斜截断 ±40%；purity 分在 `data/universe.py`（单一真源）。κ=0（默认）= **逐 bit 等价 AISS 静态板块内配比**（回归锚）；Group N（0 / 0.3 / 0.5）交 walk-forward 裁决。生效于篮子收益构造与个股分解**两层同一纯函数**（否则 WF 测不到 κ）。

#### 3.6 V1 / V2 版本
V1（默认生产）月度调仓 12-1 动量；V2 半月度（1 日 + ~月中）同样的 4 因子 / 12-1 信号——更快 cadence 而非 gating。`smart_select` 从各版本 OOS 历史中选 V1 vs V2。**AISS 血统备注**：更快动量（6-0）在 AISS 测过且**有害**，故 V2 保留 12-1。

#### 3.7 Regime `signals.regime`
`method` `"rules"` · `vix_high_threshold` 25 · `vix_extreme_threshold` 32 · `hy_spread_high_bps` 450 · `yield_curve_inversion` −0.10 · `ism_expansion` 50 · `capex_strong_zscore` 1.0 · `capex_weak_zscore` −1.0。`regime_weights` 见上方"Regime 四态"表。`defensive_sectors` `["regulated_mega","regional_utility","water_cooling"]` · `defensive_bonus_risk_off` 0.40。

### 四、投资组合 `portfolio`

| 键 | 默认值 | 说明 |
|---|---|---|
| `optimizer` | `"inv_vol"` | inv_vol / risk_parity / gmv / equal_weight |
| `cov.method` | `"ledoit_wolf"` | 协方差估计 |
| `cov.lookback_days` / `min_periods` | 252 / 63 | 协方差窗口 |
| `constraints.max_weight` | 0.55 | 单子板块上限（集中到赢家） |
| `constraints.min_weight` / `max_cash` | 0.00 / 0.50 | 下限 / 现金上限 |
| `constraints.beta_min` / `beta_max` | 0.40 / 3.00 | 允许防御低 beta 与高 beta 书 |
| `top_n_sectors` | 3 | 持仓子板块数（10 选 3；Group B 扫 2–5） |
| `min_zscore` | −0.30 | 分配权重所需最低分（一票否决） |
| `weight_scheme` | `"rank"` | rank / zscore_softmax |

### 五、调仓 `rebalance`

| 键 | 默认值 | 说明 |
|---|---|---|
| `frequency` | `"monthly"` | monthly（V2 引擎追加月中调仓） |
| `rebalance_day` | `"first_trading_day"` | 月内调仓时间 |
| `zscore_change_threshold` | 0.5 | 信号变化 < 此值则跳过该子板块 |
| `emergency_derisk_vix` | 36.0 | 紧急去风险触发 VIX |
| `emergency_cash_pct` | 0.45 | 紧急目标现金 |
| `max_monthly_turnover` | 0.80 | 单侧换手率上限 |

### 六、风险 `risk`

| 键 | 默认值 | 说明 |
|---|---|---|
| `vol_scaling.enabled` | true | 波动率缩放 |
| `vol_scaling.target_vol_annual` | 0.30 | 目标年化波动（AISS 起点；Group C 含 semivol 下行半波动探针） |
| `vol_scaling.estimation_window` | 20 | 实际波动窗口 |
| `vol_scaling.scale_threshold` | 1.5 | 仅 realized > 1.5× 历史时缩减 |
| `drawdown.monthly_dd_alert` | −0.10 | 月度回撤告警 |
| `drawdown.cumulative_dd_halve` | −0.25 | 累计回撤减半线 |
| `drawdown.cumulative_dd_recovery` | −0.12 | 解除线 |
| `vix_progressive_derisk.tiers` | 28→10% / 32→25% | VIX 阶梯现金 |
| `event_derisk.enabled` | **false** | phase-1 关闭；参数（2.5 / 2d / −4.5% / 0.5 / bottomup）保留待 C 级接线 |

VIX 完整阶梯：`< 28` 全仓 → `≥ 28` 10% cash → `≥ 32` 25% cash → `≥ 36` 45% cash（emergency 触发）。

### 七、止损 `stop_loss`（极端事件）

`portfolio_circuit_breaker` SPY 3 日 −7% · `sector_collapse` 单子板块自进入 −15% · `trailing_stop` 自峰值 −18% · `cooling_off_days` 10。

### 八、交易成本 `costs`（3 tier，真源 = `universe.STOCK_TIER`）

Tier 1（3 bps）NEE/SO/DUK/AEP/VST/CEG/NRG/ETN/EMR/GEV/PWR/VRT/TT/CARR/FSLR/KMI/WMB/OKE/AWK · Tier 2（5 bps）D/AEE/LNT/ATO/FIX/BWXT/NXT/TLN/STRL/TRGP/WTRG · Tier 3（8 bps）其余中小盘与新 IPO（OGE/BKH/POWL/VMI/MYRG/DY/LEU/UUUU/OKLO/SMR/FLNC/ARRY/BE/AWR/CWT + 各 reserve）。引擎按 base_w 篮子混合各 tier（储备股 0% 不计成本，除非被顶替）。`annual_fee_bps` 0（个股无管理费）。config 的 `costs.tier_*_tickers` 列表**零代码消费方**，仅镜像 universe.py 供查阅。

### 九、回测 `backtest`

`start_date` 2019-01-01 · `end_date` null · `initial_capital` 1_000_000 · `is_years` 3 · `oos_months` 6。

### 十、报告 / 记录

`report.output_dir` `"report/output"` · `pdf_filename` `"aeus_tearsheet.pdf"`。`portfolio_record.leverage_ratio` 0.0（无杠杆）· `interest_rate` 0.05。`risk_overlay.enabled` **false**（镜像 AISS 的裁决：V2 MA gate 会坐现金、压制策略赖以为生的高 beta 敞口，故关闭）。

---

## 回测框架

### 入口与共享引擎

| 入口 | 用途 |
|---|---|
| `AEUSBatchRun.py`（默认） | 42 参数集 × 全期回测，纯 IS，输出 CSV/Excel |
| `AEUSBatchRun.py --select` | 三阶段生产选参：WF OOS 过滤 → MCPS（全周期 equity + 今日宏观向量）→ 近 12 月 Sharpe 兜底 → `selected_param_set.json` |
| `walk_forward.py` | IS/OOS 滚动窗口回测（anchored + rolling，70 折），输出 fold 汇总 / 合成 OOS 净值 / DSR / WFE / oracle |
| `daily_backtest.sh` | V1+V2 全套（batch IS + WF IS-OOS + diagnostic + PDF + select + validate），刷新 smart_select 的 P0 缓存，结束恢复 V1；幂等 gate 标记 **"AEUS DAILY BACKTEST COMPLETE"** |
| `report/tearsheet.py` | 多页 PDF 绩效报告 |

### IS-only vs IS-OOS

- **IS-only**（默认 batch）：全周期既训练又评估，无"未见数据"验证，Sharpe 可能偏高（42 参数集多重测试偏差），用于快速筛选/建立基线。顶部 win 表即 IS 口径。
- **IS-OOS Walk-Forward**：每折在 IS 上选参 → embargo 隔离 → 在 OOS（未来数据）上验证；合成 OOS 净值 = 拼接各折 OOS 段（无重叠、无前视）。**AEUS 可实现 OOS：Sharpe 1.27 / CAGR 30.6% / MaxDD −22.1%（70 折），WFE 1.76，oracle（每折事后最优）天花板 2.39** —— 预期管理以此为准。

### 日度信号的两层输出（subsector → 个股）

`daily` / `dry-run` 的信号输出有**两层**，由 `stock_decompose.py` 衔接：

1. **subsector 层（决策层）**：10 个子板块的目标权重 / 信号分 / 动作 + subsector 级 inventory `holdings`。
2. **个股层（执行层）**：把每个持有 subsector 的目标权重按 base_w（PIT `effective_weights`，κ>0 时叠加 purity tilt）分解到底层个股 —— 组合权重 = subsector_w × within_w，股数 = floor(组合权重 × 资金 / 个股价)；按 ticker 跨子板块聚合；`build_stock_trades()` 对比上次 inventory 的 `stock_holdings` 产出逐股 BUY/SELL。

输出落点：日报 TXT 的 `STOCK-LEVEL TARGET HOLDINGS` + `STOCK TRADES` 段、日报 JSON 的 `stock_holdings`/`stock_breakdown`/`stock_trades`、inventory 的 `stock_holdings`。这与回测端的 `*_stock_decomp` Excel sheet 是同一套逻辑的两端。

### smart_select + MCPS + version_selector

`daily` 模式由 `smart_select.py` 在生产中选参（P2 日度选参 + P3 宏观 tilt + P5 版本切换，防抖 3–5 天/月限）：`macro_clusters.py` 用 autoencoder 把当日 **23 维宏观状态**编码为 latent 向量（**encoder 训练一次持久化，serving 绝不重训** —— AISS 基底漂移的教训），KMeans **6 簇**；对各参数集的全周期 OOS equity 做高斯核相似度加权（MCPS），选出最匹配当前 regime 的参数集；`version_selector` 从 `param_oos_by_regime_{v1,v2}.json`（由 `daily_backtest` 刷新）中比较 V1 vs V2 的 per-regime OOS 表现。选中的 param_set / signal_version 写入 `selected_param_set.json` 与 `inventory_aeus.json`，便于审计。

### 关于 qlib backtest path（休眠脚手架）

`backtest/engine.py` 设计为「qlib path 优先 → native loop 兜底」，但 qlib path 因 qlib `WeightStrategyBase`/`Exchange` 兼容问题始终抛错并 fallback。**native loop 是 AEUS（与 AISS / SSRS 完全相同）的实际生产引擎**，所有验证数字均出自 native loop。每次回测会打印 `qlib backtest execution failed …, falling back to native loop`——这是**良性**、预期内的日志，**不应**被判为失败或 degraded。

---

## 参数集扫描（AEUSStrategyRuns，42 个 = AISS 39 组 + Group N）

`conda run -n qlib_run python -m electric_utilities_strategy.AEUSStrategyRuns` 打印全部。

| 组 | 参数集 | 维度 |
|---|---|---|
| **A 信号权重**（6） | default · supply_chain_heavy · momentum_heavy · capex_heavy · balanced_four · momentum_capex | 四因子配比 |
| **B 集中度**（4） | concentrated_2 · standard_3 · diversified_4 · broad_5 | top-N + max_weight |
| **C 波动/回撤**（9） | vol_target_24 · vol_target_30 · vol_target_40 · no_vol_scaling · semivol_18/21/24 · dd_release_08/12 | vol_scaling + 下行半波动 + DD 离底释放 |
| **D VIX 去风险**（4） | derisk_tight · derisk_loose · no_vix_derisk · recovery_tiers_30 | 阶梯/紧急阈值 |
| **E 动量窗口**（3） | fast_momentum · standard_momentum · slow_momentum | 6-0 / 12-1 / 15-1 |
| **F 供应链外部数据**（2） | external_on · external_off | EIA/ERCOT/XBRL 确认 tilt vs 价格代理 |
| **G 优化器**（4） | opt_inv_vol · opt_risk_parity · opt_gmv · opt_equal_weight | 权重方法 |
| **H 原型**（4） | max_aggression · quality_defensive · ai_capex_tilt · supply_chain_core | 多维组合 |
| **M 单因子隔离**（3） | pure_momentum · pure_supply_chain · pure_capex | 因子归因 |
| **N 纯度倾斜**（3，AEUS 独有） | purity_tilt_off (κ=0) · purity_tilt_03 · purity_tilt_05 | 板块内动态配比强度，WF 裁决 |

> `default`（A1）= 已验证的胜负门槛配置（cs.30/sc.35/cx.25/cy.10）。**首轮全链选参（2026-08-30）：42/42 组 WF OOS 全部为正，V1 与 V2 两条链各自独立选中 `pure_supply_chain`（WF OOS Sharpe 1.40）——知识图谱本身就是 alpha**，与 AISS 选中 pure_momentum 异曲同工（各自的核心因子单飞胜出）。生产参数见 `selected_param_set.json`。

---

## Cron 定时任务

AEUS 的三个 OpenClaw cron 任务镜像 AISS（在 isolated session 中运行，向 Telegram 汇报，失败告警），**与 AISS（17:55 / 19:00 / 周日 02:00 ET）完全错峰**以避免 CPU / Polygon 限速争用：

| 任务 | 调度（ET） | 命令 |
|---|---|---|
| `aeus-daily-backtest` | 工作日 19:10 | `bash …/daily_backtest.sh` |
| `aeus-daily` | 工作日 20:20 | `bash …/aeus_pipeline.sh daily` |
| `aeus-weekly` | 周日 03:30 | `bash …/aeus_pipeline.sh weekly` |

> `daily` / `monthly` / `daily_backtest` 内置 NYSE 节假日检查（休市跳过 + exit 0）。详见 RUNBOOK。

---

## Go-live（2026-09-01）：拼接冻结 + QC 挂载

AEUS 定于 **2026-09-01 建仓上线**，业绩曲线机制**从第一天就用 splice-freeze v2**（AISS 吃了 5 周亏才冻结，AEUS 直接抄修复后的做法）：

- **固定回测段**：冻结 `frozen_param = pure_supply_chain` + 当日 vintage batch-equity CSV + **逐日数值字面量**，写入前端 `aiss_ssrs_splice_freeze.json` 的 **`"aeus"` 键**（add-only）；`live_start = 2026-09-01`。master 展示口径下固定段锚点 **$921,499 → $1,158,818**（scale 对齐 master 归一起点的显示值，**非**初始资本 —— account_aeus 以 $1,000,000 起账）。拼接点之前 = 冻结永不再动；之后 = 账本真实日收益率链接。
- **QC 模拟盘中途注资挂载**：`trading_quantconnect/ops/onboard_aeus.py`（先 `--dry-run` 看数）—— QC 侧 CashBook 一次性 deposit **K = AEUS 官方 equity**，`scalar_aeus = 官方/账本 ≈ 1.1588` 写死冻结，exporter 常驻循环自动按账本股数 × scalar 建仓。
- **不变量**：QC aeus 市值+现金 ≡ 官方口径 ≡ NAV 面板头条 —— 这同时是 M4 对账的基准。

---

## 数据来源

| 数据 | 来源 | 用途 |
|---|---|---|
| 股票 / ETF 价格 | Polygon（隔离 parquet store，yfinance 回退） | 篮子构建 + 动量 |
| 超大规模 CapEx 脉冲 | yfinance（MSFT/GOOGL/META/AMZN，**与 AISS 共源**） | capex_pulse / ai_capex_proxy |
| Hyperscaler / utility / water 真实 CapEx | SEC XBRL（YTD 去累计引擎；NEE/DUK/SO、AWK） | supply_chain 确认 tilt |
| 在手订单 backlog RPO | SEC XBRL 10-Q（GEV/PWR/EMR/ETN；$219B，YoY +40%，成分匹配 YoY） | 变压器瓶颈订单簿信号 |
| 售电量 / 燃料结构 / 装机 | EIA v2（retail-sales · operational-data · 860M） | 图谱节点 + 确认 tilt + 缺电度放大器 |
| 日频需求 + CDD/HDD | EIA v2 rto（2015-07+）+ STEO | power_demand_proxy（去天气结构性需求） |
| 天然气价格 / 库存 | FRED DHHNGSP + `price_data/eia/` 镜像（只读） | power_price_proxy |
| 变压器 PPI / 电力 CPI / 建筑用工 | FRED（PCU335311335311 等） | 确认 tilt |
| ERCOT DAM SPP + AS | ERCOT Public API（凭证，回填 991 天） | 得州枢纽电价 + 电网紧张度温度计 |
| PJM 西枢纽 DA LMP | PJM Data Miner 2 `da_hrl_lmps`（pnode 51288，2016+）| 电价脉冲 z 均值的 PJM 腿 |
| PJM DOM 区基差 | `da_hrl_lmps` pnode 34964545 − 西枢纽（2024-09-15+）| 数据中心走廊电价溢价 → 电价脉冲 z 均值 |
| PJM 分区计量负荷 | `hrl_load_metered` DOM/PEPCO/BC/AEP×4（+RTO），日 MWh，28d 均值 YoY，可得性 +12d | power_demand 节点的区域腿（z 均值）|
| PJM 日备用裕度 | `day_gen_capacity` 逐时 (eco_max−committed)/eco_max 取日最小 | `shortage_east`（取负）|
| PJM 日 0 强迫停机 | `gen_outages_by_type` forecast_date==执行日，PJM RTO（+Dominion 存档）| `shortage_east` |
| PJM DA 负荷预报误差 | `load_frcstd_hist` 运行日前最后一次评估 vs `hrl_load_metered` RTO，30d MAPE，+12d | `shortage_east` |
| 宏观（VIX, 利差, ISM…） | `price_data/macro/`（只读）+ FRED | regime + cycle_regime + MCPS 23 维 |

**数据新鲜度自愈**：
- 价格（`data/aeus_fetch_prices.py`）：loader 层按目标日 + 时间节流判断新鲜度（收盘后运行会重新拉取当日收盘）；weekly 全宇宙（含 XLU/GRID benchmark）刷新防 stale
- PIT 信号（`aeus_pipeline.sh daily`）：每日先跑一遍**非致命** update_data 再出信号（AISS 2026-08-27 血泪教训的接线：capex 冻结 57 个交易日曾让月度调仓方向做反）；宏观 store 落后时 `AEUSdailySignal` 自动 self-heal 一次后重载，失败降级不阻断
- 全部外部信号 graceful-0 / graceful-1.0 回退：任一路由断供，对应 tilt 归零 / 放大器归 1，主链（价格动量）不受影响

---

## 与 AISS / someopark 主程序的关系

AEUS 是 `semiconductor_strategy`（AISS）的整目录克隆孪生（**cloned 2026-08-30**；继承注释里的历史日期均指 AISS 血统）：**相同的引擎架构**（native loop、smart_select/MCPS、walk-forward、portfolio_record、win-criterion、V1/V2 双轨），**不同的宇宙**（AI-power 产业链 10 子板块 vs 半导体 8 子板块）、**同族但重建的核心信号**（电力知识图谱传播 vs 半导体供应链图谱）、以及一个明确的硬门槛（跑赢 XLU & GRID）。两策略共享同一最上游驱动（4 家 hyperscaler 的 AI capex）——半导体和电力的天然对称性。

AEUS 相对 AISS 的三处机制扩展：**N 档 base_w 篮子**（80/15/5 成为特例）、**purity tilt**（Group N，κ=0 逐 bit 回归锚）、**9 步 update_data 的电力 altdata 全谱**（EIA/ERCOT/PJM）。

AEUS 只读取 `price_data/macro/`、macro 模块的 EIA 镜像与 ERCOT sqlite，只增量写 `price_data/elec_strategy/`（+ `historical_runs/electric_utilities_strategy/`），**绝不触碰 `semiconductor_strategy/`** 或根脚本。设计文档 [AEUS_PLAN.md](AEUS_PLAN.md)（10 节 + 逐行核验记录）；运维操作详见 [RUNBOOK.md](RUNBOOK.md)。所有命令使用 `conda run -n qlib_run`。
