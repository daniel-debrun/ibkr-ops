# Operations

## Gateway

- **Trading mode.** The API carries no credentials: orders go to whichever account the Gateway is logged into. Port conventions are 4002 (Gateway, paper), 4001 (Gateway, live), 7497 (TWS, paper) and 7496 (TWS, live). ibkr-ops checks both the port and the account reported by the Gateway; paper accounts begin with `DU` or `DF`.
- **Read-Only API.** With the Gateway's *Read-Only API* setting enabled, orders are refused with error 321. Enable it on hosts that should never trade.
- **Availability.** Without IBC, the Gateway requires a manual login after every restart or disconnect. `deploy/gateway_keepalive.py` and `deploy/systemd/ibgateway.service` provide a reference configuration using ib_async's `IBC` and `Watchdog`. Periodic two-factor re-authentication still requires manual approval.
- **Competing sessions.** A login with the same user on another platform (mobile, web, TWS) causes 10197 / 162 "competing live session" errors.
- **Updates.** The Gateway may auto-update. Run `python -m ibkr_ops probe --handshake` after updates.

## Availability checks

| Command | Verifies |
|---|---|
| `python -m ibkr_ops probe` | The API port accepts connections |
| `python -m ibkr_ops probe --handshake` | An API session can be established (read-only; places nothing) |
| `probe.entitled(session, contract)` | A minimal historical request succeeds |

## Scheduling

Debian/Ubuntu cron ignores `CRON_TZ` and schedules in the host's local time. Either:

- write local times in the crontab (`deploy/crontab.example`) and declare each job's UTC window in code, or
- use systemd timers (`deploy/systemd/ibkr-job@.timer`), which accept an explicit time zone and, with `Persistent=true`, run jobs missed while the host was off.

`scripts/cron-run.sh <timeout> <command…>` adds a hard timeout (`timeout -k 30`), a minimal environment and UTC timestamps on every output line.

## Job guard

```python
sys.exit(cron.run("job-name", main, require_gateway=True, window=("19:50Z", 70), trading_day=True))
```

| Exit code | Meaning |
|---|---|
| 0 | Success |
| 1 | Unhandled error |
| 2 | Refused (gates, kill switch, signature verification) |
| 3 | Gateway unavailable |
| 4 | Partial failure (one or more steps failed) |
| 5 | Skipped (previous run still holds the lock; not a trading day) |
| 6 | Misfired (started outside the declared window) |

Codes 1, 3, 4 and 6 trigger an alert, as does a skip caused by a held lock.

## Heartbeats and alerting

Each guarded run writes `<state>/heartbeat/<job>.json` with start and finish times, exit code, step results and detail. A run that never starts writes nothing, so heartbeats must be checked by an independent schedule:

```bash
python -m ibkr_ops heartbeats job-a=26 job-b=8 --alert
```

This reports jobs that are stale, failed, misfired or have never run. Alerts invoke `$IBKR_OPS_ALERT_CMD <message>` (e.g. `notify-send ibkr-ops`, or a webhook script) and are always logged.

## Client ids

| Role | Id band |
|---|---|
| `cron-quotes` | 100–119 |
| `cron-history` | 200–219 |
| `cron-orders` | 300–319 |
| `interactive` | 500–519 |
| `probe` | 900–919 |

Id 0 is reserved for the Gateway's master client. An explicit `IB_CLIENT_ID` bypasses leasing.

## Environment variables

| Variable | Purpose |
|---|---|
| `IB_HOST` / `IBKR_HOST` | Gateway host (default `127.0.0.1`) |
| `IB_PORT` / `IBKR_PORT` | Gateway port (default `4002`) |
| `IB_CLIENT_ID` | Fixed client id (disables leasing) |
| `IBKR_ALLOW_LIVE` | Must be `1`, together with `Policy.allow_live`, for live orders |
| `IBKR_OPS_STATE` | State directory (locks, leases, heartbeats) |
| `IBKR_OPS_ALERT_CMD` | Alert command; the message is appended as the last argument |
| `IBKR_GATEWAY_VERSION`, `IBKR_TRADING_MODE`, `IBC_PATH`, `IBC_INI` | Gateway keep-alive configuration |
