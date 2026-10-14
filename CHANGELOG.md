# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

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
