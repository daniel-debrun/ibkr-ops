# Order safety

## Policy

`gates.Policy` (load with `Policy.from_dict`; unknown keys are rejected):

| Field | Default | Effect |
|---|---|---|
| `place` | `False` | When false, decisions are dry runs |
| `allow_live` | `False` | Live accounts additionally require `IBKR_ALLOW_LIVE=1` |
| `kill_switch` | `None` | Path; while the file exists, all orders are blocked |
| `sec_types` | `STK, CASH, OPT, FCX` | Permitted security types |
| `allowlist` | `None` | Permitted instrument keys or symbols (`None` = any) |
| `max_order_value` | 10,000 | Per-order notional cap |
| `max_batch_value` | 25,000 | Per-batch notional cap |
| `max_daily_value` | 50,000 | Notional cap per UTC day, including previously sent orders |
| `max_orders` | 50 | Orders per batch |
| `allow_market` | `False` | Market orders permitted |
| `max_slippage` | 0.02 | Maximum `abs(limit - reference) / reference` |
| `max_quote_age_s` | 1200 | Maximum age of the reference quote |
| `allow_delayed_quotes` | `False` | Accept delayed reference quotes |
| `all_or_nothing` | `True` | Any refused order blocks the batch |

## Checks

Batch level: kill switch; account reported by the Gateway; account/port agreement; live authorisation; order count; batch and daily value.

Per order: security type; allowlist; non-zero whole quantity (fractional only for cash); idempotency key not previously sent and not duplicated in the batch; reference quote present, live and fresh; limit price present (unless market orders are allowed), positive and within the slippage bound; event-contract limits in (0, 1); notional within `max_order_value`.

`gates.check` returns a `Decision` with mode `blocked`, `dry-run`, `paper` or `live`, and the reasons for every refusal.

## Execution sequence

`orders.execute(session, intents, policy, ledger)`:

1. Reads today's sent value and all sent idempotency keys from the ledger.
2. Runs `gates.check` and journals the decision.
3. For each allowed order: runs IB's what-if check (optional), journals `sent`, then transmits.
4. Awaits order status; optionally cancels unfilled quantity after `rest_seconds`.
5. Journals the final status of each order.

A `Session` opened with `readonly=True` (the default) refuses execution before anything is journalled.

## Ledger

Append-only JSONL; one record per event:

| `event` | Fields |
|---|---|
| `decision` | `mode`, `account` (redacted), `blocked`, `allowed`, `refused` |
| `what_if` / `what_if_refused` | `idem_key`, margin change, commission, warning / error |
| `sent` | `idem_key`, `key`, `qty`, `limit`, `value`, `mode` |
| `status` | `idem_key`, `order_id`, `status`, `filled`, `remaining`, `avg_price`, `error` |

## Signed inputs

`signing.verify(path.sig.json, trusted)` accepts a data file only if the ed25519 signature matches the manifest, the signing public key (or its SHA-256 fingerprint) is in `trusted`, the data file's SHA-256 matches, and `expires_at` (which must carry a UTC offset) has not passed. Key identifiers inside the manifest are not used for trust.
