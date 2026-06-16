"""venue_guard — execution routing safety rail (plan 05 §1, 09 §3/§5).

Execution is permitted ONLY on Kalshi and Polymarket US (both NY-legal).
Polymarket Global is read-only (US geoblock); any order attempt there must be
hard-blocked. Call ``assert_executable`` at the top of every order path.
"""
from __future__ import annotations

from prediction_market.config import CONFIG


class VenueGuardError(RuntimeError):
    """Raised when code attempts to place an order on a non-executable venue."""


def is_executable(venue: str) -> bool:
    return venue in CONFIG.venue.executable_venues


def assert_executable(venue: str) -> None:
    """Block any order routed to a non-executable venue (e.g. poly_global)."""
    if not is_executable(venue):
        raise VenueGuardError(
            f"venue {venue!r} is not executable; allowed: {CONFIG.venue.executable_venues}. "
            "Polymarket Global is read-only (US geoblock)."
        )


def assert_trading_enabled(venue: str) -> None:
    """Real-money kill-gate: refuse PROD order submission unless explicitly enabled.

    Demo orders (mock funds) are always allowed — that's the point of demo. But
    Kalshi PROD (real money) is hard-blocked until KALSHI_TRADING_ENABLED=true,
    so an accidental env flip can never place a real-money order. Reads unaffected.
    """
    assert_executable(venue)
    if (venue == "kalshi" and CONFIG.venue.kalshi_env == "prod"
            and not CONFIG.venue.kalshi_trading_enabled):
        raise VenueGuardError(
            "Kalshi PROD live trading is DISABLED (KALSHI_TRADING_ENABLED=false). "
            "This is a production key with REAL funds; set the flag true only when "
            "the user explicitly authorizes live order placement."
        )
    if venue == "poly_us" and not CONFIG.venue.pmus_trading_enabled:
        raise VenueGuardError(
            "Polymarket US live trading is DISABLED (PMUS_TRADING_ENABLED=false). "
            "Polymarket US is REAL money (no sandbox); set the flag true only when "
            "the user explicitly authorizes live order placement."
        )
