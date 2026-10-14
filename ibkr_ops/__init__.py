"""ibkr-ops: an operational safety layer for Interactive Brokers built on ib_async.

ib_async provides the client (protocol, event loop, order and position state). This package adds the
controls required for unattended operation (order gates, request pacing, client-id leasing, a scheduled-job
guard with heartbeats) and replaces several ib_async defaults with stricter behaviour (see ``session``).

Modules without network I/O: ``errors``, ``config``, ``client_ids``, ``pacing``, ``clean``, ``history``,
``contracts``, ``gates``, ``signing``, ``io``, ``tradingdays``, ``cron``.
Modules using ib_async: ``session``, ``orders``, ``probe.handshake``.

Documentation: ``docs/`` in the source distribution.
"""
__version__ = "0.1.0"
