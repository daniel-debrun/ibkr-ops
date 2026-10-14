"""A scripted stand-in for ``ib_async.IB``: the methods Session uses, returning ib_async's own objects
(Ticker, BarData, Trade, OrderStatus, RequestError) so the adapter is tested against the real shapes."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from types import SimpleNamespace

from eventkit import Event
from ib_async import BarData, Execution, Fill, OrderState, OrderStatus, Ticker, Trade, TradeLogEntry
from ib_async.wrapper import RequestError


@dataclass
class Script:
    account: str = "DU1234567"
    busy_ids: set = field(default_factory=set)       # clientIds answered with 326
    silent: bool = False                             # socket opens, no API handshake
    history: list = field(default_factory=list)      # per call: "bars" | "pacing" | "nodata" | "silent"
    details: dict = field(default_factory=dict)      # symbol -> list of details
    quotes: dict = field(default_factory=dict)       # symbol -> Ticker kwargs
    order_outcome: dict = field(default_factory=dict)  # symbol -> "fill" | "reject" | "readonly" | "rest"
    what_if_reject: set = field(default_factory=set)
    connects: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    cancelled: list = field(default_factory=list)
    placed: list = field(default_factory=list)


def bars(n=2):
    return [BarData(date=date(2026, 10, 13 + i), open=10.0 + i, high=10.0 + i, low=10.0 + i, close=10.0 + i,
                    volume=100, average=10.0 + i, barCount=5) for i in range(n)]


def factory(script: Script):
    def make():
        return FakeIB(script)
    return make


class FakeIB:
    RequestTimeout = 0.0
    RaiseRequestErrors = False

    def __init__(self, script: Script):
        self.s = script
        self.errorEvent = Event("errorEvent")
        self.disconnectedEvent = Event("disconnectedEvent")
        self.wrapper = SimpleNamespace(clientId=0, trades={})
        self.client = SimpleNamespace(serverVersion=lambda: 178)
        self._connected = False
        self._next = 1

    def _id(self):
        self._next += 1
        return self._next

    # --- connection -----------------------------------------------------------------------------------
    def connect(self, host, port, clientId, timeout, readonly, fetchFields):
        self.s.connects.append(clientId)
        self.s.calls.append(("connect", readonly, fetchFields))
        self.wrapper.clientId = clientId
        if clientId in self.s.busy_ids:
            self.errorEvent.emit(-1, 326, "Unable to connect as the client id is already in use.", None)
            raise ConnectionError("Socket disconnect")
        if self.s.silent:
            raise asyncio.TimeoutError()
        self._connected = True
        self.errorEvent.emit(-1, 2104, "Market data farm connection is OK:usfarm", None)
        return self

    def isConnected(self):
        return self._connected

    def disconnect(self):
        self._connected = False
        self.s.calls.append("disconnect")

    def managedAccounts(self):
        return [self.s.account]

    def reqMarketDataType(self, t):
        self.s.calls.append(("mdtype", t))

    def sleep(self, secs):
        time.sleep(secs)

    def _fail(self, rid, code, msg, contract=None):
        self.errorEvent.emit(rid, code, msg, contract)
        raise RequestError(rid, code, msg)

    # --- data -----------------------------------------------------------------------------------------
    def reqContractDetails(self, c):
        self.s.calls.append(("details", c.symbol))
        found = self.s.details.get(c.symbol)
        if found is None:
            self._fail(self._id(), 200, "No security definition has been found for the request", c)
        return found

    def reqHistoricalData(self, contract, end, duration, size, what, use_rth, formatDate=1, timeout=60):
        assert formatDate == 2
        self.s.calls.append(("history", end, duration, size, what))
        mode = self.s.history.pop(0) if self.s.history else "bars"
        rid = self._id()
        if mode == "pacing":
            self._fail(rid, 162, "Historical Market Data Service error message:API historical data query pacing violation", contract)
        if mode == "nodata":
            self._fail(rid, 162, "Historical Market Data Service error message:HMDS query returned no data: X", contract)
        if mode == "silent":
            time.sleep(timeout)          # what ib_async does: wait, cancel, return an empty list
            return []
        return bars()

    def reqMktData(self, c, generic, snapshot, regulatory):
        assert snapshot is False, "snapshots are billed; stream then cancel"
        kw = self.s.quotes.get(c.symbol)
        if kw is None:
            self.errorEvent.emit(self._id(), 200, "No security definition has been found for the request", c)
            return Ticker(contract=c)
        self.errorEvent.emit(self._id(), 10167, "Displaying delayed market data.", c)
        t = Ticker(contract=c)                    # Ticker resets prices in __post_init__; set them after
        for k, v in kw.items():
            setattr(t, k, v)
        return t

    def cancelMktData(self, c):
        self.s.cancelled.append(c.symbol)

    # --- orders ---------------------------------------------------------------------------------------
    def whatIfOrder(self, c, o):
        if c.symbol in self.s.what_if_reject:
            self._fail(self._id(), 201, "Order rejected - reason: insufficient margin", c)
        return OrderState(initMarginChange="100.0", commission=1.0)

    def placeOrder(self, c, o):
        oid = o.orderId = self._id()
        now = datetime.now(timezone.utc)
        t = Trade(contract=c, order=o, orderStatus=OrderStatus(orderId=oid, status="PendingSubmit"),
                  fills=[], log=[TradeLogEntry(now, "PendingSubmit", "")])
        self.wrapper.trades[(self.wrapper.clientId, oid)] = t
        self.s.placed.append((oid, c.symbol, o.action, float(o.totalQuantity), o.orderType))
        outcome = self.s.order_outcome.get(c.symbol, "fill")
        if outcome == "reject":
            self.errorEvent.emit(oid, 201, "Order rejected - reason: test", c)
            t.orderStatus.status = "Cancelled"
            t.log.append(TradeLogEntry(now, "Cancelled", "Error 201: Order rejected", 201))
        elif outcome == "readonly":               # ib_async: 321 is a "warning" -> ValidationError, stays active
            self.errorEvent.emit(oid, 321, "Error validating request: The API interface is currently in Read-Only mode.", c)
            t.orderStatus.status = "ValidationError"
            t.log.append(TradeLogEntry(now, "ValidationError", "Warning 321", 321))
        else:
            t.orderStatus.status = "Submitted"
            t.log.append(TradeLogEntry(now, "Submitted", ""))
            if outcome == "fill":
                t.fills.append(Fill(c, Execution(shares=float(o.totalQuantity), price=o.lmtPrice), None, now))
                t.orderStatus.status = "Filled"
                t.log.append(TradeLogEntry(now, "Filled", ""))
        return t

    def cancelOrder(self, order):
        t = self.wrapper.trades[(self.wrapper.clientId, order.orderId)]
        t.orderStatus.status = "Cancelled"
        t.log.append(TradeLogEntry(datetime.now(timezone.utc), "Cancelled", "Error 202: Order Canceled", 202))
        self.s.cancelled.append(order.orderId)
