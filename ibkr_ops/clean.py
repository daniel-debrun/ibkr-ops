"""Normalisation of IB "no value" sentinels, and quote extraction.

IB does not send null values. Absent data is represented as:

* ``-1`` for a price or implied volatility;
* ``-2`` for an option greek that was not computed;
* ``DBL_MAX`` (1.797e308) or ``UNSET_DECIMAL`` (2**127 - 1) for unset fields;
* ``NaN`` in ib_async objects;
* size ``0`` alongside a missing price (no quote, not a zero-size quote);
* bar ``volume == -1`` and ``wap <= 0`` where not applicable (e.g. FX MIDPOINT).

The functions here map all of these to ``None``. ``ticker_row`` converts an ib_async ``Ticker``.
"""
from __future__ import annotations

import math
from decimal import Decimal

_HUGE = 1e300
_UNSET_DECIMAL = float(2**127 - 1)       # the API's UNSET_DECIMAL, as a float


def num(v) -> float | None:
    """``float(v)``, or None for None, NaN, +-inf and the DBL_MAX/UNSET_DECIMAL sentinels."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or abs(f) > _HUGE or abs(f) >= _UNSET_DECIMAL:
        return None
    return f


def price(v) -> float | None:
    """A quote price; ``-1`` (and any value <= 0) means none. Event contracts trade in (0, 1), so 0 is
    never a real quote either."""
    f = num(v)
    return f if f is not None and f > 0 else None


def size(v, paired_price=None) -> float | None:
    """A quote size. ``0`` alongside a missing price is "no quote" (ib_async ``emptySize=0``), not zero."""
    f = num(v)
    if f is None or f < 0:
        return None
    if f == 0 and paired_price is not None and price(paired_price) is None:
        return None
    return f


def greek(v, is_vol: bool = False) -> float | None:
    """An option-model value: ``-2`` for an uncomputed greek, ``-1`` for an uncomputed vol or price."""
    f = num(v)
    if f is None or f == -2 or (is_vol and f < 0):
        return None
    return f


def bar_volume(v) -> float | None:
    f = num(v.normalize() if isinstance(v, Decimal) else v)
    return f if f is not None and f >= 0 else None


# --- tick types -----------------------------------------------------------------------------------------
#: field -> (live tick type, delayed tick type). Delayed ticks (66-76) arrive on their own ids.
QUOTE_TICKS = {"bid": (1, 66), "ask": (2, 67), "last": (4, 68), "close": (9, 75), "open": (14, 76),
               "high": (6, 72), "low": (7, 73), "bid_size": (0, 69), "ask_size": (3, 70),
               "last_size": (5, 71), "volume": (8, 74)}
PRICE_FIELDS = {"bid", "ask", "last", "close", "open", "high", "low"}
LIVE_PRICE_TICKS = {1, 2, 4}
DELAYED_PRICE_TICKS = {66, 67, 68}
#: tickOptionComputation types: 13 model (live), 83 delayed model
OPTION_MODEL_TICKS = {13: "live", 83: "delayed"}
#: option open interest arrives via tickSize when generic tick 101 is requested
OPEN_INTEREST_TICK = {"C": 27, "P": 28}

#: friendly name -> generic tick id to request in ``genericTickList``
GENERIC_TICKS = {"option_volume": 100, "option_oi": 101, "hist_vol": 104, "atm_iv": 106, "avg_volume": 165,
                 "auction": 225, "mark": 232, "rtvolume": 233, "shortable": 236, "trade_count": 293,
                 "fundamental_ratios": 258, "dividends": 456, "futures_oi": 588}


def quote(ticks: dict, at: str | None = None) -> dict:
    """``{tick type: raw value}`` -> one clean quote row, live taking precedence over delayed, with
    ``price_type`` = live / delayed / None (no price at all). Never falls back silently: a missing bid
    stays None, so a sizing step can't run on a stale close by accident."""
    row: dict = {"at": at} if at is not None else {}
    for field, (live, delayed) in QUOTE_TICKS.items():
        raw = ticks.get(live, ticks.get(delayed))
        row[field] = price(raw) if field in PRICE_FIELDS else None if raw is None else num(raw)
    for side in ("bid", "ask", "last"):                    # size 0 next to no price = no quote
        s = size(row[f"{side}_size"])
        row[f"{side}_size"] = None if s == 0 and row[side] is None else s
    seen = set(ticks)
    row["price_type"] = ("live" if seen & LIVE_PRICE_TICKS else
                         "delayed" if seen & DELAYED_PRICE_TICKS else None)
    return row


def mid(row: dict) -> float | None:
    b, a = row.get("bid"), row.get("ask")
    return (b + a) / 2 if b is not None and a is not None and a >= b else None


#: ``Ticker.marketDataType`` -> price_type
MARKET_DATA_NAMES = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed_frozen"}


def ticker_row(t, at: str | None = None) -> dict:
    """An ib_async ``Ticker`` -> one clean quote row (same columns as ``quote``) plus greeks and open
    interest when present. ``price_type`` is None when the ticker carries no price at all."""
    row: dict = {"at": at} if at is not None else {}
    for field in ("bid", "ask", "last", "close", "open", "high", "low"):
        row[field] = price(getattr(t, field, None))
    for field, attr in (("bid_size", "bidSize"), ("ask_size", "askSize"), ("last_size", "lastSize")):
        s = size(getattr(t, attr, None))
        row[field] = None if s == 0 and row[field[:-5]] is None else s
    row["volume"] = bar_volume(getattr(t, "volume", None))
    has_price = any(row[f] is not None for f in ("bid", "ask", "last"))
    row["price_type"] = MARKET_DATA_NAMES.get(getattr(t, "marketDataType", 0)) if has_price else None
    g = getattr(t, "modelGreeks", None)
    if g is not None:
        row.update(iv=greek(g.impliedVol, True), delta=greek(g.delta), gamma=greek(g.gamma),
                   vega=greek(g.vega), theta=greek(g.theta), und_price=greek(g.undPrice, True))
    oi = num(getattr(t, "callOpenInterest", None)), num(getattr(t, "putOpenInterest", None))
    if any(v is not None for v in oi):
        row["open_interest"] = next(v for v in oi if v is not None)
    ts = getattr(t, "lastTimestamp", None)
    row["last_time"] = ts.isoformat() if ts is not None else None
    return row
