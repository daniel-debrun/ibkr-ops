# IB API reference notes

Behaviour of the TWS API and IB Gateway relevant to automated use. These notes complement IB's official documentation.

## Error codes

| Code | Meaning | Handling |
|---|---|---|
| 2104, 2106, 2158 | Data farm connection OK | Notice |
| 2103, 2105, 2157 | Data farm connection broken | Warning; expect timeouts |
| 2174 | Timezone-less `endDateTime` deprecated | Send UTC anchors |
| 10167 | Not subscribed; displaying delayed data | Notice; tag data as delayed |
| 10090 | Part of requested data not subscribed | Warning |
| 399 | Order message (e.g. queued until open) | Warning |
| 200 | No security definition | Empty result; cache negatively |
| 162 | Pacing violation / no data / cancelled / competing session | Classified by message |
| 354, 10089, 10168 | Market data not subscribed | Use market data type 3 or subscribe |
| 10197 | Competing live session | Log out the other session |
| 326 | Client id in use | Use another client id |
| 1100 / 1101 / 1102 | Connectivity lost / restored (data lost / maintained) | Pause; resubscribe after 1101 |
| 502, 504 | Cannot connect / not connected | Reconnect |
| 10285 | Client version does not support fractional size rules | Upgrade the client |
| 321 | Request validation error (including Read-Only API mode) | Fatal for new orders |
| 201 | Order rejected | Do not retry automatically |
| 110 | Price does not conform to minimum tick | Round to tick |

## Historical data

- **Limits:** 60 requests per 10 minutes; no identical request within 15 seconds; at most 6 requests per contract, exchange and tick type within 2 seconds. A violation may appear as error 162 or as an unanswered request. Repeated violations lengthen the penalty.
- **Request size:** intraday requests of more than about one month per call are unreliable. 30-day chunks for bars of 5 minutes or more and 7-day chunks below are dependable; daily bars may be requested over multiple years in one call.
- **Anchors:** `endDateTime` as `yyyyMMdd-HH:mm:ss` in UTC; an empty string means now. There is no start parameter.
- **`formatDate=2`** returns intraday bar times as epoch seconds (UTC); otherwise times carry the instrument's time zone.
- **Adjustment:** TRADES is split-adjusted but not dividend-adjusted; ADJUSTED_LAST includes dividends. Do not combine with as-traded data without un-adjusting.
- **Coverage:** no delisted instruments; no history for expired options; no historical end-of-day option bars. Cash/FX and bonds have no TRADES data (use MIDPOINT). One `whatToShow` per request.
- **Bar sizes** must match IB's tokens exactly (`1 min`, `5 mins`, `1 hour`, `1 day`, …).

## Market data

- Market data type 1 (live) fails with 10089 for unsubscribed instruments; type 3 (delayed) returns live data where subscribed and delayed data otherwise. Delayed ticks use tick types 66–76.
- Free non-consolidated quotes are not available through the API.
- Snapshot requests are billed per request; streaming followed by cancellation is not.
- Paper accounts receive only subscriptions shared from the associated live account. Subscriptions are billed per calendar month without proration.
- Option model greeks arrive as tick type 13 (live) or 83 (delayed); open interest requires generic tick 101 (tick types 27/28).
- Absent values are encoded as sentinels (−1, −2, `DBL_MAX`, `UNSET_DECIMAL`); see `ibkr_ops.clean`.
- IB market data is licensed for the subscriber's use; redistribution is restricted.

## Contract lookups

- Concurrent `reqContractDetails` requests can stall IB's contract-definition service; issue them sequentially.
- Resolve contracts once and reuse `conId`; requests by `conId` do not depend on the lookup service.
- Cache "no security definition" (200) responses to avoid repeated lookups of unlisted instruments.

## ForecastEx event contracts

- Products are listed as `IND` on `FORECASTX`; tradeable contracts are `secType="OPT"`, `exchange="FORECASTX"`, multiplier 1, paying $1, with `right` C = YES and P = NO.
- Discovery: `reqContractDetails(Contract(symbol=<product>, secType="OPT", exchange="FORECASTX"))`. `reqSecDefOptParams` returns no results; `secType="EC"` is rejected (200).
- `localSymbol` format: `<PRODUCT>_<MMDDYY>_<STRIKE>_<YES|NO>`. `lastTradeDateOrContractMonth` is the target date plus one day.
- No historical bid/ask data is available. Hourly TRADES bars are available and aligned to the session open. Zero-volume bars repeat the previous close.
- Expired contracts and their history are retained for approximately eight days.
- Lookups by product return all listings quickly; lookups filtered by expiry are slower.

## Orders

- Rejections are delivered asynchronously; transmission does not imply acceptance.
- The Gateway may reuse the id of a rejected order; treat the latest record per id as authoritative.
- Commission reports may arrive after executions.
- Paper accounts are charged under the IBKR Pro fixed schedule.
