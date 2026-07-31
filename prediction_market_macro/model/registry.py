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
    ModelSpec("KXJOBLESSCLAIMS", "claims", "claims/0.1.0",
              {"level_weights": [0.4, 0.3, 0.2, 0.1], "sigma_floor": 0.02,
               "seasonal_clip": 0.25, "seasonal_years": 10},
              ["ICSA first prints (PIT)", "ISO-week seasonal dev", "26w robust vol"],
              _TT, True,
              "**claims/0.1.0** — log-level weighted mean + PIT ISO-week seasonal +"
              " MAD vol.\n已知失效: 突发大规模裁员冲击(模型无新闻项)、政府停摆周、"
              "假日周历法漂移(Thanksgiving/July-4 retooling 依赖 10y 样本)。\n"
              "OOS 复盘(26 期 replay): Brier 0.165-0.172 vs 市场 0.090-0.097 —"
              " **输给市场,保持 paper**;改进路径=市场先验对数池。"),
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
    ModelSpec("KXPAYROLLS", "payrolls", "payrolls/0.1.0",
              {"tail_mix": True},
              ["PAYEMS printed-change 重建 (vintages)", "claims 信号", "肥尾混合"],
              _TT, True,
              "**payrolls/0.1.0** — 首印变动重建 + claims 信号 + 肥尾混合。"
              "已知失效: 罢工/天气月、benchmark 修订月、birth-death 转折。"),
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
    ModelSpec("KXGDP", "gdp", "gdp/0.1.0",
              {"anchor": "GDPNow", "sigma_floor": 0.5},
              ["GDPNOW ALFRED vintages (PIT)", "A191RL1Q225SBEA 首印误差 σ"],
              _TT, True,
              "**gdp/0.1.0** — GDPNow 锚 + 历史 nowcast-vs-首印误差 σ。已知失效: "
              "季度早期(GDPNow 数据覆盖不足月)、政府停摆(源数据断供)、"
              "基准修订季。上线前提: Kalshi KXGDP 合约结构实测 + 两个 paper print(铁律2)。"),
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
