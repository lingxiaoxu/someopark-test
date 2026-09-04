"""reconcile/official_close.py — 某交易日的官方收盘价(Polygon 全市场日 K)。

────────────────────────────────────────────────────────────────────────────
为什么 Q 不能用 live/portfolio/read 里那份逐票价(2026-08-28 实测定案)
────────────────────────────────────────────────────────────────────────────
M4 的 equity 平面原先把 Q 定义成"QC 同一份 payload 自算 = 持仓市值 + 现金",
理由是口径内部自洽。**这个前提是错的**:那份 payload 里每票的 `p` 并不是收盘价。

8/27 收盘的三方对照:

    QC runtimeStatistics.Equity                5,739,681.75   ← 真收盘
    cash + Σ q×p(payload 里的价)              5,711,713.85   ← 差 27,968
    cash + Σ q×官方收盘价(本模块)             5,739,207.02   ← 差 QC 自报 474.73

payload 里的 `p` 停在 ~15:45,到次日凌晨两点都没再更新过,所以自算值恰好等于
15:45 那一刻的净值;而 QC 自报净值抓到了尾盘那一段(8/27 那天 +28.5k)。

**2026-09-04 修正**:本段原写"equity 曲线最后一次跳动在 16:02:16、15:47→16:02
涨 +28.5k"。实测不成立 —— 用五个 session 的存档股数×分钟 K 逐分钟扫描,QC 自报
净值恒等于 **15:58 那根分钟 K** 的账(池化 RMS 0.216bp;次优的 15:57 是 2.767bp,
16:00 是 4.152bp,识别毫无歧义;8/27 那天尾盘 13 分钟拉了 47bp,自报值精确跟在
坡顶)。另实测 QC 净值的推送滞后约 **1 分钟**(不是 15 分钟,15 分钟假设的拟合
误差差 11 倍),发布节奏 ~62-66 秒一次。真正 15 分钟延迟的是 payload 的 `p`
与我们自己的 Polygon 订阅 —— 三根线不要混为一谈。
推论:cross_check(Q − QC自报)= **15:58 → 官方收盘**那一段(最后两分钟的
tape + 收盘集合竞价),符号随机、不随规模增长、也**不因改读取时点而消失**;
9/2 实测 +2,196.89 中 +2,138.42 正是这一段,余下 58 是测量残差。

差 27,968 / gross 5,769,318 = **48.5bp**,是静止阈值 5bp 的十倍。照原写法,
equity 平面永远出不了裁决;而 `rolloff --freeze` 走同一个自算值,冻 K 会把少算
的这 2.8 万**永久焊进常数**。

所以 Q 改成:cash + Σ 逐票股数 × 官方收盘价。好处不只是"对":
  · 与 P(官方 EOD)同价源族(五策略最终都落在 Polygon 日 K 口径上),
    价格噪声两边对消,D 只反映股数与现金的差异 —— 这正是镜像保真度要量的东西;
  · 逐票可归因(符合"只按持仓 shares 去对照"的要求),不是一个黑箱总数;
  · QC 自报净值降级为**独立交叉校验**:两者差超过 gross 的 CROSS_TOL_BP 就
    拒绝出裁决(见 qc_reconcile.equity_plane)。

────────────────────────────────────────────────────────────────────────────
取数方式
────────────────────────────────────────────────────────────────────────────
用 Polygon 的**全市场分组日 K**(grouped daily),一次调用拿全市场 ~12.5k 只
(实测 0.3s),而不是逐票 29 次:少 29 倍请求、不吃限速,而且"这天到底是不是
交易日"由 resultsCount 直接回答 —— 非交易日返回空,我们据此报错而不是静默补零。

`adjusted=false`:要的是**当天实际的成交价**用来给持仓定值。复权价会让同一个
历史交易日的 Q 随日后的拆股/分红变化,昨天算出来的 D 明天就对不上了。

缺价一律抛错,绝不跳过或沿用旧价:少算一只票的市值会被 D 全额吸收,
外表完全正常,没有任何东西会报错。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import requests

_PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PKG))

from inventory_source import SourceError                             # noqa: E402

_GROUPED = ("https://api.polygon.io/v2/aggs/grouped/locale/us/market/"
            "stocks/{session}")
_TIMEOUT = 60


def grouped_closes(session: str) -> dict[str, float]:
    """该交易日全市场收盘价 {ticker: close}。非交易日/取不到一律抛。"""
    key = os.environ.get("POLYGON_API_KEY")
    if not key:
        raise SourceError("POLYGON_API_KEY 不可见 —— 先 source .env;"
                          "没有官方收盘价就定不出 Q,不出裁决")
    try:
        r = requests.get(_GROUPED.format(session=session),
                         params={"apiKey": key, "adjusted": "false"},
                         timeout=_TIMEOUT)
    except requests.RequestException as e:
        raise SourceError(f"Polygon 日 K 取数失败({e})—— 不出裁决") from e
    if r.status_code != 200:
        raise SourceError(f"Polygon 日 K HTTP {r.status_code} —— 不出裁决")
    doc = r.json()
    rows = doc.get("results") or []
    if not rows:
        raise SourceError(
            f"Polygon 在 {session} 没有任何日 K(status={doc.get('status')},"
            f"resultsCount={doc.get('resultsCount')})—— 要么不是交易日,要么"
            f"当天的日 K 还没落地。此刻定不出收盘价,不出裁决")
    out = {}
    for x in rows:
        t, c = x.get("T"), x.get("c")
        if t and c is not None:
            out[t] = float(c)
    return out


def closes_for(session: str, tickers, table: dict | None = None
               ) -> dict[str, float]:
    """点名这些票在 session 的官方收盘价;缺一只都抛。

    tickers 必须已经过 rolloff._canon 归一化(QC 用 security ID 的历史首名,
    ORCC 即现在的 OBDC;拿 QC 的展示名去查 Polygon 会查不到)。
    table 供测试注入,生产恒为 None。
    """
    want = sorted(set(tickers))
    if not want:
        raise SourceError("没有票要定价 —— Q 算不出来,不出裁决")
    tbl = grouped_closes(session) if table is None else table
    miss = [t for t in want if t not in tbl]
    if miss:
        raise SourceError(
            f"{session} 的官方收盘价缺 {len(miss)} 只: {miss} —— 少算这些票的"
            f"市值会被 D 全额吸收且不会报错,故不出裁决")
    return {t: tbl[t] for t in want}


def assert_prices_sane(closes: dict[str, float], qc_prices: dict | None,
                       canon, ratio: float = 3.0) -> None:
    """守卫:官方收盘价与 QC 自己的逐票价偏离过大 = 多半映射到了**别的证券**。

    2026-09-03 实测催生:QC 按 security ID 的历史首名记仓,Revvity 显示为 EGG
    (EG&G → PerkinElmer → Revvity)。前八例历史首名(ORCC/NB/CMB/RCHI/COH/
    FPL/GEVW/WPH)在 Polygon 上都**不存在**,漏配别名会当场抛"收盘价缺",人一看
    就知道要补别名。EGG 不同 —— Polygon 上有一家真实且无关的 Enigmatig Limited
    (9/2 收 2.76,而 Revvity 收 130.94),于是 closes_for **查得到、不报错、
    静默给 868 股 Revvity 按 2.76 估值**,Q 少算 $111,260 = 129bp。那会表现成
    "交叉校验失败",没有任何线索指向 EGG,排查极痛苦。

    判据用**倍数**不用百分比:这道守卫要抓的是"映射到了别的公司"(EGG 2.76 vs
    Revvity 130.94 = 47 倍),不是"价格陈旧"。payload 价停在 ~15:45(见本文件
    顶部)有几十分钟陈旧度,百分之几十的偏离在测试夹具里都是常态,用百分比会
    大面积误伤;而没有哪只股票会在几十分钟里涨跌 3 倍,所以 3 倍阈值既零误报
    又能把错映射一抓一个准 —— 且报错直接点名是哪只票、两个价各是多少。

    局限要说清:两家公司股价恰好同一量级时,这道守卫抓不到 —— 那种情况由 ①
    holdings 平面兜底(QC 名与 target 名对不上,逐票 0 差会立刻红)。两道防线
    针对的是同一个坑的不同侧面。

    qc_prices 为空(旧版快照没存逐票价)时跳过:守卫是加分项,不能让老档案
    无法补算。
    """
    if not qc_prices:
        return
    bad = []
    for t, p in qc_prices.items():
        c = closes.get(canon(t))
        if c is None or not p or p <= 0:
            continue
        r = c / float(p)
        if r > ratio or r < 1.0 / ratio:
            bad.append(f"{t}→{canon(t)}: 官方收盘 {c:,.2f} vs QC 自己的价 "
                       f"{float(p):,.2f}(相差 {max(r, 1/r):.1f} 倍)")
    if bad:
        raise SourceError(
            "官方收盘价与 QC 逐票价严重不符,多半是 QC 历史首名映射到了**别的"
            "证券**(如 EGG=EG&G/Revvity,而 Polygon 的 EGG 是 Enigmatig):"
            + "; ".join(bad) + " —— 补 rolloff.QC_SYMBOL_ALIAS 后重跑")
