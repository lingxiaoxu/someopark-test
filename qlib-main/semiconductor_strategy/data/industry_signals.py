"""
AISS industry-layer signals
===========================
Semiconductor supply-chain industry data, stored under
``price_data/semi_strategy/industry/``.

  1. TSMC monthly revenue YoY  (tsmc_monthly_revenue.json)
     Source: TWSE Open API ``t187ap05_L`` (official, structured JSON).  Gives the
     latest published month's revenue + YoY% + release date for every TWSE-listed
     company; we keep co_id 2330 (TSMC).  PIT availability = TWSE 出表日期 (release).
     NOTE: the free TWSE Open API exposes only the *current* published month, so
     history accrues forward from first run.  The old MOPS ``ajax_t51sb01`` /
     ``t21sc03`` history endpoints are now access-blocked, so deep history is not
     freely backfillable.  ``supply_chain.py`` therefore falls back to foundry
     price-momentum when a TSMC datapoint is unavailable for a date (the V1
     "股价代理" design).  A manually-seeded history JSON, if present, is honoured.

  2. ASML quarterly net bookings  (asml_quarterly_orders.json)
     Source: SEC EDGAR 6-K quarterly result filings (CIK 937966), parsing the
     "net bookings" figure from the press-release HTML.  PIT availability =
     acceptance/filing date.  Best-effort parse; unparsed quarters are skipped
     and the equipment edge falls back to price-momentum.

  3. DRAM spot-price proxy  (dram_spot_proxy.json)
     Source: the AISS price store — MU 20-day relative strength vs SOXX, z-scored.
     Pure computation, daily.  (V2 can swap in TrendForce weekly spot data.)

  4. Manufacturing-trend PMI proxy  (pmi_series.json)   [V2 supply-chain graph]
     Source: FRED ``IPMAN`` (Industrial Production: Manufacturing), 12-month YoY.
     ISM Manufacturing PMI is no longer free on FRED (licensing, post-2016), so
     IPMAN serves as the manufacturing-trend proxy that drives the ``pmi_proxy``
     → analog_defense edge.  PIT availability = month-end + ~20d (G.17 release).
     Missing data → the edge contributes 0 (graceful fallback, as in V1).

CLI
---
    python -m semiconductor_strategy.data.industry_signals --init
    python -m semiconductor_strategy.data.industry_signals --check-tsmc
    python -m semiconductor_strategy.data.industry_signals --check-asml
    python -m semiconductor_strategy.data.industry_signals --update-dram
    python -m semiconductor_strategy.data.industry_signals --update-pmi
    python -m semiconductor_strategy.data.industry_signals --verify
"""

from __future__ import annotations

import argparse
import html as _html
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
    from semiconductor_strategy.data import aiss_pit as pit
    from semiconductor_strategy.data import aiss_fetch_sec_data as sec
    from semiconductor_strategy.data import aiss_fetch_prices as fp
except Exception:  # pragma: no cover
    import aiss_pit as pit            # type: ignore
    import aiss_fetch_sec_data as sec  # type: ignore
    import aiss_fetch_prices as fp     # type: ignore

log = logging.getLogger("aiss.industry")

TSMC_PATH = pit.INDUSTRY_DIR / "tsmc_monthly_revenue.json"
ASML_PATH = pit.INDUSTRY_DIR / "asml_quarterly_orders.json"
DRAM_PATH = pit.INDUSTRY_DIR / "dram_spot_proxy.json"
PMI_PATH = pit.INDUSTRY_DIR / "pmi_series.json"

ASML_CIK = 937966
TSMC_CO_ID = "2330"
TWSE_MR_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"

DRAM_RS_WINDOW = 20
DRAM_Z_WINDOW = 252

# Manufacturing-trend proxy (supply-chain graph node ``pmi_proxy`` → analog_defense).
# ISM Manufacturing PMI is NOT free on FRED post-2016 (licensing), so we use
# Industrial Production: Manufacturing (FRED ``IPMAN``, monthly, 1939+, free) as
# the manufacturing-trend proxy; analog/industrial/defense chips (TXN/ADI/MCHP)
# track industrial production.  We store the 12-month YoY change.
FRED_PMI_SERIES = "IPMAN"
PMI_START = "2010-01-01"
# IPMAN (G.17) for month M is released ~mid-M+1; +20d from month-end is a
# conservative (never look-ahead) availability date.
PMI_RELEASE_LAG_DAYS = 20


# ===========================================================================
# TSMC monthly revenue (TWSE Open API)
# ===========================================================================

def _twse_get() -> list:
    import requests
    resp = requests.get(
        TWSE_MR_URL,
        headers={"User-Agent": "Mozilla/5.0 (someopark-research aiss-data)"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def update_tsmc_monthly_revenue() -> int:
    """Append the latest published TSMC monthly-revenue datapoint (forward-only).

    Returns the number of records in the store after the update.
    """
    pit.ensure_dirs()
    payload = pit.load_json(TSMC_PATH, default={"meta": {}, "records": {}})
    records = payload.setdefault("records", {})
    try:
        rows = _twse_get()
    except Exception as e:  # noqa: BLE001
        log.warning("TWSE Open API fetch failed (%s); keeping %d records", e, len(records))
        return len(records)

    tsmc = next((r for r in rows if str(r.get("公司代號", "")).strip() == TSMC_CO_ID), None)
    if tsmc is None:
        log.warning("TSMC (2330) not found in TWSE response")
        return len(records)

    period = pit.roc_yyyymm_to_iso_month(tsmc.get("資料年月"))
    release = pit.roc_yyyymmdd_to_iso(tsmc.get("出表日期"))
    try:
        yoy = float(tsmc.get("營業收入-去年同月增減(%)"))
    except (TypeError, ValueError):
        yoy = None
    try:
        ntd_thousands = float(tsmc.get("營業收入-當月營收"))
        ntd_bn = round(ntd_thousands / 1e6, 3)  # thousands -> NT$ billion
    except (TypeError, ValueError):
        ntd_bn = None

    if period and release and yoy is not None:
        records[period] = {
            "period_month": period,
            "release_date": release,
            "yoy_pct": yoy,
            "ntd_bn": ntd_bn,
        }
        log.info("TSMC %s: YoY=%.1f%% (NT$%.1fbn) release=%s", period, yoy, ntd_bn or -1, release)
    else:
        log.warning("TSMC row incomplete: period=%s release=%s yoy=%s", period, release, yoy)

    payload["meta"] = {"source": "twse_openapi_t187ap05_L", "co_id": TSMC_CO_ID,
                       "updated_at": date.today().isoformat(), "n": len(records),
                       "note": "forward-only; deep history not freely backfillable"}
    pit.save_json(TSMC_PATH, payload)
    return len(records)


def load_tsmc_monthly(start: Optional[str] = None, end: Optional[str] = None) -> pd.Series:
    """PIT-correct daily TSMC YoY% series (ffilled from release_date)."""
    payload = pit.load_json(TSMC_PATH, default={})
    recs = payload.get("records", {})
    if not recs:
        return pd.Series(dtype="float64", name="tsmc_yoy")
    avail = pit.pit_series(recs, value_field="yoy_pct", date_field="release_date")
    s = pit.reindex_pit_daily(avail, start=start, end=end)
    s.name = "tsmc_yoy"
    return s


def tsmc_value_at(as_of_date) -> Optional[dict]:
    payload = pit.load_json(TSMC_PATH, default={})
    return pit.get_latest_available(payload.get("records", {}), as_of_date, "release_date")


# ===========================================================================
# ASML quarterly net bookings (SEC EDGAR 6-K)
# ===========================================================================

# Parse patterns for ASML quarterly "net bookings" / "order intake" (EUR bn/mn).
# ASML's wording: 2024-era press releases say e.g. "net bookings in Q3 of €2.6
# billion"; later releases sometimes say "order intake".  The € sign arrives as
# the HTML entity &#8364;, so we html.unescape() before matching.
_ASML_PATTERNS = [
    re.compile(r"net\s+(?:system\s+)?bookings\b[^.]{0,70}?€\s*([0-9]+(?:\.[0-9]+)?)\s*billion", re.I),
    re.compile(r"order\s+intake\b[^.]{0,70}?€\s*([0-9]+(?:\.[0-9]+)?)\s*billion", re.I),
    re.compile(r"net\s+(?:system\s+)?bookings\b[^.]{0,70}?€\s*([0-9][0-9,]*)\s*million", re.I),
    re.compile(r"bookings\s+of\s+€\s*([0-9]+(?:\.[0-9]+)?)\s*billion", re.I),
]


def _clean_html_text(raw: str) -> str:
    t = _html.unescape(raw)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t


def _parse_net_bookings_eur_bn(raw_html: str) -> Optional[float]:
    """Extract net bookings (EUR billions) from one ASML exhibit's HTML."""
    text = _clean_html_text(raw_html)
    for i, pat in enumerate(_ASML_PATTERNS):
        m = pat.search(text)
        if not m:
            continue
        num = float(m.group(1).replace(",", ""))
        if "million" in pat.pattern:
            num = num / 1000.0
        if 0.1 <= num <= 100.0:   # sanity range for a quarter's bookings (EUR bn)
            return round(num, 3)
    return None


def _fetch_asml_bookings(accession: str, primary_document: Optional[str]) -> Optional[float]:
    """Scan a filing's exhibits (press release first) for the net-bookings number."""
    docs = sec.list_filing_documents(ASML_CIK, accession)
    htm = [d for d in docs if d.lower().endswith((".htm", ".html")) and "index" not in d.lower()]

    def _rank(name: str) -> int:
        n = name.lower()
        if "pressrelease" in n:
            return 0
        if "presentation" in n:
            return 1
        if "financialstatements" in n:
            return 2
        return 3

    candidates = sorted(htm, key=_rank)
    if primary_document and primary_document not in candidates:
        candidates.append(primary_document)
    for doc in candidates:
        try:
            raw = sec.fetch_filing_document(ASML_CIK, accession, doc)
        except Exception:  # noqa: BLE001
            continue
        val = _parse_net_bookings_eur_bn(raw)
        if val is not None:
            return val
    return None


def _reporting_quarter(filing_date: str) -> str:
    """Derive ASML's *reporting* quarter from the filing date.

    ASML 6-K filings carry no reportDate, and the result for quarter Q is filed
    after Q ends: Q4 in late January (next year), Q1 in April, Q2 in July, Q3 in
    October.  Mapping by filing month avoids the calendar-quarter collisions that
    would otherwise overwrite distinct quarters.
    """
    ts = pd.Timestamp(filing_date)
    m, y = ts.month, ts.year
    if m <= 2:
        return f"{y - 1}Q4"
    if m <= 5:
        return f"{y}Q1"
    if m <= 8:
        return f"{y}Q2"
    if m <= 11:
        return f"{y}Q3"
    return f"{y}Q4"


def update_asml_orders(max_new: int = 60, full_history: bool = True) -> int:
    """Fetch & parse ASML quarterly net bookings from SEC 6-K filings.

    Only filings not already stored (and that parse successfully) are added.
    Returns total record count after update.
    """
    pit.ensure_dirs()
    payload = pit.load_json(ASML_PATH, default={"meta": {}, "records": {}})
    records = payload.setdefault("records", {})

    try:
        filings = sec.list_filings(ASML_CIK, form="6-K",
                                   primary_doc_contains="quarterly",
                                   include_history=full_history)
    except Exception as e:  # noqa: BLE001
        log.warning("ASML 6-K enumeration failed (%s); keeping %d records", e, len(records))
        return len(records)

    have_accessions = {v.get("accession") for v in records.values()}
    parsed_new = 0
    for f in filings:
        if parsed_new >= max_new:
            break
        acc = f.get("accessionNumber")
        if acc in have_accessions:
            continue
        try:
            bookings = _fetch_asml_bookings(acc, f.get("primaryDocument"))
        except Exception as e:  # noqa: BLE001
            log.warning("ASML doc fetch failed %s: %s", acc, e)
            continue
        if bookings is None:
            log.info("ASML %s: net bookings not parsed (skipped)", f.get("filingDate"))
            continue
        timing, _reaction = sec.reaction_from_acceptance(f.get("acceptanceDateTime"))
        qkey = _reporting_quarter(f.get("filingDate"))
        records[qkey] = {
            "quarter": qkey,
            "period_end": f.get("reportDate"),
            "filing_date": f.get("filingDate"),
            "acceptance_datetime": f.get("acceptanceDateTime"),
            "release_timing": timing,
            "accession": acc,
            "net_bookings_eur_bn": bookings,
        }
        parsed_new += 1
        log.info("ASML %s: net bookings €%.2fbn (filed %s)", qkey, bookings, f.get("filingDate"))

    payload["meta"] = {"source": "sec_edgar_6k", "cik": ASML_CIK,
                       "updated_at": date.today().isoformat(), "n": len(records),
                       "parsed_new": parsed_new}
    pit.save_json(ASML_PATH, payload)
    log.info("ASML orders: %d total records (%d new)", len(records), parsed_new)
    return len(records)


def load_asml_orders(start: Optional[str] = None, end: Optional[str] = None) -> pd.Series:
    """PIT-correct daily ASML net-bookings (EUR bn) series, ffilled from filing_date."""
    payload = pit.load_json(ASML_PATH, default={})
    recs = payload.get("records", {})
    if not recs:
        return pd.Series(dtype="float64", name="asml_orders")
    avail = pit.pit_series(recs, value_field="net_bookings_eur_bn", date_field="filing_date")
    s = pit.reindex_pit_daily(avail, start=start, end=end)
    s.name = "asml_orders"
    return s


def asml_value_at(as_of_date) -> Optional[dict]:
    payload = pit.load_json(ASML_PATH, default={})
    return pit.get_latest_available(payload.get("records", {}), as_of_date, "filing_date")


# ===========================================================================
# ASML next-quarter net-sales GUIDANCE (SEC EDGAR 6-K press release)
# ===========================================================================
# 2026-08-27: ASML 从 2026Q1 起不再披露季度 net bookings —— 不是措辞变了,是整行
# 从财报里删了(2026-04-15 / 2026-07-15 全篇零命中,新闻稿只剩定性的 "order intake
# remained extremely strong")。没有任何正则能捞回一个没发布的数字。
#
# 为什么不换 Backlog: 实测 backlog 只在每年 1 月的 Q4 财报里出现一次,且给的是
# **财年总值**(FY2025 €38,797mn / FY2024 €35,938mn)。换过去等于把一条季频信号
# 降级成年频 —— 比现在更差,而且一年里有 11 个月都会被时效检测判 STALE。
#
# 改用**下季度净销售指引**: 每季必发、前瞻(与 bookings 同为"未来需求"而非已实现
# 出货),取区间中值。注意它的 SEC 覆盖面比 bookings 窄 —— 见 update_asml_guidance。
ASML_GUIDANCE_PATH = pit.INDUSTRY_DIR / "asml_quarterly_guidance.json"

# 匹配跑在**去掉全部空白**的小写文本上。原因: 交易所的 HTML 会把一个单词拆进相邻
# 的内联标签,按 `<[^>]+>` → " " 清洗后会得到 "total net sale s between"(2024-07-17
# 就是这样),任何按词写的正则都会漏。去空白后 "totalnetsalesbetween" 稳定可匹配,
# 而数字本身("11.0")内部无空格,不受影响。
_ASML_GUIDANCE_PATTERNS = [
    re.compile(r"expectsq([1-4])(\d{4})(?:total)?netsalesbetweeneur"
               r"([0-9]+(?:\.[0-9]+)?)(?:billion)?andeur([0-9]+(?:\.[0-9]+)?)billion"),
    re.compile(r"expectsq([1-4])(\d{4})(?:total)?netsalesofbetweeneur"
               r"([0-9]+(?:\.[0-9]+)?)(?:billion)?andeur([0-9]+(?:\.[0-9]+)?)billion"),
]


def _squashed_text(raw_html: str) -> str:
    """HTML → 去标签、去实体、**去全部空白**的小写串(专供上面的 pattern 匹配)。"""
    t = _html.unescape(raw_html)
    t = re.sub(r"<[^>]+>", "", t)          # 不插空格: 被标签劈开的单词要能重新粘上
    t = t.replace("€", "eur").replace(" ", "")
    return re.sub(r"\s+", "", t).lower()   # \s 也覆盖 unescape 出的 \xa0(&nbsp;)


def _parse_guidance(raw_html: str) -> Optional[dict]:
    """从一份 exhibit 里抽下季度净销售指引 → {quarter, low, high, mid} (EUR bn)。"""
    text = _squashed_text(raw_html)
    for pat in _ASML_GUIDANCE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        q, yr, lo, hi = int(m.group(1)), int(m.group(2)), float(m.group(3)), float(m.group(4))
        if not (0.5 <= lo <= hi <= 100.0):
            continue          # 区间反了或量级离谱 → 当没解析到,绝不写进存储
        return {"quarter": f"{yr}Q{q}", "low_eur_bn": lo, "high_eur_bn": hi,
                "mid_eur_bn": round((lo + hi) / 2, 3)}
    return None


def update_asml_guidance(max_new: int = 60, full_history: bool = True) -> int:
    """Parse ASML's next-quarter net-sales guidance from 6-K press releases.

    **覆盖面的硬约束**: 指引这句话只出现在*新闻稿* exhibit 里,而 ASML 并非每季都把
    新闻稿附进 6-K —— 2021-04 ~ 2023-10 以及 2025-10 的季度 6-K 只附了财务报表/中期
    报告(净销售指引在公司官网发布,没进 EDGAR)。所以本序列的历史必然短于 bookings
    的 20 期。这是数据可得性的边界,不是解析 bug;能拿到多少就记多少,并如实上报。
    """
    pit.ensure_dirs()
    payload = pit.load_json(ASML_GUIDANCE_PATH, default={"meta": {}, "records": {}})
    records = payload.setdefault("records", {})

    try:
        filings = sec.list_filings(ASML_CIK, form="6-K",
                                   primary_doc_contains="quarterly",
                                   include_history=full_history)
    except Exception as e:  # noqa: BLE001
        log.warning("ASML 6-K enumeration failed (%s); keeping %d records", e, len(records))
        return len(records)

    have = {v.get("accession") for v in records.values()}
    parsed_new = no_release = 0
    for f in filings:
        if parsed_new >= max_new:
            break
        acc = f.get("accessionNumber")
        if acc in have:
            continue
        try:
            docs = sec.list_filing_documents(ASML_CIK, acc)
        except Exception as e:  # noqa: BLE001
            log.warning("ASML doc list failed %s: %s", acc, e)
            continue
        press = [d for d in docs
                 if d.lower().endswith((".htm", ".html")) and "pressrelease" in d.lower()]
        if not press:
            no_release += 1
            continue
        got = None
        for doc in press:
            try:
                got = _parse_guidance(sec.fetch_filing_document(ASML_CIK, acc, doc))
            except Exception:  # noqa: BLE001
                continue
            if got:
                break
        if not got:
            log.info("ASML %s: guidance not parsed (press release present)", f.get("filingDate"))
            continue
        timing, _r = sec.reaction_from_acceptance(f.get("acceptanceDateTime"))
        # 键用**被指引的那个季度**(前瞻标的),不是发布季 —— 两条 6-K 不会指向同一季。
        records[got["quarter"]] = {
            **got,
            "filing_date": f.get("filingDate"),
            "acceptance_datetime": f.get("acceptanceDateTime"),
            "release_timing": timing,
            "accession": acc,
            "reported_quarter": _reporting_quarter(f.get("filingDate")),
        }
        parsed_new += 1
        log.info("ASML guidance %s: €%.2f-%.2fbn mid=%.2f (filed %s)", got["quarter"],
                 got["low_eur_bn"], got["high_eur_bn"], got["mid_eur_bn"], f.get("filingDate"))

    payload["meta"] = {"source": "sec_edgar_6k_press_release", "cik": ASML_CIK,
                       "metric": "next_quarter_total_net_sales_guidance_midpoint",
                       "updated_at": date.today().isoformat(), "n": len(records),
                       "parsed_new": parsed_new, "filings_without_press_release": no_release}
    pit.save_json(ASML_GUIDANCE_PATH, payload)
    log.info("ASML guidance: %d total records (%d new, %d filings had no press release)",
             len(records), parsed_new, no_release)
    return len(records)


def load_asml_guidance(start: Optional[str] = None, end: Optional[str] = None) -> pd.Series:
    """PIT-correct daily ASML guidance-midpoint (EUR bn) series, ffilled from filing_date."""
    payload = pit.load_json(ASML_GUIDANCE_PATH, default={})
    recs = payload.get("records", {})
    if not recs:
        return pd.Series(dtype="float64", name="asml_guidance")
    avail = pit.pit_series(recs, value_field="mid_eur_bn", date_field="filing_date")
    s = pit.reindex_pit_daily(avail, start=start, end=end)
    s.name = "asml_guidance"
    return s


def asml_guidance_value_at(as_of_date) -> Optional[dict]:
    payload = pit.load_json(ASML_GUIDANCE_PATH, default={})
    return pit.get_latest_available(payload.get("records", {}), as_of_date, "filing_date")


# ===========================================================================
# DRAM spot proxy (MU vs SOXX relative strength from the price store)
# ===========================================================================

def update_dram_proxy(end_date: Optional[str] = None, start: str = "2016-01-01",
                      refreeze: bool = False) -> int:
    """Compute MU 20-day relative strength vs SOXX, z-scored; store append-only/frozen."""
    pit.ensure_dirs()
    wide = fp.load_prices_wide(["MU", "SOXX"], start=start, end=end_date, field="AdjClose")
    if wide.empty or "MU" not in wide or "SOXX" not in wide:
        log.error("DRAM proxy: MU/SOXX prices unavailable")
        return 0
    mu_ret = wide["MU"].pct_change(DRAM_RS_WINDOW)
    sx_ret = wide["SOXX"].pct_change(DRAM_RS_WINDOW)
    rs = (mu_ret - sx_ret)                     # relative 20d return
    z = (rs - rs.rolling(DRAM_Z_WINDOW, min_periods=DRAM_Z_WINDOW // 2).mean()) / \
        rs.rolling(DRAM_Z_WINDOW, min_periods=DRAM_Z_WINDOW // 2).std().replace(0, np.nan)
    z = z.dropna()
    fresh = {d.date().isoformat(): float(v) for d, v in z.items()}
    # PIT FREEZE (append-only): MU/SOXX price re-adjustment would otherwise rewrite
    # the whole z-score history each refresh → non-reproducible backtest.
    existing = {} if refreeze else pit.load_json(DRAM_PATH, default={}).get("series", {})
    series = pit.merge_frozen(existing, fresh)
    payload = {
        "meta": {"rs_window": DRAM_RS_WINDOW, "z_window": DRAM_Z_WINDOW,
                 "updated_at": date.today().isoformat(), "frozen_append_only": True,
                 "n": len(series), "last_date": max(series) if series else None,
                 "last_value": series[max(series)] if series else None},
        "series": series,
    }
    pit.save_json(DRAM_PATH, payload)
    log.info("DRAM proxy (frozen append-only): %d pts, last %s z=%.2f (%d fresh merged)",
             len(series), payload["meta"]["last_date"], payload["meta"]["last_value"] or 0.0,
             max(0, len(series) - len(existing)))
    return len(series)


def load_dram_proxy(start: Optional[str] = None, end: Optional[str] = None) -> pd.Series:
    payload = pit.load_json(DRAM_PATH, default={})
    series = payload.get("series", {})
    if not series:
        return pd.Series(dtype="float64", name="dram_proxy")
    s = pd.Series({pd.Timestamp(k): float(v) for k, v in series.items()}).sort_index()
    s.name = "dram_proxy"
    if start:
        s = s.loc[pd.Timestamp(start):]
    if end:
        s = s.loc[:pd.Timestamp(end)]
    return s


# ===========================================================================
# Manufacturing-trend PMI proxy (FRED IPMAN YoY)
# ===========================================================================

def _fred_client():
    """Return a fredapi.Fred client, or None if unavailable (graceful fallback)."""
    import os
    key = os.environ.get("FRED_API_KEY")
    if not key:
        log.warning("FRED_API_KEY not set; PMI series cannot be updated")
        return None
    try:
        from fredapi import Fred
    except Exception as e:  # noqa: BLE001
        log.warning("fredapi unavailable (%s); PMI series cannot be updated", e)
        return None
    return Fred(api_key=key)


def update_pmi_series(start: str = PMI_START, refreeze: bool = False) -> int:
    """Append IPMAN YoY monthly datapoints with PIT release dates (append-only freeze).

    Like the DRAM/TSMC stores, history is frozen on first sight: a month's value
    is never rewritten once recorded (revisions ignored) so the backtest stays
    reproducible.  ``refreeze=True`` rebuilds from the current FRED vintage.
    Returns the number of records after the update.
    """
    pit.ensure_dirs()
    payload = pit.load_json(PMI_PATH, default={"meta": {}, "records": {}})
    records = payload.setdefault("records", {})
    fred = _fred_client()
    if fred is None:
        return len(records)
    try:
        lvl = fred.get_series(FRED_PMI_SERIES, observation_start=start).dropna()
    except Exception as e:  # noqa: BLE001
        log.warning("FRED %s fetch failed (%s); keeping %d records", FRED_PMI_SERIES, e, len(records))
        return len(records)
    if lvl.empty:
        log.warning("FRED %s returned no data", FRED_PMI_SERIES)
        return len(records)

    yoy = (lvl / lvl.shift(12) - 1.0).dropna()
    added = 0
    for ts, v in yoy.items():
        ts = pd.Timestamp(ts)
        period = f"{ts.year:04d}-{ts.month:02d}"
        if period in records and not refreeze:
            continue
        release = (ts + pd.offsets.MonthEnd(0) + pd.Timedelta(days=PMI_RELEASE_LAG_DAYS)).date().isoformat()
        records[period] = {
            "period_month": period,
            "release_date": release,
            "ipman_yoy": round(float(v), 5),
            "ipman": round(float(lvl.loc[ts]), 4),
        }
        added += 1

    payload["meta"] = {"source": f"fred_{FRED_PMI_SERIES}_yoy", "series": FRED_PMI_SERIES,
                       "release_lag_days": PMI_RELEASE_LAG_DAYS, "frozen_append_only": True,
                       "updated_at": date.today().isoformat(), "n": len(records),
                       "note": "IPMAN YoY; manufacturing-trend proxy (ISM PMI not free on FRED)"}
    pit.save_json(PMI_PATH, payload)
    log.info("PMI (IPMAN YoY, frozen append-only): %d months (%d new), latest %s",
             len(records), added, max(records) if records else None)
    return len(records)


def load_pmi_series(start: Optional[str] = None, end: Optional[str] = None) -> pd.Series:
    """PIT-correct daily IPMAN-YoY series (ffilled from release_date)."""
    payload = pit.load_json(PMI_PATH, default={})
    recs = payload.get("records", {})
    if not recs:
        return pd.Series(dtype="float64", name="pmi")
    avail = pit.pit_series(recs, value_field="ipman_yoy", date_field="release_date")
    s = pit.reindex_pit_daily(avail, start=start, end=end)
    s.name = "pmi"
    return s


def pmi_value_at(as_of_date) -> Optional[dict]:
    payload = pit.load_json(PMI_PATH, default={})
    return pit.get_latest_available(payload.get("records", {}), as_of_date, "release_date")


# ===========================================================================
# Verify
# ===========================================================================

def verify() -> bool:
    """存在性 + **时效性** 双检(2026-08-27 加后者,理由见 aiss_pit.staleness())。

    月频/季频的新鲜度基准一律取 **release_date / filing_date**(数据真正可用那天),
    绝不取 period_month / period_end —— 后者天生落后一整期,阈值会全部作废。
    """
    print("=" * 72)
    print("AISS INDUSTRY-LAYER SIGNALS")
    print("=" * 72)
    ok = True
    tp = pit.load_json(TSMC_PATH, default={}).get("records", {})
    if tp:
        latest = sorted(tp.values(), key=lambda r: r["period_month"])[-1]
        tag = pit.stale_tag(latest["release_date"], "monthly")
        print(f"  tsmc_monthly : {len(tp):4} months, latest {latest['period_month']} "
              f"YoY={latest['yoy_pct']:+.1f}% release={latest['release_date']}{tag}")
        if tag:
            ok = False
    else:
        print("  tsmc_monthly : EMPTY (forward-only source; will accrue, "
              "supply_chain uses foundry price-proxy fallback)")
    # asml_orders 是**已停更的历史序列**(ASML 2026Q1 起不再披露季度 net bookings),
    # 保留供回测复现,但不再计入 ok —— 对一个上游已经消失的源报警毫无信息量,只会
    # 制造告警疲劳。活的那条是下面的 asml_guidance。
    ap = pit.load_json(ASML_PATH, default={}).get("records", {})
    if ap:
        latest = sorted(ap.values(), key=lambda r: r.get("filing_date", ""))[-1]
        print(f"  asml_orders  : {len(ap):4} quarters, latest {latest['quarter']} "
              f"€{latest['net_bookings_eur_bn']}bn filed={latest['filing_date']}"
              f"  (RETIRED — 上游停止披露,历史保留)")
    else:
        print("  asml_orders  : EMPTY (retired series)")
    ag = pit.load_json(ASML_GUIDANCE_PATH, default={}).get("records", {})
    if ag:
        latest = sorted(ag.values(), key=lambda r: r.get("filing_date", ""))[-1]
        tag = pit.stale_tag(latest["filing_date"], "quarterly")
        print(f"  asml_guidance: {len(ag):4} quarters, latest {latest['quarter']} "
              f"€{latest['mid_eur_bn']}bn mid filed={latest['filing_date']}{tag}")
        if tag:
            ok = False
    else:
        print("  asml_guidance: EMPTY (equipment edge uses price-proxy fallback)")
        ok = False
    dram = load_dram_proxy()
    if len(dram):
        tag = pit.stale_tag(dram.index[-1].date(), "daily")
        print(f"  dram_proxy   : {len(dram):4} pts {dram.index[0].date()}→{dram.index[-1].date()} "
              f"last z={dram.iloc[-1]:+.2f}{tag}")
        if tag:
            ok = False
    else:
        print("  dram_proxy   : MISSING"); ok = False
    pm = pit.load_json(PMI_PATH, default={}).get("records", {})
    if pm:
        latest = sorted(pm.values(), key=lambda r: r["period_month"])[-1]
        tag = pit.stale_tag(latest["release_date"], "monthly")
        print(f"  pmi (IPMAN)  : {len(pm):4} months, latest {latest['period_month']} "
              f"YoY={latest['ipman_yoy']*100:+.1f}% release={latest['release_date']}{tag}")
        if tag:
            ok = False
    else:
        print("  pmi (IPMAN)  : EMPTY (analog_defense pmi edge contributes 0 → fallback)")
    print("=" * 72)
    print("RESULT:", "OK" if ok else "STALE/PARTIAL (price-proxy fallbacks cover gaps)")
    return ok


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description="AISS industry-layer signals")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--check-tsmc", action="store_true")
    ap.add_argument("--check-asml", action="store_true",
                    help="ASML 指引(活)+ net bookings(已停更,仅补历史)")
    ap.add_argument("--update-dram", action="store_true")
    ap.add_argument("--update-pmi", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--end", default=None)
    args = ap.parse_args()

    did = False
    if args.init or args.check_tsmc:
        update_tsmc_monthly_revenue(); did = True
    if args.init or args.check_asml:
        update_asml_guidance(full_history=True); did = True
        # bookings 已停更;仍跑一次是为了在 ASML 万一恢复披露时自动接上,
        # 解析不到只会打一行 info,不影响退出码。
        update_asml_orders(full_history=args.init); did = True
    if args.init or args.update_dram:
        update_dram_proxy(end_date=args.end); did = True
    if args.init or args.update_pmi:
        update_pmi_series(refreeze=args.init); did = True
    _ok = True
    if args.verify or did:
        _ok = verify()
    if not did and not args.verify:
        print("Nothing to do. Use --init / --check-tsmc / --check-asml / --update-dram / --update-pmi / --verify.")
    # 退出码只在**显式** --verify 时反映体检结果 —— 理由见 company_signals.main()
    # 同处注释(每日 update 天天红 = 告警疲劳 = 本次要修的病理本身)。
    if args.verify and not _ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
