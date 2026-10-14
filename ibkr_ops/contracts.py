"""Instrument keys, ib_async contract construction, and a contract-details cache.

Contract lookups (``reqContractDetails``) should not be issued concurrently: parallel lookups can stall IB's
contract-definition service. ``ContractCache`` serialises lookups and records negative results ("no security
definition", error 200) for a configurable period. Resolved contracts should be addressed by ``conId``
thereafter, which does not depend on the lookup service.

ForecastEx event contracts (exchange ``FORECASTX``) are listed as ``secType="OPT"`` with ``right`` C = YES and
P = NO, multiplier 1, paying $1. They are discovered with
``reqContractDetails(Contract(symbol=<product>, secType="OPT", exchange="FORECASTX"))``;
``reqSecDefOptParams`` returns no results for them.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .io import atomic_write_text

US_EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS", "IEX", "ISLAND", "NMS", "PINK"}
CA_EXCHANGES = {"TSE", "VENTURE", "CVE", "OMEGA", "PURE"}


@dataclass(frozen=True)
class Instrument:
    """A tradeable thing as a short string key, round-tripping through ``parse``/``key``:

    ``SPY`` stock (SMART/USD) · ``EUR.USD`` FX (IDEALPRO, quantity in base currency) · ``SPY 20261120 450 C``
    option · ``BOND:<CUSIP>`` · ``FCX:UHLAX 20261008 88 C`` ForecastEx (C = YES, P = NO) · ``CON:<conId>``
    a pinned contract.
    """
    symbol: str
    sec_type: str = "STK"
    currency: str = "USD"
    expiry: str = ""
    strike: float = 0.0
    right: str = ""
    exchange: str = ""
    con_id: int = 0

    @property
    def multiplier(self) -> float:
        return 100.0 if self.sec_type == "OPT" else 1.0

    @property
    def key(self) -> str:
        if self.con_id and self.sec_type == "CON":
            return f"CON:{self.con_id}"
        if self.sec_type == "OPT":
            return f"{self.symbol} {self.expiry} {self.strike:g} {self.right}"
        if self.sec_type == "FCX":
            return f"FCX:{self.symbol} {self.expiry} {self.strike:g} {self.right}"
        if self.sec_type == "CASH":
            return f"{self.symbol}.{self.currency}"
        if self.sec_type == "BOND":
            return f"BOND:{self.symbol}"
        return self.symbol

    @classmethod
    def parse(cls, key) -> "Instrument":
        if isinstance(key, Instrument):
            return key
        key = str(key).strip().upper()
        parts = key.split()
        if key.startswith("CON:"):
            return cls(key, "CON", con_id=int(key[4:]))
        if key.startswith("FCX:"):
            return cls(parts[0][4:], "FCX", expiry=parts[1], strike=float(parts[2]), right=parts[3][0],
                       exchange="FORECASTX")
        if len(parts) == 4:
            return cls(parts[0], "OPT", expiry=parts[1], strike=float(parts[2]), right=parts[3][0])
        if key.startswith("BOND:"):
            return cls(key[5:], "BOND")
        if len(key) == 7 and key[3] == ".":
            return cls(key[:3], "CASH", currency=key[4:], exchange="IDEALPRO")
        return cls(key)


def to_contract(inst: Instrument | str):
    """An ib_async ``Contract`` for an instrument (imported lazily)."""
    from ib_async import Contract

    i = Instrument.parse(inst)
    if i.con_id:
        return Contract(conId=i.con_id, exchange=i.exchange or "SMART")
    if i.sec_type == "CASH":
        return Contract(secType="CASH", symbol=i.symbol, currency=i.currency, exchange=i.exchange or "IDEALPRO")
    if i.sec_type in ("OPT", "FCX"):
        fcx = i.sec_type == "FCX"
        return Contract(secType="OPT", symbol=i.symbol, lastTradeDateOrContractMonth=i.expiry, strike=i.strike,
                        right=i.right, exchange="FORECASTX" if fcx else i.exchange or "SMART",
                        multiplier="1" if fcx else "100", currency=i.currency)
    return Contract(secType=i.sec_type, symbol=i.symbol, currency=i.currency, exchange=i.exchange or "SMART")


def pick_listing(details: list):
    """From several ``ContractDetails``, the US listing, else Canadian, else the first."""
    canadian = first = None
    for d in details:
        c = d.contract
        primary = (getattr(c, "primaryExchange", "") or getattr(c, "exchange", "") or "").upper()
        if primary in US_EXCHANGES:
            return c
        if primary in CA_EXCHANGES and canadian is None:
            canadian = c
        first = first or c
    return canadian or first


# --- ForecastEx ---------------------------------------------------------------------------------------------
def parse_fcx_local_symbol(local_symbol: str) -> dict:
    """``UHLAX_101126_75_YES`` -> product, target date (YYYYMMDD), strike, side, right (C=YES, P=NO).

    IB's ``lastTradeDateOrContractMonth`` is the target date + 1 day; this reads the target from the symbol."""
    product, mmddyy, strike, side = local_symbol.split("_")
    target = f"20{mmddyy[4:6]}{mmddyy[0:2]}{mmddyy[2:4]}"
    return {"product": product, "target": target, "strike": float(strike), "side": side,
            "right": "C" if side == "YES" else "P"}


# --- cache --------------------------------------------------------------------------------------------------
@dataclass
class Resolved:
    con_id: int
    local_symbol: str = ""
    exchange: str = ""
    expiry: str = ""
    strike: float = 0.0
    right: str = ""
    multiplier: str = ""
    time_zone: str = ""
    trading_hours: str = ""


class ContractCache:
    """A per-day JSON cache of resolved contracts plus a negative cache for "doesn't exist" answers.

    ``lookup(key, fetch)`` calls ``fetch()`` (which runs ``reqContractDetails`` and returns ``[Resolved]``,
    ``[]`` for a 200) only on a miss, and only one fetch runs at a time across threads."""

    _lock = threading.Lock()                  # class-wide: one lookup at a time per process

    def __init__(self, path: Path, negative_ttl: float = 3 * 3600, clock: Callable[[], float] = time.time):
        self.path, self.negative_ttl, self.clock = Path(path), negative_ttl, clock
        self.data = {"found": {}, "missing": {}}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except ValueError:
                pass                          # a torn file is a cache miss, not a crash

    def lookup(self, key: str, fetch: Callable[[], list]) -> list[Resolved]:
        if key in self.data["found"]:
            return [Resolved(**r) for r in self.data["found"][key]]
        miss_at = self.data["missing"].get(key)
        if miss_at is not None and self.clock() - miss_at < self.negative_ttl:
            return []
        with self._lock:
            found = list(fetch())
        if found:
            self.data["found"][key] = [asdict(r) for r in found]
            self.data["missing"].pop(key, None)
        else:
            self.data["missing"][key] = self.clock()
        self.save()
        return found

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, json.dumps(self.data, indent=1, sort_keys=True))


def daily_cache_path(directory: os.PathLike, day: str) -> Path:
    return Path(directory) / f"contracts-{day}.json"
