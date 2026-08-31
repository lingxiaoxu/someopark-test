"""
AEUS SEC EDGAR fetcher (HTTP I/O layer)
=======================================
Adapted from the project's ``MRPTFetchEarnings.py`` ``acceptance_datetime``
pattern (which is read-only and unchanged).  This module is the **HTTP I/O +
generic XBRL/submission parsing** layer used by ``industry_signals.py`` (ASML
quarterly net bookings) and ``company_signals.py`` (MU inventory/COGS for DIO).

It deliberately contains NO strategy logic — just:
    * dynamic CIK resolution from SEC's official ticker map (cached)
    * submissions enumeration (recent + paged history) by form type
    * XBRL ``companyfacts`` retrieval + concept extraction (PIT ``filed`` dates)
    * filing primary-document download (raw HTML/text)
    * the BMO/AMC ``acceptance_datetime`` reaction-date helper

SEC compliance
--------------
SEC requires a descriptive ``User-Agent`` with a contact and recommends
< 10 requests/second.  We send a UA and throttle to be a good citizen.

All data is fetched read-only over the network; this module writes nothing to
disk (caching of the CIK map is in-memory per process).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

log = logging.getLogger("aeus.sec")

# SEC wants a real contact in the UA. Override via env if desired.
import os as _os
SEC_USER_AGENT = _os.environ.get(
    "AEUS_SEC_USER_AGENT",
    "someopark-research AEUS-data admin@someopark.test",
)
_SEC_MIN_INTERVAL = 0.15   # seconds between SEC requests (~6.7 req/s, < 10 limit)
_last_request_ts = [0.0]

# Verified CIKs (looked up from SEC company_tickers.json on 2026-05-29).
# Used as fallback if the live ticker-map lookup fails.
_VERIFIED_CIK: Dict[str, int] = {
    "ASML": 937966,
    "MU": 723125,
    "NVDA": 1045810,
    "TSM": 1046179,
    "AVGO": 1730168,
    "AMD": 2488,
    "INTC": 50863,
}

_CIK_MAP_CACHE: Optional[Dict[str, int]] = None


def _throttle() -> None:
    dt = time.time() - _last_request_ts[0]
    if dt < _SEC_MIN_INTERVAL:
        time.sleep(_SEC_MIN_INTERVAL - dt)
    _last_request_ts[0] = time.time()


def sec_get(url: str, timeout: int = 45, retries: int = 3):
    """GET a SEC URL with the required UA, throttling, and retry. Returns Response."""
    import requests
    last_err = None
    for attempt in range(retries):
        _throttle()
        try:
            resp = requests.get(url, headers={"User-Agent": SEC_USER_AGENT,
                                              "Accept-Encoding": "gzip, deflate"},
                                timeout=timeout)
            resp.raise_for_status()
            return resp
        except Exception as e:  # noqa: BLE001
            last_err = e
            wait = 2 ** attempt
            log.warning("SEC GET failed (%d/%d) %s: %s (retry %ds)",
                        attempt + 1, retries, url, e, wait)
            time.sleep(wait)
    raise RuntimeError(f"SEC GET failed after {retries} attempts: {url} ({last_err})")


def sec_get_json(url: str) -> dict:
    return sec_get(url).json()


# ---------------------------------------------------------------------------
# CIK resolution
# ---------------------------------------------------------------------------

def _load_cik_map() -> Dict[str, int]:
    global _CIK_MAP_CACHE
    if _CIK_MAP_CACHE is not None:
        return _CIK_MAP_CACHE
    try:
        d = sec_get_json("https://www.sec.gov/files/company_tickers.json")
        _CIK_MAP_CACHE = {v["ticker"].upper(): int(v["cik_str"]) for v in d.values()}
    except Exception as e:  # noqa: BLE001
        log.warning("CIK map fetch failed (%s); using verified fallbacks only", e)
        _CIK_MAP_CACHE = dict(_VERIFIED_CIK)
    return _CIK_MAP_CACHE


def get_cik(ticker: str) -> Optional[int]:
    """Resolve a ticker to its SEC CIK (live map first, verified fallback)."""
    t = ticker.upper()
    cik = _load_cik_map().get(t)
    if cik is None:
        cik = _VERIFIED_CIK.get(t)
    return cik


def cik10(cik: int) -> str:
    return f"{int(cik):010d}"


# ---------------------------------------------------------------------------
# Submissions enumeration (recent + paged history)
# ---------------------------------------------------------------------------

def _filings_frame_from_recent(recent: dict) -> List[dict]:
    cols = ["form", "filingDate", "reportDate", "accessionNumber",
            "primaryDocument", "acceptanceDateTime", "primaryDocDescription"]
    n = len(recent.get("accessionNumber", []))
    out = []
    for i in range(n):
        out.append({c: (recent.get(c, [None] * n)[i]) for c in cols})
    return out


def list_filings(
    cik: int,
    form: Optional[str] = None,
    primary_doc_contains: Optional[str] = None,
    include_history: bool = True,
) -> List[dict]:
    """List a company's filings (newest first).

    Parameters
    ----------
    cik : int
    form : str, optional
        Filter to an exact form type (e.g. "6-K", "10-Q", "10-K").
    primary_doc_contains : str, optional
        Keep only filings whose ``primaryDocument`` contains this substring
        (e.g. "form6-kquarterly" to isolate ASML quarterly result filings).
    include_history : bool
        Also pull the older paged submission files (full history).

    Returns
    -------
    list[dict] with keys: form, filingDate, reportDate, accessionNumber,
        primaryDocument, acceptanceDateTime, primaryDocDescription
    """
    base = sec_get_json(f"https://data.sec.gov/submissions/CIK{cik10(cik)}.json")
    filings = _filings_frame_from_recent(base.get("filings", {}).get("recent", {}))
    if include_history:
        for extra in base.get("filings", {}).get("files", []):
            try:
                page = sec_get_json(f"https://data.sec.gov/submissions/{extra['name']}")
                filings.extend(_filings_frame_from_recent(page))
            except Exception as e:  # noqa: BLE001
                log.warning("Failed paging submissions file %s: %s", extra.get("name"), e)

    def _keep(f: dict) -> bool:
        if form is not None and f.get("form") != form:
            return False
        if primary_doc_contains is not None:
            pd_ = (f.get("primaryDocument") or "").lower()
            if primary_doc_contains.lower() not in pd_:
                return False
        return True

    kept = [f for f in filings if _keep(f)]
    kept.sort(key=lambda f: (f.get("filingDate") or ""), reverse=True)
    return kept


def filing_index_url(cik: int, accession: str) -> str:
    acc_nodash = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/"


def fetch_filing_document(cik: int, accession: str, primary_document: str) -> str:
    """Download the raw text/HTML of a filing's primary document."""
    url = filing_index_url(cik, accession) + primary_document
    return sec_get(url).text


def list_filing_documents(cik: int, accession: str) -> List[str]:
    """List all document filenames inside a filing (from its directory index.json).

    Used to reach press-release / presentation *exhibits* (e.g. ASML reports its
    quarterly net-bookings number in an EX-99 press release, not the 6-K cover).
    """
    url = filing_index_url(cik, accession) + "index.json"
    try:
        d = sec_get_json(url)
        return [it["name"] for it in d.get("directory", {}).get("item", [])]
    except Exception as e:  # noqa: BLE001
        log.warning("Filing dir listing failed %s: %s", accession, e)
        return []


# ---------------------------------------------------------------------------
# XBRL companyfacts
# ---------------------------------------------------------------------------

def fetch_company_facts(cik: int) -> dict:
    """Full XBRL companyfacts JSON for a CIK."""
    return sec_get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10(cik)}.json")


def concept_series(
    facts: dict,
    concept: str,
    taxonomy: str = "us-gaap",
    unit_preference: Optional[List[str]] = None,
    forms: Optional[List[str]] = None,
) -> List[dict]:
    """Extract a flat, de-duplicated time series for one XBRL concept.

    Returns a list of {end, start, val, filed, form, fp, fy, frame} sorted by
    ``end`` ascending.  ``filed`` is the PIT availability date.  When multiple
    facts share an ``end`` date (restatements / overlapping units), the one with
    the latest ``filed`` is kept.
    """
    node = facts.get("facts", {}).get(taxonomy, {}).get(concept)
    if not node:
        return []
    units = node.get("units", {})
    pref = unit_preference or ["USD", "shares", "pure"]
    unit_keys = [u for u in pref if u in units] or list(units.keys())

    by_end: Dict[str, dict] = {}
    for uk in unit_keys:
        for item in units.get(uk, []):
            if forms and item.get("form") not in forms:
                continue
            end = item.get("end")
            if end is None:
                continue
            rec = {
                "end": end,
                "start": item.get("start"),
                "val": item.get("val"),
                "filed": item.get("filed"),
                "form": item.get("form"),
                "fp": item.get("fp"),
                "fy": item.get("fy"),
                "frame": item.get("frame"),
                "unit": uk,
            }
            prev = by_end.get(end)
            if prev is None or (rec.get("filed") or "") > (prev.get("filed") or ""):
                by_end[end] = rec
    return sorted(by_end.values(), key=lambda r: r["end"])


# ---------------------------------------------------------------------------
# acceptance_datetime → reaction date (BMO/AMC) — mirrors MRPTFetchEarnings
# ---------------------------------------------------------------------------

def reaction_from_acceptance(acceptance_dt_str: Optional[str]):
    """Return ('AMC'|'BMO'|'INTRADAY'|'UNKNOWN', reaction_date|None).

    Same convention as MRPTFetchEarnings._is_after_market: UTC hour >= 20 → AMC
    (market reacts next session), <= 13 → BMO (same day), else INTRADAY.
    """
    if not acceptance_dt_str:
        return "UNKNOWN", None
    try:
        dt = datetime.fromisoformat(acceptance_dt_str.replace("Z", "+00:00"))
        utc_hour = dt.hour
        d = dt.date()
        if utc_hour >= 20:
            return "AMC", d + timedelta(days=1)
        if utc_hour <= 13:
            return "BMO", d
        return "INTRADAY", d
    except Exception:  # noqa: BLE001
        return "UNKNOWN", None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for tk in ("ASML", "MU", "NVDA"):
        print(f"{tk:5} CIK = {get_cik(tk)}")
    asml = get_cik("ASML")
    qf = list_filings(asml, form="6-K", primary_doc_contains="quarterly", include_history=False)
    print(f"\nASML quarterly 6-K (recent): {len(qf)}")
    for f in qf[:3]:
        print("  ", f["filingDate"], f["accessionNumber"], f["primaryDocument"],
              "| acc:", f.get("acceptanceDateTime"))
    mu = get_cik("MU")
    facts = fetch_company_facts(mu)
    inv = concept_series(facts, "InventoryNet", forms=["10-Q", "10-K"])
    print(f"\nMU InventoryNet points (10-Q/K): {len(inv)}; latest: {inv[-1] if inv else None}")
