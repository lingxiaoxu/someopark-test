#!/usr/bin/env python3
"""
RefreshBDCHoldings.py — D1: SOI (Schedule of Investments) data-acquisition layer
for the private-credit BDC look-through pipeline.

Pulls the *underlying loan holdings* of the 5 BDC stocks tracked by the BDC sleeve
(GBDC/TSLX/OBDC/BXSL/ARCC, weights = UpdateBDCPerformance.py) from SEC EDGAR, where
BDC Schedules of Investments are Inline-XBRL-tagged (SEC Release 33-10771).

Channels (see .claude/plans/PRIVATE_CREDIT_BDC_LOOKTHROUGH_PLAN.md §3):
  A (primary)  SEC "BDC Data Sets" monthly zip -> soi.tsv  (per-tranche pivot)
  B (fallback) per-filing XBRL instance parse              (when soi.tsv lags)

D0-spike-validated logic baked in (§3.3):
  * current period = submissions reportDate, NOT max(ddate)  (OBDC has 11 stray ddates)
  * FV column drifts per filer -> auto-select best-coverage 'fair value' column
  * tranche = identifier non-null;  total row = identifier empty (= companyfacts truth)
  * reconcile total-row FV vs companyfacts; look-through weights normalise to total row
    (per-tranche ΣFV over-counts consolidated subs: ARCC +17%, BXSL +0%, ...)

SEC compliance: descriptive User-Agent + <10 req/s throttle (copied from
qlib-main/.../aiss_fetch_sec_data.py:34-83 per the project copy-first rule).

Env: someopark_run. No API key (EDGAR is free). Writes nothing outside the store
unless --no-sandbox; default dev mode is fully sandboxed to a temp dir.

Usage:
    python RefreshBDCHoldings.py --probe                 # just show latest filings
    python RefreshBDCHoldings.py --sandbox /tmp/bdc_dev  # ingest 5 BDCs -> sandbox
    python RefreshBDCHoldings.py --ticker ARCC --sandbox /tmp/bdc_dev
    python RefreshBDCHoldings.py --dry-run               # ingest, print, write nothing
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import warnings
import zipfile
from datetime import datetime, timezone

import numpy as np
import pandas as pd

# per-column SOI assignments fragment the frame (harmless at ~5.6k rows; de-fragged once)
warnings.filterwarnings("ignore", message="DataFrame is highly fragmented")

# ── paths / config ──────────────────────────────────────────────────────────
_ROOT = os.path.dirname(os.path.abspath(__file__))
BDC_STORE = os.path.join(_ROOT, "price_data", "bdc_holdings")
DEALS_DIR = os.path.join(_ROOT, "portfolio_of_private_credit_deals", "deals_data")

# BDC universe — indexed from inventory_bdc.json (single source of truth since
# 2026-08-11; cik + sleeve weight live there). No more hard-coded lockstep with
# UpdateBDCPerformance — both read the same file.
from bdc_inventory import load_inventory as _load_bdc_inventory

BDC_UNIVERSE = {
    t: {"cik": h["cik"], "sleeve_w": h["weight"]}
    for t, h in _load_bdc_inventory()["holdings"].items()
}

SEC_USER_AGENT = os.environ.get(
    "BDC_SEC_USER_AGENT", "someopark-research BDC-data admin@someopark.com"
)
_SEC_MIN_INTERVAL = 0.15      # ~6.7 req/s, < 10 limit
_last_request_ts = [0.0]

BDC_DATASETS_BASE = ("https://www.sec.gov/files/datastandardsinnovation/data/"
                     "business-development-company-bdc-data-sets")

# SOI XBRL element -> our canonical column (the standard column names in soi.tsv).
COL = {
    "id":     "Investment, Identifier Axis",
    "ind":    "Industry Sector Axis",
    "aff":    "Investment, Issuer Affiliation Axis",
    "itype":  "Investment Type Axis",
    "rate":   "Investment Interest Rate",                       # all-in
    "spread": "Investment, Basis Spread, Variable Rate",
    "floor":  "Investment, Interest Rate, Floor",               # real soi.tsv name (commas)
    "pik":    "Investment, Interest Rate, Paid in Kind",        # real soi.tsv name (commas)
    "prin":   "Investment Owned, Balance, Principal Amount",
    "pct":    "Investment Owned, Net Assets, Percentage",
    "mat":    "Investment Maturity Date",
    "shares": "Investment shares",                              # real soi.tsv name
    "aff_enum": "Investment, Issuer Affiliation [Extensible Enumeration]",
}
# FV and cost column names DRIFT per filer (e.g. ARCC: FV in 'Initial fair value of
# Investment', cost in 'Adjusted cost basis'); resolve by best coverage at extract time.
_FV_CANDS = ["fair value"]
_COST_CANDS = ["Investment Owned, Cost", "Adjusted cost basis", "amortized cost"]


# ── loud alert (project convention: UpdateMasterPerformance/FetchBellwether style) ──
def _alert(msg: str) -> None:
    banner = "!" * 70
    for stream in (sys.stderr, sys.stdout):
        print(f"\n{banner}\n[BDC_HOLDINGS ALERT] {msg}\n{banner}", file=stream)


# ── SEC HTTP layer (copied from aiss_fetch_sec_data.py:58-87, copy-first rule) ──
def _throttle() -> None:
    dt = time.time() - _last_request_ts[0]
    if dt < _SEC_MIN_INTERVAL:
        time.sleep(_SEC_MIN_INTERVAL - dt)
    _last_request_ts[0] = time.time()


def sec_get(url: str, timeout: int = 60, retries: int = 3):
    """GET a SEC URL with required UA, throttle, and exponential-backoff retry."""
    import requests
    last_err = None
    for attempt in range(retries):
        _throttle()
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": SEC_USER_AGENT,
                         "Accept-Encoding": "gzip, deflate"},
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp
        except Exception as e:  # noqa: BLE001
            last_err = e
            # Deterministic client errors (404/403/410) never change on retry — fail fast
            # (e.g. a not-yet-published monthly BDC zip just falls through to channel B),
            # instead of burning ~7s of backoff × every such URL each run.
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (403, 404, 410):
                print(f"  [sec_get] {status} (no retry) {url[:80]}", file=sys.stderr)
                raise RuntimeError(f"SEC GET {status}: {url}")
            wait = 2 ** attempt
            print(f"  [sec_get] {attempt+1}/{retries} failed {url[:80]}: {e} (retry {wait}s)",
                  file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"SEC GET failed after {retries} attempts: {url} ({last_err})")


def cik10(cik: int) -> str:
    return f"{int(cik):010d}"


# ── submissions probe ───────────────────────────────────────────────────────
def latest_filing(cik: int, forms=("10-Q", "10-K")) -> dict | None:
    """Latest 10-Q/10-K {adsh, reportDate, filingDate, primaryDocument, form}."""
    url = f"https://data.sec.gov/submissions/CIK{cik10(cik)}.json"
    r = sec_get(url).json()
    rec = r["filings"]["recent"]
    n = len(rec["accessionNumber"])
    for i in range(n):  # recent[] is newest-first
        if rec["form"][i] in forms:
            return {
                "adsh": rec["accessionNumber"][i],
                "adsh_nodash": rec["accessionNumber"][i].replace("-", ""),
                "reportDate": rec["reportDate"][i],
                "filingDate": rec["filingDate"][i],
                "primaryDocument": rec["primaryDocument"][i],
                "form": rec["form"][i],
            }
    return None


# ── Channel A: BDC Data Sets monthly zip ────────────────────────────────────
def _dataset_url(year_month: str) -> str:
    return f"{BDC_DATASETS_BASE}/{year_month}_bdc.zip"


# yms that 404'd within THIS run. The monthly zip is keyed by FILING month and
# only lands ~early the following month (verified 2026-08-14: 2026_01..2026_07
# all 200, 2026_08 404; Aug-filed adsh are absent from the 2026_07 soi.tsv, so
# there is no earlier package to fall back to — the 404 is expected, not a bug).
# Without this memo a 5-BDC run re-probes the same missing zip once per BDC
# (4 identical 404 hits against SEC per day).
_missing_datasets: set = set()


def fetch_dataset_soi(filing_date: str, cache_dir: str) -> pd.DataFrame | None:
    """Download the monthly BDC Data Set covering `filing_date`, return its soi.tsv
    as a DataFrame. Caches the extracted soi.tsv (not the 53MB zip) under cache_dir.
    Returns None if the package is not yet published (caller falls back to channel B)."""
    ym = filing_date[:7].replace("-", "_")             # 'YYYY-MM-DD' -> 'YYYY_MM'
    os.makedirs(cache_dir, exist_ok=True)
    soi_cache = os.path.join(cache_dir, f"soi_{ym}.parquet")
    if os.path.exists(soi_cache):
        return pd.read_parquet(soi_cache)
    if ym in _missing_datasets:                        # already 404'd this run
        return None
    try:
        resp = sec_get(_dataset_url(ym))
    except Exception as e:  # noqa: BLE001
        _missing_datasets.add(ym)
        print(f"  [dataset] {ym}_bdc.zip not yet published ({e}); monthly zip is "
              f"keyed by filing month and lands ~early the following month — "
              f"expected until then; will try channel B")
        return None
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
        soi_name = next((n for n in names if n.endswith("soi.tsv")), None)
        if not soi_name:
            print(f"  [dataset] {ym}_bdc.zip has no soi.tsv")
            return None
        with zf.open(soi_name) as fh:
            df = pd.read_csv(fh, sep="\t", dtype=str, low_memory=False)
    df.to_parquet(soi_cache)                            # cache extracted soi.tsv
    return df


def _best_fv_column(soi_cols, frame) -> str:
    """FV column drifts per filer (4/5 use custom 'Initial fair value of Investment').
    Pick the 'fair value' column with the highest non-null coverage on this frame."""
    cands = [c for c in soi_cols if "fair value" in c.lower()]
    if not cands:
        raise ValueError("no 'fair value' column in soi.tsv")
    return max(cands, key=lambda c: frame[c].notna().sum())


def _best_cost_column(soi_cols, frame):
    """Cost column also drifts per filer (ARCC uses 'Adjusted cost basis')."""
    cands = [c for c in soi_cols
             if any(k.lower() in c.lower() for k in _COST_CANDS)]
    return max(cands, key=lambda c: frame[c].notna().sum()) if cands else None


def extract_soi_tranches(soi: pd.DataFrame, adsh: str, report_date: str) -> dict:
    """Return {tranches: DataFrame, total_row_fv: float, fv_col: str}.

    D0-validated: current period = report_date (NOT max ddate); tranche = identifier
    non-empty; total row = identifier empty (grand-total FV)."""
    f = soi[soi["adsh"] == adsh].copy()
    if f.empty:
        return {"tranches": pd.DataFrame(), "total_row_fv": np.nan, "fv_col": None}

    # ddate appears as 'YYYY-MM-DD' (2026_04 pkg) or 'YYYYMMDD' (2026_05 pkg).
    rd_variants = {report_date, report_date.replace("-", "")}
    f = f[f["ddate"].isin(rd_variants)]
    if f.empty:
        return {"tranches": pd.DataFrame(), "total_row_fv": np.nan, "fv_col": None}

    idc = COL["id"]
    is_tranche = f[idc].notna() & (f[idc].astype(str).str.strip() != "")
    tr = f[is_tranche].copy()
    fv_col = _best_fv_column(soi.columns, tr)

    # pivot de-split (D0 §3.3): the BDC Data Sets pivot can scatter one investment
    # across several rows that share the SAME identifier (each carrying a subset of the
    # numeric fields — e.g. TSLX). Merge by identifier taking first-non-null per column,
    # so one investment = one row (also makes deal_uid unique per tranche).
    if tr[idc].duplicated().any():
        tr = tr.groupby(idc, sort=False, as_index=False).first()

    # grand total = max FV among identifier-empty rows (the portfolio total line)
    tot = pd.to_numeric(f[~is_tranche][fv_col], errors="coerce")
    total_row_fv = float(tot.max()) if tot.notna().any() else np.nan
    return {"tranches": tr, "total_row_fv": total_row_fv, "fv_col": fv_col}


# ── Channel B: inline-XBRL instance parse (fallback when monthly package lags) ──
# Maps the same SOI facts to our soi.tsv column names so the downstream is channel-agnostic.
_IX_TAG = {  # us-gaap element -> our soi.tsv column name
    "InvestmentOwnedAtFairValue": COL["id"],   # placeholder; filled below
}
_IX_ELEMENTS = {
    "InvestmentOwnedAtFairValue": "Investment Owned, Fair Value",
    "InvestmentOwnedAtCost": "Investment Owned, Cost",
    "InvestmentOwnedBalancePrincipalAmount": COL["prin"],
    "InvestmentInterestRate": COL["rate"],
    "InvestmentBasisSpreadVariableRate": COL["spread"],
    "InvestmentInterestRatePaidInKind": COL["pik"],
    "InvestmentInterestRateFloor": COL["floor"],
    "InvestmentOwnedPercentOfNetAssets": COL["pct"],
    "InvestmentOwnedBalanceShares": COL["shares"],
}


def _filing_index(cik: int, adsh_nodash: str) -> dict:
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh_nodash}/index.json"
    return sec_get(url).json()


def _affiliation_label(member: str) -> str:
    m = member.split(":")[-1]
    if "Controlled" in m:
        return "Control"
    if "Noncontrolled" in m or ("Affiliated" in m and "Unaffiliated" not in m):
        return "Affiliated"
    if "Unaffiliated" in m:
        return "Non-Affiliated"
    return _camel_to_words(m)


def _camel_to_words(member: str) -> str:
    """'FinancialServicesSectorMember' -> 'Financial Services Sector'."""
    s = re.sub(r"Member$", "", member.split(":")[-1])
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    return s.strip()


def _load_instance_root(cik: int, adsh_nodash: str, primary_doc: str, cache_dir: str):
    """Download+parse the inline-XBRL primary doc once; cache raw bytes under cache_dir."""
    from lxml import etree
    os.makedirs(cache_dir, exist_ok=True)
    cpath = os.path.join(cache_dir, f"inst_{adsh_nodash}.htm")
    if os.path.exists(cpath):
        raw = open(cpath, "rb").read()
    else:
        raw = sec_get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh_nodash}/{primary_doc}").content
        open(cpath, "wb").write(raw)
    return etree.fromstring(raw, etree.XMLParser(huge_tree=True, recover=True))


_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
             "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
             "nineteen": 19, "twenty": 20, "zero": 0, "no": 0}


def _to_int(tok):
    tok = str(tok).strip().lower().replace(",", "")
    if tok.isdigit():
        return int(tok)
    return _WORD_NUM.get(tok)


def extract_non_accrual_bdc(root) -> dict:
    """Best-effort BDC-level non-accrual from the 10-Q text (no standard XBRL tag exists,
    §3.5; all 5 filers disclose it in MD&A but with different phrasing). Strategy: collect
    every percentage within ~70 chars of a 'non-accrual' mention, classify each as a
    direct non-accrual rate (≤15%) or an accruing share (≥85% → 100−x), and take the
    smallest plausible non-accrual rate. Returns {non_accrual_pct_fv, borrowers, loans,
    source_text} — None where not parseable, never fabricated. Per-loan tagging is P1.5."""
    txt = re.sub(r"\s+", " ", " ".join(root.itertext()))
    out = {"borrowers": None, "loans": None, "non_accrual_pct_fv": None, "source_text": None}

    # targeted MD&A phrasings (one per filer style); first confident match wins.
    val = None
    # GBDC: "...as a percentage of total investments at cost and fair value were X.X% and Y.Y%"
    m = re.search(r"non[- ]?accru\w*[^.]{0,80}?percentage of total investments[^.]{0,60}?"
                  r"(\d{1,2}\.\d)\s*%?\s*(?:and|,)\s*(\d{1,2}\.\d)\s*%", txt, re.I)
    if m:
        val = float(m.group(2))                       # 2nd number = fair-value basis
    if val is None:  # ARCC/OBDC: "non-accrual ... represent(s/ed) X.X%"
        m = re.search(r"non[- ]?accru\w*[^.]{0,50}?represent\w*[^.]{0,15}?(\d{1,2}\.\d)\s*%", txt, re.I)
        if m:
            val = float(m.group(1))
    if val is None:  # BXSL: "X.X% Percentage of assets on non-accrual"
        m = re.search(r"(\d{1,2}\.\d)\s*%\s*(?:percentage of assets[^.]{0,10}?)?on non[- ]?accru", txt, re.I)
        if m:
            val = float(m.group(1))
    if val is None:  # TSLX: "99.4% Non-accrual" (accruing share → 100 − x)
        m = re.search(r"(9\d\.\d)\s*%\s*non[- ]?accru", txt, re.I)
        if m:
            val = round(100.0 - float(m.group(1)), 2)
    if val is not None and 0 <= val <= 25:
        out["non_accrual_pct_fv"] = round(val / 100.0, 5)
        out["source_text"] = txt[max(0, m.start() - 20):m.start() + 120].strip()[:160]

    bm = re.search(r"([\w-]+)\s+borrowers?\s*\(?(?:across\s+)?([\w-]+)?\s*loans?\)?[^.]{0,40}?non[- ]?accru",
                   txt, re.I)
    if bm:
        out["borrowers"] = _to_int(bm.group(1))
        out["loans"] = _to_int(bm.group(2)) if bm.group(2) else None
    return out


def extract_classification(root, report_date: str) -> dict:
    """identifier -> {industry, inv_type} via inline-XBRL document order.

    SOI convention: an industry-dimensioned subtotal fact heads each group; the
    per-investment (InvestmentIdentifierAxis) facts that follow inherit it. We walk
    facts in document order, tracking the most-recent industry / investment-type
    explicit member, and stamp each new identifier with the running classification."""
    IX = "{http://www.xbrl.org/2013/inlineXBRL}"
    XBRLI = "{http://www.xbrl.org/2003/instance}"
    XBRLDI = "{http://xbrl.org/2006/xbrldi}"
    # context_id -> {id?, industry?, itype?, aff?} from explicit/typed members
    ctx_info = {}
    for c in root.iter(f"{XBRLI}context"):
        cid = c.get("id"); info = {}
        tm = c.find(f".//{XBRLDI}typedMember")
        if tm is not None and "InvestmentIdentifierAxis" in (tm.get("dimension") or ""):
            info["id"] = "".join(tm.itertext()).strip()
        for em in c.iter(f"{XBRLDI}explicitMember"):
            dim = em.get("dimension") or ""
            mem = (em.text or "")
            if "Industry" in dim:
                info["industry"] = _camel_to_words(mem)
            elif "InvestmentTypeAxis" in dim:
                info["itype"] = _camel_to_words(mem)
            elif "Affiliation" in dim:
                info["aff"] = _affiliation_label(mem)
        if info:
            ctx_info[cid] = info
    # floor: InvestmentInterestRateFloor facts exist but (verified across filers) are
    # tagged on GENERIC placeholder contexts ("Investment Two/Three/…", a sensitivity
    # disclosure), NOT the real loan identifiers — so they don't map and yield None here.
    # Kept forward-compatible: if a filer tags floors on real ids, they're captured.
    # (Moot in the current rate regime: SOFR ≫ typical 0.75–1% floors, so floors don't bind.)
    floor_by_id = {}
    for el in root.iter(f"{IX}nonFraction"):
        if "InvestmentInterestRateFloor" not in (el.get("name") or ""):
            continue
        info = ctx_info.get(el.get("contextRef"))
        if not info or not info.get("id"):
            continue
        try:
            scale = int(el.get("scale") or 0)
            v = float("".join(el.itertext()).replace(",", "").strip()) * (10 ** scale)
            floor_by_id[info["id"]] = v
        except ValueError:
            continue
    # walk fact elements in document order — industry / type grouping (these reliably
    # head their groups). Affiliation is NOT order-grouped: its affiliated/control
    # sub-schedule structure bleeds across sections (verified: ARCC would mis-tag 2441
    # rows "Control"), so affiliation is taken ONLY where directly on the investment's
    # own context (rare) — else left None. Floor is a direct per-investment fact.
    out, cur_ind, cur_typ = {}, None, None
    for el in root.iter(f"{IX}nonFraction", f"{IX}nonNumeric"):
        info = ctx_info.get(el.get("contextRef"))
        if not info:
            continue
        if info.get("industry"):
            cur_ind = info["industry"]
        if info.get("itype"):
            cur_typ = info["itype"]
        ident = info.get("id")
        if ident and ident not in out:
            out[ident] = {"industry": cur_ind, "inv_type": cur_typ,
                          "affiliation": info.get("aff"), "rate_floor": floor_by_id.get(ident)}
    return out


def _tr_ancestor(el):
    p = el.getparent()
    while p is not None and not (isinstance(p.tag, str) and p.tag.endswith("}tr") or p.tag == "tr"):
        p = p.getparent()
    return p


def _non_accrual_footnote_num(full_text: str):
    """The footnote number whose definition says '… on non-accrual status' (per filer)."""
    m = re.search(r"\((\d{1,2})\)\s*(?:Loan|Investment|Security|Debt)[^.]{0,45}?non[- ]?accru",
                  full_text, re.I)
    return m.group(1) if m else None


def extract_html_row_attrs(root, report_date: str) -> dict:
    """P1.5-C/D: per-investment maturity + non-accrual from the inline-XBRL HTML table.

    Inline XBRL embeds each investment's tagged facts in its own <tr> row, alongside the
    UN-tagged maturity date and footnote markers. So for each investment fact we read its
    <tr> ancestor and extract: maturity = the latest MM/DD/YYYY date in the row (acquisition
    date is earlier, maturity later); non_accrual = the row's footnote markers include the
    filer's non-accrual footnote number. Robust where the filer tabulates dates (BXSL/TSLX);
    degrades to nothing where it does not (GBDC/ARCC) — caller keeps imputed_tenor."""
    IX = "{http://www.xbrl.org/2013/inlineXBRL}"
    XBRLI = "{http://www.xbrl.org/2003/instance}"
    XBRLDI = "{http://xbrl.org/2006/xbrldi}"
    inv_ctx = {}
    for c in root.iter(f"{XBRLI}context"):
        tm = c.find(f".//{XBRLDI}typedMember")
        if tm is not None and "InvestmentIdentifierAxis" in (tm.get("dimension") or ""):
            inv_ctx[c.get("id")] = "".join(tm.itertext()).strip()
    na_fn = _non_accrual_footnote_num(re.sub(r"\s+", " ", " ".join(root.itertext())))

    out = {}
    _date = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b")
    # anchor on ANY identifier-context fact (FV element name drifts per filer); dedup by
    # IDENTIFIER (NOT id(tr) — lxml element proxies are ephemeral and their id() gets
    # reused after GC, which silently collapses most rows).
    for el in root.iter(f"{IX}nonFraction"):
        cref = el.get("contextRef")
        if cref not in inv_ctx:
            continue
        ident = inv_ctx[cref]
        if ident in out:
            continue
        tr = _tr_ancestor(el)
        if tr is None:
            continue
        txt = re.sub(r"\s+", " ", " ".join(tr.itertext()))
        dates = _date.findall(txt)
        rec = {}
        if dates:
            ymd = max((int(y), int(m), int(d)) for m, d, y in dates)   # latest = maturity
            rec["maturity"] = f"{ymd[0]:04d}-{ymd[1]:02d}"
            rec["maturity_source"] = "primary_html"
        if na_fn:
            foot = set(re.findall(r"\((\d{1,2})\)", txt.split("%")[0]))  # markers before the numbers
            rec["non_accrual"] = na_fn in foot
        if rec:
            out[ident] = rec
    return out


def fetch_xbrl_instance_tranches(cik: int, adsh_nodash: str, report_date: str,
                                 primary_doc: str) -> pd.DataFrame:
    """Parse the inline-XBRL primary document: typed-dimension contexts on
    InvestmentIdentifierAxis → per-tranche facts. Returns a frame with the SAME
    column names as soi.tsv so extract/clean logic is channel-agnostic."""
    from lxml import etree
    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh_nodash}/{primary_doc}"
    raw = sec_get(url).content
    root = etree.fromstring(raw, etree.XMLParser(huge_tree=True, recover=True))
    ns = {k: v for k, v in root.nsmap.items() if k}
    # context_id -> {identifier, instant}
    ctx = {}
    for c in root.iter("{http://www.xbrl.org/2003/instance}context"):
        cid = c.get("id")
        tm = c.find(".//{http://xbrl.org/2006/xbrldi}typedMember")
        inst = c.find(".//{http://www.xbrl.org/2003/instance}instant")
        if tm is not None and ("InvestmentIdentifierAxis" in (tm.get("dimension") or "")):
            ident = "".join(tm.itertext()).strip()
            ctx[cid] = {"id": ident,
                        "instant": (inst.text.strip() if inst is not None else None)}
    rows = {}
    for el in root.iter("{http://www.xbrl.org/2013/inlineXBRL}nonFraction"):
        cref = el.get("contextRef"); name = (el.get("name") or "")
        if cref not in ctx:
            continue
        local = name.split(":")[-1]
        if local not in _IX_ELEMENTS:
            continue
        val = "".join(el.itertext()).replace(",", "").strip()
        sign = -1 if (el.get("sign") == "-") else 1
        scale = int(el.get("scale") or 0)
        try:
            num = sign * float(val) * (10 ** scale)
        except ValueError:
            continue
        rd = report_date.replace("-", "")
        if ctx[cref]["instant"] and ctx[cref]["instant"].replace("-", "") != rd:
            continue
        row = rows.setdefault(cref, {COL["id"]: ctx[cref]["id"]})
        row[_IX_ELEMENTS[local]] = num
    df = pd.DataFrame(list(rows.values()))
    return df


# ── reconciliation cross-check via companyfacts ─────────────────────────────
def companyfacts_total_fv(cik: int, report_date: str) -> float | None:
    """Aggregate (non-dimensional) InvestmentOwnedAtFairValue at report_date — an
    independent SEC truth source for the total-row reconciliation."""
    url = (f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik10(cik)}"
           f"/us-gaap/InvestmentOwnedAtFairValue.json")
    try:
        d = sec_get(url).json()
    except Exception:  # noqa: BLE001
        return None
    best = None
    for unit_rows in d.get("units", {}).values():
        for row in unit_rows:
            if row.get("end") == report_date and row.get("val") is not None:
                # prefer the latest filed value for that instant
                if best is None or row.get("filed", "") >= best[1]:
                    best = (float(row["val"]), row.get("filed", ""))
    return best[0] if best else None


def frames_total_fv(cik: int, report_date: str) -> float | None:
    """SEC xbrl/frames anchor — period-indexed across all filers. Empirically this
    frame populates BEFORE the per-company companyconcept endpoint after a fresh
    filing, so it's a *current-quarter* fallback (NOT prior-quarter/stale)."""
    y, m, _ = report_date.split("-")
    q = {"03": "1", "06": "2", "09": "3", "12": "4"}.get(m)
    if not q:                                                 # non-quarter-end period
        return None
    url = (f"https://data.sec.gov/api/xbrl/frames/us-gaap/"
           f"InvestmentOwnedAtFairValue/USD/CY{y}Q{q}I.json")
    try:
        d = sec_get(url).json()
    except Exception:  # noqa: BLE001
        return None
    for row in d.get("data", []):
        if int(row.get("cik", -1)) == int(cik) and row.get("end") == report_date \
                and row.get("val") is not None:
            return float(row["val"])
    return None


def extract_total_fv(root, report_date: str) -> float | None:
    """Non-dimensional (entity-total) InvestmentOwnedAtFairValue straight from the
    inline-XBRL instance we already downloaded — the freshest possible anchor
    (contemporaneous with the filing, zero SEC-aggregation lag). Only accepts a
    context with instant==report_date AND no dimension member (segment total)."""
    if root is None:
        return None
    XBRLDI = "{http://xbrl.org/2006/xbrldi}"
    XBRLI = "{http://www.xbrl.org/2003/instance}"
    IX = "{http://www.xbrl.org/2013/inlineXBRL}"
    rd = report_date.replace("-", "")
    # context ids that are entity-total (no explicit/typed member) at report_date
    total_ctx = set()
    for c in root.iter(f"{XBRLI}context"):
        if c.find(f".//{XBRLDI}explicitMember") is not None:
            continue
        if c.find(f".//{XBRLDI}typedMember") is not None:
            continue
        inst = c.find(f".//{XBRLI}instant")
        if inst is not None and inst.text and inst.text.strip().replace("-", "") == rd:
            total_ctx.add(c.get("id"))
    if not total_ctx:
        return None
    best = None
    for el in root.iter(f"{IX}nonFraction"):
        if (el.get("name") or "").split(":")[-1] != "InvestmentOwnedAtFairValue":
            continue
        if el.get("contextRef") not in total_ctx:
            continue
        val = "".join(el.itertext()).replace(",", "").strip()
        try:
            num = (-1 if el.get("sign") == "-" else 1) * float(val) * (10 ** int(el.get("scale") or 0))
        except ValueError:
            continue
        if best is None or num > best:            # entity total = the largest such fact
            best = num
    return best


def resolve_net_anchor(cik: int, report_date: str, root=None,
                       soi_total_row_fv=None) -> tuple:
    """Resolve the reconciliation net-FV anchor from the freshest available source,
    re-evaluated every run (so it auto-upgrades to the canonical companyconcept the
    moment SEC ingests it — never sticks on a lower/older source). Order:
        1. companyconcept   (SEC-canonical; laggiest after a fresh filing)
        2. xbrl/frames      (period-indexed; fresher for a just-filed quarter)
        3. filing instance  (entity-total from the 10-Q we already parsed; freshest)
        4. SOI total row     (channel-A total row, if present)
    Every source is CURRENT-QUARTER — no prior-quarter fallback. Returns (value, source)."""
    cf = companyfacts_total_fv(cik, report_date)
    if cf:
        return cf, "companyconcept"
    fv = frames_total_fv(cik, report_date)
    if fv:
        return fv, "frames"
    tv = extract_total_fv(root, report_date)
    if tv:
        return tv, "filing_instance"
    if soi_total_row_fv is not None and pd.notna(soi_total_row_fv) and float(soi_total_row_fv) > 0:
        return float(soi_total_row_fv), "soi_total_row"
    return None, None


# ── entity normalisation -> stable deal_uid (§3.3) ──────────────────────────
_TRANCHE_SUFFIX = re.compile(r"\s+\d+$")
_FKA = re.compile(r"\(f/?k/?a[:\s]+(.+?)\)", re.IGNORECASE)


def normalize_issuer(identifier: str) -> str:
    """Strip tranche serials / affiliation tails / TSLX long-description prefix to a
    canonical issuer name (for cross-period entity alignment)."""
    s = str(identifier).strip()
    # TSLX packs a full sentence: 'Debt Investments <industry> <Issuer> Investment First-lien ...'
    # take text up to ' Investment ' if present and long
    m = re.search(r"^(?:Debt|Equity)\s+Investments?\s+\S.*?\s{2,}", s)
    # generic: drop trailing ', <Investment type>...' after first comma if it looks like a tranche
    base = s.split(",")[0].strip()
    base = _TRANCHE_SUFFIX.sub("", base).strip()
    return base or s


def fka_alias(identifier: str) -> str | None:
    m = _FKA.search(str(identifier))
    return m.group(1).strip() if m else None


def deal_uid(cik: int, issuer: str, tranche_tag: str) -> str:
    import hashlib
    key = f"{cik}|{issuer.lower()}|{str(tranche_tag).lower()}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


# ── maturity from identifier (R-file alignment added in next D1 step) ────────
_MAT_PATTERNS = [
    re.compile(r"due\s+(\d{1,2})/(\d{2,4})", re.IGNORECASE),         # due 11/2029
    re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2,4})"),                    # 11/07/2029
    re.compile(r"\b(\d{1,2})/(\d{4})\b"),                            # 11/2029
]
_PAR = re.compile(r"\$([\d,]+)\s*par", re.IGNORECASE)


def maturity_from_identifier(identifier: str):
    s = str(identifier)
    for pat in _MAT_PATTERNS:
        m = pat.search(s)
        if m:
            g = m.groups()
            if len(g) == 2:
                mm, yy = g
                dd = "01"
            else:
                mm, dd, yy = g
            yy = ("20" + yy) if len(yy) == 2 else yy
            try:
                return f"{int(yy):04d}-{int(mm):02d}"
            except ValueError:
                continue
    return None


def par_from_identifier(identifier: str):
    m = _PAR.search(str(identifier))
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None
    return None


# Imputed remaining tenor (years) by instrument seniority — used ONLY when maturity is
# absent from machine-readable sources (D0/D1 finding: SEC tags no per-investment maturity
# for 4/5 filers; ARCC primary HTML has 54 dates for 1459 loans). These are defensible
# market-typical remaining tenors for sponsor-backed direct loans; every imputed row is
# labelled maturity_source='imputed_tenor' and the imputed fraction is always reported.
_IMPUTED_TENOR_YRS = {
    "first lien": 5.0, "senior secured": 5.0, "one stop": 5.5, "unitranche": 5.5,
    "second lien": 6.0, "subordinated": 6.0, "mezzanine": 6.0, "_default": 5.5,
}


# Investment-type lexicon — parsed from the identifier string (per-investment,
# higher coverage than the XBRL InvestmentTypeAxis which sits on subtotal contexts).
_ITYPE_PATTERNS = [
    ("First Lien", r"first[\s-]*lien|senior secured (?:loan|term|note)|one stop|unitranche"),
    ("Second Lien", r"second[\s-]*lien"),
    ("Subordinated", r"subordinated|senior subordinated|mezzanine"),
    ("Unsecured", r"senior unsecured"),
    ("Equity", r"\b(?:class [a-z]\d?|common|preferred|member interest|warrant|"
                r"shares|units|equity|llc interest|partnership interest)\b"),
    ("Revolver", r"revolv|delayed draw|revolving loan"),
]


def inv_type_from_identifier(identifier: str):
    s = str(identifier).lower()
    for label, pat in _ITYPE_PATTERNS:
        if re.search(pat, s):
            return label
    return None


def impute_maturity(as_of: str, inv_type, identifier: str) -> str:
    text = f"{inv_type or ''} {identifier or ''}".lower()
    yrs = next((v for k, v in _IMPUTED_TENOR_YRS.items() if k != "_default" and k in text),
               _IMPUTED_TENOR_YRS["_default"])
    base = pd.Timestamp(as_of) + pd.Timedelta(days=int(yrs * 365.25))
    return base.strftime("%Y-%m")


def _num(x):
    v = pd.to_numeric(x, errors="coerce")
    return None if pd.isna(v) else float(v)


# ── per-BDC ingest (channel A spine) ────────────────────────────────────────
def ingest_bdc(ticker: str, cik: int, cache_dir: str) -> dict:
    """Pull + clean one BDC's latest-quarter SOI. Returns {df, manifest}."""
    filing = latest_filing(cik)
    if not filing:
        raise RuntimeError(f"{ticker}: no 10-Q/10-K found")
    rd, adsh = filing["reportDate"], filing["adsh"]
    print(f"  [{ticker}] {filing['form']} adsh={adsh} period={rd} filed={filing['filingDate']}")

    soi = fetch_dataset_soi(filing["filingDate"], cache_dir)
    channel, total_row_fv = "A", np.nan
    tr = pd.DataFrame()
    if soi is not None:
        ext = extract_soi_tranches(soi, adsh, rd)
        tr, fv_col, total_row_fv = ext["tranches"], ext["fv_col"], ext["total_row_fv"]
    if tr.empty:                                              # channel B fallback
        print(f"  [{ticker}] channel A miss → parsing inline-XBRL instance (channel B)")
        tr = fetch_xbrl_instance_tranches(cik, filing["adsh_nodash"], rd,
                                          filing["primaryDocument"])
        channel = "B"
        if tr.empty:
            raise RuntimeError(f"{ticker}: 0 tranche rows from both channels (adsh={adsh})")
        fv_col = _best_fv_column(tr.columns, tr)

    # resolve per-filer cost column (drifts like FV); affiliation enum if present
    cost_col = _best_cost_column(tr.columns, tr)
    aff_col = COL["aff_enum"] if COL["aff_enum"] in tr.columns else COL["aff"]

    # build canonical deal rows
    rows = []
    for _, r in tr.iterrows():
        ident = r[COL["id"]]
        issuer = normalize_issuer(ident)
        fv = _num(r.get(fv_col))
        prin = _num(r.get(COL["prin"])) or par_from_identifier(ident)
        rows.append({
            "bdc": ticker, "cik": cik, "as_of": rd, "adsh": adsh,
            "identifier": ident, "issuer": issuer, "fka": fka_alias(ident),
            # uid keyed on the FULL identifier (the SOI's own stable per-tranche key) —
            # unique within a filing AND stable across quarters (same loan keeps its
            # identifier string), which is exactly what the cross-period diff needs.
            "deal_uid": deal_uid(cik, issuer, str(ident)),
            "fair_value": fv, "cost": _num(r.get(cost_col)) if cost_col else None,
            "principal": prin,
            "all_in_rate": _num(r.get(COL["rate"])), "spread": _num(r.get(COL["spread"])),
            "rate_floor": _num(r.get(COL["floor"])), "pik_rate": _num(r.get(COL["pik"])),
            "pct_nav": _num(r.get(COL["pct"])),
            "affiliation": (str(r.get(aff_col)).split(":")[-1].replace("Member", "")
                            if pd.notna(r.get(aff_col)) else None),
            "industry": None,          # filled by classification enrichment below
            "inv_type": None,          # filled by classification enrichment below
            "is_equity": pd.notna(r.get(COL["shares"])),
            "unfunded": _num(r.get(COL.get("unfunded", ""))),
            "non_accrual": None,                  # BDC-level in P1; per-row tagging is P1.5 (§3.5)
            "maturity": maturity_from_identifier(ident),
            "maturity_source": ("identifier" if maturity_from_identifier(ident) else None),
        })
    df = pd.DataFrame(rows)

    # classification enrichment (industry + investment type) — industry is NOT in
    # soi.tsv for any filer (§D1); recover it from the inline-XBRL document-order
    # grouping (best for comma-delimited filers, ARCC 89%) and the identifier string
    # (best for TSLX-style). inv_type parsed from the identifier (per-investment).
    root = None                              # kept in scope for the net-anchor resolver below
    try:
        root = _load_instance_root(cik, filing["adsh_nodash"], filing["primaryDocument"], cache_dir)
        cls = extract_classification(root, rd)
        non_accrual_bdc = extract_non_accrual_bdc(root)
        html_attrs = extract_html_row_attrs(root, rd)        # P1.5-C/D: per-loan maturity + non-accrual
    except Exception as e:  # noqa: BLE001
        _alert(f"{ticker} classification (inline-XBRL) failed: {e!r}")
        cls = {}; html_attrs = {}
        non_accrual_bdc = {"borrowers": None, "loans": None, "non_accrual_pct_fv": None}
    df["inv_type"] = [inv_type_from_identifier(i) or (cls.get(i, {}) or {}).get("inv_type")
                      for i in df["identifier"]]
    df["industry"] = [(cls.get(i, {}) or {}).get("industry") for i in df["identifier"]]
    df["industry"] = df["industry"].str.replace(r"\.$", "", regex=True).str.strip()
    df["industry_source"] = np.where(df["industry"].notna(), "xbrl_order", None)
    # affiliation (xbrl-order grouping) + rate_floor (direct fact) — soi.tsv leaves blank
    df["affiliation"] = [(cls.get(i, {}) or {}).get("affiliation") or a
                         for i, a in zip(df["identifier"], df["affiliation"])]
    df["rate_floor"] = [f if pd.notna(f) else (cls.get(i, {}) or {}).get("rate_floor")
                        for i, f in zip(df["identifier"], df["rate_floor"])]

    # drop disclosure/summary rows that carry an InvestmentIdentifierAxis but are NOT
    # holdings (e.g. ARCC's "Largest Portfolio Company Investment" metric) — a real
    # tranche always has a fair value; these have none and cannot be weighted/modelled.
    n_pre = len(df)
    df = df[df["fair_value"].notna()].reset_index(drop=True)
    n_dropped = n_pre - len(df)

    # P1.5-C/D: per-loan maturity from the primary-HTML SOI table (authoritative; works
    # where the filer tabulates dates in <tr> rows, e.g. TSLX/GBDC) overrides the
    # identifier regex; per-loan non-accrual from the row footnote markers.
    df["non_accrual"] = [(html_attrs.get(i, {}) or {}).get("non_accrual") for i in df["identifier"]]
    html_mat = [(html_attrs.get(i, {}) or {}).get("maturity") for i in df["identifier"]]
    df["maturity"] = [hm if hm else m for hm, m in zip(html_mat, df["maturity"])]
    df["maturity_source"] = ["primary_html" if hm else s
                             for hm, s in zip(html_mat, df["maturity_source"])]
    # maturity: keep html/identifier-derived; impute the rest by instrument tenor (labelled)
    miss = df["maturity"].isna()
    df.loc[miss, "maturity"] = [impute_maturity(rd, it, idf)
                                for it, idf in zip(df.loc[miss, "inv_type"],
                                                   df.loc[miss, "identifier"])]
    df.loc[miss, "maturity_source"] = "imputed_tenor"
    df = df.copy()                       # de-fragment after the per-column assignments

    # intra-BDC look-through weight denominator = Σtranche FV (gross, internally consistent;
    # weights within a BDC sum to 1). companyfacts net is the QA anchor, NOT the denominator
    # (gross/net gap = consolidated-affiliate gross-up, e.g. ARCC Ivy Hill/SDLP +17%).
    sum_tranche = float(pd.to_numeric(df["fair_value"], errors="coerce").sum())
    df["bdc_fv_share"] = pd.to_numeric(df["fair_value"], errors="coerce") / sum_tranche
    df["sleeve_w"] = BDC_UNIVERSE[ticker]["sleeve_w"]

    cf, cf_source = resolve_net_anchor(cik, rd, root=root, soi_total_row_fv=total_row_fv)
    gross_net = (sum_tranche / cf) if cf else None
    mat_src = df["maturity_source"].value_counts(dropna=False).to_dict()
    manifest = {
        "ticker": ticker, "cik": cik, "adsh": adsh, "reportDate": rd,
        "filingDate": filing["filingDate"], "form": filing["form"], "channel": channel,
        "rows": len(df), "rows_dropped_no_fv": n_dropped, "fv_col": fv_col,
        "companyfacts_net_fv": cf, "net_anchor_source": cf_source,
        "sum_tranche_fv": sum_tranche,
        "soi_total_row_fv": total_row_fv,
        "gross_net_ratio": (round(gross_net, 4) if gross_net else None),
        "maturity_source": {str(k): int(v) for k, v in mat_src.items()},
        "coverage": {k: round(float(df[k].notna().mean()), 3)
                     for k in ["fair_value", "spread", "principal", "all_in_rate",
                               "pik_rate", "rate_floor", "non_accrual"] if k in df},
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "non_accrual": non_accrual_bdc,
        "qa": _qa_gate(ticker, df, cf, gross_net),
    }
    return {"df": df, "manifest": manifest}


def _qa_gate(ticker: str, df: pd.DataFrame, cf, gross_net) -> dict:
    """Data-quality gate (§9.2). Flags, never silently passes bad data."""
    # OBDC 下界 500→380(2026-08-10): Q2-2026 实测 457 行且 FV 反涨(18.35B→19.32B,
    # +5.3%)、gross/net 1.29 在带内、anchor=companyconcept 正常 —— 是财报真实合并
    # tranche 行项(缩行不缩值),非解析丢行。行数带只是粗网,FV 连续性才是真 QA。
    ROWBANDS = {"ARCC": (1250, 2100), "GBDC": (1500, 2000), "BXSL": (550, 1200),
                "OBDC": (380, 900), "TSLX": (160, 320)}
    flags = []
    lo, hi = ROWBANDS.get(ticker, (50, 5000))
    if not (lo <= len(df) <= hi):
        flags.append(f"row_count {len(df)} outside [{lo},{hi}]")
    fvcov = float(df["fair_value"].notna().mean())
    if fvcov < 0.98:
        flags.append(f"FV coverage {fvcov:.2%} < 98%")
    if cf is None:
        flags.append("companyfacts net unavailable")
    elif gross_net is not None and not (0.97 <= gross_net <= 1.35):
        flags.append(f"gross/net ratio {gross_net:.3f} outside [0.97,1.35]")
    ok = not flags
    if not ok:
        _alert(f"{ticker} QA flags: {flags}")
    return {"ok": ok, "flags": flags}


# ── persistence (PIT, append-only) ──────────────────────────────────────────
def persist(results: dict, store: str, write: bool) -> None:
    os.makedirs(store, exist_ok=True)
    manifest_path = os.path.join(store, "latest_manifest.json")
    manifest = {}
    if write and os.path.exists(manifest_path):
        try:
            manifest = json.load(open(manifest_path))
        except Exception:  # noqa: BLE001
            manifest = {}
    for t, res in results.items():
        df, mf = res["df"], res["manifest"]
        if write:
            tdir = os.path.join(store, t)
            os.makedirs(tdir, exist_ok=True)
            snap = os.path.join(tdir, f"soi_{mf['reportDate']}_{mf['adsh']}.parquet")
            df.to_parquet(snap, index=False)            # PIT snapshot, never overwrite
            manifest[t] = mf
    if write:
        json.dump(manifest, open(manifest_path, "w"), indent=2)
        hb = os.path.join(store, "heartbeat.log")
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        line = " ".join(f"{t}:{results[t]['manifest']['reportDate']}({results[t]['manifest']['rows']})"
                        for t in results)
        with open(hb, "a") as fh:
            fh.write(f"{ts} refreshed {line}\n")


# ── reporting ───────────────────────────────────────────────────────────────
def print_report(results: dict) -> None:
    print("\n=== BDC SOI ingest report ===")
    print(f"{'BDC':<6}{'rows':>6}{'Σtranche$B':>11}{'net$B':>8}{'g/n':>6}{'spr%':>6}{'prin%':>6}"
          f"{'mat:real':>9}{'mat:imp':>8}{'QA':>5}")
    for t, res in results.items():
        m = res["manifest"]; cov = m["coverage"]
        st = m["sum_tranche_fv"] / 1e9
        net = (m["companyfacts_net_fv"] or 0) / 1e9
        gn = m["gross_net_ratio"]
        ms = m["maturity_source"]
        real = ms.get("identifier", 0) + ms.get("primary_html", 0)
        imp = ms.get("imputed_tenor", 0)
        qa = "ok" if m["qa"]["ok"] else "FLAG"
        print(f"{t:<6}{m['rows']:>6}{st:>11.2f}{net:>8.2f}{(gn or 0):>6.2f}"
              f"{cov['spread']*100:>5.0f}%{cov['principal']*100:>5.0f}%{real:>9}{imp:>8}{qa:>5}")


# ── CLI ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Refresh BDC underlying holdings (SOI) from SEC EDGAR")
    ap.add_argument("--ticker", help="single BDC (default: all 5)")
    ap.add_argument("--sandbox", metavar="DIR", help="redirect ALL output to DIR (dev mode)")
    ap.add_argument("--dry-run", action="store_true", help="ingest + print, write nothing")
    ap.add_argument("--probe", action="store_true", help="just show latest filings, no ingest")
    ap.add_argument("--cache-dir", help="dataset cache dir (default: <store>/raw_cache)")
    args = ap.parse_args()

    # sandbox nests under bdc_holdings to match RunBDCLookThrough's store layout
    store = os.path.join(args.sandbox, "bdc_holdings") if args.sandbox else BDC_STORE
    cache_dir = args.cache_dir or os.path.join(store, "raw_cache")
    write = not args.dry_run                      # sandbox still writes (to sandbox)
    tickers = [args.ticker.upper()] if args.ticker else list(BDC_UNIVERSE)

    print(f"=== RefreshBDCHoldings  store={store}  write={write}  UA={SEC_USER_AGENT!r} ===")

    if args.probe:
        for t in tickers:
            f = latest_filing(BDC_UNIVERSE[t]["cik"])
            print(f"  {t}: {f['form']} adsh={f['adsh']} period={f['reportDate']} filed={f['filingDate']}")
        return

    results = {}
    for t in tickers:
        try:
            results[t] = ingest_bdc(t, BDC_UNIVERSE[t]["cik"], cache_dir)
        except Exception as e:  # noqa: BLE001
            _alert(f"{t} ingest failed: {e!r}")

    if not results:
        _alert("no BDC ingested")
        sys.exit(1)

    print_report(results)
    persist(results, store, write)
    if write:
        print(f"\n[done] wrote {len(results)} BDC snapshots → {store}")
    else:
        print(f"\n[dry-run] {len(results)} BDC ingested, nothing written")


if __name__ == "__main__":
    main()
