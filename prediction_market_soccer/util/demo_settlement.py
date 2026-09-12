"""Validate binary Demo settlement facts without changing the original response.

Kalshi REST uses finalized plus settlement_ts; settled is a filter/WebSocket
event name. Accept the old internal settled spelling only with the same evidence.
Source: https://docs.kalshi.com/getting_started/market_lifecycle
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


def _time(value):
    parsed=value if isinstance(value,datetime) else datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Settlement time must include its timezone')
    return parsed.astimezone(timezone.utc)


def terminal_binary_settlement(market, *, ticker, observed_at=None):
    """Return the original binary result only after a timestamped terminal fact."""
    if (not isinstance(market,dict) or market.get('ticker')!=ticker
            or market.get('status') not in ('finalized','settled')
            or market.get('result') not in ('yes','no')
            or market.get('market_type','binary')!='binary'
            or market.get('is_provisional') is True):
        return None
    try:
        settled=_time(market['settlement_ts'])
        observed=_time(observed_at) if observed_at is not None else datetime.now(timezone.utc)
        if settled>observed:
            return None
        if market.get('settlement_value_dollars') is not None:
            value=Decimal(str(market['settlement_value_dollars']))
            if not value.is_finite() or value!=int(market['result']=='yes'):
                return None
    except (KeyError,TypeError,ValueError,InvalidOperation):
        return None
    return market['result']
