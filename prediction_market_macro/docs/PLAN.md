# prediction_market_macro · 总体开发计划(PLAN.md v3)

> **家族定位**:`prediction_market_<类别>` 是平台化分支体系——现有 `prediction_market/`(世界杯)是
> **母版**,macro 是第一个分支,之后有 `prediction_market_nba`(NBA)、`prediction_market_soccer`
> (五大联赛/欧冠/欧联/欧国联/欧洲杯)。macro 因此承担双重使命:自身盈利 + 沉淀"从母版继承"的
> 标准化模式(§16-§18),让后续分支照抄。前端统一挂在 `someo-park-investment-management`。

2026-07-26。目标:把 Kalshi 规律性宏观市场做成 数据 → 预测 → 定价 → 决策 → 风控 → 复盘 的完整系统,
**告诉我们每个 bet 该不该下、下哪边、下多少**;并且——这是 v2 的重心——
(a) **不同节奏(日/周/月/每次FOMC/季/年)的市场各有一条常规运营管线,regularly 跑、自动查、不可错过**;
(b) **建模/回测/预测的基础设施深挖**,为后续几十个系列的开发做好地基。
沿用 `prediction_market/`(世界杯)验证过的纪律,零代码依赖,只复制模式。

## 已交付基线(本 plan 的起点)

1. `prediction_market_macro/` 模块骨架:config/ ingest/ venues/kalshi/ model/ strategy/ exec/ jobs/ ops/ research/ data/output/ docs/ tests/ util/(README 已写明职责与 someopark_run 环境)
2. `research/discover_series.py`:可重跑全量发现(2,905 系列/6,943 open 事件)→ `data/output/kalshi_macro_catalog.json`
3. `docs/SERIES_CATALOG.md`:人工筛注的完整系列目录(含结算源、已知坑)

## 0-bis. 开发边界(硬约束,先于一切设计)

1. **后端只准在 `prediction_market_macro/` 内开发**:someopark-test 主目录与其它一切目录
   (DailySignal/VIXForecast/RegimeDetector、`prediction_market/`、`dfm/`、qlib-main …)
   **一律只读**——可以 import 调用(如 `run_vix_forecast`),一个字不许改。
   **代码与状态全部住 macro 目录**(Chronos ckpt、db、日志、plist 本体);仅有的两个
   目录外写入点,白名单明示:
   (a) `someo-park-investment-management/public/data/macro_*.json` 与 macro PDF
       (frontend_export 的输出目标——数据文件,非代码);
   (b) `~/Library/LaunchAgents/com.someopark.macro*.plist`(launchd 的硬性安装位置,
       安装=从 macro/ops/ 复制,plist 内以绝对路径回指 macro 目录)。
   此外无第三个写入点;审计=遍历 git status + 文件系统 mtime
2. **前端只在 `someo-park-investment-management/` 内动,且股票 view 与 prediction view
   并存不删**:现有 stock/prediction 两套视图一个不删、语义不改;macro 组件全部走
   "copy prediction view → 到 `src/components/macro/` 内修改"(§16.2);对现有文件的触碰
   收敛到**最小接线清单**:`App.tsx`(appMode 三态)、`Sidebar.tsx`(第二按钮)、
   `ChatArea.tsx`(欢迎页三分支+chat 传参)、`server/index.ts`(挂 macroAnalyze 路由)、
   `server/routes/chat.ts`(§16.4 增量注入)、i18n 五语言包(加 `macro.*` 词条)——
   此清单之外的现有文件只增不改;上述文件的改动全部是加分支,不改既有分支行为
3. **Chat 感知 macro 但 prompt 路径保持纯洁**:someoagent 模式与非 someoagent 模式
   (cloud/local 两条 agent 路径)的 **coding approach 系统 prompt 一字不改**——macro 知识
   只经 §16.4 的"artifact grounding 注入"进入 chat,与 WC 的注入模式完全同构;
   验收:coding prompt 相关文件 diff 必须为空

---

## 1. 市场全景(按节奏,即运营管线的分道依据)

**每周**(3 硬数据 + 另类):★KXJOBLESSCLAIMS(周四初请,唯一周频硬宏观);KXWTIW/KXBRENTW/KXNATGASW/KXAAAGASW 油气汽油周盘;KXGOLDW/SILVERW/COPPERW 金属;另类 KXTSAW(TSA)/KXUSFLYCAN(航班取消)/KXAMSAVO(牛油果)

**每月**(发布日驱动,核心战场):
- 通胀:★KXCPI/KXCPICORE(MoM 阶梯)、KXCPIYOY/KXCPICOREYOY、★KXPCECORE、KXUSPPIYOY、KXCPINDEX、CPI 分项(住房/汽油/二手车/机票/鸡蛋)、Truflation 系列
- 就业:★KXPAYROLLS、★KXU3、KXADP、KXCHCUTS、KXTEMPHELP
- 其他:KXISMPMI、KXUSRETAIL、房地产三件套(KXEHSALES/KXHOUSINGSTART/KXBUILDPERMS)、KXAAAGASM、KXPOWERKWH

**每次 FOMC**(8 次/年):★KXFEDDECISION(五腿)+★KXFED(阶梯,互检)、KXDOTPLOT、KXFOMCDISSENTCOUNT、KXFOMCGUIDE(Warsh 时代新品种)、KX2YFOMC/KXDXYFOMC(决议日行情);外加 11 家外国央行 KXCBDECISION*(BoC/ECB/BoE/BoJ/RBA/RBNZ/BoK/Banxico/RBI/以色列/南非)

**每季**:★KXGDP(BEA 各口径)、德法 GDP 初值 | **每日**:KXWTI、KXUST2/5/7/10/30、KXEURUSD/KXUSDJPY、KXAAAGASD、标普/纳指全套 | **年度**:油气极值、降息次数、KXRECSSNBER、Sahm 规则

**优先级**:P0=FOMC(51 次加息史的 FRED 管道直接复用)+CPI nowcast+周初请+非农/失业率+core PCE;
P1=AAA 汽油+油气周盘(期货曲线对照)+美债日频+GDP(GDPNow 对照)+外国央行(OIS 对照);
P2=CPI 分项+Truflation+另类周频+COMBO↔单腿一致性套利。

---

## 2. 凭证与基建(已全部核实,零新申请)

| 用途 | 来源 | 说明 |
|---|---|---|
| 环境 | conda `someopark_run` | 一切 python 经 `conda run -n someopark_run` |
| FRED/ALFRED | `FRED_API_KEY`(根 .env) | **一律 ALFRED vintage 做 PIT** |
| 期货/FX/新闻 | `POLYGON_API_KEY`(根 .env) | CL/NG/RB/GC aggs、C:EURUSD、`/v2/reference/news` |
| Kalshi 行情 | 公开无鉴权 | **价格必须逐 ticker 拉 orderbook**(已验证) |
| Kalshi 交易 | `KALSHI_PROD_API_KEY_ID`+RSA key(prediction_market/.env) | 后期;demo 套先行 |
| nemotron | `http://100.76.95.43:11434/v1`,model `nemotron-3-super:120b` | box A;**全局锁串行**,可降级 |
| 官方日历/文本 | 无需 key | BLS/BEA/EIA/Fed 官网 + 声明文本抓取 |

.env 纪律:根 `.env` 与 `prediction_market/.env` 都 source;settings 缺 key 报名字,绝不静默 None。

---

## 3. 架构总览

```
 BLS/BEA/EIA/Fed日历   FRED/ALFRED   Polygon(期货/FX/新闻)   Kalshi行情      nemotron(boxA)
        │                  │                │                  │                │
        ▼                  ▼                ▼                  ▼                │
 ingest/calendars    ingest/fred    ingest/polygon_data   ingest/kalshi_md     │
        └─────────┬────────┴──────────┬─────┘                  │               │
                  ▼                   ▼                        ▼               ▼
            data/macro.db ◄──────────────────────────────(全部落库)──── analysis/llm
                  │
   ┌──────────────┼──────────────────┬───────────────────┐
   ▼              ▼                  ▼                   ▼
 features/    model/<series>   research/(回测/实验)   scheduler(§8 节奏管线+看门狗)
 (特征store)  (预测分布)                                    │
   └──────┬───────┘                                        │ 驱动
          ▼                                                ▼
   strategy/(devig→edge→decision→sizing) ──► ops/ledger(PIT台账) ──► ops/{pnl,risk,report}
                                                           └──► exec/(paper→real,最后开)
```

原则:一切进 sqlite(`data/macro.db`);模型只读库;任何一步可独立重放;时间戳 UTC;
决策带"当时可见数据水位"。

---

## 4. `config/` — 配置与系列注册表

`settings.py`:`Settings` dataclass(db_path/output_dir/各 key/kalshi_base/ollama_base/
trading_enabled=False 硬默认)+ `load_settings()`(双 .env 装载,缺 key 报名)。

`registry.py`:`REGISTRY: dict[str, SeriesSpec]` 单一事实来源:

```python
@dataclass(frozen=True)
class SeriesSpec:
    ticker: str          # "KXCPI"
    family: str          # inflation|fed|labor|gdp|energy|rates|alt
    cadence: str         # "daily"|"weekly"|"monthly"|"per_event"|"quarterly"|"annual"
    calendar: str        # calendars key:"BLS_CPI"/"DOL_CLAIMS"/"FOMC"/…(daily 类填 "MARKET_DAYS")
    settle_source: str   # 精确到舍入规则与初值/修正值口径——上线前人工核 rulebook,test 断言非空
    structure: str       # ladder|binary|categorical
    unit: str; round_rule: str
    model: str           # model/ 模块名
    prior_source: str|None   # "polygon_futures:CL"/"kalshi:KXFED"/None
    priority: int        # 0/1/2
    lanes: list[str]     # 参与的运营管线(§8):["release_day","daily_snapshot",…]
```

P0 首批注册:KXFEDDECISION, KXFED, KXCPI, KXCPICORE, KXCPIYOY, KXCPICOREYOY, KXPCECORE,
KXJOBLESSCLAIMS, KXPAYROLLS, KXU3。

---

## 5. `ingest/` — 数据接入(幂等、缓存、全落库)

- **fred.py** `FredPIT.series(sid, as_of=None)`(as_of→ALFRED vintage)、`latest_release(sid)`;
  核心序列 CPIAUCSL/CPILFESL/PCEPILFE/UNRATE/PAYEMS/ICSA/DFEDTARU/DGS2..30/T5YIE/GASREGW/DCOILWTICO/GDPC1;
  表 `fred_obs(sid, obs_date, value, vintage_date)`——vintage 维度是回测正确性的根
- **polygon_data.py** `futures_daily(root)/futures_intraday(root,day)/fx_daily(pair)/news(query,since)`;
  表 fut_daily/fut_intraday/fx_daily/news
- **calendars.py** `CALENDARS: dict[str, list[ReleaseEvent]]`(BLS_CPI/BLS_JOBS/DOL_CLAIMS/BEA_GDP/
  BEA_PCE/FOMC/EIA_NG/EIA_PETRO/MARKET_DAYS);2026H2-2027 手工硬编码,`refresh_from_web()` 只校验;
  `next_release(cal, now)`、`releases_between(cal, a, b)`
- **kalshi_md.py** `KalshiMD.events/markets/orderbook/candles/settled/snapshot_series`;
  限速 0.12s+重试;表 quotes/contracts/settlements
- **store.py** `init_db()` 幂等;表:fred_obs, fut_daily, fut_intraday, fx_daily, news, quotes,
  contracts, settlements, releases, **features, preds, models, experiments, runs, coverage,**
  decisions, fills, marks, llm_annotations。不删历史行。
  **并发纪律**:tick/watchdog/refresh 三个进程共库——WAL 模式 + busy_timeout=30s +
  写事务短小;watchdog 只读 + 只写 runs/alerts 两表,与业务写路径不交叉。

---

## 5-bis. PIT 完备性总设计(逐源泄露审计,2026-07-27 复查定稿)

> 复查发现"用 ALFRED vintage"一句话不足以封死泄露——以下 7 个具体泄露口逐一给出机制级封堵。
> **总机制:双时钟 + 单一取数口 + 运行时断言 + 测试矩阵**,四层全上。

### 5-bis.1 双时钟(two-clock)数据模型

库内**每一行数据**都带两个时间:`event_time`(数据描述的时点)与 `knowledge_time`
(**我们最早可能知道它的时刻**)。一切 PIT 过滤只认 knowledge_time:

| 源 | knowledge_time 的确定方法 | 封堵的泄露 |
|---|---|---|
| FRED 修订类(CPI/PAYEMS/GDP/PCE/ICSA) | ALFRED `realtime_start`(**日粒度**)+ **release 日历的官方发布时刻 join**(CPI=08:30 ET 等)→ 日内精确 | 【口1】ALFRED 只有日期:发布日早晨 07:00 的 asof 若按"当日 vintage 可见"即泄露——T-1h 预测正卡在这个口上 |
| FRED 市场类(DGS*/DCOILWTICO) | 数据日收盘时刻;**只用已收盘完整 bar** | 【口2】"今天"的日 bar 未收盘即引用 = 用了盘中未来 |
| Polygon 期货/FX | bar 结束时刻 ≤ asof 才可见(日 bar=收盘,分钟 bar=bar end) | 同口2 |
| Kalshi 自建快照/candles | 快照 ts;candle 覆盖 [t,t+1h) 的行 knowledge_time=t+1h | 【口3】candle 按开始时间取用 = 提前一小时看到未来价 |
| 第三方 nowcast(GDPNow/Cleveland) | **各自官方 vintage 表**(两家都发布历史 vintage,入库 `nowcast_vintages`) | 【口4】用"当前值"回填历史特征 = 把终值当过程值 |
| 无官方 vintage 的源(Truflation/AAA 页面/新闻聚合) | **自建 first_seen_ts**:我们的 ingest 第一次存到该值的时刻即其 knowledge_time | 【口5】上线前的历史无法补 PIT → 该特征**禁止进入上线前回测**,只从我们开始记录之日起累积可用史 |
| 新闻/LLM 标注 | 文章 `published_utc`;标注行记 llm 版本,replay 只喂 published_utc≤asof 的文章 | 标注晚于文章生成不构成泄露(输入只有文章),但须锁 llm 版本 |
| 手工登记表(sep_dots/强度表) | 每行强制 `entered_at` + 官方可知时刻(点阵=SEP 发布 14:00 ET) | 手工数据无时间戳=默认泄露 |
| DoL 季节因子(claims SA) | 因子**成套带 vintage**(每年重定基改写全史);asof 用当时在役的因子套 | 【口6】用今年因子算去年 SA = 全序列泄露 |

### 5-bis.2 标签(y)也要 PIT——最隐蔽的口

【口7】**训练/回测标签必须用 ALFRED 首发 vintage(y_first),不是今天的修订值(y_latest)**。
NFP 首发与终值差经常 ±50k+:用修订史训练是在学"修orted 后的真相",而 Kalshi 按**首发 print 结算**
——模型该学的恰是首发的误差结构。落库双列 `y_first / y_latest`;回测评分、健康巡检、
校准全部对 y_first;y_latest 只用于研究修订模式(本身可作特征:上月修订方向)。
交叉验证:y_first 必须与 Kalshi `settlements` 逐期对得上,对不上=结算细则读错,全系列停(铁律2)。

### 5-bis.3 Chronos-2 的 PIT 协议(基础模型特有的两个口)

- **微调泄露**:ckpt 元数据强制记 `trained_through`;推理时运行时断言 `trained_through ≤ asof`,
  违反直接 raise。live 每周 refit 自然满足;**回测 replay 禁用微调 ckpt**(除非做逐窗 walk-forward
  refit,成本另议)——回测一律 zero-shot
- **预训练污染**:chronos-2 基座本身见过历史宏观序列——**发布日之前的回测成绩带污染风险,
  一律标注 discount、不得作为加权依据**;Chronos 的权重只由其上线后 live-forward 的 paper
  成绩决定(preds 表从部署日开始积累的真 OOS)

### 5-bis.4 执法机制(不靠自觉)

1. **单一取数口**:模型只准经 `feature_frame(series, asof)` 取数,内部统一 knowledge_time≤asof
   过滤;直接 SELECT 原始表的模型代码 = code review 驳回项
2. **运行时断言**:每个 `Pred` 落库时记录 `data_horizon = max(所用行的 knowledge_time)`,
   写入前断言 `data_horizon ≤ asof`——不是测试期抽查,是每次预测的在线守卫
3. **测试矩阵 `tests/test_pit.py`**(M0 就位,每系列模型上线加一组):
   - 金丝雀:predict(asof=T) → 人为注入 T 之后的数据重跑 → 输出必须逐位一致
   - 单调性:knowledge_time 视角下 feature_frame(T1) ⊆ feature_frame(T2), T1<T2
   - 发布晨测试:CPI 日 asof=08:00 **不得**见当日 print,09:00 必须见(卡口1)
   - 标签测试:抽样断言回测标签 == ALFRED 首发值(卡口7)
4. **日历的 PIT**:发布**日程**前向已知可用(future covariate 合法);但日程本身会改
   (停摆推迟)——calendars 存 `scheduled_ts` + `actual_ts` 双列,回测用当时的 scheduled、
   结算对账用 actual
5. **时区纪律**:库内一切时间戳 UTC;发布时刻以 ET 定义 → 逐日期按 EDT/EST 折算成 UTC
   存进日历(硬编码 "08:30 ET" 直接换算会在 DST 切换周错一小时——3 月/11 月各有一次,
   而 3 月切换周恰有 CPI/FOMC)

## 6. 统一预测抽象 — `model/common.py`

```python
@dataclass class Pred:  series; period; dist: Dist; asof: datetime; model_version: str; inputs: dict
Dist = GaussianMix | Empirical(samples) | Categorical
def grid_pmf(dist, round_rule) -> dict[float,float]      # 先离散到结算网格(0.1%/1k/…)的点质量
def survival(pmf, strike, strict: bool) -> float          # P(print > x) 或 P(print >= x)——腿定价的原子
def leg_fair(pmf, leg: Contract) -> float                 # 按 strike_type(greater/greater_or_equal/…)取 survival
def brier(...); def crps(...); def logscore(...)
```

一切模型输出 `Pred`;**核心表示是"结算网格上的点质量 + 生存函数"而非桶向量**(§18 实测:主力市场
全是累积阈值梯)。strict(>) 与 ≥ 的区分是一阶定价输入——CPI strike 恰在 0.1 网格上,
P(print=0.1) 可达 30%+,"Above 0.1%" 在 print=0.1 时结 NO。策略/评估层对所有系列通用。

---

## 7. `model/` — 逐系列模型(P0 签名,详见各节)

- **fed.py**:`market_prior`(KXFED devig,与 KXFEDDECISION 互检)+`reaction_function`(FRED PIT 特征:
  core CPI yoy/ΔU3(12m)/U3/headline-core 缺口/油价动量/上次决议/点阵鹰派数;51 次加息史判别式
  + 1990-2026 会议面板有序 logit)+`statement_risk`(llm)→ 对数池合成
- **cpi.py**:`energy_nowcast`(GASREGW+RB 期货传导)+`core_nowcast`(AR+季节+shelter 滞后;Cleveland
  nowcast 只作特征)→ headline 合成;**YoY 由 MoM 精确换算**(基数已知)→ MoM↔YoY 一致性即套利探测
- **claims.py**:ICSA NSA 状态空间+52 周季节因子+假日 dummy+新闻冲击项;DoL 当年 SA 因子换算
- **payrolls.py / u3.py**:ADP+claims 桥接、胖尾 σ~70k;U3 转移矩阵 Categorical
- **pce.py**:CPI 分项→PCE 桥接回归;**CPI 发布日后 PCE 合约滞后重定价 = 核心猎场**
- **energy.py**:`wti_weekly/natgas_weekly`(前月期货中枢+实现波动 GBM 模拟;EIA 库存日方差膨胀)、
  `aaa_gas_month`(RB→零售传导分布回归)
- **gdp.py**:GDPNow+NY Fed nowcast 加权,σ 按距发布天数的历史误差曲线
- **ts_foundation.py — Chronos-2 时序基础模型桥(调查定稿 2026-07-27)**:仓库已有生产级范例
  `VIXForecast.py`(根目录,DailySignal `--vix-forecast` 调用):`amazon/chronos-2` 微调,
  CTX 504 天、预测 10 日、past_covariates(VIX9D/VIX3M)+future_covariates(FOMC 日程,前向已知)、
  零泄露设计、双微调模型按 walk-forward OOS Dir Acc 加权(0.542/0.458)、ckpt 新鲜度检查
  (`_ckpt_is_fresh`)、结果缓存、someopark_run env。macro 的用法(**铁律:不改 VIXForecast.py
  与其 ckpt 一个字**):
  (a) **模式复刻**:在本模块内建 `Chronos2Bridge`,照抄其零泄露调用形态(context 截至 asof、
      前向已知日历作 future_covariates、分位数输出),对**连续价格型系列**跑推理:
      WTI/NG 周月盘、UST 日频、FX、AAA 汽油、ICSA claims——输出分位数 → `Empirical` Dist,
      作为 §19.2 对数池的第三成员(统计模型/市场隐含/Chronos)
  (b) **只读调用现成信号**:`from VIXForecast import run_vix_forecast`(带缓存,幂等)——
      VIX 10 日方向作为 fed/风险类模型的 risk-regime 特征
  (c) **每日多次推理**:推理轻(秒级),凌晨 refresh + 事件窗 tick 各跑一次;fine-tune 重,
      沿用其 ckpt 新鲜度模式**每周一次**、walk-forward 验证后才换 ckpt
  (d) 各系列 Chronos 预测独立进 preds 表(model_version="chronos2/..."),与统计模型
      同台接受巡检与回测——赢了才加权,输了权重归零,不感情用事
  (e) **先测后用(用户定稿)**:Chronos 上线走 §7-bis 组件采纳协议——shadow 模式先跑
      (落 preds、不参与决策),逐系列积累 live-forward 成绩(周频 ≥8 期、月频 ≥3 期),
      对统计基线与市场基线双赢才入对数池;权重=该 shadow 窗的相对表现,不预设
- **dfm_bridge.py — DFM 金融数学核心桥(深度调查定稿 2026-07-27;football 部分忽略)**:
  `dfm/` 根目录是 Chen et al. 2025 扩散因子模型的完整实现:OU 前向/逆向(`diffusion.py`)、
  因子分数网络(`score_network.py`)、DSM 训练(`score_matching.py`)、采样(`sampling.generate_samples`)、
  合成基准(`synthetic.FactorModel`)、评估套件(`metrics/evaluation`:分布/子空间/协方差误差)、
  组合验证(`portfolio.py` min-var,cvxpy)。**已验证战绩**(`results_real/summary.json`):
  真实美股 n=120/d=128/k=8 四个 split,扩散协方差全胜 sample 与 Ledoit-Wolf——
  正是"样本太少、维度不小"时的数据模拟器。macro 用法(**只读 import,输出一律落 macro 目录**):
  (a) **联合宏观情景引擎**(首选,吃它已验证的强项):月度宏观面板(d=数十:CPI 分项/claims/
      NFP/油气/各期限收益率,n=100-300 月)训练 → 生成 5,000 组**保持跨系列协方差结构**的
      联合情景 → `risk.scenario_var` 从"逐 print 独立 ±2σ"升级为相关联合情景
      (同月 CPI+PCE+claims 持仓的真实相关敞口)
  (b) **小样本家族的不确定性量化**:FOMC 会议级特征向量(近政权 n~30)、假日周 claims 等
      n 太少的场景,DFM 放大出合成情景给反应函数做压力测试与置信带——
      **只用于不确定性/风险,严禁作为被评估模型的训练数据**(循环污染,铁律)
  (c) **回测稳健性 bootstrap**:合成 print 序列面板对策略 PnL 做结构保持的压力重放
  (d) 采纳同走 §7-bis:情景引擎的门 = 在留出宏观面板上,DFM 生成协方差胜 sample/LW
      (直接复用 dfm 自带 evaluation 指标);不达标就继续用独立 ±2σ,不硬上

### 7-bis. 组件采纳协议(Chronos/DFM/一切新组件,"确定可以用"的统一门)

任何新预测/模拟组件入系统一律三段式,写死不例外:
1. **接入即 shadow**:组件输出照常落库(preds/scenarios,带 model_version),
   **不参与任何决策**;from day one 接受 §9.6 巡检
2. **采纳门**(逐系列/逐用途独立过关):live-forward shadow 窗内(周频 ≥8 期、月频 ≥3 期、
   FOMC ≥2 会)对"现任方案"与市场基线**双优**(Brier/CRPS 或用途对应指标);样本不够就继续等,
   不用回测成绩顶替(Chronos 预训练污染、DFM 无历史 shadow——回测证据只降不升)
3. **入池与退出**:过门才进对数池/风控管线,初始权重=shadow 窗相对表现;入池后巡检
   连续 2 窗劣化 → 权重归零回 shadow——采纳可逆,退出无感情

---

## 8. 节奏管线与运营(v2 核心之一:regularly、自动查、不可错过)

### 8.0 基础节奏:每日凌晨全量重估(运行方式的主心骨,用户定稿)

**无论各市场自身频率(每日/每周/每月/每年 8 次 FOMC/年度),一律每天重估**——与世界杯系统
的每日 refresh 同模式。每天固定凌晨(默认 05:00 ET,在 08:30 数据发布前、与 WC refresh 错峰)
跑 `ops/refresh.py` 全量:

1. **ingest 全刷**:FRED 增量、期货/FX 收盘、AAA、新闻、全部注册系列的 Kalshi 快照;
   **含新事件自动发现**——每个注册系列查 open events,新出现的期(如 KXCPI-26SEP 上架)
   自动进 contracts 表 + coverage 表落 scheduled 行,不靠人工发现新合约
2. **全系列全周期 predict**:对**每一个有 open 合约的 (series, period)** 重新 `predict(asof=now)`
   落一行 preds——FOMC 盘距会议还有 3 周也每天重估(模型是 asof 的函数,重估免费);
   CPI 月度盘随着当月汽油/期货/claims 逐日进来,预测每天都在移动——这正是 preds 表
   收敛轨迹(§9.3)的数据来源
3. **每日 scan/decide**:edge 出现在哪天就哪天下(paper),不等发布日——闸门(§11)照常把关;
   lane 的 T-1h 定稿只是发布窗的**加密重估**,不是唯一决策时点
4. mark 持仓、结算对账、导出前端 `macro_*.json`、日报(含覆盖矩阵与 MISSED)
5. **每日模型健康巡检(§9.6)**:滚动 OOS 重打分 + 模型 break 探测 + 台账回归自检——
   模型悄悄坏掉比没有模型更危险,巡检与预测同频每天跑

**可执行的 SLA(写进 watchdog)**:任何 open 合约的最新 pred 年龄 ≤24h;凌晨 refresh 后
`coverage_report` 逐 (series, period) 核对当天 pred 行,缺席即 MISSED 告警——
"每天都更新"不靠自觉,靠看门狗。

### 8.1 分道(lane)设计——事件窗加密,叠加在 §8.0 之上

每个系列按 `SeriesSpec.lanes` 挂进若干条管线;**管线 = 生命周期状态机 + 由日历物化的应跑任务**:

```
生命周期(每系列每期一行,表 coverage):
 scheduled → armed → snapshotting → predicted → decided|passed → frozen → settled → reconciled
              │            │            │           │              │         │          │
           T-24h        窗口内每5min   T-1h 定稿   闸门判定     T-10min    结算源     对账+归因
```

| lane | 适用 | 节奏细则 |
|---|---|---|
| `release_day` | CPI/NFP/claims/PCE/GDP/零售/房地产 | T-24h arm(拉全特征、预测初稿)→ T-2h 快照加密(5min)→ T-1h 预测定稿+决策 → T-10min 冻结 → 发布后 3min 重估(捕捉 PCE 滞后/一致性错定价)→ 结算对账 |
| `fomc_week` | KXFEDDECISION/KXFED/DOTPLOT/DISSENT/GUIDE/外国央行 | T-7d 起每日 statement_risk+一致性扫描 → 决议日走 release_day 状态机 |
| `weekly_close` | WTIW/NATGASW/AAAGASW/金属/TSAW/航班 | 周一 arm(期货中枢定价)→ 每日 snapshot → 周四/五决策窗 → 周末结算对账 |
| `daily_snapshot` | KXWTI/UST*/FX/AAAGASD | 每日 2 次快照(开盘后/收盘前);只记价不决策(P1 后开决策) |
| `quarterly` | GDP/德法 GDP | 挂 release_day,日历稀疏 |
| `annual_watch` | 极值/衰退/Sahm | 每周一次快照+月度重估,无高频决策 |

### 8.2 不可错过机制(watchdog,独立于业务代码)

```
表 runs(id, lane, series, period, due_ts, status[due|done|late|MISSED], done_ts, log_path)
```

1. **物化应跑表**:`scheduler.materialize(horizon=30d)` 每天把未来 30 天所有 lane×series×period 的
   应跑任务写入 runs(由 calendars 推导)——"应该发生什么"先于"发生了什么"存在
2. **执行器**:`scheduler.tick()`(cron 每 15min)领取 due 任务执行,写 done_ts;执行失败重试 2 次后标 late
3. **看门狗**:`scheduler.watchdog()`(cron 每小时,**独立进程**)扫描 `due_ts < now-30min 且 status=due|late`
   → 标 MISSED + 写 `ops/alerts.log` + 醒目进入日报头部;**决策类任务过窗只标 MISSED 绝不补跑**
   (迟到的决策=用了未来数据),快照/对账类任务允许 catch-up
4. **覆盖矩阵**:`ops/coverage_report()` 输出 系列×期 的状态矩阵(日报附),任何洞一眼可见;
   目标 SLA:P0 系列 release_day 覆盖率 100%,快照类 ≥98%
5. **数据新鲜度门**:每次 predict 前检查各输入源 staleness(FRED 最新 obs 距今、quotes 快照年龄、
   期货收盘日期),超阈值 → 决策自动降级为 PASS(理由=stale_data),进台账

### 8.3 调度总表(launchd;M0 期先手动跑)

**调度载体用 macOS launchd(照抄母版)**:WC 系统用 `com.someopark.prediction{refresh,live,matchtrigger}.plist`
——macro 同款三件:`com.someopark.macrorefresh.plist`(**每日凌晨 05:00 ET 全量重估,§8.0 主心骨**;
周日加 --weekly)、`com.someopark.macrotick.plist`(15min tick,只在事件窗有活)、
`com.someopark.macrowatchdog.plist`(每小时,独立进程,含 24h pred 新鲜度 SLA 核查)。
plist 内 `bash -lc 'cd <repo> && set -a && source .env && source prediction_market/.env && set +a &&
conda run -n someopark_run python -m prediction_market_macro.jobs.tick'` 的既有模式;
materialize 由每日 refresh 顺带增量补(不单设)。M0 期先手动跑,M6 注册 launchd。

---

## 9. 建模/回测/预测基础设施(v2 核心之二:深挖地基)

### 9.1 特征仓 `model/features.py`

```python
def feature_frame(series: str, asof: datetime) -> dict[str, float]
    # 唯一取数入口:内部全走 FredPIT(as_of)/库内快照,返回该时点可见的特征字典
    # 写表 features(series, asof, name, value, source, vintage)——决策可复现的物证
def feature_defs(series) -> list[FeatureDef]   # 名称/来源/变换/滞后 声明式注册,模型不自己拼数据
```

铁律:模型函数签名一律 `predict(asof)`,**内部不得出现"现在"**;测试用例:同一 asof 重放必须逐位一致。

### 9.2 模型注册与版本 `model/registry.py`

```python
@dataclass class ModelSpec: series; name; version: str  # "cpi/1.2.0" semver
    params: dict; feature_set: list[str]; trained_through: date; frozen: bool
表 models(series, version, params_json, trained_through, created_ts, card_md)
```

- 每次 refit 产生新 version,老版本参数永久保留(决策台账记录当时 version → 任何历史决策可重放)
- refit 协议:**只允许 walk-forward**(trained_through 单调递增);refit 节奏入 registry
  (claims 月度 refit、CPI 季度、fed 每次会议后追加一行样本)
- model card(card_md):特征表、样本窗、已知失效场景——每系列上线闸门的审阅材料

### 9.3 预测存储 `preds` 表

```
preds(series, period, asof, model_version, dist_json, ladder_json, created_ts)
```
每次 predict 落一行(含盘中多次)——预测历史本身是一等数据:
(a) 决策复现;(b) 预测漂移分析(asof 越近发布,分布应收敛,不收敛=模型病);(c) 与市场价对齐做 lead-lag 研究。

### 9.4 回测引擎 `research/backtest.py`

```python
def backfill_settled(series)            # Kalshi settled 结果 + candles 历史价入库,能拿多深拿多深
def replay(series, start, end, model_version=None, asof_offsets=("-24h","-1h")) -> BTReport
    # 对每个历史 print:ALFRED vintage 重建当时可见世界 → predict → 用 candles 当时价 scan/decide
    # → 真实结算记 PnL;BTReport: n/roi/brier_model/brier_market/calib_bins/pnl_curve/edge_capture
def experiment(name, series, grid: dict) -> ExpReport
    # 参数网格 × replay;表 experiments(name, config_hash, series, window, metrics_json, ts)
    # config_hash 保证同配置不重算;ExpReport 附 leaderboard(按 net-of-fee ROI 与 brier 双排序)
```

统计检验标配:模型 vs 市场基线的 **Diebold-Mariano 检验**(Brier 差)、bootstrap ROI 置信区间、
按 print 分块的置换检验——单系列样本小,显著性要诚实报告,不显著就写不显著。

### 9.5 评估与上线闸门 `research/eval.py`

`calibration_table`(10 分位)、`edge_capture`(实现 PnL/账面 edge,量滑点+逆选择)、`drift_check`。
**上线闸门(每系列独立)**:回放 ≥12 个 print(claims≥26 周)且 brier_model<brier_market 且 ROI>0
且 edge_capture>0.4 且校准无系统偏斜——全过才 real,否则永远 paper。闸门结果写 model card。
**市场基线的取样约定**:brier_market 一律用与我们 pred **同 asof** 的快照 devig 概率——
不同时点的比较是伪比较(市场 T-5min 永远赢 T-24h 的模型,那不叫输)。

### 9.6 每日模型健康巡检 `research/health.py`(§8.0 第 5 步的实现)

每天凌晨随 refresh 全量跑,输出 `macro_health.json`(进日报置顶区 + 前端 coverage 页):

```python
def daily_health() -> HealthReport:
    # 1. 滚动 OOS 重打分:每系列取最近 N 个已结算 print(claims N=26,月度 N=12,FOMC N=8),
    #    用台账里当时的 pred 重算 Brier/CRPS/log-score 滚动曲线——只读 preds+settlements,零重训
    # 2. break 探测器(任一触发 → 系列降级 paper + 告警):
    #    a) 滚动 Brier 差于市场基线(同 asof 快照口径,见 §9.5)连续 2 窗
    #    b) CRPS 滚动均值超其自身 12 期均值 +2σ(突然变笨)
    #    c) 收敛性病征:距发布 <48h 时预测分布熵不降反升(§9.3 preds 轨迹)
    #    d) 特征异常:feature_frame 任一输入超出其 5 年 z=4 包络(数据源坏了先于模型坏)
    #    e) Chronos 桥专项:推理输出分位数交叉/NaN(基础模型加载坏)
    # 3. 台账回归自检:随机抽 3 条历史决策,用其 inputs_json+model_version 离线重放 fair,
    #    与台账记录逐位比对——抓"代码悄悄变了"(依赖升级/重构回归),这是 WC 可复现纪律的自动化
    # 4. 汇总:per-series 红黄绿灯;红灯系列自动进 risk.circuit_breaker 流程
```

与 §9.4/§9.5 的关系:回测+闸门是**上线前审判**(重、每周/按需);本节是**上线后心电图**(轻、每天)。
runs 表为它物化每日任务,漏跑即 MISSED——巡检自身也被看门狗盯着。

---

## 10. `analysis/llm.py` — nemotron(box A,可降级)

`Nemo.ask_json(system, user, schema, timeout=180)`:OpenAI 兼容端点,**全局 threading.Lock 串行**,
失败一次重试后返回 None(不 block 决策)。用例:`fomc_statement_diff`(鹰鸽分)、`news_risk_tags`
(裁员/能源冲击/fed_speak 标注,喂 claims/energy 模型冲击项)、`weekly_narrative`(周报叙事)。
全部输出进 `llm_annotations` 表,可审计可关闭。

## 11. `strategy/` — 分布→决策(全系列通用)

devig.py(**两条路径**:categorical→multiplicative/power 归一;累积阈值梯→逐腿双边 devig 后
**isotonic 回归强制生存函数单调**,差分得市场隐含 pmf)|
fees.py(**精确费模**:Kalshi taker fee `0.07·C·P(1-P)` 按**每笔成交**收,结算不收——
默认持有到结算只付**入场单边**费;早退才加出场费;maker 挂单免 taker 费 → P1 执行升级
"maker-first 入场"预计省 30-50% 费用,edge 门槛可随之降)|
edge.py(`scan(series,pred,quotes)->list[Struct]`——**枚举的是结构不只是腿,且每腿双侧**:
YES 侧与 NO 侧独立订单簿、独立错价,fair_no=1-fair_yes 对 ask_no 同算 edge;
结构=单腿(Y/N)/ 相邻 strike 价差(YES(>x)+NO(>y) 合成桶 (x,y])/ 2 宽桶;
每个结构算 net edge 与最大损失)| decision.py(闸门:net_edge≥0.04、**结构内每腿** depth≥$50、
|fair−market_devig|≤0.25、距结算>30min、同 print 事件不加仓)| sizing.py(¼-Kelly 按结构
payoff 分布算、$1-cap)| consistency.py(**免模型四件套**:KXFED↔KXFEDDECISION 互推、
CPI MoM↔YoY 硬换算、COMBO↔单腿、**阈值梯单调性套利**——ask(>x)<bid(>y),x<y 即无风险,每次快照必扫)。

**exit.py — 持仓退出策略(母版 smart_exit 的宏观版,入场只是半个决策)**:
默认**持有到结算**(宏观合约周期短、点差贵,频繁进出被费吃掉)。三个例外走早退,
全部经台账(新增 decision 行,append-only):
1. **edge 反转退出**:每日重估后该结构 net edge < −0.06(模型说我们错了)且退出侧有
   足够深度 → 按 mid±滑点罚退出;−0.06 与入场 +0.04 之间是"持有带",防抖动
2. **红灯强退**:该系列被 §9.6 巡检降级 → 当日复核,模型性质问题则退出全部该系列持仓
3. **事件冻结例外**:发布前 10min 冻结窗内不新开也不退出(跳价窗挂单=送钱);
   发布后重估窗按 1 处理

## 12. `ops/` — 台账/PnL/风控/报表

- **ledger.py**:decisions 表 append-only 禁 UPDATE(撤销=新增 cancel 行);每条含 inputs_json+
  model_version+gate_snapshot——离线可复现 fair
- **pnl.py**:`mark_all`(orderbook mid M2M)/`settle_pass`(对账+归因:|z|<1 运气,>2 模型,中间混合)/
  `report`(总/族/系列 ROI、hit、Brier、edge captured)
- **risk.py**:LIMITS(per_event $5/per_family $20/per_release_day $30/gross $100/相关簇 $40——同一
  print 的 MoM/YoY/COMBO/分项算一簇);发布前 10min 冻结;`scenario_var`(每晚:基础版
  逐 print ±2σ;DFM 情景引擎过 §7-bis 采纳门后升级为 5,000 组保持跨系列协方差的联合情景);
  `circuit_breaker`(滚动 20 单 Brier 输给市场 → 降 paper+人工复核)
- **report.py**:日报(决策+marks+**覆盖矩阵+MISSED 告警**+7 天日历)/周报(+llm 叙事+校准图),
  复制 pdf_style 排版约定
- **refresh.py**:总入口 = §8.0 每日全量重估的实现体(steps 表模式):ingest 全刷 → **全部
  open (series,period) 逐个 predict 落 preds** → 每日 scan/decide(paper)→ mark → 结算对账 →
  导出全家桶 + 日报;幂等,<5min(不含回测);当天重复运行安全(preds 追加、决策去重)

## 13. `exec/` — 交易(最后开)

`kalshi_exec.py`:RSA 签名(demo 套先行)、限价单、`KALSHI_TRADING_ENABLED` 三层开关
(settings 默认 F → 每系列闸门 → circuit_breaker)。paper 模式 = 决策照写台账、fills 表记虚拟成交价
(ask 成交假设+滑点罚 1¢),与 real 共用全部下游。
**资金(bankroll)**:paper 期虚拟 $1,000 起算(ROI 有分母);实盘前的 bankroll 数额、
Kalshi 账户注资与额度确认是**用户操作项**,LIMITS(§12)以 bankroll 百分比重述后生效。

---

## 14. 里程碑(含 infra 验收)

- **M0 骨架+数据+调度地基(2 天)**:settings/registry/store/fred(ALFRED)/kalshi_md/calendars
  + **scheduler.materialize/tick/watchdog + runs/coverage 表**;
  验收:refresh 跑通、P0 十系列 quotes+FRED 落库、**runs 表物化未来 30 天且 watchdog 能抓出一个人为 MISSED**、
  **每日 refresh 对全部 open (series,period) 各落一条 pred(含 3 周后的 FOMC 盘)且 24h 新鲜度 SLA 核查生效**、
  **test_pit 四件套通过(金丝雀/单调性/发布晨/标签)+ data_horizon 在线断言生效**
- **M1 claims 端到端(1-2 天)**:features/claims 模型/preds/strategy/ledger/paper 决策 + release_day lane 实跑;
  验收:本周四初请全链路打印决策链,结算自动对账归因——第一条血管
- **M2 CPI+PCE(2-3 天)**:cpi/pce + MoM↔YoY 一致性;验收:8 月 CPI 发布日 release_day lane 全状态机走通
- **M3 FOMC(2 天)**:fed.py+llm 层+fomc_week lane;验收:9 月会议前全依据链决策(对照 7 月人工分析)
- **M4 回测 infra(2-3 天)**:backfill_settled/replay/experiment/DM 检验/上线闸门报告;
  验收:claims 与 CPI 各一份 BTReport + experiments 表有网格记录
- **M5 能源+就业+weekly_close lane(2 天)**:energy/payrolls/u3;验收:P0-P1 全系列 fair 看板
- **M6 风控报表+调度注册(1-2 天)**:limits 实测、日报含覆盖矩阵、circuit breaker 演练、
  launchd 三件套注册;之后才谈实盘
- **M7 前端 macro 家族(2-3 天)**:按 §16.2 三步走——整体复制 prediction/ → macro/,
  三态 appMode+Sidebar 第二按钮,MacroUpcoming(近期数据混排),MACRO_ITEMS ~20 视图,
  5 语言 i18n,macroAnalyze 路由;验收:**Step C 清洁审计三条全过**(grep 零 WC 残留、
  双模式 10 次互切无串数据)+ build:wc+deploy 后手机可用
- **M8 家族约定定稿(半天)**:§17 清单对照 macro 实际实现逐条勾验,形成 nba/soccer 开仓模板文档

## 15. 母版借鉴映射表(prediction_market/ → macro,逐模块盘点)

原则不变:**复制模式、不 import 代码**。下表是对母版的实地盘点(2026-07-27),右列 = macro 对应物。

| 母版资产 | 是什么 | macro 借鉴方式 |
|---|---|---|
| `strategy/decision_model.py` `decide()` | **单一纯函数入口**,生产/回测/报表三处共用同一决策逻辑 | 原样照搬架构:macro 的 `strategy/decision.py` 也必须是唯一入口,replay/生产/报表全走它——"what would we bet"永远只有一个答案 |
| `model/oos_eval.py` | 冻结模型 OOS 纪律:方向性体检、**只许触发结构修复、禁止对着调参**;bootstrap CI | 直接移植为 `research/oos_eval.py`:每系列模型 version 冻结后,后续 print 全是真 OOS;同款纪律条款写进 model card |
| `model/calibrate.py` | brier/log_loss/bootstrap_ci/reliability_curve 工具箱 | 复制到 `model/common.py`(补 CRPS/logscore) |
| `ops/walkforward_eval.py` | PIT walk-forward 闸门:改进必须先在 walk-forward 上证明降 Brier 才准上线 | 移植为 refit 协议的执行器:每次模型升版跑 walk-forward 对照旧版,赢了才切 |
| `ops/decision_backtest.py` + `backtest/replay.py` + `backtest/metrics.py` | PIT 决策回放 | §9.4 replay 的骨架蓝本 |
| `ops/param_sweep.py` | 参数网格+结果落 JSON 供前端 | §9.4 experiment 的蓝本;导出 `macro_param_sweep.json` |
| `strategy/xv_monitor.py` + `cross_venue.py` | 跨场所/模型 vs 市场 divergence 监控(只读) | macro 版 = 模型 fair vs Kalshi devig 的逐系列 divergence 看板(`macro_divergence.json`);PM 宏观盘上线后自然扩展成真跨场所 |
| `ops/backfill_price_ticks.py` + `milestone_export.py` | **price-track**:每笔决策的入场价与后续里程碑价,mark-to-market 展示 | 移植:决策后每小时快照该合约 mid 直至结算 → `macro_pricetrack.json`;这是"决策看起来对不对"的最直观视图 |
| `ops/settle_bets.py` | 结算对账 | §12 settle_pass 蓝本 |
| `ops/performance_report.py` / `risk_report.py` + `pdf_style.py` | 绩效/风险双报告,JSON+PDF 双输出 | 同款双输出;pdf_style 排版约定复制 |
| `ops/refresh_all.py` steps 表 | `(名字, lambda)` 步骤表逐步跑、单步失败不拖垮全局、逐步打 ✓/✗ | `ops/refresh.py` 同构 |
| `ops/frontend_export.py` | 汇总各导出 → 前端 `public/data/*.json` 单点写入 | 同构:`macro_*.json` 全家桶(§16) |
| `venues/kalshi/{auth,orders,market_data}.py` | RSA 签名、下单、限速行情——**已在生产验证过的 Kalshi 接入** | 这是唯一允许"参考实现细节"的模块(auth 签名协议是死的):照协议重写,接口对齐 |
| `exec/executor.py` | $1-cap 信号生成、`KALSHI_TRADING_ENABLED` 开关纪律 | §13 蓝本 |
| launchd plists | macOS 调度载体(不是 cron) | §8.3 已改用 launchd 三件套 |
| `jobs/live_poller.py` + `ops/match_trigger.py` | 事件驱动的赛中轮询(开赛自动触发) | release_day lane 的"发布时刻快轮询"同构(T-30min 起 5min→发布后 1min) |
| docs/PLAN_AUDIT.md + 闸门文化 | 每个 plan 项有验收审计 | macro 建 `docs/PLAN_AUDIT.md`,M 里程碑逐项打勾 |
| 微足球上线脚本 `online_microfootball.sh` | 预检→执行→三道校验→部署 的 sh 封装 | M6 后给 macro 也做一个 `ops/macro_health.sh`(预检数据源+看门狗状态+导出新鲜度) |

**不借鉴**(明确排除):足球专属模型(dixon_coles/xg/squad…)、in-play 全家桶(macro 无"赛中")、
API-Football ingest、DFM(除非未来做"宏观情景模拟放大",另立 plan)。

## 16. 前端(挂进 someo-park-investment-management,macro 专属视图族)

母版模式:`PredictionArtifactGrid.tsx` 注册表(type+i18nKey+Icon)→ `PredictionArtifact.tsx` 的
`REGISTRY[type]` 映射视图组件 → 数据一律来自 `public/data/*.json`(frontend_export 单点写)→
AI 分析按钮走 server route 调 nemotron(缓存于 box A)。macro 完全同构,新增 `macro_*` 家族:

| artifact type | 视图 | 数据文件 |
|---|---|---|
| `macro_board` | **主看板**:未来 14 天发布日历×系列,每格 fair/ask/edge/decision 状态灯 | `macro_board.json` |
| `macro_fed` | FOMC 专页:决议概率(模型/KXFED/KXFEDDECISION 三列)、反应函数特征表、声明鹰鸽分、点阵图 | `macro_fed.json` |
| `macro_inflation` | CPI/PCE 族:MoM 阶梯分布图(模型 vs 市场 devig 柱状对比)、YoY 换算一致性、分项 nowcast | `macro_inflation.json` |
| `macro_labor` | claims 周频序列+预测带、NFP/U3 阶梯 | `macro_labor.json` |
| `macro_energy` | WTI/NG/汽油周月盘 vs 期货曲线 | `macro_energy.json` |
| `macro_divergence` | 全系列 模型 vs 市场 divergence 排序表(xv_monitor 同构) | `macro_divergence.json` |
| `macro_decisions` | 今日决策+持仓 marks+price-track 走势 | `macro_decisions.json`, `macro_pricetrack.json` |
| `macro_performance` | ROI/hit/Brier/edge-capture,分族分系列(performance_report 同构) | `macro_performance.json` |
| `macro_calibration` | 校准曲线+OOS 报告(oos_eval 输出;reliability 图) | `macro_oos.json` |
| `macro_coverage` | **运营覆盖矩阵**(系列×期状态机 + MISSED 告警)——运营透明度是一等公民 | `macro_coverage.json` |
| `macro_risk` | 敞口/limits/scenario VaR(risk_report 同构) | `macro_risk.json` |
| `macro_overview` | 系统/模型说明书(system_overview 同构) | `macro_overview.json` |
| `macro_pdfs` | 日报/周报 PDF 下载 | ops 生成的 PDF |

### 16.1 入口与模式机制(用户定稿的设计,照此实施)

**不动 World Cup 入口**。现状:`Sidebar.tsx` 顶部 App Mode Selector 一个按钮,`App.tsx` 的
`appMode: 'stock'|'prediction'`(localStorage `sp-appMode`)+ `data-mode` 属性驱动全站主题,
`ChatArea.tsx` 欢迎页按 mode 条件渲染 近期比赛(PredictionUpcoming)+ 20 键格(PredictionArtifactGrid)。

改造(最小侵入):
1. `appMode` 扩为三态 `'stock'|'prediction'|'macro'`(localStorage 兼容:未知值回落 stock)
2. Sidebar 的 selector 区域**在 WC 按钮正下方加第二个同款按钮**(同样式、同反色规则):
   - WC 按钮:stock/macro 态点击 → 进 prediction;prediction 态点击 → 回 stock
   - MACRO 按钮:stock/prediction 态点击 → 进 macro;macro 态点击 → 回 stock
   - 即**两个模式互相之间一键直切**(prediction 里点 MACRO 直接切 macro,反之亦然)
3. `data-mode="macro"` 复用 prediction 的暗色壳,仅换 accent 变量(一眼可分是 macro);
   App.tsx 所有 `appMode === 'prediction'` 的分支逐处审:涉及"进入非 stock 模式"的改为
   `appMode !== 'stock'`,涉及 WC 专属数据的保持原判断
4. ChatArea 欢迎页三分支:prediction → 近期比赛+WC 格;macro → **近期数据(MacroUpcoming)**+macro 格
   - **近期数据**卡片流 = WC"近期比赛"的宏观对应物:按 `macro_board.json` 的
     `next_releases[]` 排序取最近若干期——**不分日/周/月,谁最近谁在前**(本周四初请、下周三 CPI、
     FOMC…混排),每卡:系列名+期+倒计时+fair vs market 迷你条+decision 状态灯,点击跳对应 artifact

### 16.2 复制-改造法(整体复制 WC 前端家族,再清洁成 macro)

**方法(用户定稿):先原样复制、后系统性改造、最后清洁审计**——不是从零写,保证结构/交互/样式
与 WC 完全同构;三步各有验收:

**Step A 整体复制**(一次性,保持可编译):
```
src/components/prediction/  → src/components/macro/   (整目录复制)
  PredictionArtifactGrid.tsx → MacroArtifactGrid.tsx
  PredictionArtifact.tsx     → MacroArtifact.tsx      (母版 ~2000 行注册表+视图集)
  PredictionUpcoming.tsx     → MacroUpcoming.tsx
  MatchCard.tsx              → ReleaseCard.tsx
  usePoll.ts                 → 保留复用(无 WC 语义)
  CountryName / TrajectoryPlayer / AdvanceMode → 复制后**删除**(见 Step B 清单)
src/contexts/PredictionFocusContext → MacroFocusContext(focus 语义:country→series)
server/routes/microfootballAnalyze.ts → macroAnalyze.ts
```

**Step B 改造+删除**(WC 语义 → macro 语义的映射表):
| WC 原件 | macro 化 |
|---|---|
| `PREDICTION_ITEMS`(20 项 wc_*) | `MACRO_ITEMS`:§16 表的 13 项起步,扩到 ~20(补 macro_claims_history、macro_fomc_history、macro_consistency(套利扫描页)、macro_pricetrack 独立页、macro_params(experiment 面板)、macro_venues(API/系列说明)、macro_llm(声明/新闻标注流)) |
| `REGISTRY['wc_*']` 各视图 | 对应改绑 `macro_*`;可复用的通用件(DataTable/KV/Loading/ErrorBox/tab 条/AiResult)**抽到 src/components/shared/** 双方共用,不复制两份 |
| MatchCard(对阵卡) | ReleaseCard(发布卡:系列+期+倒计时+edge 灯) |
| CountryName 跨视图跳转 | SeriesName 跳转(点系列名→弹出"出现在这些视图"同款 popover,含滚动修复) |
| upcoming.json 轮询 | macro_board.json 轮询(usePoll 同参) |
| AdvanceMode(常规/晋级切换) | 删除(macro 无此概念);同类位置留 engine/anchored 式切换的仅是 fed 页的"模型/市场"视角 tab |
| TrajectoryPlayer/microfootball | 删除 |
| i18n `prediction.*` 词条 | 新 namespace `macro.*` 五语言全量新写;**不**复用 prediction 词条 |

**Step C 清洁审计**(验收门,写进 M7):
```
grep -rn "wc_\|worldcup\|World Cup\|tCountry\|countryKey\|match\b" src/components/macro/  → 必须 0 命中(注释含)
grep -rn "prediction\." src/components/macro/ → 只允许 import shared 组件,词条全是 macro.*
两模式互切 10 次往返:localStorage 状态正确、主题正确、无串数据(macro 页不请求 upcoming.json,反之亦然)
```

### 16.3 数据与服务约定

- **数据通道**:`ops/frontend_export.py` 单点写 `public/data/macro_*.json`;运行时 tunnel `/data`
  读取,build 快照兜底;与 WC 文件同目录**前缀隔离**;两条 refresh 管线(WC/macro)互不触碰对方文件
- **AI 分析**:`macroAnalyze.ts` 复刻 nemotron 调用+box A 缓存+串行纪律;按钮触发,绝不自动
- **i18n**:五语言 `macro.*`;**部署**:同一 build:wc+firebase deploy,零新基建
- **家族预留**:Sidebar 的双按钮结构即未来 N 按钮结构(nba/soccer 各加一键),appMode 联合类型
  逐类别扩展——这是 §17 家族约定在前端的落点

### 16.4 Chat 感知 macro(与 prediction 模式完全同构,prompt 路径零改动)

现状(实地核查):chat 前端有 `agentMode: 'cloud'|'local'` 两条 agent 路径;
`server/routes/chat.ts` 接收 `appMode`,经 `detectArtifacts(lastContent, appMode)` 做
**模式限定**的视图识别(prediction 态只认 `wc_*`),再经 `predictionContextForArtifacts()`
把权威数据作为 grounding **追加注入**——基础 system prompt 本体从不被改。macro 照此三件:

1. `detectArtifacts` 加 macro 分支:`appMode==='macro'` → 只识别 `macro_*` 视图关键词
   (中英词表进 macro 侧词典文件,不塞进 WC 词典)
2. 新建 `server/tools/macroMarketTool.ts`:`macroContextForArtifacts(types)` 读
   `public/data/macro_*.json` 拼权威数据块(标题如 "## Macro prediction-market data
   (authoritative)"),**与 predictionMarketTool 平行文件、零共享代码**
3. `chat.ts` 在现有 WC 注入点之后加对称的 additive 分支(macro 态注 macro 块);
   cloud/local 两条 agent 路径同享此注入(注入发生在消息组装层,与 agent 选择正交)

**纯洁性约束(0-bis-3 的执行细则)**:someoagent/非 someoagent 的 coding approach 系统
prompt 文件一字不改;macro 的一切知识都是"消息里多一段数据块",不是新 prompt;
验收 = coding prompt 文件 diff 为空 + stock 态 chat 行为回归测试(问股票问题,答案不带任何
macro/WC 数据块)。

## 17. 平台家族约定(prediction_market_<类别> 的分支标准,macro 定稿)

后续 nba/soccer 直接照此清单开仓,不再重新发明:

1. **目录契约**:config(注册表)/ingest/venues/model/strategy/exec/jobs/ops/research/data/output/docs/tests
2. **五问接口**:每个分支对每个开放合约回答 fair/edge/decision/size/复盘——`decide()` 单入口纯函数
3. **统一抽象**:`Pred/Dist/ladder_probs`(macro 定义)对任何品类通用(NBA spread 阶梯、足球 3-way 皆可表达)
4. **台账契约**:decisions 表 schema(append-only、inputs_json、model_version、gate_snapshot)全家族同构
   ——将来可做家族级汇总账本
5. **导出契约**:`<类别>_*.json` 前缀进同一个 public/data;前端按 `<类别>_` 注册 artifact 家族
6. **调度契约**:launchd `com.someopark.<类别>{tick,watchdog,refresh}.plist` 三件套 + runs/coverage 看门狗
7. **闸门契约**:paper→上线闸门(OOS 赢市场+ROI>0+校准+edge_capture)→real 三级,circuit breaker 标配
8. **文档契约**:docs/{PLAN.md, SERIES_CATALOG.md(或赛程等价物), PLAN_AUDIT.md} + README 职责表
9. **隔离契约**:分支间零 import、独立 db、独立调度;共享仅 conda env、.env keys、前端壳与 pdf_style 排版约定

## 18. 市场结构实测与交易结构设计(2026-07-27 API 逐系列核实)

### 18.1 结构分类(不是猜的,是拉出来的)

| 结构型 | 系列(实测) | 腿形态 |
|---|---|---|
| **T1 累积阈值梯**(主力!) | KXCPI(10腿,`Above X%`,strict >,0.1 网格)、KXPCECORE(5腿)、KXPAYROLLS(13腿,10-30k 间距)、KXU3(14腿,0.1 网格)、KXFED(11腿,25bp)、**KXJOBLESSCLAIMS(9腿,`At least X`,≥,5k 网格)** | 每腿独立 binary,合集构成单调生存函数;腿间**不**互斥、总和≠1 |
| **T2 categorical 互斥腿** | KXFEDDECISION(H25/H26/C25/C26/H0 五腿)、KXCBDECISION*、KXDOTPLOT | 互斥近完备,devig 归一 |
| **T3 单腿 binary** | KXRECSSNBER、KXSAHM、KXFOMCGUIDE、KXAAAGASD(涨/跌) | 单个 yes/no |
| **T4 range 桶**(少数) | 部分 KXGDP/能源月盘出现 between(floor+cap) | 真互斥桶,按 T2 处理 |

**关键语义差异(结算级)**:CPI/U3/PAYROLLS/FED 用 strict `greater`;CLAIMS 用 `greater_or_equal`。
CPI/U3 的 strike 与结算舍入网格重合 → 等号情形概率质量巨大(P(CPI=0.2%)常 25-35%),
`>0.2%` 与 `≥0.2%` 的公允价差就是这坨质量——**registry 每腿必须带 strict 标志,测试断言覆盖**。

### 18.2 对设计的强制约束(已回写 §6/§11)

1. **表示层**:预测的原子输出 = 结算网格点质量 pmf + 生存函数;桶概率是派生物
2. **devig 双路径**:阈值梯 = 逐腿双边 devig + isotonic 单调化(违反单调 = 免费钱,先扫套利再谈模型)
3. **交易的默认形态是"桶价差"不是单腿**:YES(>x)+NO(>y) 合成 (x,y] 桶,把仓位放在我们密度
   最集中的 1-2 格,而非裸买尾部腿——同等 edge 下方差小得多;edge.py 枚举结构而非枚举腿
4. **流动性形态**:консensus 附近 strike 厚、深尾单边或无价——scan 必须容忍单边簿;spread 需两腿同过深度门
5. **免模型收益的顺位提升**:单调性违约扫描零成本、每快照必跑,是 T1 结构送的

## 19. 准确率提升路线(按预期收益排序)

1. **直接建模"舍入后的 print"**:BLS 发布未舍入 CPI 指数(3 位小数)——用未舍入序列建 AR/nowcast,
   最后一步与舍入算子卷积得网格 pmf。0.1 网格上 1 格 = 30pt 概率,舍入边界建模本身就是最大的
   单项准确率来源(市场普遍用连续正态近似,在等号格上系统性错价)
2. **对数池 ensemble**:统计模型 + 市场隐含(isotonic 后的 pmf)+ 公开 nowcast(Cleveland CPI、
   GDPNow、NY Fed)三源,权重 walk-forward 学;分歧大时降杠杆而非硬压——准确率不够时用选择性弥补
3. **校准层**:walk-forward 预测上拟合 isotonic 校准(母版 probability_calibration 同构),
   Kelly 只吃校准后概率——未校准的过自信是 Kelly 的毒药
4. **选择性下注即"有效准确率"**:熵门(分布太平→PASS)、分歧门(|model−market|>0.25→怀疑自己)、
   逐 strike 的历史 edge_capture 记录(哪些格我们真的赢过钱,只在赢过的格加码)
5. **最新鲜输入纪律**:T-1h 定稿前强制刷新 AAA 日度、RB 期货、当周新闻标签;发布前数据每小时
   都在贬值,staleness 门(§8.2)保证不用旧料做新决策
6. **误差归因反哺**:结算后 |z|>2 的 miss 聚类分析(月度)→ 特征追加(假日周、罢工、政府停摆);
   母版"结构修复 only、禁调参"纪律原样适用
7. **跨系列桥**:CPI→PCE(发布日重定价)、claims 4 周均值→NFP、RB→CPI 能源分项——信息在族内
   流动比单系列堆特征便宜
8. **LLM 结构断点层**:统计模型天然错过的离散事件(大罢工/停摆/飓风)由 news_risk_tags 注入
   方差膨胀或均值移位;政府停摆同时触发**发布延期**的运营处理(coverage 状态机加 postponed 态)
9. **时点套利即实现层准确率**:PCE 在 CPI 日后的滞后重定价、YoY 在 MoM 基数已知后的确定性换算、
   发布后 3min 重估窗——模型没变准,但捕获的 edge 变多

## 20. 铁律(写进代码与测试)

1. PIT 或死(§5-bis 全套):双时钟 knowledge_time、单一取数口、`data_horizon≤asof` 在线断言、
   标签用首发 vintage、Chronos 双口协议、test_pit 四件套——任何新特征/新源先过 §5-bis.1 表格归类
2. 结算细则错一次=全系列停:settle_source 非空断言;新系列先 paper 两个 print
3. 手续费前置:一切 edge net-of-fee;回测同
4. 决策过窗只标 MISSED 绝不补跑;快照可 catch-up
5. 流动性现实:size≤depth 20%;edge_capture<0.4 降级
6. nemo 串行+可降级;不 block 决策路径
7. 显著性诚实:样本小就报"不显著",不粉饰
8. 不碰 WC 系统:零 import、独立 db、独立 cron;共享仅 conda env 与 .env keys
9. **VIXForecast/Chronos 只读**:可 import 调用 `run_vix_forecast`、可复刻其调用模式,
   但不改其代码/ckpt/缓存一个字;macro 自己的 Chronos 微调 ckpt 存 macro 自己的目录
10. **模型健康巡检与预测同频**:每天跑,漏跑=MISSED;红灯系列自动降 paper,人工复核才复活
11. **开发边界(0-bis)**:后端只写 `prediction_market_macro/`;前端只按最小接线清单触碰既有
    文件、stock/prediction 视图并存不删;coding approach prompt 一字不改——三条全部有
    grep/diff 级验收
12. **dfm/ 只读 + 反循环**:金融核心只 import 调用、输出落 macro 目录;DFM 合成数据
    只用于风险情景/不确定性量化/压力回测,**永不作为被评估模型的训练数据**
13. **组件先测后用(§7-bis)**:一切新组件 shadow 起步、live-forward 过门才入池、劣化自动退出;
    回测证据只能否决不能放行(Chronos 预训练污染、shadow 缺席场景一律等真样本)
