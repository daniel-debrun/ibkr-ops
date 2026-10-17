# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- `cron.run(trading_day=True)` judged the trading day on the UTC date, so a US job running after 20:00 ET (EDT) saw the next day; it now uses the New York date.
- `cron.setup_logging` let ib_async's INFO lines (positions, account values) write full IB account ids to job logs; a filter now redacts account ids in every record.

## [0.1.0] - 2026-10-14

### Added
- `session.Session`: ib_async adapter with read-only default, raised request errors and history timeouts, serialised contract lookups, client-id leasing, port pre-check and connectivity tracking.
- `gates`: pre-trade order policy (dry run, paper-only, kill switch, caps, allowlists, quote freshness, slippage bound, idempotency, all-or-nothing).
- `orders.execute`: gated execution with an append-only ledger, IB what-if checks, status confirmation and optional rest-and-cancel.
- `pacing`: historical-data rate limiter and backoff.
- `cron`: scheduled-job guard with locking, UTC logging, Gateway pre-flight, schedule windows, step isolation, exit codes, heartbeats and alerting.
- `signing`: ed25519 verification of signed order inputs.
- Supporting modules: `errors`, `config`, `client_ids`, `clean`, `history`, `contracts`, `io`, `tradingdays`, `probe`, and a command-line interface.
- Deployment references: crontab example, systemd units and timer, Gateway keep-alive via IBC.
