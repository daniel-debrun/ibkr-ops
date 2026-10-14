"""Classification of IB API error and notice codes.

The TWS API reports failures, warnings and status notices through a single error channel. ``classify`` maps
a code (and, for code 162, its message) to a ``Kind`` that determines handling: notices and warnings never
terminate a request; connectivity and client-id errors are tracked at session level; ``NO_DATA`` is a valid
empty answer; all other kinds terminate the request.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Kind(str, Enum):
    INFO = "info"                  # status notice; never ends a request
    WARNING = "warning"            # worth logging; the request continues
    CONNECTIVITY = "connectivity"  # Gateway <-> IB link lost/restored, or client <-> Gateway socket lost
    CLIENT_ID = "client_id"        # another session already holds this clientId: pick another
    PACING = "pacing"              # slow down and retry
    NO_DATA = "no_data"            # a valid answer: nothing there (no contract, no bars)
    ENTITLEMENT = "entitlement"    # missing subscription, or a competing live session holds the data
    REJECTED = "rejected"          # an order was refused
    FATAL = "fatal"                # the request failed


@dataclass(frozen=True)
class Code:
    kind: Kind
    meaning: str
    action: str


CODES: dict[int, Code] = {
    # --- connectivity -------------------------------------------------------------------------------
    326: Code(Kind.CLIENT_ID, "client id already in use", "retry with another clientId (see client_ids.lease)"),
    502: Code(Kind.CONNECTIVITY, "couldn't connect to TWS/Gateway", "check the Gateway is running and the API port is enabled"),
    504: Code(Kind.CONNECTIVITY, "not connected", "reconnect"),
    1100: Code(Kind.CONNECTIVITY, "Gateway lost its connection to IB", "pause requests until 1101/1102"),
    1101: Code(Kind.CONNECTIVITY, "connection restored, data lost", "resubscribe market data"),
    1102: Code(Kind.CONNECTIVITY, "connection restored, data maintained", "carry on"),
    1300: Code(Kind.CONNECTIVITY, "socket port reset", "reconnect on the new port"),
    # --- status notices -----------------------------------------------------------------------------
    2104: Code(Kind.INFO, "market data farm connection is OK", "none"),
    2106: Code(Kind.INFO, "HMDS data farm connection is OK", "none"),
    2158: Code(Kind.INFO, "sec-def data farm connection is OK", "none"),
    2103: Code(Kind.WARNING, "market data farm connection is broken", "expect timeouts until it recovers"),
    2105: Code(Kind.WARNING, "HMDS data farm connection is broken", "expect history timeouts until it recovers"),
    2157: Code(Kind.WARNING, "sec-def data farm connection is broken", "expect contract lookups to stall"),
    2174: Code(Kind.WARNING, "timezone-less endDateTime is deprecated", "send 'yyyyMMdd-HH:mm:ss' in UTC (history.end_anchor)"),
    2176: Code(Kind.WARNING, "fractional share size rules", "none"),
    10167: Code(Kind.INFO, "requested market data is not subscribed; displaying delayed data", "none: tag rows price_type=delayed"),
    10090: Code(Kind.WARNING, "part of the requested market data is not subscribed", "fields may be missing"),
    399: Code(Kind.WARNING, "order message (e.g. will not be placed until the market opens)", "log; the order is still working"),
    # --- no data ------------------------------------------------------------------------------------
    200: Code(Kind.NO_DATA, "no security definition found", "treat as []; negative-cache the lookup"),
    # --- entitlement ----------------------------------------------------------------------------------
    354: Code(Kind.ENTITLEMENT, "requested market data is not subscribed", "use market data type 3 (delayed) or subscribe"),
    10089: Code(Kind.ENTITLEMENT, "requested market data requires additional API subscription", "free non-consolidated quotes don't reach the API: use type 3 or subscribe"),
    10168: Code(Kind.ENTITLEMENT, "not subscribed and delayed data is not enabled", "enable delayed data or subscribe"),
    10197: Code(Kind.ENTITLEMENT, "no market data during a competing live session", "log out the other (mobile/web/TWS) session"),
    # --- fatal / version ----------------------------------------------------------------------------
    10285: Code(Kind.FATAL, "API version does not support fractional size rules", "the client is too old for this Gateway: upgrade ib_async (or ibapi 10.45+ from the TWS API zip; PyPI's ibapi 9.81 fails every history call)"),
    321: Code(Kind.FATAL, "error validating request (incl. 'API is in Read-Only mode')", "for orders: untick Read-Only API in the Gateway, deliberately"),
    103: Code(Kind.FATAL, "duplicate order id", "take a fresh id from nextValidId/reqIds"),
    # --- orders -------------------------------------------------------------------------------------
    201: Code(Kind.REJECTED, "order rejected", "read the message; never retry blindly"),
    110: Code(Kind.REJECTED, "price does not conform to the minimum price variation", "round to the contract's tick"),
    202: Code(Kind.INFO, "order cancelled", "none"),
    # --- pacing -------------------------------------------------------------------------------------
    420: Code(Kind.PACING, "invalid real-time query / pacing violation", "back off"),
    # --- benign follow-ups --------------------------------------------------------------------------
    300: Code(Kind.INFO, "can't find EId with ticker id (cancel of a finished request)", "none"),
    366: Code(Kind.INFO, "no historical data query found for ticker id", "none"),
}


def classify(code: int, msg: str = "") -> Kind:
    """The kind of an IB error ``code``; ``msg`` disambiguates the overloaded 162."""
    code = int(code)
    if code == 162:                                # one code, three meanings: read the text
        m = msg.lower()
        if "pacing" in m:
            return Kind.PACING
        if "no data" in m or "returned no data" in m:
            return Kind.NO_DATA                    # e.g. ForecastEx bid/ask history, no trades in window
        if "cancel" in m:
            return Kind.INFO                       # "API historical data query cancelled"
        if "competing live session" in m:
            return Kind.ENTITLEMENT
        return Kind.FATAL
    if code in CODES:
        return CODES[code].kind
    if 2100 <= code < 2200:                        # 21xx are farm/status notices
        return Kind.INFO
    return Kind.FATAL


def ends_request(kind: Kind) -> bool:
    """Whether an error carrying a reqId should end the wait on that request."""
    return kind not in (Kind.INFO, Kind.WARNING)


def describe(code: int, msg: str = "") -> str:
    c = CODES.get(int(code))
    kind = classify(code, msg)
    hint = f" -> {c.action}" if c else ""
    return f"[{code} {kind.value}] {msg}{hint}"


class IBError(RuntimeError):
    """An IB error that ended a request. ``kind`` says what to do about it."""

    def __init__(self, code: int, msg: str, req_id: int | None = None):
        self.code, self.msg, self.req_id = int(code), msg, req_id
        self.kind = classify(code, msg)
        super().__init__(describe(code, msg))


class PacingError(IBError):
    pass


class GatewayDown(ConnectionError):
    """No Gateway answered: the port is closed, or the socket opened but no handshake came back."""


class ClientIdInUse(ConnectionError):
    pass
