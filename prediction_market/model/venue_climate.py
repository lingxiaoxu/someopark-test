"""model/venue_climate.py — match-level venue-climate λ suppression (plan 19).

Heat and (especially) altitude slow the game and tire players, which empirically
trims goals and nudges matches toward draws; a CLOSED roof + AC neutralises heat
(not altitude). This is a property of the FIXTURE, not a team, so it's a symmetric
multiplier on BOTH lambdas (compresses scorelines → slightly more draws, without
changing who's favoured). Static, fully-sourced stadium facts — no live weather API.

Bounded + parameter-controlled: the suppression index is in [0, 1]; the actual λ
effect is `venue_climate_weight × index × _BASE`, and `venue_climate_weight` defaults
to 0 (off). Altitude dominates (Mexico City 2,240 m); June/July heat is secondary and
only counts at open-air / uncontrolled venues.
"""
from __future__ import annotations

_BASE = 0.10   # max log-λ suppression at index=1 BEFORE the config weight (kept small)

# venue_name → (altitude_m, climate_controlled, summer_heat 0..1)
# climate_controlled = closed roof + AC neutralises heat (altitude still applies).
_VENUES: dict[str, tuple[int, bool, float]] = {
    "Estadio Azteca":         (2240, False, 0.3),   # Mexico City — extreme altitude
    "Estadio Akron":          (1560, False, 0.5),   # Guadalajara — high altitude + warm
    "Estadio BBVA":           (540,  False, 0.9),   # Monterrey — very hot
    "AT&T Stadium":           (180,  True,  1.0),   # Dallas — brutal heat, but roof + AC
    "NRG Stadium":            (15,   True,  1.0),   # Houston — brutal heat/humidity, roof + AC
    "Hard Rock Stadium":      (2,    False, 0.95),  # Miami — extreme heat/humidity, canopy only
    "Arrowhead Stadium":      (270,  False, 0.85),  # Kansas City — hot
    "Mercedes-Benz Stadium":  (320,  True,  0.8),   # Atlanta — hot/humid, retractable roof
    "Lincoln Financial Field":(10,   False, 0.6),   # Philadelphia — warm/humid
    "MetLife Stadium":        (7,    False, 0.55),  # NY/NJ — warm/humid
    "SoFi Stadium":           (30,   False, 0.4),   # LA — canopy, mild-warm
    "Gillette Stadium":       (30,   False, 0.35),  # Boston — mild
    "Levi's Stadium":         (5,    False, 0.4),   # SF Bay — mild-warm
    "Lumen Field":            (5,    False, 0.25),  # Seattle — mild
    "BMO Field":              (80,   False, 0.3),   # Toronto — mild
    "BC Place":               (5,    True,  0.2),   # Vancouver — mild, retractable roof
}


def venue_index(venue_name: str | None) -> float:
    """Goal-suppression index in [0, 1] for a venue (0 = neutral). Altitude dominates;
    heat counts only where it isn't neutralised by a closed roof + AC."""
    if not venue_name:
        return 0.0
    v = _VENUES.get(venue_name.strip())
    if not v:
        return 0.0
    alt, controlled, heat = v
    alt_idx = min(alt / 2240.0, 1.0)              # 0 at sea level, 1 at Mexico City
    heat_idx = 0.0 if controlled else heat
    # altitude weighted ~2× heat (it's the stronger, better-evidenced effect)
    return max(0.0, min(1.0, 0.66 * alt_idx + 0.34 * heat_idx))


def venue_log_suppression(venue_name: str | None, weight: float) -> float:
    """Symmetric log-λ suppression (≥0) to subtract from BOTH lambdas. 0 if weight=0."""
    if weight <= 0:
        return 0.0
    return weight * _BASE * venue_index(venue_name)
