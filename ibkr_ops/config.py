"""Gateway endpoint resolution and paper/live identification.

The API carries no credentials: the account is whichever one the Gateway (or TWS) is logged into. Port
numbers are a convention only (Gateway 4002 paper / 4001 live; TWS 7497 / 7496), so the trading mode is
determined from both the port and the account reported by the Gateway (see ``gates.account_mode``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

#: port -> (application, mode) by IB's defaults. A Gateway can be configured to any port.
PORTS = {4001: ("gateway", "live"), 4002: ("gateway", "paper"), 7496: ("tws", "live"), 7497: ("tws", "paper")}
PAPER_PORTS = {p for p, (_, mode) in PORTS.items() if mode == "paper"}

#: Paper accounts: DU (individual), DF (advisor master). Live: U, F, I, ...
PAPER_PREFIXES = ("DU", "DF")

MARKET_DATA_TYPES = {"live": 1, "realtime": 1, "frozen": 2, "delayed": 3, "delayed_frozen": 4}


def _env(*names: str, default=None):
    for n in names:
        v = os.environ.get(n)
        if v not in (None, ""):
            return v
    return default


@dataclass(frozen=True)
class Endpoint:
    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int | None = None          # None: lease one (client_ids.lease)

    @property
    def mode(self) -> str:
        """``paper``/``live`` by port convention, ``unknown`` for a custom port."""
        return PORTS.get(self.port, ("", "unknown"))[1]

    @classmethod
    def resolve(cls, cfg: dict | None = None) -> "Endpoint":
        """Config keys ``host``/``port``/``client_id``, then env ``IB_HOST``/``IBKR_HOST``, ``IB_PORT``/
        ``IBKR_PORT``, ``IB_CLIENT_ID``, then the paper Gateway on localhost."""
        cfg = cfg or {}
        cid = cfg.get("client_id", _env("IB_CLIENT_ID"))
        return cls(host=str(cfg.get("host") or _env("IB_HOST", "IBKR_HOST", default="127.0.0.1")),
                   port=int(cfg.get("port") or _env("IB_PORT", "IBKR_PORT", default=4002)),
                   client_id=None if cid in (None, "") else int(cid))


def is_paper_account(account: str) -> bool:
    return str(account).upper().startswith(PAPER_PREFIXES)


def redact(account: str) -> str:
    """``DU1234567`` -> ``DU*******``: account ids stay out of logs and notes."""
    a = str(account)
    head = len(a) - len(a.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"))
    return a[:head] + "*" * (len(a) - head)


def market_data_type(spec) -> int:
    """``live``/``frozen``/``delayed``/``delayed_frozen`` or 1-4 -> the ``reqMarketDataType`` code.

    Default 3 (delayed): it serves live ticks where the account is subscribed and delayed ones otherwise,
    where type 1 fails with 10089 on anything unsubscribed."""
    if spec is None:
        return 3
    if isinstance(spec, (int, float)) and int(spec) in (1, 2, 3, 4):
        return int(spec)
    try:
        return MARKET_DATA_TYPES[str(spec).strip().lower().replace("-", "_")]
    except KeyError:
        raise ValueError(f"unknown market data type {spec!r}") from None
