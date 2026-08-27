"""model/venue_climate.py — venue altitude as a λ effect, keyed by CITY (club edition).

The World Cup version was a hand-built table of the 16 North American host stadiums with
an altitude column and a June/July "summer heat" column, applied as a SYMMETRIC λ
suppression to both sides. None of that survives the move to clubs: the 12 competitions
play at 651 stadiums across 421 cities, and club seasons run August–May (Europe) or
January–December (South America), so a fixed "summer heat" flag is meaningless without a
date-aware weather source we do not have.

What does survive — and what the club data supports strongly — is ALTITUDE, and it is the
one real venue effect in this module's remit: the Andes sit inside our fixture set (La Paz
3,640 m, Cusco 3,400 m, Quito 2,850 m, Bogotá 2,640 m).

FITTED ON OUR OWN FIXTURES, and it corrected the inherited model twice:

  1. The effect is ASYMMETRIC, not symmetric. Altitude does not trim both λ; it inflates
     the acclimatised host's and deflates the visitor's.
  2. The sign of the total is the OPPOSITE of the WC premise. Goals per match at ≥2,800 m
     are 2.88 vs 2.32 at sea level in the same competitions — altitude venues are higher
     scoring, not lower.

Design of the fit (CONMEBOL Libertadores + Sudamericana, the only competitions whose
visitors cross altitude bands): for the clubs whose home venue is in a band, compare their
own home results in that band against their own away results below 1,000 m. Differencing
the same clubs against themselves removes club quality, which is the confound that would
otherwise read "weak Bolivian club" as "altitude". h = home advantage in log-λ:

    band            n_home  n_away    h        excess vs sea level     z
    0–1,000 m         434     333   +0.127     (baseline)              —
    1,000–2,200 m      25      23   +0.412     +0.285 ± 0.238        1.20
    2,200–2,800 m      31      26   +0.232     +0.105 ± 0.199        0.53
    2,800 m+           58      56   +0.776     +0.649 ± 0.140        4.63

Only the top band is real, and it is very real: at ≥2,800 m the host's λ multiplies by
about e^0.65 ≈ 1.9 and the visitor's by 1/1.9, on top of the ordinary home edge. Below
2,800 m nothing is distinguishable from the baseline — consistent with the physiology
(unacclimatised VO2max falls sharply only above roughly 2,500 m).

STILL OFF BY DEFAULT. `venue_climate_weight` stays 0.0, per plan §2.2. Two things must
happen before it is raised, and neither is this module's to do:
  * `match_pricing.price_match` currently passes only `venue_name` and applies a single
    symmetric multiplier. The altitude effect needs the fixture's `venue_city` and an
    ANTI-symmetric application (host ×e^edge, visitor ×e^-edge). Until that call site
    changes, `altitude_home_log_edge` has no consumer.
  * The fit is CONMEBOL-only. It is the only place we can measure it, and it is also the
    only place our fixtures go above 2,800 m — but it means the constant is not validated
    against a European or a neutral-venue final.
"""
from __future__ import annotations

# Cities in our own fixture set at or above 800 m, from the `fixture.venue_city` values as
# API-Football spells them (both spellings kept where the feed is inconsistent — an
# unmatched key silently means "sea level", so the variants must be listed, not guessed).
# Below 800 m every city is neutral by construction: the fit finds nothing there, and
# listing 400 sea-level cities would only invite drift. Values are public city-centre
# elevations; a stadium sits within ~100 m of its city centre, which is immaterial next to
# the 800 m band width.
_CITY_ALTITUDE_M: dict[str, int] = {
    # Bolivia / Peru / Ecuador / Colombia — the band that actually moves the model
    "El Alto": 4150, "Potosí": 4090, "Potosi": 4090, "La Paz": 3640,
    "Cusco": 3400, "Huancayo": 3250, "Quito": 2850, "Sucre": 2810,
    "Riobamba": 2750, "Bogota": 2640, "Bogotá, D.C.": 2640, "Ambato": 2580,
    "Cochabamba": 2570, "Cuenca": 2550, "Sangolqui": 2510, "Arequipa": 2335,
    "Calama": 2260, "Manizales": 2160, "Medellin": 1495, "Ibague": 1285,
    "Cali": 1018, "Santiago de Cali": 1018, "Bucaramanga": 959,
    "Caracas": 900, "San Cristóbal": 825,
    # Brazil — the highest domestic grounds; all below the effective band
    "Brasília": 1172, "Brasília, Distrito Federal": 1172, "Curitiba": 934,
    "Belo Horizonte": 852, "Caxias do Sul": 817,
    "Bragança Paulista": 817, "Bragança Paulista, São Paulo": 817,
    # UEFA qualifying rounds reach these; listed for completeness, all sub-threshold
    "Abovyan": 1400, "Encamp": 1300, "Andorra la Vella": 1023, "Yerevan": 990,
    "Almaty": 800,
}

# (lower bound m, excess home log-λ edge over the sea-level baseline, bootstrap se, z, n)
# Regenerate with `fit_altitude_home_edge`; see the module docstring for the design.
_ALTITUDE_BANDS: tuple[tuple[int, float, float, float, int], ...] = (
    (0,    0.000, 0.054, 0.00, 434),
    (1000, 0.285, 0.238, 1.20, 25),
    (2200, 0.105, 0.199, 0.53, 31),
    (2800, 0.649, 0.140, 4.63, 58),
)
_SHRINK_Z0 = 2.0        # same rule as model/knockout_late_draw: |z|≈2 keeps half the effect
NEUTRAL_BELOW_M = 800   # below this the table is deliberately empty, so lookups return 0

# Exposure ramp endpoints: nothing measurable below NEUTRAL_BELOW_M, and 3,600 m (La Paz)
# is the top of our fixture set, so the index saturates there.
_EXPOSURE_TOP_M = 3600


def _shrunk_edge(excess: float, z: float) -> float:
    """Pull a band's raw excess toward zero by its own significance, so a band that has
    not earned its estimate contributes ~nothing without needing a hand-set switch."""
    w = (z * z) / (z * z + _SHRINK_Z0 * _SHRINK_Z0)
    return excess * w


# metres -> shrunk excess home log-λ edge. Today only the ≥2,800 m band is non-trivial.
ALTITUDE_HOME_LOG_EDGE: tuple[tuple[int, float], ...] = tuple(
    (lo, round(_shrunk_edge(ex, z), 4)) for lo, ex, _se, z, _n in _ALTITUDE_BANDS)


def city_altitude_m(place: str | None) -> int:
    """Altitude of a fixture's city in metres; 0 for anything not in the table."""
    if not place:
        return 0
    return _CITY_ALTITUDE_M.get(place.strip(), 0)


def altitude_home_log_edge(place: str | None, weight: float) -> float:
    """EXTRA home-side log-λ edge from altitude, on top of the ordinary home advantage.

    Apply ANTI-symmetrically: λ_home ×= exp(+edge), λ_away ×= exp(-edge). Returns 0 at
    weight 0 (the production default) and 0 for every venue below the fitted band, so
    wiring it up cannot move a European or Brazilian fixture by accident."""
    if weight <= 0:
        return 0.0
    alt = city_altitude_m(place)
    edge = 0.0
    for lo, e in ALTITUDE_HOME_LOG_EDGE:
        if alt >= lo:
            edge = e
    return weight * edge


def venue_index(venue_name: str | None = None, *, city: str | None = None) -> float:
    """Altitude EXPOSURE in [0, 1] — a monotone display/gating measure, not the effect.

    The traded quantity is `altitude_home_log_edge`, which is a step function of the fitted
    bands and is NOT monotone below 2,800 m (the middle bands are noise). This index exists
    so panels and guards can rank venues sensibly; do not price off it.

    Accepts a city (preferred — the table is keyed by city) or falls back to matching the
    venue name, which only helps for grounds named after their city."""
    alt = city_altitude_m(city) or city_altitude_m(venue_name)
    if alt <= NEUTRAL_BELOW_M:
        return 0.0
    return max(0.0, min(1.0, (alt - NEUTRAL_BELOW_M) / (_EXPOSURE_TOP_M - NEUTRAL_BELOW_M)))


def venue_log_suppression(venue_name: str | None, weight: float,
                          *, city: str | None = None) -> float:
    """LEGACY symmetric λ suppression — always 0.0 now, deliberately.

    Kept because `match_pricing.price_match` still calls it, and removing it there is
    another owner's change. It returns 0 because the club fit contradicts the World Cup
    premise it encoded: goals at ≥2,800 m are HIGHER than at sea level (2.88 vs 2.32 per
    match in the same competitions), and the effect is anti-symmetric, not symmetric.
    Returning a suppression here would trim both λ in the wrong direction. Use
    `altitude_home_log_edge` instead; see the module docstring for the call-site change."""
    return 0.0


# ── offline refit ────────────────────────────────────────────────────────────
def fit_altitude_home_edge(conn, *, bands: tuple[int, ...] = (0, 1000, 2200, 2800),
                           boots: int = 3000, seed: int = 7) -> list[tuple]:
    """Refit `_ALTITUDE_BANDS` from soccer.db (CONMEBOL cups only — see the docstring).

    For a club T playing an average opponent, log(λ_T/λ_opp) is 2q+2h at home and 2q-2h_low
    away, so differencing T's own home and away books cancels q and leaves the venue's home
    advantage. Bootstrap over fixtures for the SE."""
    import collections
    import math
    import random

    rows = [r for r in conn.execute(
        "SELECT venue_city, home_api_id, away_api_id, home_goals, away_goals FROM fixture "
        "WHERE status_short='FT' AND home_goals IS NOT NULL AND league_id IN (13, 11)")]
    if not rows:
        return []
    # a club's home altitude = the altitude it most often hosts at (grounds get shared)
    seen = collections.defaultdict(collections.Counter)
    for r in rows:
        seen[r["home_api_id"]][city_altitude_m(r["venue_city"])] += 1
    club_alt = {t: c.most_common(1)[0][0] for t, c in seen.items()}

    def books(band_lo, band_hi):
        clubs = {t for t, a in club_alt.items() if band_lo <= a < band_hi}
        H, A = [], []
        for r in rows:
            va = city_altitude_m(r["venue_city"])
            if r["home_api_id"] in clubs and band_lo <= va < band_hi:
                H.append((r["home_goals"], r["away_goals"]))
            if r["away_api_id"] in clubs and va < 1000:
                A.append((r["away_goals"], r["home_goals"]))
        return H, A

    def ratios(H, A):
        eps = 1e-6
        lh = sum(g for g, _ in H) / len(H); la = sum(a for _, a in H) / len(H)
        aw = sum(g for g, _ in A) / len(A); ao = sum(a for _, a in A) / len(A)
        return math.log(max(lh, eps) / max(la, eps)), math.log(max(aw, eps) / max(ao, eps))

    edges = [(bands[i], bands[i + 1] if i + 1 < len(bands) else 99999)
             for i in range(len(bands))]
    H0, A0 = books(*edges[0])
    a0, b0 = ratios(H0, A0)
    h_low = (a0 - b0) / 4.0                      # baseline band: a-b = 4*h_low

    def h_of(H, A):
        a, b = ratios(H, A)
        return (a - b) / 2.0 - h_low             # a = 2q+2h_band, b = 2q-2h_low

    rnd = random.Random(seed)
    out = []
    for lo, hi in edges:
        H, A = books(lo, hi)
        if len(H) < 15 or len(A) < 15:
            continue
        excess = h_of(H, A) - h_low
        draws = []
        for _ in range(boots):
            hh = [H[rnd.randrange(len(H))] for _ in H]
            aa = [A[rnd.randrange(len(A))] for _ in A]
            try:
                draws.append(h_of(hh, aa) - h_low)
            except Exception:
                pass
        m = sum(draws) / len(draws)
        se = math.sqrt(sum((x - m) ** 2 for x in draws) / max(len(draws) - 1, 1))
        out.append((lo, round(excess, 3), round(se, 3),
                    round(excess / se, 2) if se else 0.0, len(H)))
    return out


if __name__ == "__main__":
    from prediction_market_soccer.ingest import store

    print(f"{'band(m)':>9s} {'nH':>5s} {'excess':>8s} {'se':>7s} {'z':>6s} {'shrunk':>8s}")
    for lo, excess, se, z, n in fit_altitude_home_edge(store.init_db()):
        print(f"{lo:>9d} {n:5d} {excess:+8.3f} {se:7.3f} {z:6.2f} {_shrunk_edge(excess, z):+8.4f}")
    print("\nexposure index samples:")
    for c in ("La Paz", "Quito", "Bogota", "Medellin", "Belo Horizonte", "Madrid", "London"):
        print(f"  {c:16s} alt={city_altitude_m(c):5d}m  index={venue_index(city=c):.3f}  "
              f"home_edge@w=1 {altitude_home_log_edge(c, 1.0):+.4f}")
