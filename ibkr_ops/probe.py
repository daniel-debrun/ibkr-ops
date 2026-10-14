"""Gateway availability checks, in increasing cost:

1. ``port_open``: the API port accepts TCP connections.
2. ``handshake``: an API session is established (the Gateway is logged in and the API is enabled).
3. ``entitled``: a minimal historical request succeeds (subscriptions, competing sessions and client
   version are all in order). Only a real request establishes this.
"""
from __future__ import annotations

import socket

from .config import Endpoint, redact


def port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def handshake(endpoint: Endpoint | None = None, timeout: float = 10.0) -> dict:
    """Connect read-only (no order sync) with a leased probe clientId; report accounts (redacted), port
    mode and server version. Places nothing and requests no market data."""
    from .errors import GatewayDown
    from .session import Session

    ep = endpoint or Endpoint.resolve()
    out = {"host": ep.host, "port": ep.port, "port_mode": ep.mode, "ok": False}
    if not port_open(ep.host, ep.port):
        out["error"] = "port closed"
        return out
    try:
        with Session(ep, role="probe", connect_timeout=timeout) as s:
            out.update(ok=True, client_id=s.client_id, server_version=s.ib.client.serverVersion(),
                       accounts=[redact(a) for a in s.accounts], link_ok=s.link_ok, notices=s.notices[-5:])
    except GatewayDown as e:
        out["error"] = str(e)
    return out


def entitled(session, contract, what: str | None = None) -> tuple[bool, str]:
    """One tiny history request (``2 D`` of daily bars): proves the data path end to end."""
    try:
        bars = session.history(contract, duration="2 D", bar_size="1 day", what=what, timeout=20, backoff=())
    except Exception as e:  # noqa: BLE001 - every failure is the answer here
        return False, str(e)
    return (bool(bars), f"{len(bars)} bars" if bars else "no bars returned")
