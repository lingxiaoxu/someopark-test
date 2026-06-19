"""Static system catalog — the single source of truth for the "what is this
system" narrative that the PDF reports embed (interfaces, modes, schedule,
inputs/outputs, value). Kept as plain data so both the performance PDF and any
future frontend can render the exact same overview.
"""
from __future__ import annotations

def honest_headline(trade_grade: bool, calibrated_brier: float | None = None,
                    uniform_brier: float = 0.6667) -> str:
    """State-aware headline — reflects the CURRENT calibration-gate verdict, not a
    fixed stance. Passes once the calibrated model beats the uniform baseline."""
    cb = f"{calibrated_brier:.4f}" if calibrated_brier is not None else "—"
    if trade_grade:
        return (
            f"系统状态:已达可交易等级。经概率校准后模型 Brier {cb} ≤ 均匀基线 "
            f"{uniform_brier:.4f},纪律闸门放行。实际下单仍受 $1 硬上限、场所可执行性"
            "与单场正向 edge 三重约束——闸门管「能不能交易」,这三项管「具体下不下、下多少」。"
        )
    return (
        f"系统状态:纪律闸门拦截中。校准后模型 Brier {cb} 仍劣于均匀基线 "
        f"{uniform_brier:.4f},未达可交易等级,系统拒绝下任何真钱单。这是设计目标:"
        "宁可不交易,也不拿没验证过的边缘去亏钱。"
    )


# Backward-compatible default (gate-blocking phrasing); callers should prefer
# honest_headline(state) so the text tracks the live calibration verdict.
HONEST_HEADLINE = honest_headline(False)

# (类别, 命令 python -m prediction_market.<x>, 作用)
INTERFACES = [
    ("数据", "ingest.bootstrap", "一次性拉全量(球队/球员/对阵/赛程),建立增量水位"),
    ("数据", "ingest.refresh", "增量刷新(赛果、比分、live 状态)"),
    ("预测", "model.match_pricing", "单场 3-way 公允价(主/平/客),含点球大战建模"),
    ("预测", "model.tournament", "模拟冠军概率(48 队)"),
    ("预测", "model.golden_boot", "金靴(进球王)球员概率"),
    ("预测", "model.xg_form", "xG-form 评分加成(PIT):球队近期 xG 比比分更低噪,提升预测"),
    ("策略", "strategy.decision_model", "赛前下注决策:价值选边(在可交易边里挑最优)+ 分数凯利 + 置信度定额($0.2–$2);决策阈值 0.02(择时保护)"),
    ("策略", "strategy.smart_exit", "智能择时/超调止盈:市场价超调高于 live 公允价时现金出锁利——实现口径的核心 alpha(39%→67% 盈利)"),
    ("策略", "ops.decision_backtest", "决策模型 PIT 回测(选边/退出时点/CLV 实测)"),
    ("执行", "exec.executor", "每日赛前下单信号:decide() 选边定额 → $1 硬顶 cap → 闸门放行才下单"),
    ("策略", "strategy.compare", "模型 vs 市场偏离扫描(赛前)"),
    ("策略", "strategy.inplay_arb", "盘中每分钟:套利 / 相对价值 / 15 个战术(8 个数据挖掘:闷平爆发/应得未得/无效控球/阵型脆弱/单点失效/晚段进球/庄家交叉验证/临门修正 + 超调止盈)"),
    ("运维", "ops.team_styles_export", "球队风格分型(9 类数据驱动:控球/直接/高压/高效/碾压/高量…)"),
    ("运维", "ops.reach_round_export", "晋级盘(48 队 × 5 轮:小组出线/16/8/4/决赛,模型%/Kalshi¢/Poly¢/边缘)"),
    ("运维", "ops.backfill_price_ticks", "每分钟价格回填(Poly Global prices-history),供细粒度择时研究"),
    ("运维", "ops.schedule", "赛程表(美东 ET + 美西 PT 双时区)"),
    ("运维", "ops.monitor", "健康报告(数据新鲜度/预算/校准/错误率)"),
    ("运维", "ops.performance_report", "收益/准确度报告(本 PDF)"),
    ("运维", "ops.risk_report", "风险报告(配套 PDF)"),
    ("调度", "jobs.hourly_job", "整点任务(刷数据+扫偏离+健康)"),
    ("调度", "jobs.live_poller", "盘中每分钟轮询(内部调 inplay_arb)"),
]

# (模式, 说明)
MODES = [
    ("demo(当前)", "Kalshi demo 环境,假钱 $10,可走完整下单生命周期测试"),
    ("prod(未启用)", "真钱;KALSHI_TRADING_ENABLED + PMUS_TRADING_ENABLED 双闸全关"),
    ("read-only", "Polymarket Global 只读取价,不交易"),
    ("纪律闸门", "模型未达标 → 所有边缘信号被拦(不论环境)"),
    ("$1 硬顶", "任何单 notional ≤ $1.00,代码层 enforce_order_cap() 强制"),
]

# (时机, 跑什么, 频率)
SCHEDULE = [
    ("每天一次(赛前)", "ingest.refresh → model.tournament → strategy.compare", "1×/天"),
    ("整点", "jobs.hourly_job", "每小时"),
    ("比赛进行中", "jobs.live_poller(→ inplay_arb)", "每分钟"),
    ("随时查看", "ops.schedule / performance_report / risk_report", "按需"),
]

# (项, 位置/说明)
INPUTS = [
    ("行情/赛事数据", "prediction_market/data/wc.db(SQLite 唯一真相源;API-Football 拉一次存中央,含 xG/阵型/逐球员/每分钟价格)"),
    ("密钥", "prediction_market/.env(已 gitignore;Kalshi/PMUS key 在 ~/.config/someopark/)"),
    ("API 预算", "API-Football Pro:7500 req/天(UTC 重置);增量水位避免重复拉"),
]
OUTPUTS = [
    ("tournament.json / golden_boot.json", "冠军 / 金靴概率"),
    ("reach_round.json", "晋级盘:48 队 × 5 轮,模型%/Kalshi¢/Poly¢/边缘"),
    ("upcoming.json", "近期比赛卡片:决策选边 + 双口径计划(入场 + 计划智能择时离场)"),
    ("xv_matches.json", "模型 vs 市场偏离扫描"),
    ("inplay_live.json / inplay_opportunities.json", "盘中实时模型 + 15 个战术 + totals 盘口 + 套利/相对价值"),
    ("match_signals.json", "每日赛前下单决策(decide() 选边定额,$1 硬顶)"),
    ("milestone_marks.json", "价格轨迹 + 智能择时现金出(实现口径:逐场买/卖/实现盈亏)"),
    ("team_styles.json", "球队风格分型(9 类)"),
    ("performance_report.json / .pdf", "收益/准确度报告:实现(决策+择时)/ 持有 / argmax 三口径"),
    ("risk_report.json / .pdf", "风险报告"),
    ("oos_report.json", "样本外校准(Brier 闸门依据)"),
]
OUTPUT_DIR = "prediction_market/data/output/"

VALUE = [
    "一套校准过的赛事概率模型:百万次锦标赛模拟,队伍强度融合世界排名、阵容质量、近期状态、"
    "球员评分,以及对手强度加权的攻防 form(弱旅防线扎实即压低强队期望进球);每场结束后自动重算并重新校准。",
    "概率 + 每合约价格(¢)双口径:每个可成交概率旁同步显示合约价(并区分模型公允价、场馆含 vig 价、"
    "去 vig 后的隐含概率),并对每笔赛前押注做「入场→终场」六里程碑盯市,直观验证赛前判断是否被市场逐步确认。",
    "一套赛前下注决策模型(非「押最可能」而是「押最被低估」):在能过各自门槛的边里挑最优、分数凯利(¼)定量、"
    "置信度($0.2–$2)缩放,对冷门高估与平局 regime 设更高门槛;全程点对点(PIT)、无未来泄漏,真实下单仍受 $1 硬顶。",
    "真实策略不是持有到终场,而是「决策 + 智能择时现金出」:开赛后市场对进球/事件过度反应、价格冲高于 live 公允价时就卖出锁利。"
    "PIT 验证把同一批注从 7W-11L/+114¢(持有)提升到 12-15 注盈利/+486¢(实现口径)。三视图与 PDF 都按「实现 / 持有 / argmax」三口径并列。",
    "跨 Kalshi / Polymarket 的实时错价发现(赛前偏离 + 盘中每分钟 15 个战术,含 8 个数据挖掘战术 + 超调止盈 + totals 总进球盘)。",
    "晋级盘(48 队 × 5 轮)+ 球队风格分型(9 类)+ xG-form / 东道主先验 / 平局纪律等数据驱动的预测增强。",
    "一套强制纪律——只在模型达标(校准后 Brier 优于均匀基线)且真有可成交边缘时才动钱,每单硬顶 $1。",
    "怎么看到价值:准确度&盈亏、价格轨迹(¢)、模型 vs 市场、校准、风险等视图,加上两份 PDF"
    "(收益/准确度、风险)即是答案——三处逐场战绩同源对账、永远一致。",
]
