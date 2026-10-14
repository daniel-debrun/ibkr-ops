# Architecture

## Layering

```
application / scheduled job
        │
   ibkr_ops.cron        scheduled-job guard: lock, logging, pre-flight, schedule window, exit codes, heartbeat
   ibkr_ops.orders      gated execution: gates → ledger → what-if → place → confirm
   ibkr_ops.gates       pre-trade policy (pure)
   ibkr_ops.session     ib_async adapter with stricter defaults
        │
   ib_async.IB          client: protocol, event loop, order/position state
        │
   IB Gateway / TWS
```

Control flow for scheduled jobs and order execution is diagrammed in [flows.md](flows.md).

Modules without network I/O (`errors`, `config`, `client_ids`, `pacing`, `clean`, `history`, `contracts`, `gates`, `signing`, `io`, `tradingdays`, `cron`) can be used independently of a Gateway connection. They operate on plain values and duck-typed objects.

## Session behaviour relative to ib_async

`Session` wraps `ib_async.IB` and changes the following defaults (verified against ib_async 2.1.0):

| Area | ib_async default | `Session` behaviour |
|---|---|---|
| Request errors | `RaiseRequestErrors = False`: a failed request completes with an empty result | Enabled; errors raise `IBError` / `PacingError`. "No data" responses (200, 162 HMDS) return `[]`. |
| History timeouts | Request cancelled; empty list returned | `TimeoutError` raised, so callers can back off |
| Read-only mode | `readonly=True` only skips order synchronisation; `placeOrder` still transmits | Read-only by default; `place` / `what_if` raise `ReadOnlySession` |
| Contract qualification | `qualifyContracts` issues lookups concurrently | `qualify` / `contract_details` are serialised per process |
| Order code 321 | New order left in `ValidationError` (counted as active) | Reported as `Rejected` unless the order reached a working state |
| End anchors | A `date` is formatted as 23:59:59 local time | Anchors sent as explicit UTC strings (`yyyyMMdd-HH:mm:ss`) |
| Client ids | Caller-supplied; a duplicate surfaces as a connect timeout | Leased per role; error 326 advances to the next id |
| Startup sync | Positions, orders, executions and account updates fetched | Read-only sessions skip order, execution and account sync |
| Gateway unavailable | Connection error raised by the event loop | Port pre-check raises `GatewayDown` immediately |

The underlying `IB` instance is available as `Session.ib` for functionality not wrapped here.

## Error model

`errors.classify(code, message)` assigns each IB code a `Kind`:

| Kind | Effect |
|---|---|
| `INFO`, `WARNING` | Logged; never terminates a request |
| `CONNECTIVITY` | Tracked in `Session.link_ok` / `notices` |
| `CLIENT_ID` | Triggers a retry with the next leased client id |
| `PACING` | Raises `PacingError`; retried with backoff |
| `NO_DATA` | Returns an empty result |
| `ENTITLEMENT`, `REJECTED`, `FATAL` | Raises `IBError` |

Code 162 is classified by its message (pacing violation, no data, cancelled, competing session).

## State

Locks, client-id leases and heartbeats are stored under `$IBKR_OPS_STATE` (default `$XDG_STATE_HOME/ibkr-ops`, i.e. `~/.local/state/ibkr-ops`). Order ledgers and data outputs are written to caller-supplied paths.
