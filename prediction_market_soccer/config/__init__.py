"""Central configuration for the prediction_market_soccer project.

Re-exports the singleton ``CONFIG`` and the dataclasses so callers can do::

    from prediction_market_soccer.config import CONFIG
"""
from .config import (
    CONFIG,
    Config,
    ModelConfig,
    Paths,
    RiskConfig,
    SoccerConfig,
    VenueConfig,
)

__all__ = [
    "CONFIG", "Config", "ModelConfig", "Paths", "RiskConfig", "SoccerConfig", "VenueConfig",
]
