<p align="center">
  <img src="../public/SOMEO PARK矢量源文件 Big Square.svg" alt="Someopark" width="160"/>
</p>

<h1 align="center">prediction_market_soccer</h1>
<p align="center"><b>欧洲/南美俱乐部足球预测市场量化系统 · Kalshi + Polymarket · 12 项赛事</b></p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11-blue?logo=python&logoColor=white"/>
  <img src="https://img.shields.io/badge/conda-someopark__run-green?logo=anaconda&logoColor=white"/>
  <img src="https://img.shields.io/badge/venues-Kalshi%20%7C%20Polymarket-orange"/>
  <img src="https://img.shields.io/badge/comps-12%20%2F%20single%20standard-teal"/>
  <img src="https://img.shields.io/badge/model-Dixon--Coles%20%7C%20Season%20MC-purple"/>
  <img src="https://img.shields.io/badge/mode-paper%20only-red"/>
</p>

---

本模块是世界杯系统(`../prediction_market/`)的**俱乐部足球 fork**:2026-08-26 由整目录复制(tag `wc-baseline-20260826` 为回滚锚点)改造而来。设计蓝图与逐文件处置表见 [`TRANSFORM_PLAN.md`](TRANSFORM_PLAN.md)(单一真值,含四轮批判性核验记录)。

> **隔离铁律**(与世界杯模块同款):完全自包含于 `prediction_market_soccer/` —— 读自己的
> `.env`、写自己的 `data/soccer.db`、前端只写 `public/data/soccer/` 命名空间;
> **绝不改动世界杯模块的任何文件/数据/进程**。
>
> **⚠ 当前状态:纸面(paper)模式 + 冷启动观察期。** 双下单开关硬默认 false、单笔 $1 硬上限;
> 校准闸门每联赛攒 30 场结算后才逐联赛放行(§3.5)。

## 覆盖范围(12 项赛事,单一标准,不分级)

| 组 | 赛事(API-Football id / Kalshi GAME 系列) |
|---|---|
| 五大联赛 | 英超 39/KXEPLGAME · 西甲 140/KXLALIGAGAME · 意甲 135/KXSERIEAGAME · 德甲 78/KXBUNDESLIGAGAME · 法甲 61/KXLIGUE1GAME |
| 欧战 | 欧冠 2/KXUCLGAME · 欧联 3/KXUELGAME · 欧协联 848/KXUECLGAME |
| 南美 | 解放者杯 13 · 南美杯 11 · 巴甲 71/KXBRASILEIROGAME · 阿甲 128/KXARGPREMDIVGAME |

全部配置集中在 **`config/leagues.py`(League Registry)**:加一个联赛 = 加一条 registry 记录 + 一份别名 JSON。每场比赛由 `stage_of()/caps_for()` 判定形态(league / cup_two_leg / cup_single)与市场能力(有无 advance、两回合 `agg`、ET/点球规则)——前后端都只认 `caps`,零字符串猜测。

## 与世界杯模块的核心差异(C1-C6)

| # | 差异 | 实现 |
|---|---|---|
| C1 | 淘汰赛判定 | `"group" not in round` 子串判断 → registry `stage_of/caps`(§3.0 能力矩阵) |
| C2 | 主场优势 | 东道主专属 → **每场真实主客**;per-league `base_mu/home_adv` 由上季结果拟合(`ops/fit_league_params`,EPL ha=0.221) |
| C3 | 先验 | 48 队 FIFA rank → 俱乐部三锚(上季积分表 + ClubElo + 市场盘,`ingest/club_prior.py`) |
| C4 | 赛季结构 | 锦标赛 MC → **联赛赛季 MC**(`model/league_season.py`:真实赛程 + per-league tie-break + 数学锁定)+ KO 树(`model/ucl_phase.py`) |
| C5 | 两回合 tie | `dixon_coles.two_leg_advance_prob / tie_advance_prob`(合计携带、无客场进球、UEFA ET→点球 / CONMEBOL 直接点球);实时晋级带 `agg` 注入 |
| C6 | FC26 天赋 | 国籍轴 → 俱乐部轴(`team`+`leagueName` 列,3,414 人/129 队);风格先验由 `playStyles` 标签自动生成(`ops/team_styles_export`) |

## 运行

```bash
cd /Users/xuling/code/someopark-test && \
  set -a && source .env && source prediction_market_soccer/.env && set +a && \
  conda run -n someopark_run --no-capture-output \
  python -m prediction_market_soccer.ops.refresh_all --ingest
```

| 入口 | 频率 | 干什么 |
|---|---|---|
| `ops/refresh_and_deploy.sh` | 每日 07:30 ET(`com.someopark.soccerrefresh`) | 摄入→模拟→全部导出→前端 build→Firebase deploy |
| `ops/refresh_and_deploy.sh --trigger` | 每 15 分钟(`com.someopark.soccertrigger`) | 有新结算才跑全管线 |
| `ops/live_refresh.sh` | 每 60s(`com.someopark.soccerlive`) | 比赛窗口内:live 摄入→in-play/upcoming/里程碑导出(窗口外 1 次 DB 查询秒退) |

三个 launchd 任务全部新 label,与世界杯三件套(空转中)互不相干;全部管线带 `fcntl.flock` 单实例锁;API 预算三档调速器(§6.1:>3500 减 players、>5000 减 odds,自限 6,500/7,500)。

## 前端

appMode `'soccer'`(照 macro 模式先例挂载):`src/components/soccer/` 七视图(赛季盘/积分榜/比赛定价/今日预测/赛程/滚球/模型说明),数据 `public/data/soccer/*.json`,`npm run sync:soccer` 同步。advance 开关/两回合徽章/签表卡显隐全部由后端 `caps` 驱动,前端零赛制特判。

## 诚实披露(v1)

- 西甲/意甲 H2H tie-break 以 GD 近似(R4);ClubElo 源当前不可达 → 先验以表锚运行;
- in-play 17 条战术常数沿用世界杯 n=26 标定,攒 100+ 俱乐部场次后重估(R5);
- 阿甲季后赛规则按"直接点球"保守缺省(R11);巴甲无 FC26 授权 → 天赋锚以 API 赛季评分兜底(§3.8-e);
- 冠军概率现阶段先验主导(市场锚 Phase 3b 接入),与 Kalshi 盘的分歧如实展示在 season_odds/xv_champion。

<p align="center"><sub>Someo Park Investment Management · 研究用途 · 本页任何数字都不是投资建议</sub></p>
