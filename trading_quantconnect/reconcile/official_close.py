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

拉 QC 的 equity 曲线可以看到:8/27 最后一次跳动在 16:02:16,收盘前 15 分钟
(15:47→16:02)净值涨了 +28.5k —— 就是尾盘那一段。而 payload 里的 `p` 停在
~15:45,到次日凌晨两点都没再更新过。所以自算值恰好等于 15:45 那一刻的净值。

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
