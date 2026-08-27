"""model/registry.py — model registration & versioning (PLAN §9.2).

models(series, version, params_json, trained_through, created_ts, card_md): every
version's params are kept forever; decisions record model_version, so any historical
decision replays against the exact registered spec. Refits are walk-forward only
(trained_through monotone increasing — enforced on register).

`ensure_registered(conn)` seeds/refreshes the CURRENT code versions with their model
cards; the refresh pipeline calls it daily (idempotent — INSERT OR IGNORE keyed on
(series, version), so a card only changes when the semver bumps).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone


@dataclass(frozen=True)
class ModelSpec:
    series: str
    name: str
    version: str               # "cpi/0.1.0" semver
    params: dict
    feature_set: list[str]
    trained_through: date
    frozen: bool
    card_md: str


def register(conn, spec: ModelSpec) -> bool:
    prev = conn.execute(
        "SELECT MAX(trained_through) m FROM models WHERE series=?", (spec.series,)
    ).fetchone()
    if prev["m"] is not None and spec.trained_through.isoformat() < prev["m"]:
        raise ValueError(f"walk-forward violation: {spec.series} trained_through"
                         f" {spec.trained_through} < registered {prev['m']}")
    cur = conn.execute(
        "INSERT OR IGNORE INTO models(series, version, params_json, trained_through,"
        " created_ts, card_md) VALUES(?,?,?,?,?,?)",
        (spec.series, spec.version,
         json.dumps({"params": spec.params, "feature_set": spec.feature_set,
                     "frozen": spec.frozen}, ensure_ascii=False),
         spec.trained_through.isoformat(),
         datetime.now(timezone.utc).isoformat(), spec.card_md))
    return cur.rowcount > 0


# ── current code versions + model cards (§9.2: 特征表/样本窗/已知失效场景) ────
_TT = date(2026, 7, 27)     # stats models are parameter-light; window = data at build

_CARDS: list[ModelSpec] = [
    ModelSpec("KXJOBLESSCLAIMS", "claims", "claims/0.2.0",
              {"level_weights": [0.4, 0.3, 0.2, 0.1], "sigma_floor": 0.02,
               "seasonal_clip": 0.25, "seasonal_years": 10,
               "seasonal_estimator": "mad_screen:10"},
              ["ICSA first prints (PIT)", "ISO-week seasonal dev (screened)",
               "26w robust vol"],
              _TT, True,
              "**claims/0.2.0** — log-level weighted mean + PIT ISO-week seasonal +"
              " MAD vol。0.2.0(#197 / PR-11)把季节中心从 10 年**均值**换成**离群筛过**"
              "的均值:2020 年 3-4 月 ICSA 从 23 万跳到 690 万,log 偏差 +2.66/+2.69,"
              "污染 ISO 周 12/13/14/15/18;`seasonal_clip` 挡不住它——clip 是**残留伤害"
              "的大小**,不是防线。筛阈 k=10 由全样本 MAD 间隙推出(非 COVID 最大偏差"
              " 7.64 MAD、COVID 衰减尾 7.8-10.1、干净间隙在 16.9 以上),**不是扫参选的**"
              "——扫参最优是 k=6。\n已知失效: 突发大规模裁员冲击(模型无新闻项)、"
              "政府停摆周、假日周历法漂移(Thanksgiving/July-4 retooling 依赖 10y 样本)。\n"
              "OOS 复盘(45 期 replay,0.1.0 → 0.2.0): Brier 0.1523 → 0.1295、"
              "sd(z) 1.379 → 1.106、10/90 尾外 33.3% → 28.9%,区间 LL +0.310 nats/事件。"
              "**仍输给市场**(同窗市场 0.090-0.097),保持 paper;改进路径=市场先验对数池。"),
    ModelSpec("KXCPI", "cpi", "cpi/0.1.0",
              {"gasoline_passthrough": True, "horizon_widen_per_month": 0.10},
              ["CPIAUCSL unrounded index (PIT)", "GASREGW passthrough"],
              _TT, True,
              "**cpi/0.1.0** — 未取整指数 MoM 模型 + 汽油传导;YoY = 精确变量代换"
              "(链式跨未印月份)。\n已知失效: OER/shelter 拐点、关税冲击月、"
              "远月(>2 期)链式误差累积(σ 每月 +10% 补偿)。"),
    ModelSpec("KXCPICORE", "cpi", "cpi/0.1.0", {}, ["同 KXCPI, core 指数"], _TT, True,
              "同 cpi/0.1.0,CPILFESL。失效场景同 KXCPI(能源项不适用)。"),
    ModelSpec("KXCPIYOY", "cpi", "cpi/0.1.0", {}, ["同 KXCPI, YoY 代换"], _TT, True,
              "同 cpi/0.1.0 YoY 通道;基月缺印时链式 MoM,误差随距离放大。"),
    ModelSpec("KXCPICOREYOY", "cpi", "cpi/0.1.0", {}, ["同 KXCPICORE, YoY"], _TT, True,
              "同 cpi/0.1.0 core YoY 通道。"),
    ModelSpec("KXPCECORE", "pce", "pce/0.1.0",
              {"bridge": "CPI-core regression"},
              ["CPILFESL→PCEPILFE bridge (PIT)", "回归残差 σ"], _TT, True,
              "**pce/0.1.0** — CPI-core 桥回归。已知失效: CPI/PCE 权重月度背离"
              "(医疗/金融服务权重差)、年度基准修订月。"),
    ModelSpec("KXPAYROLLS", "payrolls", "payrolls/0.2.0",
              {"tail_mix": True, "sigma_window": 24, "sigma_mult": 1.0, "tail_mult": 2.55},
              ["PAYEMS printed-change 重建 (vintages)", "claims 信号",
               "自身残差滚动 robust scale", "肥尾混合"],
              _TT, True,
              "**payrolls/0.2.0** — 首印变动重建 + claims 信号 + 肥尾混合;"
              "σ 由**模型自身近 24 个月残差**的 1.4826·MAD 定标,不再是常数。\n"
              "为什么改: 0.1.0 的 0.8·N(0,55k)+0.2·N(0,140k)(sd 79,624)蕴含 "
              "P(|e|>1sd)=23.2%,2010-2026 剔 2020 实测 **41.3%**;常数 σ 的 MLE 重拟合 "
              "sd=137,443,按锚聚类自助 90% 区间 [120,396, 160,772] —— 线上宽度在区间之外。"
              "且宽度非平稳(74k/65k/149k/94k 四个时代),常数必错。\n"
              "视野: **σ 不随期数变宽,这是实测结论不是遗漏** —— h=1..6 的 robust sd 比值 "
              "1.00/0.96/1.00/0.90/0.95/0.93。mu 是三月均值,几乎不含月度专属信息。\n"
              "已知失效: 罢工/天气月、benchmark 修订月、birth-death 转折;"
              "σ 窗口滞后于波动率突变(24 个月半衰)。"),
    ModelSpec("KXU3", "u3", "u3/0.1.0",
              {"kernel": "empirical delta", "kfold_convolution": True},
              ["UNRATE 首印 Δ 核 (PIT)", "多月卷积"], _TT, True,
              "**u3/0.1.0** — 经验 Δ 核 + 多月卷积。已知失效: 参与率跳变、"
              "普查临时工月;0.1 取整边界(print==strike 概率大,strict > 定价关键)。"),
    ModelSpec("KXFEDDECISION", "fed", "fed/0.1.0",
              {"w_rule": 0.4, "w_market": 0.6},
              ["历史条件规则 (base rates)", "KXFED 梯子市场先验 (log-pool)"], _TT, True,
              "**fed/0.1.0** — 条件规则 × 市场先验对数池。已知失效: 会间紧急行动、"
              "主席更迭政权变化(规则样本 1990-2026)。"),
    ModelSpec("KXFED", "fed", "fed/0.1.0", {},
              ["decision categorical → 上界梯子 Empirical"], _TT, True,
              "fed/0.1.0 的梯子投影;失效同 KXFEDDECISION。"),
    ModelSpec("KXGDP", "gdp", "gdp/0.2.0",
              {"anchor": "GDPNow", "sigma_floor": 0.5, "ar_window": 80, "ar_winsor": 2.5},
              ["GDPNOW ALFRED vintages (PIT)", "A191RL1Q225SBEA 首印误差 σ",
               "首印 winsorised AR(1)(离季 mu 收缩 + 视野 σ)"],
              _TT, True,
              "**gdp/0.2.0** — 当季: GDPNow 锚 + 历史 nowcast-vs-首印误差 σ(同 0.1.0)。"
              "**离季(k≥1 季)**: mu = m + φ^k·(GDPNow − m),σ = "
              "hypot(σ_nowcast·φ^k, σ_ar·√Σφ^2j)。\n"
              "为什么改: 0.1.0 离季直接拿当季 nowcast 当 mu,σ 恒为 hypot(1.302, 0.5)"
              "=1.394pp 且与 k 无关 —— 于是 2026-Q4/2027-Q1/Q2/Q3 四张合约逐 bit 相同。"
              "实测 GDPNow 对 Q+k 首印 RMSE: k=0 **0.99pp**,k=1..4 **2.54/2.63/2.18/2.76**"
              "(剔 2020),是 k=1 的**阶跃**而非斜坡(φ=0.364/0.338/−0.026 三个样本期)。"
              "OOS PIT 136 条/37 个锚: RMSE 2.533 → **1.57**;按锚聚类自助 B=4000 "
              "差值 +0.772pp,90% 区间 [+0.395, +1.146],P(收缩更好)=100%。\n"
              "winsorise 2.5% 是用来**替代手工剔 2020** 的(生产看不到未来);"
              "不 winsorise 时 φ 在含疫情窗口上翻成 −0.23。\n"
              "已知失效: 季度早期(GDPNow 数据覆盖不足月)、政府停摆(源数据断供)、"
              "基准修订季;AR(1) 假设增长无结构性断裂(疫情式断裂靠 winsorise 兜,不是靠模型)。"
              "上线前提: Kalshi KXGDP 合约结构实测 + 两个 paper print(铁律2)。"),
    ModelSpec("KXWTIW", "energy", "energy/0.1.0",
              {"n_samples": 20000, "vol_window": 20, "vol_floor": 0.008, "rng_seed": 0},
              ["CL front-month closes (fut_daily, PIT)", "20d MAD vol"], _TT, True,
              "**energy/0.1.0 WTI** — 无漂移 GBM,前月收盘锚。已知失效: OPEC 会议/"
              "地缘冲击周(已实现波动滞后)、合约换月日锚跳变、EIA 库存日方差膨胀未建模。"),
    ModelSpec("KXNATGASW", "energy", "energy/0.1.0",
              {"n_samples": 20000, "vol_window": 20, "vol_floor": 0.015, "rng_seed": 0},
              ["NG front-month closes (fut_daily, PIT)", "20d MAD vol"], _TT, True,
              "**energy/0.1.0 NG** — 同 WTI。已知失效: 极端天气预报窗(NG 对气温"
              "预报敏感,模型无天气项)、储气报告日。"),
    ModelSpec("KXAAAGASW", "energy", "energy/0.1.0",
              {"trend_damp": 0.5, "sigma_inflate": 1.5, "sigma_floor": 0.01},
              ["GASREGW weekly (ALFRED PIT)", "4w damped trend"], _TT, True,
              "**energy/0.1.0 AAA** — **GASREGW(EIA)是 AAA 结算价的代理**:两者"
              "存在系统性水平偏移(EIA 周一采样 vs AAA 日更),偏移未校准前该系列"
              "只影子判分、不入决策加权;σ×1.5 补偿代理噪声。已知失效: 炼厂事故周、"
              "飓风季批发-零售传导提速。"),
]


def ensure_registered(conn) -> int:
    n = 0
    for spec in _CARDS:
        try:
            if register(conn, spec):
                n += 1
        except ValueError:
            pass                       # older trained_through than a live refit — skip seed
    conn.commit()
    return n
