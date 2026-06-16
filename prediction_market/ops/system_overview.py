"""Static system catalog — the single source of truth for the "what is this
system" narrative that the PDF reports embed (interfaces, modes, schedule,
inputs/outputs, value). Kept as plain data so both the performance PDF and any
future frontend can render the exact same overview.
"""
from __future__ import annotations

HONEST_HEADLINE = (
    "诚实结论:系统现在是「只看不买」状态。不是没做好执行,而是纪律闸门"
    "(calibration gate)在主动拦截——模型在已结算的小组赛上 Brier 仍劣于均匀基线"
    "(0.667),尚未达到可交易等级,所以系统拒绝下任何真钱单。这正是设计目标:"
    "宁可不交易,也不拿没验证过的边缘去亏钱。"
)

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
    "一个校准过的赛事概率模型(已修巴西高估:France 18.5% / Brazil 11.2%)。",
    "跨 Kalshi / Polymarket 的实时错价发现(赛前偏离 + 盘中每分钟套利)。",
    "一套强制纪律——只在真有边缘且模型达标时才动钱,且每单硬顶 $1。",
    "怎么看到价值:performance_report(准确度/校准 P&L)+ risk_report(闸门/敞口/预算)两份报告即是答案。",
]
