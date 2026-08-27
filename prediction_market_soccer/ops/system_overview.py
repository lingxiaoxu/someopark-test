"""Static system catalog — the single source of truth for the "what is this system"
narrative that the PDF reports embed (interfaces, modes, schedule, inputs/outputs,
value). Kept as plain data so both the performance PDF and the frontend overview card
render the exact same overview.

Club edition. Every row below describes THIS module — twelve club competitions priced
per-league off the League Registry — and was checked against the live tree and the real
exports in data/output/ rather than carried over from the World Cup fork. Volatile
numbers (Brier, sample size, gate verdict) stay OUT of these static tables and are
passed in at render time via ``honest_headline`` / ``headline_i18n``; only structural
counts that change when the system changes (12 competitions, 399 clubs, 10 styles) are
written down here.
"""
from __future__ import annotations

def honest_headline(trade_grade: bool, calibrated_brier: float | None = None,
                    uniform_brier: float = 0.6667, n: int | None = None) -> str:
    """State-aware headline — reflects the CURRENT calibration-gate verdict, not a
    fixed stance. Passes once the calibrated model beats the uniform baseline.

    Returns the Chinese string for callers that want a ready-made line, and
    ``headline_i18n()`` returns the {key, args} form the 5-language frontend
    renders — the headline used to be Chinese-only, which showed Chinese text to
    every English, Japanese, Spanish and French reader of the overview card."""
    cb = f"{calibrated_brier:.4f}" if calibrated_brier is not None else "—"
    ns = f",n={n}" if n is not None else ""
    if trade_grade:
        return (
            f"系统状态:池化校准闸门已放行。校准后模型 Brier {cb} ≤ 均匀基线 "
            f"{uniform_brier:.4f}{ns}。但放行不等于开火:系统仍是纸面模式,双下单开关默认关闭、"
            "单笔 $1 硬上限,且每项赛事要自攒 30 场结算才用自己的校准器、逐项放行(§3.5)。"
            "闸门管「能不能交易」,这三项管「具体下不下、下多少」。"
        )
    return (
        f"系统状态:纪律闸门拦截中。校准后模型 Brier {cb} 仍劣于均匀基线 "
        f"{uniform_brier:.4f}{ns},未达可交易等级,系统拒绝下任何真钱单。这是设计目标:"
        "宁可不交易,也不拿没验证过的边缘去亏钱。"
    )


def headline_i18n(trade_grade: bool, calibrated_brier: float | None = None,
                  uniform_brier: float = 0.6667, n: int | None = None) -> dict:
    """{key, args} for the same headline, resolved by the frontend in its own language."""
    # Two distinct closed states, and the reader deserves to know which one:
    # "cold start" means we have not seen enough settled matches to judge the model,
    # while "blocked" means we HAVE judged it and it did not clear the bar.
    from prediction_market_soccer.model.probability_calibration import PER_LEAGUE_MIN_N
    if trade_grade:
        key = "overview.gateOpen"
    else:
        key = ("overview.gateColdStart" if (n or 0) < PER_LEAGUE_MIN_N
               else "overview.gateBlocked")
    return {
        "key": key,
        "args": {
            "brier": (f"{calibrated_brier:.4f}" if calibrated_brier is not None else "—"),
            "uniform": f"{uniform_brier:.4f}",
            "n": n if n is not None else 0,
        },
    }


# Backward-compatible default (gate-blocking phrasing); callers should prefer
# honest_headline(state) so the text tracks the live calibration verdict.
HONEST_HEADLINE = honest_headline(False)

# (类别, 命令 python -m prediction_market_soccer.<x>, 作用)
INTERFACES = [
    ("数据", "ingest.soccer_ingest", "12 项赛事的赛程/赛果/阵容/赔率增量摄入;每项赛事各一条水位线,TTL 内跳过不重复拉"),
    ("数据", "ingest.club_prior", "俱乐部先验三锚:上季积分表 + ClubElo + 市场盘(C3)"),
    ("数据", "ingest.fc_ingest", "EA FC 26 球员评分,俱乐部轴(team + leagueName 列);playStyles 标签自动生成风格先验(C6)"),
    ("数据", "ops.bootstrap_aliases", "从 Kalshi 在售事件回填俱乐部别名层(场所队码 ↔ club_id,每项赛事一份 JSON)"),
    ("预测", "model.run_model", "全模型编排:逐赛事 先验 → 实时强度(阵容/状态/xG 融合)→ 赛季蒙卡 → 落库"),
    ("预测", "model.match_pricing", "单场 3-way 公允价(主/平/客):Dixon-Coles 比分矩阵 + per-league home_adv(C2,每场按真实主客计)"),
    ("预测", "model.league_season", "联赛赛季蒙卡(C4):真实剩余赛程 + per-league tie-break + 数学锁定 → 冠军/前 N/降级概率"),
    ("预测", "model.ucl_phase", "欧战与南美杯:瑞士制 36 队联赛阶段 + 淘汰树冠军模拟"),
    ("预测", "model.dixon_coles", "两回合 tie 晋级概率(C5):合计携带、无客场进球、UEFA 加时→点球 / CONMEBOL 直接点球"),
    ("预测", "model.inplay", "盘中实时 3-way:比分/红牌/剩余时间逐分钟重定价"),
    ("预测", "model.inplay_advance", "盘中 2-way 晋级概率:常规→加时→点球逐分钟更新,两回合注入首回合合计 agg"),
    ("预测", "model.top_scorer", "每项赛事射手王(进球王)球员概率"),
    ("预测", "model.xg_form", "xG-form 评分加成(PIT):球队近期 xG 比比分更低噪,提升预测"),
    ("策略", "strategy.decision_model", "赛前下注决策:价值选边(在可交易边里挑最优)+ 分数凯利 + 置信度定额($0.2–$2)"),
    ("策略", "strategy.smart_exit", "智能择时/超调止盈:市场价超调高于 live 公允价时现金出锁利——实现口径的核心 alpha"),
    ("策略", "strategy.xv_monitor", "模型 vs 市场偏离扫描:单场盘 + 赛季冠军盘两个产品"),
    ("策略", "strategy.inplay_arb", "盘中每分钟:套利 / 相对价值 / 战术信号(按 intent 分区:持仓管理 / 新入场 / 事件)"),
    ("策略", "strategy.inplay_arb_advance", "盘中 2-way 晋级盘战术版(删平局类、重标定阈值)+ 2 态对冲;与三向盘并行"),
    ("策略", "ops.decision_backtest", "决策模型 PIT 回测(选边 / 离场时点 / CLV 实测)"),
    ("执行", "exec.executor", "下单信号:decide() 选边定额 → $1 硬顶 cap → 该赛事闸门放行才下单"),
    ("运维", "ops.refresh_all --ingest", "一条命令跑完摄入 → 模拟 → 全部导出;flock 单实例锁,每步独立守卫(一步超预算不饿死后续步)"),
    ("运维", "ops.season_odds_export", "赛季盘:12 项赛事 × 冠军/前 N/降级/晋级 家族,模型% vs Kalshi¢ vs 边缘"),
    ("运维", "ops.upcoming_export", "赛前比赛卡:决策选边 + 双口径计划 + caps(有无 advance / 两回合 agg / 加时·点球规则)"),
    ("运维", "ops.schedule_export", "滚动赛程窗口(前 7 天 + 未来);12 项赛事约 3,300 场/季,不整季导出"),
    ("运维", "ops.schedule", "赛程表 CLI:桌面 ET + 当地开球时间(欧洲 CET / 南美 UTC-3)"),
    ("运维", "ops.team_styles_export", "球队风格分型(399 队 × 10 风格矩阵,每队 1-2 风格;FC26 playStyles 先验 + live 指标混合)"),
    ("运维", "ops.milestone_export", "价格轨迹 + 智能择时现金出(实现口径:逐场买/卖/实现盈亏)"),
    ("运维", "ops.backfill_price_ticks", "每分钟价格回填,供细粒度择时研究(晋级盘走 _advance 版,独立表)"),
    ("运维", "ops.calibrate_fit", "概率校准拟合:池化一份 + 每项赛事各一份;n≥30 才切自己的校准器(§3.5)"),
    ("运维", "ops.param_select_club", "每联赛、按时间切分的参数选择(而非一次全局 sweep 定 12 项赛事)"),
    ("运维", "ops.monitor", "健康报告(数据新鲜度 / API 预算 / 校准 / 错误率)"),
    ("运维", "ops.performance_report", "收益/准确度报告(本 PDF)"),
    ("运维", "ops.risk_report", "风险报告(配套 PDF)"),
    ("调度", "ops/refresh_and_deploy.sh", "每日全管线:摄入 → 模拟 → 全部导出 → 前端 build → Firebase 部署"),
    ("调度", "ops.match_trigger", "廉价闸门:有新结算才触发全管线,窗口外一次 DB 查询秒退、零 API"),
    ("调度", "ops.live_refresh", "比赛窗口内 live 摄入 + 盘中/赛前/里程碑导出(窗口外秒退)"),
]

# (模式, 说明)
MODES = [
    ("paper(当前)", "纸面模式:全链路跑通、逐笔记账,但不下真钱单;Kalshi demo 环境可走完整下单生命周期测试"),
    ("prod(未启用)", "真钱;KALSHI_TRADING_ENABLED + PMUS_TRADING_ENABLED 双闸默认 false,order 层拒绝提交"),
    ("read-only", "Polymarket Global 只读取价(美国地理封锁);venue_guard 对非可执行场所硬拦下单"),
    ("纪律闸门(逐赛事)", "每项赛事自攒 30 场结算才用自己的校准器并放行;不足则用池化校准定价、该赛事闸门保持关闭(§3.5)"),
    ("$1 硬顶", "任何单 notional ≤ $1.00,代码层强制;置信度只在 $0.2–$2 envelope 内缩放后再 cap"),
    ("并发与预算", "全部管线 fcntl.flock 单实例锁;API-Football 三档调速(>3500 减 players、>5000 减 odds,自限 6,500/7,500)"),
]

# (时机, 跑什么, 频率)
SCHEDULE = [
    ("每日 07:30 ET", "ops/refresh_and_deploy.sh(launchd com.someopark.soccerrefresh)", "1×/天"),
    ("每 15 分钟", "ops/refresh_and_deploy.sh --trigger → ops.match_trigger 判有无新结算", "96×/天(多数秒退)"),
    ("比赛窗口内", "ops/live_refresh.sh(com.someopark.soccerlive)→ 盘中 + 赛前 + 里程碑导出", "每 60s"),
    ("随时查看", "ops.schedule / performance_report / risk_report / monitor", "按需"),
]

# (项, 位置/说明)
INPUTS = [
    ("赛事注册表", "config/leagues.py(12 项赛事单一标准、不分级):stage_of/caps_for 判赛制形态与市场能力;"
                   "加一个联赛 = 加一条 registry 记录 + 一份别名 JSON,零代码改动"),
    ("行情/赛事数据", "prediction_market_soccer/data/soccer.db(SQLite 唯一真相源;399 家俱乐部、逐赛季对阵表,"
                      "含 xG/阵容/逐球员/赔率/里程碑价格)"),
    ("密钥", "prediction_market_soccer/.env(已 gitignore;Kalshi/PMUS key 在 ~/.config/someopark/)"),
    ("API 预算", "API-Football Pro:7500 req/天(UTC 重置);每项赛事各一条水位线,增量摄入避免重复拉"),
]
OUTPUTS = [
    ("soccer_model.json", "全模型快照:12 项赛事的队伍强度 + 赛季概率(前端主数据源)"),
    ("season_odds.json", "赛季盘:每项赛事的 冠军 / 前 N / 降级 / 晋级 家族,模型% vs Kalshi¢ vs 边缘"),
    ("upcoming.json", "赛前比赛卡:决策选边 + 双口径计划(入场 + 计划智能择时离场)+ caps"),
    ("schedule.json", "滚动赛程窗口(前 7 天 + 未来),12 项赛事合并按开球排序"),
    ("xv_matches.json / xv_champion.json", "模型 vs 市场偏离:单场盘 + 赛季冠军盘"),
    ("inplay_live.json / inplay_live_advance.json", "盘中实时:3-way 模型 + 战术 + 套利/相对价值;2-way 晋级盘并行独立"),
    ("match_signals.json", "每日赛前下单决策(decide() 选边定额,$1 硬顶)"),
    ("milestone_marks.json", "价格轨迹 + 智能择时现金出(实现口径:逐场买/卖/实现盈亏)"),
    ("team_styles.json", "球队风格分型(399 队 × 10 风格矩阵,每队 1-2 风格)"),
    # No angle brackets in any cell: reportlab parses these strings as mini-XML, so a bare
    # "<comp>" placeholder would raise instead of printing (ratings_epl.json … one per comp).
    ("form.json / squad.json / ratings_{赛事}.json", "近期状态、阵容强度、每项赛事队伍评分表(12 份)"),
    ("calibration.json / oos_report.json", "概率校准(池化 + 逐赛事)与样本外指标——交易闸门的依据"),
    ("backtest.json", "近 60 天 PIT 回测(横跨 12 项赛事)"),
    ("performance_report.json / .pdf", "收益/准确度报告:实现(决策+择时)/ 持有 / argmax 三口径"),
    ("risk_report.json / .pdf", "风险报告"),
    ("frontend_overview.json", "前端总览卡数据契约(本页五张表的机读版)"),
]
OUTPUT_DIR = "prediction_market_soccer/data/output/"

VALUE = [
    "一套覆盖 12 项俱乐部赛事的校准概率模型,单一标准、不分级:五大联赛 + 欧冠/欧联/欧协联 + "
    "解放者杯/南美杯/巴甲/阿甲。每项赛事的形态(联赛轮 / 两回合淘汰 / 单场淘汰)与市场能力都由 "
    "League Registry 的 caps 判定,前后端零赛制特判——加一个联赛只需加一条 registry 记录。",
    "俱乐部化的强度与主场优势:队伍强度融合上季积分表、ClubElo、市场盘三锚 + 阵容质量、近期状态、"
    "对手强度加权的攻防 form;主场优势是 per-league 拟合、按每场真实主客计,"
    "每场结束后自动重算并重新校准。",
    "赛季级产品,不只是单场:联赛赛季蒙卡按真实剩余赛程 + per-league tie-break + 数学锁定给出 "
    "冠军/前 N/降级概率;欧战走瑞士制联赛阶段 + 淘汰树;全部与 Kalshi 赛季盘逐档对价。",
    "概率 + 每合约价格(¢)双口径:每个可成交概率旁同步显示合约价(区分模型公允价、场馆含 vig 价、"
    "去 vig 后的隐含概率),并对每笔赛前押注做「入场→终场」里程碑盯市,直观验证赛前判断是否被市场逐步确认。",
    "一套赛前下注决策模型(非「押最可能」而是「押最被低估」):在能过各自门槛的边里挑最优、分数凯利定量、"
    "置信度($0.2–$2)缩放,对冷门高估与平局 regime 设更高门槛;全程点对点(PIT)、无未来泄漏,"
    "真实下单仍受 $1 硬顶。",
    "真实策略不是持有到终场,而是「决策 + 智能择时现金出」:开赛后市场对进球/事件过度反应、价格冲高于 "
    "live 公允价时就卖出锁利。三视图与 PDF 都按「实现 / 持有 / argmax」三口径并列,同源对账、永远一致。",
    "跨 Kalshi / Polymarket 的实时错价发现:赛前偏离扫描 + 盘中每分钟战术信号(含超调止盈与总进球盘),"
    "两回合淘汰赛另有独立的 2-way「谁晋级」链路——带首回合合计 agg 的实时晋级概率、晋级版战术、2 态对冲、"
    "按晋级方结算的智能择时,与 90 分钟三向盘完全并行,互不影响。",
    "399 家俱乐部 × 10 风格的分型矩阵(FC26 playStyles 先验 + live 指标混合)、xG-form、per-league "
    "参数选择等数据驱动的预测增强,全部按赛事分别拟合,而不是一套全局常数套 12 项赛事。",
    "一套强制纪律——逐赛事放行:一项赛事要自攒 30 场结算、其校准后 Brier 优于均匀基线,才允许它产生交易信号;"
    "不够就用池化校准定价、闸门关着。每单硬顶 $1,双下单开关默认关闭。",
    "怎么看到价值:赛季盘、积分榜、比赛定价、今日预测、赛程、滚球、模型说明七个前端视图,"
    "加上两份 PDF(收益/准确度、风险)即是答案——三处逐场战绩同源对账、永远一致。",
]
