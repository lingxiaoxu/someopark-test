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
    ("预测", "model.tournament", "蒙特卡洛冠军概率(48 队)"),
    ("预测", "model.golden_boot", "金靴(进球王)球员概率"),
    ("策略", "strategy.compare", "模型 vs 市场偏离扫描(赛前)"),
    ("策略", "strategy.inplay_arb", "盘中每分钟:套利 / 相对价值 / 战术"),
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
    ("行情/赛事数据", "prediction_market/data/cache.db(SQLite,API-Football 拉一次存中央)"),
    ("密钥", "prediction_market/.env(已 gitignore;Kalshi/PMUS key 在 ~/.config/someopark/)"),
    ("API 预算", "7000 req/月 上限,增量水位避免重复拉"),
]
OUTPUTS = [
    ("tournament.json / golden_boot.json", "冠军 / 金靴概率"),
    ("xv_matches.json", "模型 vs 市场偏离扫描"),
    ("inplay_opportunities.json", "盘中套利/相对价值/战术"),
    ("performance_report.json / .pdf", "收益/准确度报告"),
    ("risk_report.json / .pdf", "风险报告"),
    ("oos_report.json", "样本外校准(Brier 闸门依据)"),
]
OUTPUT_DIR = "prediction_market/data/output/"

VALUE = [
    "一套校准过的赛事概率模型:百万次锦标赛模拟,队伍强度融合世界排名、阵容质量、近期状态、"
    "球员评分,以及对手强度加权的攻防 form(弱旅防线扎实即压低强队期望进球);每场结束后自动重算并重新校准。",
    "概率 + 每合约价格(¢)双口径:每个可成交概率旁同步显示合约价(并区分模型公允价、场馆含 vig 价、"
    "去 vig 后的隐含概率),并对每笔赛前押注做「入场→终场」六里程碑盯市,直观验证赛前判断是否被市场逐步确认。",
    "跨 Kalshi / Polymarket 的实时错价发现(赛前偏离 + 盘中每分钟套利/相对价值/战术)。",
    "一套强制纪律——只在模型达标(校准后 Brier 优于均匀基线)且真有可成交边缘时才动钱,每单硬顶 $1。",
    "怎么看到价值:准确度&盈亏、价格轨迹(¢)、模型 vs 市场、校准、风险等视图,加上两份 PDF"
    "(收益/准确度、风险)即是答案——三处逐场战绩同源对账、永远一致。",
]
