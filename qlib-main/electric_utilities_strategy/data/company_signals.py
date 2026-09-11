"""
AEUS company-layer signals
==========================
Company-specific derived signals stored under ``price_data/elec_strategy/company/``.

  1. AI CapEx pulse  (capex_pulse.json)
     Equal-weight 3-month return of the four hyperscalers (MSFT/GOOGL/META/AMZN),
     z-scored over a 24-month rolling window.  Pure price computation, daily,
     fetched **independently via yfinance** (these names are NOT in the tradeable
     universe and NOT in the price store) — fully decoupled from loader.py.

  2. Utility CapEx proxy  (utility_capex_actual.json)   [AEUS analog of MU DIO]
     Aggregate REAL quarterly CapEx of the regulated mega-utilities
     (NEE / DUK / SO) from SEC XBRL ``PaymentsToAcquirePropertyPlantAndEquipment``
     — the rate-base growth engine, i.e. how fast the regulated grid is being
     built out.  YoY% of the group sum; PIT availability = latest ``filed``.
     Reuses the same YTD-decumulation engine (``_standalone_quarters``) that the
     AISS lineage built for MU COGS + hyperscaler CapEx.

  3. Water-utility CapEx proxy  (water_capex_actual.json)  [AEUS_PLAN §4.1]
     Same engine over AWK / WTRG — the cooling-water buildout confirmation for
     the water_cooling subsector.

  4. Hyperscaler ACTUAL quarterly CapEx  (hyperscaler_capex_actual.json)
     Inherited from AISS unchanged — the same four hyperscalers drive both
     chip demand and datacenter power demand.

All are read back PIT-correctly via ``load_*`` (see ``aeus_pit``).

CLI
---
    python -m electric_utilities_strategy.data.company_signals --init
    python -m electric_utilities_strategy.data.company_signals --update-capex
    python -m electric_utilities_strategy.data.company_signals --update-utility-capex
    python -m electric_utilities_strategy.data.company_signals --update-water-capex
    python -m electric_utilities_strategy.data.company_signals --update-hyperscaler-capex
    python -m electric_utilities_strategy.data.company_signals --verify
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_QLIB_DIR = _THIS_DIR.parents[1]
_PROJECT_DIR = _THIS_DIR.parents[2]
for _p in (str(_QLIB_DIR), str(_PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from electric_utilities_strategy.data import aeus_pit as pit
    from electric_utilities_strategy.data import aeus_fetch_sec_data as sec
    from electric_utilities_strategy.data import universe as U
except Exception:  # pragma: no cover
    import aeus_pit as pit          # type: ignore
    import aeus_fetch_sec_data as sec  # type: ignore
    import universe as U            # type: ignore

log = logging.getLogger("aeus.company")

CAPEX_PATH = pit.COMPANY_DIR / "capex_pulse.json"

CAPEX_TICKERS = U.CAPEX_PULSE_TICKERS          # [MSFT, GOOGL, META, AMZN]
CAPEX_LOOKBACK_DAYS = 63                        # ~3 trading months
CAPEX_ZSCORE_WINDOW = 504                       # ~24 trading months
CAPEX_HISTORY_START = "2015-01-01"             # gives z-scores from ~2017

# --- Utility / water CapEx groups (AEUS analogs; CIKs verified live against
# --- SEC company_tickers.json on 2026-08-30) -------------------------------
# --- 共同注册人 CIK(2026-09-07)---------------------------------------------
# SEC 的 companyfacts 把一份**合并多注册人**的 10-Q/10-K 派给其中**一个** CIK。
# 自 Q2-2026 起三家公用事业的合并申报都落到了子公司名下(母公司口径的数值,
# 带**母公司自己的** accession):DUK 2026H1 8,240mn 在 CIK 17797、
# SO 2026H1 6,639mn 在 CIK 1004155。只查母公司 CIK 就什么都看不到 ——
# utility_capex 因此冻在 CY2026Q1(filed 2026-05-05),125 天后触发 STALE 红灯。
# 历史并集是有意的:Gulf Power(44545)2019 年已卖给 NEE,但仍托管它在册那些年的事实。
COREGISTRANT_CIKS = {
    753308:  [37634],                                              # NEE → FPL
    1326160: [17797, 20290, 30371, 37637, 78460, 81020, 1094093],  # DUK 各子公司
    92122:   [3153, 41091, 44545, 66904, 1004155, 1160661],        # SO 各子公司
}
_COREG_CACHE: dict = {}

UTILITY_CAPEX_PATH = pit.COMPANY_DIR / "utility_capex_actual.json"
UTILITY_CAPEX = {
    # NEE 结构上不可达(2026-09-07 双 CIK 复验):CIK 753308 的 taxonomies 恰为
    # ['dei','us-gaap','ffd'],无任何 PaymentsToAcquirePropertyPlantAndEquipment /
    # ProductiveAssets / ...AndIntangibleAssets;唯一的共同注册人 FPL 37634 零条
    # PaymentsToAcquire*。真实元素是**公司扩展标签**(nee:CapitalExpendituresOfFPL 等),
    # 而 companyfacts 结构上不暴露扩展 taxonomy。元素名还不稳定(2021 前叫
    # ...OfPublicUtility、2022 拆成 ...OfFPLSegment + ...OfGulfPowerSegment)。
    # **严禁**用 CapitalExpendituresIncurredButNotYetPaid 或 AFUDC 概念替代 ——
    # 那是权责发生/资本化津贴,不是现金 capex。
    # 保留此条目仅为记录设计意图;它今天贡献 0 个季度,组聚合实际是 DUK+SO。
    # 注意:group engine 当前**不做组成匹配**,NEE 一旦可达,仅它入组就会注入
    # 约 +130% 的假 YoY 台阶(NEE H1-2026 19,389mn vs 现 DUK+SO 组 14,879mn)。
    "NEE": (753308,  "PaymentsToAcquirePropertyPlantAndEquipment"),
    "DUK": (1326160, "PaymentsToAcquirePropertyPlantAndEquipment"),
    "SO":  (92122,   "PaymentsToAcquirePropertyPlantAndEquipment"),
}
UTILITY_MIN_COMPANIES = 2   # 实际含义:DUK 与 SO 两家都必需(NEE 结构上不可达,见上)

WATER_CAPEX_PATH = pit.COMPANY_DIR / "water_capex_actual.json"
WATER_CAPEX = {
    "AWK":  (1410636, "PaymentsToAcquirePropertyPlantAndEquipment"),
    "WTRG": (78128,   "PaymentsToAcquirePropertyPlantAndEquipment"),
}
# 2026-08-30 实测:WTRG 近年只按年度 tag capex(YTD 链断,无法去累计出单季),
# min=2 会把序列冻死在 2018;AWK(全美最大水务、DC 冷却水主角)单家序列完整
# 到当季 → min=1,WTRG 有干净季度时自动并入(composition 记录在 companies_mn)。
WATER_MIN_COMPANIES = 1

# --- N1: Hyperscaler ACTUAL quarterly CapEx (SEC XBRL) ---------------------
# Precise quarterly CapEx from 10-Q/10-K filings — augments the daily price-proxy
# capex_pulse with the real spend figure (PIT = SEC ``filed`` date).
# CIKs + XBRL concepts verified live 2026-05-30 (companyconcept API).  AMZN uses
# PaymentsToAcquireProductiveAssets (its PP&E tag stops in 2017).
HYPERSCALER_CAPEX_PATH = pit.COMPANY_DIR / "hyperscaler_capex_actual.json"
HYPERSCALER_CAPEX = {
    "MSFT":  (789019,  "PaymentsToAcquirePropertyPlantAndEquipment"),   # 2009+
    "GOOGL": (1652044, "PaymentsToAcquirePropertyPlantAndEquipment"),   # 2015+
    "META":  (1326801, "PaymentsToAcquirePropertyPlantAndEquipment"),   # 2012+
    "AMZN":  (1018724, "PaymentsToAcquireProductiveAssets"),            # 2018+
}
_FRAME_Q = re.compile(r"^CY\d{4}Q[1-4]$")     # SEC calendar-quarter standardized frame
HYPERSCALER_MIN_COMPANIES = 3                  # need >=3 of 4 for a meaningful aggregate
HYPERSCALER_Z_WINDOW = 12                      # quarters for YoY z-score


# ===========================================================================
# AI CapEx pulse
# ===========================================================================

def _download_capex_prices(start: str, end: Optional[str]) -> pd.DataFrame:
    import warnings as _w
    _w.filterwarnings("ignore")
    import yfinance as yf
    raw = yf.download(CAPEX_TICKERS, start=start, end=end, auto_adjust=True, progress=False)
    if isinstance(raw.columns, pd.MultiIndex):
        close = raw["Close"]
    else:
        close = raw[["Close"]].rename(columns={"Close": CAPEX_TICKERS[0]})
    close.index = pd.to_datetime(close.index).normalize()
    keep = [t for t in CAPEX_TICKERS if t in close.columns]
    return close[keep].dropna(how="all")


def compute_capex_pulse(close: pd.DataFrame) -> pd.Series:
    """Equal-weight 3M return of the 4 hyperscalers, 24M rolling z-score."""
    ret_3m = close.pct_change(CAPEX_LOOKBACK_DAYS)
    pulse = ret_3m.mean(axis=1)  # equal-weight across available names
    mu = pulse.rolling(CAPEX_ZSCORE_WINDOW, min_periods=CAPEX_ZSCORE_WINDOW // 2).mean()
    sd = pulse.rolling(CAPEX_ZSCORE_WINDOW, min_periods=CAPEX_ZSCORE_WINDOW // 2).std()
    z = (pulse - mu) / sd.replace(0, np.nan)
    return z.dropna()


def update_capex_pulse(end_date: Optional[str] = None, start: str = CAPEX_HISTORY_START,
                       refreeze: bool = False) -> int:
    """Recompute the CapEx-pulse z-score series and persist it (append-only/frozen)."""
    pit.ensure_dirs()
    close = _download_capex_prices(start, end_date)
    if close.empty:
        log.error("CapEx pulse: no hyperscaler prices downloaded")
        return 0
    z = compute_capex_pulse(close)
    fresh = {d.date().isoformat(): float(v) for d, v in z.items()}
    # PIT FREEZE (append-only): keep already-recorded values immutable, append
    # only newer dates.  The rolling z-score would otherwise rewrite history on
    # every refresh (yfinance adj-prices drift with dividends) → non-reproducible
    # backtest.  Pass --refreeze to reseed the frozen baseline from scratch.
    existing = {} if refreeze else pit.load_json(CAPEX_PATH, default={}).get("series", {})
    series = pit.merge_frozen(existing, fresh)
    payload = {
        "meta": {
            "tickers": CAPEX_TICKERS,
            "lookback_days": CAPEX_LOOKBACK_DAYS,
            "zscore_window": CAPEX_ZSCORE_WINDOW,
            "updated_at": date.today().isoformat(),
            "frozen_append_only": True,
            "n": len(series),
            "last_date": max(series) if series else None,
            "last_value": series[max(series)] if series else None,
        },
        "series": series,
    }
    pit.save_json(CAPEX_PATH, payload)
    log.info("CapEx pulse (frozen append-only): %d points, last %s z=%.2f (%d fresh dates merged)",
             len(series), payload["meta"]["last_date"], payload["meta"]["last_value"] or 0.0,
             max(0, len(series) - len(existing)))
    return len(series)


def load_capex_pulse(start: Optional[str] = None, end: Optional[str] = None) -> pd.Series:
    """Daily CapEx-pulse z-score Series (PIT-clean: each value is same-day prices)."""
    payload = pit.load_json(CAPEX_PATH, default={})
    series = payload.get("series", {})
    if not series:
        return pd.Series(dtype="float64", name="capex_pulse")
    s = pd.Series({pd.Timestamp(k): float(v) for k, v in series.items()}).sort_index()
    s.name = "capex_pulse"
    if start:
        s = s.loc[pd.Timestamp(start):]
    if end:
        s = s.loc[:pd.Timestamp(end)]
    return s


# ===========================================================================
# MU DIO (SEC XBRL)
# ===========================================================================

def _duration_facts(facts: dict, concept: str,
                    forms: tuple = ("10-Q", "10-K")) -> dict:
    """All duration facts for ``concept``, keyed by ``(start, end)``.

    **``(start, end)`` — not ``end`` alone.**  ``sec.concept_series`` de-dupes by
    ``end``, which is the right key for *instant* concepts (InventoryNet) but wrong
    for *duration* concepts: a 10-Q tags both the fiscal-YTD span and the standalone
    quarter with the SAME ``end`` and the SAME ``filed`` date, so an end-keyed dict
    silently keeps whichever the file happened to list first.  That is exactly how
    MU's Q2/Q3 COGS were lost (see ``_standalone_quarters``).

    Dedups within a key by EARLIEST ``filed`` — a restatement must not retroactively
    change what we believed on the original filing date (PIT).
    """
    node = facts.get("facts", {}).get("us-gaap", {}).get(concept)
    if not node:
        return {}
    out: dict = {}
    for item in node.get("units", {}).get("USD", []):
        if item.get("form") not in forms:
            continue
        s, e, val, fy = (item.get("start"), item.get("end"),
                         item.get("val"), item.get("fy"))
        if not s or not e or val is None or fy is None:
            continue
        try:
            days = (date.fromisoformat(e) - date.fromisoformat(s)).days
        except Exception:
            continue
        if days < 60 or days > 380:
            continue  # keep quarterly..annual durations; drop instants & odd ranges
        filed = item.get("filed") or ""
        key = (s, e)
        prev = out.get(key)
        if prev is None or (filed and filed < (prev["filed"] or "9999")):
            out[key] = {"start": s, "end": e, "val": float(val), "filed": filed,
                        "fy": fy, "fp": item.get("fp"), "days": days}
    return out


_ANCHOR_TOL_DAYS = 7   # 52/53 周申报人会把同一财年起点标偏几天(SO 2013: 12-29 / 12-30)


def _fiscal_year_starts(recs: list) -> set:
    """真实财年起点集合。两个独立证人,任一即可:
    ``fp == "FY"`` 且 355 <= days <= 380,或 ``fp == "Q1"`` 且 80 <= days <= 100。

    **刻意不用 min(start)、也不用"任何 ~365 天事实"**:AMZN 同时打 YTD 阶梯、显式独立
    季度和 TTM 事实(2017-07-01 → 2018-06-30,364 天,fp=Q2);把 TTM 起点当锚会得出
    13035 − 3074 = 9961 / 273 天的伪季度。
    """
    out = set()
    for r in recs:
        fp, d = r.get("fp"), r["days"]
        if (fp == "FY" and 355 <= d <= 380) or (fp == "Q1" and 80 <= d <= 100):
            out.add(r["start"])
    return out


def _ytd_chain(recs: list, anchor: str) -> list:
    """以 anchor 为财年起点的 YTD 链:``|start − anchor| <= _ANCHOR_TOL_DAYS`` 入链
    (严格相等会漏掉 SO 2013 那种 2012-12-29/12-30 的偏移标注,留下 275 天的 Q4),
    同一 ``end`` 上精确起点优先,其次最早 filed。按 end 升序返回。"""
    a = date.fromisoformat(anchor)
    best: dict = {}
    for r in recs:
        if r["days"] > 370:
            continue
        try:
            off = abs((date.fromisoformat(r["start"]) - a).days)
        except Exception:
            continue
        if off > _ANCHOR_TOL_DAYS:
            continue
        cur = best.get(r["end"])
        if cur is None:
            best[r["end"]] = r
            continue
        cur_off = abs((date.fromisoformat(cur["start"]) - a).days)
        if off < cur_off or (off == cur_off
                             and (r["filed"] or "9999") < (cur["filed"] or "9999")):
            best[r["end"]] = r
    return sorted(best.values(), key=lambda r: r["end"])


def _prefer_quarter(cand: dict, cur) -> bool:
    """cand 是否应取代 cur:隐含时长落在 60–110 的压过不落在的(183 天的数不是关于
    这个季度的"另一种看法",它根本就不是这个季度);再比最早 filed;再比距 91 天的远近。"""
    if cur is None:
        return True
    ci, ui = 60 <= cand["days"] <= 110, 60 <= cur["days"] <= 110
    if ci != ui:
        return ci
    cf, uf = cand["filed"] or "9999", cur["filed"] or "9999"
    if cf != uf:
        return cf < uf
    return abs(cand["days"] - 91) < abs(cur["days"] - 91)


def _standalone_quarters(facts: dict, concept: str,
                         forms: tuple = ("10-Q", "10-K"),
                         prefer_tagged: bool = False) -> dict:
    """Standalone (single-quarter) values for a cumulative-duration concept.

    → ``{end_iso: {"val", "filed", "start", "days", "fy", "fp", "source"}}``

    Default path is **decumulation** of the fiscal-YTD chain, which every filer
    supports (META tags only YTD CapEx):
        Q1 = YTD(Q1);  Q2 = YTD(Q2) - YTD(Q1);  …;  Q4 = FY - YTD(Q3).
    Availability = the *later* YTD's ``filed`` date, which is the day the
    subtraction first becomes computable — the earlier term is already public by
    then, so this stays PIT-honest.

    ``prefer_tagged=True`` additionally lets an explicitly-tagged ~90-day fact
    override the decumulated value for the same ``end``.  It is **off by default**
    on purpose: for MU the two agree exactly (2026Q2 tagged 6,105mn vs decumulated
    12,102-5,997 = 6,105mn; 2026Q3 6,400 vs 18,502-12,102 = 6,400), but for the
    hyperscalers it was historically not neutral.  2026-09-11 起链条按财年起点锚定
    (见下),默认路径已能靠纯 decumulation 找回 AMZN 那些季度(CY2018Q3 = 3352mn/92d,
    n=4),``prefer_tagged`` 退回为纯粹的旁证;hyperscaler 冻结已于同日在操作者批准下
    解除。Flipping it is a deliberate, separately-reviewed change.

    2026-08-27: added when MU's "quarterly" DIO turned out to be annual.  MU tags
    BOTH a 181-day YTD and a 90-day standalone fact on ``end=2026-02-26``, both
    ``filed=2026-03-19``; the old end-keyed dedup kept the YTD, and the downstream
    80-100 day filter then dropped it.  Only each year's Q1 (whose YTD *is* the
    quarter) survived, so the signal moved once a year, every December.
    """
    by_se = _duration_facts(facts, concept, forms=forms)
    if not by_se:
        return {}
    recs = sorted(by_se.values(),
                  key=lambda r: (r["start"], r["end"], r["filed"] or "9999"))

    # 2026-09-11 财年锚定(取代按 SEC ``fy`` 分桶)。``fy`` 是**申报的**财年:10-K 给
    # 自己年度事实打的 fy 与同年三份 10-Q 不同(SO 2015:Q1/Q2/Q3 的 YTD 都是
    # fy=2016,364 天的 FY 事实是 fy=2015,四条全都 start=2015-01-01)。按 fy 分桶
    # 会把年度数字与它自己的前驱分开,Q4 = FY − 0 = 整年。这就是为什么幻影**全是**
    # Q4:Q4 是唯一必须"用 10-K 年度数减 10-Q YTD"才能导出的季度,而这两类文档的 fy
    # 标注互相矛盾。链按真实财年起点(_fiscal_year_starts)锚定,跨 fy 桶收集。
    out: dict = {}
    for anchor in sorted(_fiscal_year_starts(recs)):
        prev_val, prev_end = 0.0, anchor
        prev_end_d = date.fromisoformat(anchor)
        for r in _ytd_chain(recs, anchor):
            end_d = date.fromisoformat(r["end"])
            # days 用**端到端**计算(end − prev_end),即使某一级标偏几天也精确 ——
            # 这正是让下游守卫看得见残渣的关键。
            cand = {"val": r["val"] - prev_val, "filed": r["filed"], "start": prev_end,
                    "days": (end_d - prev_end_d).days, "fy": r.get("fy"),
                    "fp": r.get("fp"), "source": "decumulated"}
            prev_val, prev_end, prev_end_d = r["val"], r["end"], end_d
            if _prefer_quarter(cand, out.get(r["end"])):
                out[r["end"]] = cand

    # 只填不覆盖的 tagged 补位:只触及 decumulation 留空或留下不可用值的 end。
    # 没有它,AMZN CY2017Q3(链缺 Q1/Q2 级)会从 3074/91d 退化成一个 272 天的伪值
    # 再被守卫丢掉。
    for r in recs:
        if not (80 <= r["days"] <= 100):
            continue
        cur = out.get(r["end"])
        if cur is None or not (60 <= cur["days"] <= 110):
            out[r["end"]] = {**r, "source": "tagged"}

    if prefer_tagged:
        # Explicitly-tagged standalone quarters win over anything decumulated above;
        # among two tagged facts for the same end, earliest filed wins (as above).
        for r in recs:
            if not (80 <= r["days"] <= 100):
                continue
            cur = out.get(r["end"])
            if (cur is None or cur.get("source") != "tagged"
                    or (r["filed"] and r["filed"] < (cur["filed"] or "9999"))):
                out[r["end"]] = {**r, "source": "tagged"}
    return out


# ===========================================================================
# Group aggregate CapEx engine (shared by utility / water / hyperscaler groups)
# — the AISS hyperscaler aggregation logic, parameterised (AEUS_PLAN §4).
# ===========================================================================

def _coregistrants_for(parent_cik: int, parent_accns: set, label: str) -> list:
    """该母公司已 pin 的共同注册人 CIK 列表(带漂移告警,但**不自动合并**)。"""
    return list(COREGISTRANT_CIKS.get(int(parent_cik), []))


def _merge_registrant_facts(parent_cik: int, label: str, tk: str,
                            provenance: "Optional[dict]" = None) -> dict:
    """母公司 companyfacts + 共同注册人托管的**母公司口径**事实。

    准入守卫是**强制的**:只放行 accession 出现在母公司 submissions 列表里的事实。
    不能用 accession 前缀判断 —— 前 10 位是**报送代理**而非注册人(例:MSFT 的
    10-Q accession 以 Donnelley 的 CIK 开头)。2026-09-07 实测:放行 4 条
    (17797×2、1004155×2),拒绝 190 条子公司自有事实。

    list_accession_numbers 在分页失败时会抛错,异常会一路冒到调用方的
    per-company try/except —— 这是有意的:宁可这一家缺席,也不要拿一个不完整的
    白名单去做准入判断(那会拒绝全部共同注册人事实,静默重现正在修的 staleness)。
    """
    facts = sec.fetch_company_facts(int(parent_cik))
    coregs = COREGISTRANT_CIKS.get(int(parent_cik), [])
    if not coregs:
        return facts
    parent_accns = _COREG_CACHE.get(int(parent_cik))
    if parent_accns is None:
        parent_accns = sec.list_accession_numbers(int(parent_cik), include_history=True)
        _COREG_CACHE[int(parent_cik)] = parent_accns
    merged = 0
    rejected = 0
    hosts: dict = {}
    for co in coregs:
        cf = sec.fetch_company_facts_optional(int(co))
        if not cf:
            continue
        for taxo, concepts in (cf.get("facts") or {}).items():
            for cname, cbody in (concepts or {}).items():
                for unit, items in ((cbody or {}).get("units") or {}).items():
                    for it in items or []:
                        if it.get("accn") in parent_accns:
                            dst = (facts.setdefault("facts", {}).setdefault(taxo, {})
                                        .setdefault(cname, {"units": {}})
                                        .setdefault("units", {}).setdefault(unit, []))
                            if not any(x.get("accn") == it.get("accn")
                                       and x.get("start") == it.get("start")
                                       and x.get("end") == it.get("end") for x in dst):
                                dst.append(dict(it)); merged += 1
                                hosts[it.get("accn")] = int(co)
                        else:
                            rejected += 1
    log.info("%s %s: co-registrant merge — %d facts merged, %d rejected (own filings)",
             label, tk, merged, rejected)
    if provenance is not None and merged:
        provenance.setdefault("coregistrant_hosted", {})[tk] = hosts
    return facts


def _compute_group_capex(companies: dict, min_companies: int, label: str,
                         provenance: "Optional[dict]" = None) -> dict:
    """Aggregate a company group's real quarterly CapEx by calendar quarter.

    For each calendar quarter we sum the companies that reported a clean single
    quarter (require >= ``min_companies``), record the *availability* date as
    the latest ``filed`` among them (conservative / PIT-safe), and compute YoY
    vs the same quarter a year earlier.  Returns {frame: record}.
    """
    per_frame: dict = {}      # frame -> {ticker: {"val","filed","end"}}
    for tk, (cik, concept) in companies.items():
        try:
            facts = _merge_registrant_facts(int(cik), label, tk, provenance)
        except Exception as e:  # noqa: BLE001
            log.warning("%s CapEx: %s (CIK %s) fetch failed: %s", label, tk, cik, e)
            continue
        for fr, rec in _raw_quarterly_capex(facts, concept).items():
            per_frame.setdefault(fr, {})[tk] = rec

    records: dict = {}
    for fr in sorted(per_frame):
        comp = per_frame[fr]
        if len(comp) < min_companies:
            continue
        total = sum(c["val"] for c in comp.values())
        filed = max((c["filed"] or "") for c in comp.values())
        end = max((c["end"] or "") for c in comp.values())
        records[fr] = {
            "period_end": end,
            "filed_date": filed,        # PIT availability = last of the group's filings
            "capex_usd_mn": round(total / 1e6, 1),
            "n_companies": len(comp),
            "companies_mn": {t: round(c["val"] / 1e6, 1) for t, c in comp.items()},
        }

    # YoY% vs same calendar quarter a year earlier —— **成分匹配**(2026-09-11)
    # 原写法是 sum(本季全体) / sum(去年同季全体):成员进出时打印的是**组成变化**
    # 而非增长。实测 water CY2016Q1 +407.9%(真值 +3.8%)—— 那是 WTRG 独家的
    # CY2015Q1 对 AWK+WTRG 的 CY2016Q1;hyperscaler CY2018Q1 +194.9% 是 AMZN 入组。
    # 两侧只对**交集**求和;门是 len(common) >= min_companies —— 与
    # industry_signals.update_backlog_rpo 同一模式、同一字段名 yoy_members,
    # 两个聚合器要读成同一个模式。
    for fr in records:
        yr, q = int(fr[2:6]), int(fr[7])
        prev_fr = f"CY{yr - 1}Q{q}"
        cur, prev = per_frame.get(fr, {}), per_frame.get(prev_fr, {})
        common = sorted(set(cur) & set(prev))
        yoy = None
        if len(common) >= min_companies:
            cur_sum = sum(cur[t]["val"] for t in common)
            prev_sum = sum(prev[t]["val"] for t in common)
            if prev_sum > 0:
                yoy = round((cur_sum / prev_sum - 1.0) * 100.0, 1)
        records[fr]["capex_yoy_pct"] = yoy
        records[fr]["yoy_members"] = common
    return records


def _update_group_capex(path: Path, companies: dict, min_companies: int,
                        label: str) -> int:
    pit.ensure_dirs()
    records = _compute_group_capex(companies, min_companies, label)
    if not records:
        log.error("%s CapEx: no records computed", label)
        return 0
    payload = {
        "meta": {
            "ciks": {t: c for t, (c, _) in companies.items()},
            "min_companies": min_companies,
            "updated_at": date.today().isoformat(),
            "n": len(records),
        },
        "records": records,
    }
    pit.save_json(path, payload)
    latest = sorted(records.values(), key=lambda r: r["period_end"])[-1]
    log.info("%s CapEx: %d quarters; latest %s sum=$%.1fB YoY=%s filed=%s",
             label, len(records), latest["period_end"],
             latest["capex_usd_mn"] / 1e3, latest["capex_yoy_pct"],
             latest["filed_date"])
    return len(records)


def _load_group_capex_yoy(path: Path, name: str,
                          start: Optional[str] = None,
                          end: Optional[str] = None) -> pd.Series:
    """PIT-correct daily series of a group's CapEx YoY% (ffilled from filed)."""
    payload = pit.load_json(path, default={})
    records = {k: v for k, v in payload.get("records", {}).items()
               if v.get("capex_yoy_pct") is not None}
    if not records:
        return pd.Series(dtype="float64", name=name)
    avail = pit.pit_series(records, value_field="capex_yoy_pct", date_field="filed_date")
    daily = pit.reindex_pit_daily(avail, start=start, end=end)
    daily.name = name
    return daily


# --- N2: Utility CapEx proxy (NEE/DUK/SO — rate-base growth engine) --------

def compute_utility_capex() -> dict:
    return _compute_group_capex(UTILITY_CAPEX, UTILITY_MIN_COMPANIES, "Utility")


def update_utility_capex() -> int:
    return _update_group_capex(UTILITY_CAPEX_PATH, UTILITY_CAPEX,
                               UTILITY_MIN_COMPANIES, "Utility")


def load_utility_capex_yoy(start: Optional[str] = None, end: Optional[str] = None) -> pd.Series:
    return _load_group_capex_yoy(UTILITY_CAPEX_PATH, "utility_capex_yoy", start, end)


def utility_capex_value_at(as_of_date) -> Optional[dict]:
    payload = pit.load_json(UTILITY_CAPEX_PATH, default={})
    return pit.get_latest_available(payload.get("records", {}), as_of_date, "filed_date")


# --- N3: Water-utility CapEx proxy (AWK/WTRG — cooling-water buildout) -----

def compute_water_capex() -> dict:
    return _compute_group_capex(WATER_CAPEX, WATER_MIN_COMPANIES, "Water")


def update_water_capex() -> int:
    return _update_group_capex(WATER_CAPEX_PATH, WATER_CAPEX,
                               WATER_MIN_COMPANIES, "Water")


def load_water_capex_yoy(start: Optional[str] = None, end: Optional[str] = None) -> pd.Series:
    return _load_group_capex_yoy(WATER_CAPEX_PATH, "water_capex_yoy", start, end)


def water_capex_value_at(as_of_date) -> Optional[dict]:
    payload = pit.load_json(WATER_CAPEX_PATH, default={})
    return pit.get_latest_available(payload.get("records", {}), as_of_date, "filed_date")


# ===========================================================================
# N1: Hyperscaler ACTUAL quarterly CapEx (SEC XBRL)
# ===========================================================================

def _raw_quarterly_capex(facts: dict, concept: str) -> dict:
    """Decumulate YTD CapEx → standalone calendar-quarter values.

    CapEx is a cumulative-duration flow: most filers (e.g. META) tag only the
    fiscal-YTD figure, so a 3-month-duration filter would miss Q2/Q3/Q4.  Instead
    we group facts by fiscal year, isolate the YTD chain (facts sharing the FY
    start date), and difference consecutive YTDs:
        Q1 = YTD(Q1);  Q2 = YTD(Q2) - YTD(Q1);  Q3 = YTD(Q3) - YTD(Q2);  Q4 = FY - YTD(Q3).
    Each standalone quarter is bucketed by the CALENDAR quarter of its ``end``
    (aligning the 4 firms' differing fiscal years) with the EARLIEST filing date
    for PIT correctness.  Returns {calendar_frame: {val, filed, end}}.

    2026-08-27: the decumulation itself moved to ``_standalone_quarters`` so MU's
    COGS and the hyperscalers' CapEx share one implementation; this function is now
    just the calendar-quarter bucketing on top.
    """
    out: dict = {}
    for end, r in _standalone_quarters(facts, concept).items():
        days = r.get("days") or 91
        if not (60 <= days <= 110):
            # 一条 decumulation 残渣:YTD 链缺级,这个差跨了好几个季度。
            # 与 compute_mu_dio 里作者自己那条守卫同因同法;capex 路径只是一直没用上。
            # 财年锚定修好之后,幸存者只剩"链头"——它更早的那些级 SEC companyfacts
            # 根本不收(当前 9 家全在 2017 年前)——不可约,丢弃是对的:这个数不是季度,
            # 也没有任何东西可以把它缩放过去。守卫放在这里而不是 _standalone_quarters:
            # 那个原语被 compute_mu_dio 与 test_xbrl_decumulation 直接依赖,而"这是不是
            # 一个日历季度"本来就该在装桶的地方问。
            # 分级:远古残渣永久存在走 INFO;近两年内的丢弃是活数据事件(2026-11-06
            # 那种"Q2 缺失 → Q3 合并两季"的签名)走 WARNING。带 entityName 是因为
            # 9 家组员共用同一个 concept 字符串,不带名字这条告警对谁都一样。
            recent = int(end[:4]) >= date.today().year - 2
            (log.warning if recent else log.info)(
                "CapEx %s (%s): dropping %s — decumulated span %d days ($%.0fmn); the "
                "YTD chain is missing a rung%s", concept,
                facts.get("entityName", "?"), end, days, r["val"] / 1e6,
                " (RECENT — check for a co-registrant filing)" if recent else "")
            continue
        try:
            edt = date.fromisoformat(end)
        except Exception:
            continue
        frame = f"CY{edt.year}Q{(edt.month - 1) // 3 + 1}"
        filed = r["filed"]
        prev = out.get(frame)
        if prev is None or (filed and filed < (prev.get("filed") or "9999")):
            out[frame] = {"val": float(r["val"]), "filed": filed, "end": end}
    return out


def compute_hyperscaler_capex() -> dict:
    """Aggregate the 4 hyperscalers' real quarterly CapEx by calendar quarter.

    Delegates to the shared group engine (behaviour and payload schema are
    byte-identical to the AISS-lineage implementation this was factored from).
    """
    return _compute_group_capex(HYPERSCALER_CAPEX, HYPERSCALER_MIN_COMPANIES,
                                "Hyperscaler")


def update_hyperscaler_capex() -> int:
    return _update_group_capex(HYPERSCALER_CAPEX_PATH, HYPERSCALER_CAPEX,
                               HYPERSCALER_MIN_COMPANIES, "Hyperscaler")


def load_hyperscaler_capex_yoy(start: Optional[str] = None, end: Optional[str] = None) -> pd.Series:
    """PIT-correct daily series of aggregate hyperscaler CapEx YoY% (ffilled from filed)."""
    return _load_group_capex_yoy(HYPERSCALER_CAPEX_PATH, "hyperscaler_capex_yoy",
                                 start, end)


def hyperscaler_capex_value_at(as_of_date) -> Optional[dict]:
    payload = pit.load_json(HYPERSCALER_CAPEX_PATH, default={})
    return pit.get_latest_available(payload.get("records", {}), as_of_date, "filed_date")


# ===========================================================================
# Snapshot (industry + company merged) — used by smart_select / reports
# ===========================================================================

def get_aeus_signals_snapshot(as_of_date) -> dict:
    """Return all slow + derived AEUS signals available as of ``as_of_date``."""
    snap: dict = {}
    cap = load_capex_pulse(end=str(as_of_date))
    snap["capex_pulse_zscore"] = float(cap.iloc[-1]) if len(cap) else None
    uc = utility_capex_value_at(as_of_date)
    snap["utility_capex_usd_mn"] = (uc or {}).get("capex_usd_mn")
    snap["utility_capex_yoy_pct"] = (uc or {}).get("capex_yoy_pct")
    wc = water_capex_value_at(as_of_date)
    snap["water_capex_yoy_pct"] = (wc or {}).get("capex_yoy_pct")
    hc = hyperscaler_capex_value_at(as_of_date)
    snap["hyperscaler_capex_usd_mn"] = (hc or {}).get("capex_usd_mn")
    snap["hyperscaler_capex_yoy_pct"] = (hc or {}).get("capex_yoy_pct")
    # industry layer (imported lazily to avoid a hard cycle)
    try:
        from electric_utilities_strategy.data import industry_signals as ind
    except Exception:  # pragma: no cover
        import industry_signals as ind  # type: ignore
    gen = ind.elec_gen_value_at(as_of_date)
    snap["elec_gen_yoy_latest"] = (gen or {}).get("yoy_pct")
    bk = ind.backlog_value_at(as_of_date)
    snap["backlog_rpo_usd_bn"] = (bk or {}).get("rpo_usd_bn")
    snap["backlog_rpo_yoy_pct"] = (bk or {}).get("yoy_pct")
    gas = ind.load_gas_price_proxy(end=str(as_of_date))
    snap["gas_price_z"] = float(gas.iloc[-1]) if len(gas) else None
    return snap


def verify() -> bool:
    """存在性 + **时效性** 双检。

    2026-08-27 加时效检查:此前只查 `if len(x)`,于是 capex_pulse 冻在 2026-06-04
    整整 57 个交易日,每周 weekly 照打 `RESULT: OK`。阈值与语义见 aeus_pit.staleness()。
    """
    cap = load_capex_pulse()
    print("=" * 70)
    print("AEUS COMPANY-LAYER SIGNALS")
    print("=" * 70)
    ok = True
    if len(cap):
        # 日频(每个交易日一个 z 点),用最后一个数据点本身当新鲜度基准
        tag = pit.stale_tag(cap.index[-1].date(), "daily")
        print(f"  capex_pulse : {len(cap):5} pts {cap.index[0].date()}→{cap.index[-1].date()} "
              f"last z={cap.iloc[-1]:+.2f}{tag}")
        if tag:
            ok = False
    else:
        print("  capex_pulse : MISSING"); ok = False

    def _check_group(name: str, path: Path, fatal: bool = True) -> bool:
        nonlocal ok
        payload = pit.load_json(path, default={})
        recs = payload.get("records", {})
        if recs:
            latest = sorted(recs.values(), key=lambda r: r["period_end"])[-1]
            first = sorted(recs.values(), key=lambda r: r["period_end"])[0]
            # 季频:基准取 filed_date(真正可用那天),不取 period_end —— 后者天生晚 90 天
            tag = pit.stale_tag(latest["filed_date"], "quarterly")
            print(f"  {name:17}: {len(recs):3} quarters {first['period_end']}→{latest['period_end']}, "
                  f"latest sum=${latest['capex_usd_mn']/1e3:.1f}B YoY={latest['capex_yoy_pct']}% "
                  f"filed={latest['filed_date']}{tag}")
            if tag:
                ok = False
            return True
        print(f"  {name:17}: MISSING")
        if fatal:
            ok = False
        return False

    _check_group("utility_capex", UTILITY_CAPEX_PATH)
    _check_group("water_capex", WATER_CAPEX_PATH)
    _check_group("hyperscaler_capex", HYPERSCALER_CAPEX_PATH)
    print("=" * 70)
    print("RESULT:", "OK" if ok else "STALE/INCOMPLETE")
    return ok


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description="AEUS company-layer signals")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--update-capex", action="store_true")
    ap.add_argument("--update-utility-capex", "--init-utility-capex",
                    dest="utility_capex", action="store_true")
    ap.add_argument("--update-water-capex", "--init-water-capex",
                    dest="water_capex", action="store_true")
    ap.add_argument("--update-hyperscaler-capex", "--init-hyperscaler-capex",
                    dest="hyperscaler_capex", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--end", default=None)
    args = ap.parse_args()

    did = False
    if args.init or args.update_capex:
        update_capex_pulse(end_date=args.end); did = True
    if args.init or args.utility_capex:
        update_utility_capex(); did = True
    if args.init or args.water_capex:
        update_water_capex(); did = True
    if args.init or args.hyperscaler_capex:
        update_hyperscaler_capex(); did = True
    _ok = True
    if args.verify or did:
        _ok = verify()
    if not did and not args.verify:
        print("Nothing to do. Use --init / --update-capex / --update-utility-capex / "
              "--update-water-capex / --update-hyperscaler-capex / --verify.")
    # 退出码只在**显式** --verify 时才反映体检结果。
    # 为什么不在 update 之后也退非零: update_data 是每日跑的,而 ASML(2026 起停止
    # 披露季度 bookings)之类结构性缺口会让它天天红,红久了就没人看 —— 正是本次要
    # 修的病理本身。weekly 走的是显式 `--verify`(aeus_pipeline.sh),
    # 那条路径必须炸。
    if args.verify and not _ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
