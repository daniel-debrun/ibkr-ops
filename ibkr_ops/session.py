"""A blocking API session built on ib_async, with stricter defaults for unattended use.

    with Session(role="cron-quotes") as s:          # pre-flight, leased client id, connect, data type
        rows = s.quotes({"SPY": to_contract("SPY")})
        bars = s.history(to_contract("SPY"), duration="1 M", bar_size="1 day")
        s.ib                                        # the underlying ib_async.IB

Behaviour relative to ib_async defaults:

* **Read-only by default.** ib_async's ``readonly`` flag only skips order synchronisation; ``placeOrder``
  remains available. A read-only ``Session`` refuses ``place`` and ``what_if``.
* **Request errors raise.** ``IB.RaiseRequestErrors`` is enabled and errors are mapped to ``errors.IBError``
  kinds. "No data" responses (200, 162 HMDS) return ``[]``.
* **History timeouts raise** ``TimeoutError`` (ib_async returns an empty list), enabling backoff.
* **History is paced** by a ``Pacer`` with 30/60/120 s backoff.
* **End anchors are explicit UTC strings.** ib_async interprets a bare ``date`` as local time.
* **Contract lookups are serialised.** ``IB.qualifyContracts`` issues lookups concurrently; ``qualify``
  issues them one at a time and requires an unambiguous match.
* **Pre-flight and client ids.** A closed port raises ``GatewayDown`` immediately; client ids are leased per
  role, and error 326 advances to the next id.
* **Order rejection.** A new order receiving code 321 is left in ``ValidationError`` (an active state) by
  ib_async; ``await_orders`` reports it as ``Rejected`` unless it reached a working state.
* **Connectivity tracking.** Codes 1100 / 1101 / 1102 update ``link_ok``; all notices are retained in
  ``notices``.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from . import client_ids, clean, history, probe
from .config import Endpoint, market_data_type, redact
from .errors import ClientIdInUse, GatewayDown, IBError, Kind, PacingError, classify, describe
from .pacing import Pacer, with_backoff

log = logging.getLogger("ibkr_ops")

WORKING = {"PreSubmitted", "Submitted", "Filled"}
DONE = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}
_TIMEOUTS = (TimeoutError, asyncio.TimeoutError)


def _default_ib():
    from ib_async import IB

    return IB()


def contract_key(c) -> tuple:
    return (getattr(c, "conId", 0), getattr(c, "symbol", ""), getattr(c, "secType", ""),
            getattr(c, "lastTradeDateOrContractMonth", ""), getattr(c, "strike", 0.0), getattr(c, "right", ""),
            getattr(c, "exchange", ""), getattr(c, "currency", ""))


class ReadOnlySession(PermissionError):
    pass


class Session:
    """One API connection. Use as a context manager; ``close`` is idempotent."""

    _lookup_lock = threading.Lock()              # one contract lookup at a time per process

    def __init__(self, endpoint: Endpoint | None = None, *, role: str = "interactive", id_span: int = 20,
                 readonly: bool = True, market_data: int | str = 3, connect_timeout: float = 15.0,
                 request_timeout: float = 30.0, pacer: Pacer | None = None,
                 ib_factory: Callable | None = None, check_port: bool = True, sync_on_connect: bool | None = None):
        self.endpoint = endpoint or Endpoint.resolve()
        self.role, self.id_span, self.readonly = role, id_span, readonly
        self.market_data = market_data_type(market_data)
        self.connect_timeout, self.request_timeout = connect_timeout, request_timeout
        self.pacer = pacer or Pacer()
        self.ib_factory = ib_factory or _default_ib
        self.check_port = check_port
        # a read-only job doesn't need executions/account updates synced at connect; order jobs do
        self.sync_on_connect = (not readonly) if sync_on_connect is None else sync_on_connect
        self.ib = None
        self.client_id: int | None = None
        self.link_ok = True
        self.notices: list[str] = []
        self.order_errors: dict[int, list[str]] = {}
        self._quote_errors: dict[tuple, str] = {}
        self._cid_rejected = False
        self._stack = contextlib.ExitStack()

    # --- lifecycle --------------------------------------------------------------------------------------
    def __enter__(self) -> "Session":
        try:
            self.connect()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> None:
        ep = self.endpoint
        if self.check_port and not probe.port_open(ep.host, ep.port):
            raise GatewayDown(f"nothing listening on {ep.host}:{ep.port}: is IB Gateway running and logged in?")
        if ep.client_id is not None:
            self._connect(ep.client_id)
            return
        base = client_ids.ROLES.get(self.role, client_ids.ROLES["interactive"])
        tried: set[int] = set()
        while True:
            cm = client_ids.lease(base, self.id_span, frozenset(tried))
            cid = cm.__enter__()                    # NoFreeClientId propagates when the band is exhausted
            try:
                self._connect(cid)
            except ClientIdInUse:
                cm.__exit__(None, None, None)
                tried.add(cid)
                continue
            except BaseException:
                cm.__exit__(None, None, None)
                raise
            self._stack.callback(cm.__exit__, None, None, None)
            return

    def _connect(self, cid: int) -> None:
        from ib_async import StartupFetchALL, StartupFetchNONE

        self._disconnect()
        ib = self.ib = self.ib_factory()
        ib.RaiseRequestErrors = True
        ib.errorEvent += self._on_error
        ib.disconnectedEvent += self._on_disconnect
        self._cid_rejected = False
        try:
            ib.connect(self.endpoint.host, self.endpoint.port, clientId=cid, timeout=self.connect_timeout,
                       readonly=self.readonly,
                       fetchFields=StartupFetchALL if self.sync_on_connect else StartupFetchNONE)
        except (*_TIMEOUTS, ConnectionError, OSError) as e:
            self._disconnect()
            if self._cid_rejected:
                raise ClientIdInUse(f"clientId {cid} is in use") from None
            raise GatewayDown(f"no API handshake from {self.endpoint.host}:{self.endpoint.port} "
                              f"({type(e).__name__}: {e}); is the Gateway logged in with the API enabled?") from None
        if self._cid_rejected:
            self._disconnect()
            raise ClientIdInUse(f"clientId {cid} is in use")
        self.client_id = cid
        ib.reqMarketDataType(self.market_data)
        log.info("connected %s:%s clientId=%s accounts=%s port-mode=%s readonly=%s", self.endpoint.host,
                 self.endpoint.port, cid, ",".join(redact(a) for a in self.accounts), self.endpoint.mode,
                 self.readonly)

    def _disconnect(self) -> None:
        if self.ib is not None:
            with contextlib.suppress(Exception):
                self.ib.errorEvent -= self._on_error
                self.ib.disconnectedEvent -= self._on_disconnect
            with contextlib.suppress(Exception):
                self.ib.disconnect()
        self.ib = None

    def close(self) -> None:
        self._disconnect()
        self._stack.close()

    def _on_disconnect(self) -> None:
        self.link_ok = False
        log.warning("gateway: disconnected")

    def _on_error(self, reqId, code, msg, contract=None) -> None:
        kind = classify(code, msg)
        text = describe(code, msg)
        if reqId == -1:
            if kind is Kind.CLIENT_ID:
                self._cid_rejected = True
            elif kind is Kind.CONNECTIVITY:
                self.link_ok = code not in (1100, 504)
            (log.debug if kind is Kind.INFO else log.warning)("gateway: %s", text)
            self.notices.append(text)
            return
        if self.ib is not None and (self.ib.wrapper.clientId, reqId) in getattr(self.ib.wrapper, "trades", {}):
            self.order_errors.setdefault(reqId, []).append(text)
        if contract is not None and kind not in (Kind.INFO, Kind.WARNING):
            self._quote_errors[contract_key(contract)] = text
        (log.debug if kind is Kind.INFO else log.info)("req %s: %s", reqId, text)

    @property
    def accounts(self) -> list[str]:
        return list(self.ib.managedAccounts()) if self.ib else []

    @property
    def account(self) -> str:
        acc = self.accounts
        if len(acc) != 1:
            raise RuntimeError(f"expected one account from the Gateway, got {len(acc)}; pass one explicitly")
        return acc[0]

    # --- requests ---------------------------------------------------------------------------------------
    def _need(self) -> None:
        if self.ib is None or not self.ib.isConnected():
            raise GatewayDown("not connected")

    def _call(self, fn, *args, timeout: float | None = None, **kwargs):
        """Run a blocking ib_async call with a timeout, mapping its errors to ours."""
        from ib_async.wrapper import RequestError

        self._need()
        old = self.ib.RequestTimeout
        self.ib.RequestTimeout = timeout or self.request_timeout
        try:
            return fn(*args, **kwargs)
        except RequestError as e:
            kind = classify(e.code, e.message)
            if kind is Kind.NO_DATA:
                return []
            raise (PacingError if kind is Kind.PACING else IBError)(e.code, e.message, e.reqId) from None
        except _TIMEOUTS:
            raise TimeoutError(f"{getattr(fn, '__name__', 'request')} got no answer in "
                               f"{timeout or self.request_timeout:.0f}s") from None
        finally:
            if self.ib is not None:
                self.ib.RequestTimeout = old

    def contract_details(self, contract, timeout: float = 30.0) -> list:
        """``reqContractDetails``, one at a time across the process. Unknown contract (200) -> []."""
        with self._lookup_lock:
            return self._call(self.ib.reqContractDetails, contract, timeout=timeout)

    def qualify(self, contract, timeout: float = 30.0):
        """The single contract matching ``contract`` (US listing preferred for stocks), else None."""
        from .contracts import pick_listing

        details = self.contract_details(contract, timeout=timeout)
        if not details:
            return None
        if len(details) > 1 and getattr(contract, "secType", "") != "STK":
            log.warning("ambiguous contract %s: %d matches", contract, len(details))
            return None
        return pick_listing(details)

    def history(self, contract, end="", duration: str = "1 M", bar_size: str = "1 day", what: str | None = None,
                use_rth: bool = True, timeout: float = 60.0, backoff=(30.0, 60.0, 120.0)) -> list:
        """ib_async ``BarData`` list (``history.bar_rows`` tidies it). Paced; pacing errors and timeouts back
        off. ``formatDate=2``: intraday bars come back as tz-aware UTC datetimes."""
        from ib_async.wrapper import RequestError

        size = history.bar_size(bar_size)
        what = what or history.default_what_to_show(getattr(contract, "secType", "STK"))
        anchor = history.end_anchor(end) if end else ""
        ckey = contract_key(contract)
        key = (ckey, anchor, duration, size, what, use_rth)

        def once():
            self._need()
            self.pacer.wait(key=key, contract=(ckey, what))
            t0 = time.monotonic()
            try:
                bars = self.ib.reqHistoricalData(contract, anchor, duration, size, what, use_rth,
                                                 formatDate=2, timeout=timeout)
            except RequestError as e:
                kind = classify(e.code, e.message)
                if kind is Kind.NO_DATA:
                    return []
                raise (PacingError if kind is Kind.PACING else IBError)(e.code, e.message, e.reqId) from None
            if not bars and time.monotonic() - t0 >= timeout * 0.95:
                raise TimeoutError(f"history for {ckey[1] or ckey[0]} got no answer in {timeout:.0f}s")
            return list(bars)

        return with_backoff(once, delays=backoff, log=log.warning)

    def quotes(self, contracts: dict, wait: float = 4.0, batch: int = 90, generic: str = "",
               want_greeks: bool = False) -> dict:
        """``{key: contract}`` -> ``{key: clean row}`` (``clean.ticker_row`` plus ``error``).

        Streams ``batch`` lines at a time (accounts start at ~100 lines), waits up to ``wait`` s, then cancels.
        Streaming is free where ``snapshot=True`` is billed per request."""
        self._need()
        out, items = {}, list(contracts.items())
        for i in range(0, len(items), batch):
            chunk = items[i:i + batch]
            tickers = {key: self.ib.reqMktData(c, generic, False, False) for key, c in chunk}
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                if all(self._quote_done(t, c, want_greeks) for (key, c), t in zip(chunk, tickers.values())):
                    break
                self.ib.sleep(0.1)
            at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for key, c in chunk:
                with contextlib.suppress(Exception):
                    self.ib.cancelMktData(c)
                row = clean.ticker_row(tickers[key], at)
                row["error"] = self._quote_errors.pop(contract_key(c), None)
                out[key] = row
        return out

    def _quote_done(self, t, c, want_greeks: bool) -> bool:
        if contract_key(c) in self._quote_errors:
            return True
        has_book = clean.price(t.bid) is not None and clean.price(t.ask) is not None
        if not want_greeks:
            return has_book
        oi = clean.num(t.callOpenInterest) is not None or clean.num(t.putOpenInterest) is not None
        return has_book and t.modelGreeks is not None and oi

    # --- orders (use orders.execute, which runs the gates first) ----------------------------------------
    def _writable(self) -> None:
        if self.readonly:
            raise ReadOnlySession("this session is read-only; open Session(readonly=False) to send orders")

    def what_if(self, contract, order, timeout: float = 15.0):
        """IB's margin/commission preview (``OrderState``); nothing is placed."""
        self._writable()
        return self._call(self.ib.whatIfOrder, contract, order, timeout=timeout)

    def place(self, contract, order):
        """Send one order; returns the ib_async ``Trade``. Gate it first (``orders.execute``)."""
        self._writable()
        self._need()
        return self.ib.placeOrder(contract, order)

    def cancel(self, trade) -> None:
        self._writable()
        self.ib.cancelOrder(trade.order)

    def await_orders(self, trades: list, timeout: float = 10.0) -> dict:
        """Placed is not accepted: wait until each order is working, done, or clearly refused.
        Returns ``{orderId: {"status", "filled", "remaining", "avg_price", "error"}}`` where status is
        ``Rejected`` for an order IB refused (an error before it ever went live), else IB's status."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(t.orderStatus.status in WORKING | DONE for t in trades):
                break
            self.ib.sleep(0.1)
        return {t.order.orderId: self.order_state(t) for t in trades}

    def order_state(self, trade) -> dict:
        st = trade.orderStatus.status
        errs = [f"[{e.errorCode}] {e.message}" for e in trade.log if getattr(e, "errorCode", 0)]
        errs += self.order_errors.get(trade.order.orderId, [])
        went_live = any(e.status in WORKING for e in trade.log)
        if (st in ("Cancelled", "Inactive", "ValidationError") and not went_live and errs
                and not trade.filled()):
            st = "Rejected"
        elif st in ("PendingSubmit", "ValidationError", ""):
            st = "Unacknowledged" if not errs else "Rejected"
        return {"status": st, "filled": trade.filled(), "remaining": trade.remaining(),
                "avg_price": trade.orderStatus.avgFillPrice or None, "error": "; ".join(dict.fromkeys(errs)) or None}
