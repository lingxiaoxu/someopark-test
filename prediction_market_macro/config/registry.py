"""config/registry.py — the series registry: single source of truth (PLAN §4).

Every tradable series is one SeriesSpec. `settle_source` is the human-verified settlement
rule read from the Kalshi rulebook — EXACT rounding + first-print convention + strict/gte
threshold semantics (PLAN §18.1: measured 2026-07-27). tests/test_m0.py asserts
completeness; an empty settle_source is a build failure.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SeriesSpec:
    ticker: str                # Kalshi series ticker, e.g. "KXCPI"
    family: str                # inflation | fed | labor | gdp | energy | rates | alt
    cadence: str               # daily | weekly | monthly | per_event | quarterly | annual
    calendar: str              # ingest/calendars.py key
    settle_source: str         # precise settlement rule (rulebook-verified, non-empty)
    structure: str             # ladder | categorical | binary
    unit: str                  # %mom | %yoy | pct | k_jobs | count
    round_rule: float          # settlement grid step (0.1 for CPI %, 1000 for claims, ...)
    strict_gt: bool            # True: "Above X" settles NO on == ; False: ">= X" (claims)
    model: str                 # model/<name>.py binding
    prior_source: str | None   # market prior, e.g. "kalshi:KXFED"
    priority: int              # 0/1/2
    lanes: tuple[str, ...]     # scheduler lanes (§8.1); daily reforecast is implicit for all
    fred_first_release: str | None = None   # ALFRED series for the FIRST-print label (y_first)


# ── P0 registry (PLAN §4; structures measured via API 2026-07-27) ─────────────
REGISTRY: dict[str, SeriesSpec] = {s.ticker: s for s in [
    SeriesSpec(
        ticker="KXFEDDECISION", family="fed", cadence="per_event", calendar="FOMC",
        settle_source=("FOMC statement target-range change vs prior meeting: Hike 25bps / "
                       "Hike >25bps / Cut 25bps / Cut >25bps / maintain. Settles on the "
                       "official Fed implementation note the day of the decision."),
        structure="categorical", unit="decision", round_rule=25.0, strict_gt=False,
        model="fed", prior_source="kalshi:KXFED", priority=0, lanes=("fomc_week",),
        fred_first_release="DFEDTARU"),
    SeriesSpec(
        ticker="KXFED", family="fed", cadence="per_event", calendar="FOMC",
        settle_source=("Fed funds TARGET RANGE UPPER BOUND after the meeting; legs 'Above X%' "
                       "strict >; upper bound on 25bp grid (e.g. 3.75)."),
        structure="ladder", unit="pct", round_rule=0.25, strict_gt=True,
        model="fed", prior_source="kalshi:KXFEDDECISION", priority=0, lanes=("fomc_week",),
        fred_first_release="DFEDTARU"),
    SeriesSpec(
        ticker="KXCPI", family="inflation", cadence="monthly", calendar="BLS_CPI",
        settle_source=("BLS CPI-U SA headline MoM %, FIRST release, rounded to 0.1 as published "
                       "(BLS 08:30 ET). Legs 'Above X%' strict >: print == strike settles NO."),
        structure="ladder", unit="%mom", round_rule=0.1, strict_gt=True,
        model="cpi", prior_source=None, priority=0, lanes=("release_day",),
        fred_first_release="CPIAUCSL"),
    SeriesSpec(
        ticker="KXCPICORE", family="inflation", cadence="monthly", calendar="BLS_CPI",
        settle_source=("BLS CPI-U SA core (ex food & energy) MoM %, FIRST release, rounded 0.1. "
                       "Legs 'Above X%' strict >."),
        structure="ladder", unit="%mom", round_rule=0.1, strict_gt=True,
        model="cpi", prior_source=None, priority=0, lanes=("release_day",),
        fred_first_release="CPILFESL"),
    SeriesSpec(
        ticker="KXCPIYOY", family="inflation", cadence="monthly", calendar="BLS_CPI",
        settle_source=("BLS CPI-U headline YoY %, FIRST release, rounded 0.1 as published. "
                       "Legs 'Above X%' strict >."),
        structure="ladder", unit="%yoy", round_rule=0.1, strict_gt=True,
        model="cpi", prior_source=None, priority=0, lanes=("release_day",),
        fred_first_release="CPIAUCSL"),
    SeriesSpec(
        ticker="KXCPICOREYOY", family="inflation", cadence="monthly", calendar="BLS_CPI",
        settle_source="BLS CPI-U core YoY %, FIRST release, rounded 0.1. Legs 'Above X%' strict >.",
        structure="ladder", unit="%yoy", round_rule=0.1, strict_gt=True,
        model="cpi", prior_source=None, priority=0, lanes=("release_day",),
        fred_first_release="CPILFESL"),
    SeriesSpec(
        ticker="KXPCECORE", family="inflation", cadence="monthly", calendar="BEA_PCE",
        settle_source=("BEA core PCE price index MoM %, FIRST release, rounded 0.1 (BEA 08:30 ET). "
                       "Legs 'Above X%' strict >."),
        structure="ladder", unit="%mom", round_rule=0.1, strict_gt=True,
        model="pce", prior_source=None, priority=0, lanes=("release_day",),
        fred_first_release="PCEPILFE"),
    SeriesSpec(
        ticker="KXJOBLESSCLAIMS", family="labor", cadence="weekly", calendar="DOL_CLAIMS",
        settle_source=("DoL initial jobless claims, SA, ADVANCE (first) print, thousands "
                       "(DoL 08:30 ET Thu). Legs 'At least X' >= — print == strike settles YES."),
        structure="ladder", unit="count", round_rule=250.0, strict_gt=False,
        model="claims", prior_source=None, priority=0, lanes=("release_day",),
        fred_first_release="ICSA"),
    SeriesSpec(
        ticker="KXPAYROLLS", family="labor", cadence="monthly", calendar="BLS_JOBS",
        settle_source=("BLS nonfarm payrolls MoM change, FIRST release, thousands (BLS 08:30 ET). "
                       "Legs 'Above X' strict >."),
        structure="ladder", unit="k_jobs", round_rule=1000.0, strict_gt=True,
        model="payrolls", prior_source=None, priority=0, lanes=("release_day",),
        fred_first_release="PAYEMS"),
    SeriesSpec(
        ticker="KXU3", family="labor", cadence="monthly", calendar="BLS_JOBS",
        settle_source=("BLS unemployment rate U-3 SA, FIRST release, rounded 0.1 as published. "
                       "Legs 'Above X%' strict >: print == strike settles NO."),
        structure="ladder", unit="pct", round_rule=0.1, strict_gt=True,
        model="u3", prior_source=None, priority=0, lanes=("release_day",),
        fred_first_release="UNRATE"),
    SeriesSpec(
        ticker="KXWTIW", family="energy", cadence="weekly", calendar="WTI_WEEKLY",
        settle_source=("WTI front-month NYMEX settlement Friday, to the cent. Legs are $1-wide "
                       "'X.00 to X.99' BETWEEN buckets + open tails (measured 2026-07-28)."),
        structure="ladder", unit="$bbl", round_rule=0.01, strict_gt=True,
        model="energy", prior_source="futures:CL", priority=1, lanes=("weekly_close",),
        fred_first_release="DCOILWTICO"),
    SeriesSpec(
        ticker="KXNATGASW", family="energy", cadence="weekly", calendar="NG_WEEKLY",
        settle_source=("Henry Hub front-month settlement Friday. Legs 'Above $X.X99' strict > "
                       "(measured 2026-07-28)."),
        structure="ladder", unit="$mmbtu", round_rule=0.001, strict_gt=True,
        model="energy", prior_source="futures:NG", priority=1, lanes=("weekly_close",),
        fred_first_release=None),
    SeriesSpec(
        ticker="KXAAAGASW", family="energy", cadence="weekly", calendar="AAA_WEEKLY",
        settle_source=("AAA national average regular gasoline, Monday reading, to 0.001. "
                       "Legs 'Above X.XX0' settle YES on an exact tie — observed, not "
                       "assumed: KXAAAGASW-26AUG31-4.080 settled YES with the AAA print at "
                       "exactly 4.080 (2026-08-31). Model anchor is the EIA weekly proxy "
                       "(GASREGW) — level offset noted in model card; shadow judges."),
        # 2026-09-02: was strict_gt=True ("strict >", an assumption written at build
        # time). The first exact tie in 867 settled legs went YES; under True the health
        # settle-label check called it a mismatch, tripped the GLOBAL breaker two days
        # running, force-exited every open position and blocked 104 opens. A 0.001-grid
        # series ties for real, so this flag also moves P(y == strike) onto the YES side
        # of leg_fair, where Kalshi puts it. Other 'greater' series keep True: no tie has
        # been observed on them yet, and this flag is set by evidence, not by analogy.
        structure="ladder", unit="$gal", round_rule=0.001, strict_gt=False,
        model="energy", prior_source=None, priority=1, lanes=("weekly_close",),
        fred_first_release="GASREGW"),
    SeriesSpec(
        ticker="KXGDP", family="gdp", cadence="quarterly", calendar="BEA_GDP",
        settle_source=("BEA advance estimate of real GDP growth, annualized SAAR, "
                       "first print as published 08:30 ET on the scheduled release day "
                       "(bea.gov). Ladder legs per Kalshi KXGDP rulebook; PAPER-ONLY "
                       "until the live contract structure is measured (铁律 2: two "
                       "paper prints minimum before any gate consideration)."),
        structure="ladder", unit="pct_saar", round_rule=0.1, strict_gt=True,
        model="gdp", prior_source=None, priority=1, lanes=("quarterly",),
        fred_first_release="A191RL1Q225SBEA"),
]}


def p0() -> list[SeriesSpec]:
    return [s for s in REGISTRY.values() if s.priority == 0]


# When a series' tie rule was CORRECTED, the rule in force before the correction, so a
# reader replaying a decision recorded earlier prices it the way production did then.
# {series: ((corrected_at_iso, strict_gt_before), ...)}
STRICT_GT_HISTORY: dict[str, tuple[tuple[str, bool], ...]] = {
    "KXAAAGASW": (("2026-09-02T12:00:00+00:00", True),),
}


def effective_strike_type(series: str, strike_type: str | None,
                          asof: str | None = None) -> str:
    """The strike type to PRICE and SETTLE a leg with: the contract's nominal type selects
    the family (greater / less / between), the series' registered `strict_gt` decides the
    tie ON the line.

    2026-09-02: Kalshi labels KXAAAGASW legs 'greater' and settled the first exact tie
    (26AUG31-4.080, AAA print 4.080) YES. Every consumer read the contract's word
    literally — `strike_type or "greater"` — so the health check called a correct
    settlement a mismatch and tripped the global breaker two mornings running, and
    leg_fair put the tie's probability mass on the NO side of a 0.001-grid series
    that ties for real. `strict_gt` is set from observed settlements, not analogy; a
    series with no observed tie keeps its registered value.
    """
    st = strike_type or "greater"
    if st in ("greater", "greater_or_equal"):
        spec = REGISTRY.get(series)
        strict = spec.strict_gt if spec is not None else True
        if asof is not None:
            # PIT: the flag in force at `asof` — STRICT_GT_HISTORY lists corrections
            # newest-last, each row = (corrected_at, flag_before_that_correction)
            asof_s = asof.isoformat() if hasattr(asof, "isoformat") else str(asof)
            for corrected_at, before in STRICT_GT_HISTORY.get(series, ()):
                if asof_s < corrected_at:
                    strict = before
                    break
        return "greater" if strict else "greater_or_equal"
    return st


def strict_gt_at(series: str, asof=None) -> bool:
    """The series' tie rule as a bool, PIT: the flag in force at `asof` (datetime or ISO
    string; None = now). For `enumerate_structs(..., strict=...)` call sites that price a
    whole ladder at once."""
    return effective_strike_type(series, None, asof=None if asof is None
                                 else (asof.isoformat() if hasattr(asof, "isoformat")
                                       else str(asof))) == "greater"
