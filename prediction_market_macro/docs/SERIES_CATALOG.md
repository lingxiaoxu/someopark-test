# Kalshi 宏观市场总目录（SERIES_CATALOG）

生成:2026-07-26 · 来源:`research/discover_series.py` 全量扫描(2,905 个 open 系列、6,943 个 open 事件)
原始数据:`data/output/kalshi_macro_catalog.json`(全部系列含元数据)
本文档 = 人工筛注后的**规律性宏观系列**(每周/每月/每次数据发布必开新事件),即系统化开发的目标清单。
★ = 一线优先(流动性/数据源/建模可行性综合)

## 1. Fed / 利率决议(每次 FOMC,一年 8 次)

| 系列 | 内容 | 结算源 | 备注 |
|---|---|---|---|
| ★ KXFEDDECISION | 决议方向 hike/cut/maintain(5 腿) | Federal Reserve | 13 个 open 事件排到 2027 |
| ★ KXFED | 会后联邦基金利率区间(阶梯) | Fed BoG | 与 DECISION 可互检/套利 |
| KXFEDHIKE | 下次加息时点 | Federal Reserve | |
| KXRATECUT / KXRATECUTCOUNT / KXFEDCHGCOUNT | 年内降息(次数) | Federal Reserve | annual |
| KXDOTPLOT | 点阵图结果 | Fed BoG | SEP 会议(3/6/9/12月) |
| KXFOMCDISSENTCOUNT | 异议票数 | 新闻源 | |
| KXFOMCGUIDE | 声明是否含前瞻指引 | 新闻源 | Warsh 时代新品种 |
| KXEMERCUTS / KXFEDMEET | 紧急降息/紧急会议 | Federal Reserve | 尾部品种 |
| KX2YFOMC / KXDXYFOMC | FOMC 当日 2Y 收益率/美元指数变动 | Treasury/ICE | 事件日波动品种 |
| KXEFFR | EFFR 高于/低于 | NY Fed | |
| KXBALANCESHEET | Fed 资产负债表规模 | Federal Reserve | |
| KXCBDECISION{CANADA,EU,ENGLAND,JAPAN,AUSTRALIA,NZ,KOREA,MEXICO,INDIA,ISRAEL,SA} | 11 家外国央行决议 | 各央行 | 每次议息;BoC 最活跃(3 事件) |

## 2. 通胀(每月 CPI/PCE/PPI 发布日)

| 系列 | 内容 | 结算源 | 备注 |
|---|---|---|---|
| ★ KXCPI / KXCPICORE | CPI MoM 头条/核心(阶梯) | BLS | 各 5 个月份 open |
| ★ KXCPIYOY / KXCPICOREYOY | CPI YoY 头条/核心 | BLS | |
| ★ KXPCECORE | 核心 PCE(Fed 目标口径) | BEA | 6 个月份 open |
| KXCPINDEX | CPI-U 指数点位 | BLS | |
| KXCPICOMBO / KXFEDCOMBO / KXEMPLOYMENTCOMBO | 组合盘(parlay) | BLS 等 | |
| KXCPICOREHEAD | 核心>头条? | | 结构品种 |
| KXUSPPIYOY | PPI YoY | BLS | |
| KXECONSTAT{CPI,CPICORE,CPIYOY,CORECPIYOY,U3} | ECONSTAT 平行系列 | BLS | 与主系列并行,可比价 |
| CPI 分项: KXSHELTERCPI / KXUSGASCPI / KXUSEDCARCPI / KXAIRFARECPI / KXEGGS | 住房/汽油/二手车/机票/鸡蛋 | BLS | 分项建模空间大 |
| KXTRUF{EGGS,GAS,HOUCPI,CCI,PDEBT,TSA,CMC} | Truflation 实时指数系列 | Truflation | 月度;nowcast 对照数据源 |
| 快餐价格: KXBKNUGGETS/KXCFACHICKSAND/KXCHIPBURRITO/KXDDCOLDBREW/KXPOPCHICKSAND/KXSBUXSAR/KXTBCRUNCHWRAP/KXWENBACONATOR/KXCOSTCOHOTDOG/KXAMSAVO(周) | 微观价格 | 商家菜单 | 趣味盘,流动性薄 |
| 国际: KXBRAZILINF / KXARMOMINF / KXSAMOMINF | 巴西/阿根廷/南非通胀 | 各国统计局 | |

## 3. 就业(每月非农周五 + 每周四初请)

| 系列 | 内容 | 结算源 | 备注 |
|---|---|---|---|
| ★ KXPAYROLLS | 非农新增(阶梯) | BLS | 5 个月份 open |
| ★ KXU3 | 失业率 | BLS | 另有 KXECONSTATU3、KXUE(Trading Economics 口径,9 事件) |
| ★ KXJOBLESSCLAIMS | **每周**初请失业金(阶梯) | DoL | 全目录唯一每周宏观硬数据盘 |
| KXADP | ADP 私营就业(月) | ADP | 非农前哨 |
| KXCHCUTS / KXCHAICUTS | Challenger 裁员 / AI 是否第一裁员原因 | Challenger | 月度 |
| KXTEMPHELP | 临时工就业增减 | BLS | 领先指标 |
| KXLFPRATE / KXU3MAX / KXSAHM | 参与率 / 失业率年内峰值 / Sahm 规则触发 | BLS | annual |
| KXTECHLAYOFF / KXLAYOFFSY* | 科技裁员 | FRED/layoffs.fyi | |
| KXBRAZILU | 巴西失业率 | IGBE | |

## 4. GDP / 增长 / 景气(季度发布 + 月度)

| 系列 | 内容 | 结算源 | 备注 |
|---|---|---|---|
| ★ KXGDP | 美国季度 GDP 环比折年(阶梯) | BEA | advance/second/third 各口径 |
| KXGDPYEAR | 年度 GDP | BEA | 5 事件 |
| KXRECSSNBER | NBER 衰退 | NBER/BEA | |
| KXIMFRECESS | IMF 全球衰退 | IMF | |
| KXISMPMI | ISM 制造业 PMI | ISM | 月度 |
| KXUSRETAIL | 美国零售销售 MoM | Census | 月度 |
| KXDEGDP{QOQF,YOYF} / KXFRGDP{QOQP,YOYP} | 德/法 GDP 初值 | Trading Economics | 季度 |
| KXUKRETAIL / KXSARETAIL / KXSATRADEBAL / KXSKEXPYOY | 英/南非零售、南非贸易、韩国出口 | 各国 | 月度 |
| KXTRADEDEFICIT | 美国贸易逆差 | Census | custom |

## 5. 能源(日/周/月/年全频率覆盖)

| 系列 | 内容 | 结算源 | 频率 |
|---|---|---|---|
| ★ KXWTI / KXWTIW / KXWTIH | WTI 当日收盘 / 周区间 / 小时 | ICE·Pyth | daily/weekly/hourly |
| KXWTIMAX / KXWTIMIN / KXWTIDIRY | WTI 年内高/低/方向 | ICE | annual |
| KXBRENTD / KXBRENTW / KXBRENTMON | Brent 日/周/月 | Pyth | |
| ★ KXNATGASD / KXNATGASW / KXNATGASMON | 天然气日/周/月 | Pyth·EIA | |
| KXNGASMAX / KXNGASMIN | 天然气年内高/低 | EIA | annual |
| ★ KXAAAGASD / KXAAAGASW / KXAAAGASM | AAA 全国汽油价 日涨跌/周/月度点位 | AAA | daily/weekly/monthly |
| KXAAAGASMAX/MIN{,CA,TX,FL,NY} | 州级年度油价极值 | AAA | annual |
| KXPOWERKWH | 美国平均电价 | BLS | monthly |
| KXIRANCRUDE / KXBARRELS | 伊朗原油产量 / 石油桶数 | EIA 等 | |

## 6. 利率市场 / 金融(每日滚动)

| 系列 | 内容 | 结算源 | 频率 |
|---|---|---|---|
| ★ KXUST{2,5,7,10,30}A | 各期限美债收益率 | US Treasury | daily |
| KX10Y2Y / KX10Y2YDATE / KXNOTE10Y | 10Y-2Y 利差 / 10Y 年度 | FRED/Treasury | |
| KXEURUSD / KXUSDJPY | 汇率日区间 | ICE | daily |
| KXUSDBRLMAX(M) / KXUSDX | USD/BRL、美元指数 | ICE | 月/年 |
| KXGOLDW / KXSILVERW / KXCOPPERW | 金/银/铜周价格 | — | weekly |
| KXINX* / KXNASDAQ100* | 标普/纳指 日内、日、周、月、年全套 | — | hourly→annual |
| KXFEAR | CNN 恐惧贪婪指数 | CNN | custom |
| KXFM30YMTG / KXMORTGAGERATE | 30Y 房贷利率 | Freddie Mac | one_off(可能季节性重开) |
| KXCREDITRATING / KXCREDITC | 美国主权评级下调 / SOFR 危机 | 评级机构/NY Fed | 尾部 |

## 7. 房地产(每月发布)

| 系列 | 内容 | 结算源 |
|---|---|---|
| KXEHSALES | 成屋销售 | NAR |
| KXHOUSINGSTART / KXBUILDPERMS | 新屋开工 / 营建许可 | Census |
| KXNYCRENTSY | NYC 租金 | StreetEasy(annual) |
| 加拿大房价系列 KX{CAL,EDM,MTL,OTTAWA,TOR,VAN,CAN}HOME 等 | 城市房价 | CREA(annual) |

## 8. 另类高频(每周)

| 系列 | 内容 | 结算源 |
|---|---|---|
| KXTSAW | TSA 安检人数(周) | TSA |
| KXUSFLYCAN | 航班取消数(周) | FlightAware |
| KXAMSAVO | 牛油果价格(周) | USDA |
| KXFEDTWEETS / KXIMFTWEETS / KXWEFTWEETS | 机构推文数(周) | X |

## 开发优先级建议(P0 → P2)

- **P0(数据源强、日历确定、可建模)**: KXFEDDECISION+KXFED(FOMC 反应函数,已有 FRED 管道)、KXCPI/KXCPICORE/KXCPIYOY(CPI nowcast:Cleveland Fed nowcast+能源分项)、KXJOBLESSCLAIMS(唯一周频硬数据,状态空间模型)、KXPAYROLLS/KXU3(就业日)、KXPCECORE(CPI→PCE 桥接模型)
- **P1**: KXAAAGAS 系列(AAA 数据可直接抓,EIA 周报领先)、KXWTIW/KXNATGASW(期货曲线对照)、KXUST 日频(期货隐含)、KXGDP(GDPNow 对照)、KXADP/KXCHCUTS、KXCBDECISION 外国央行(OIS 对照)
- **P2**: CPI 分项、Truflation 系列、KXTSAW/航班、快餐价格、国际数据、组合盘(COMBO 与单腿的定价一致性套利)

## 已知坑(继承 WC 系统经验)

- 公开 API 的 `/markets` 列表接口不回价格(bid/ask=None),**必须逐 ticker 拉 `/orderbook`**(见 7 月 FOMC 核查)
- 元数据里 settlement_sources 有脏数据(很多系列错标为"BLS- Employment S…"),以系列页面为准
- `frequency` 字段不完全可靠:如 KXFEDDECISION 标为 custom(实际每次 FOMC)、KXGDP 标 custom(实际每季)——按事件节奏人工归类,勿直接信 frequency
