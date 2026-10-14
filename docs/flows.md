# Flows

## General flow: scheduled job

A job run under `cron.run`, opening a `Session` and issuing paced requests. Exit codes are listed in [operations.md](operations.md#job-guard).

```mermaid
flowchart TD
    start(["cron.run(name, main)"]) --> lock{"Job lock<br/>acquired?"}
    lock -- no --> skipLock["Exit 5: skipped<br/>(previous run active)"]
    lock -- yes --> window{"Inside declared<br/>UTC window?"}
    window -- no --> misfire["Exit 6: misfired"]
    window -- yes --> tday{"Trading day?<br/>(if required)"}
    tday -- no --> skipDay["Exit 5: skipped"]
    tday -- yes --> port{"Gateway port<br/>open?"}
    port -- no --> down["Exit 3: Gateway down"]
    port -- yes --> main["main(job)"]

    main --> connect

    subgraph session ["Session"]
        connect["Lease client id<br/>from role band"] --> ibconn["ib_async connect"]
        ibconn -- "error 326" --> nextId["Next client id"] --> ibconn
        ibconn -- "no handshake" --> gwDown["GatewayDown"]
        ibconn -- ok --> mdt["Set market data type"]
        mdt --> pace["Pacer.wait"]
        pace --> req["ib_async request"]
        req -- "data" --> rows["Rows returned"]
        req -- "no data (200, 162 HMDS)" --> empty["Empty result"]
        req -- "pacing error or timeout" --> backoff["Backoff 30 / 60 / 120 s"]
        backoff -- retry --> pace
        backoff -- exhausted --> reqErr["IBError / TimeoutError"]
        req -- "other error" --> reqErr
    end

    rows --> steps
    empty --> steps
    reqErr --> stepFail["Step recorded as failed"] --> steps
    gwDown --> down

    steps{"All steps<br/>succeeded?"}
    steps -- yes --> ok["Exit 0"]
    steps -- no --> partial["Exit 4: partial"]
    main -- "Refused raised" --> refused["Exit 2: refused"]
    main -- "unhandled exception" --> error["Exit 1: error"]

    ok & partial & refused & error & down & misfire & skipDay --> beat["Write heartbeat"]
    skipLock --> beatLock["Write heartbeat"]
    beat --> alertQ{"Exit 1, 3, 4 or 6?"}
    alertQ -- yes --> alert["Run alert command"]
    alertQ -- no --> done(["Release lock"])
    alert --> done
    beatLock --> alertLock["Run alert command"]
```

Missed runs leave no heartbeat, so an independent schedule checks for them:

```mermaid
flowchart LR
    sched(["Independent schedule"]) --> hb["ibkr_ops heartbeats JOB=HOURS --alert"]
    hb --> each{"For each job"}
    each -- "no heartbeat" --> never["never ran"]
    each -- "older than HOURS" --> stale["stale"]
    each -- "last exit not 0 or 5" --> failed["failed / misfired"]
    each -- "recent and ok" --> fine["ok"]
    never & stale & failed --> alert["Alert and exit 1"]
```

## Special flow: order execution

`orders.execute` for a batch of order intents. Every refusal is journalled before the flow stops.

```mermaid
flowchart TD
    start(["orders.execute(session, intents, policy, ledger)"]) --> ro{"policy.place on a<br/>read-only session?"}
    ro -- yes --> roErr["ReadOnlySession raised<br/>(nothing journalled)"]
    ro -- no --> state["Read ledger:<br/>today's sent value, sent idem_keys"]
    state --> acct["Account from Gateway"]
    acct --> batch

    subgraph gates ["gates.check"]
        direction TB
        batch["Batch checks:<br/>kill switch, account vs port,<br/>live authorisation, order count"]
        per["Per-order checks:<br/>sec type, allowlist, quantity, idem_key,<br/>quote live and fresh, limit and slippage,<br/>order value"]
        caps["Batch and daily value caps,<br/>all-or-nothing"]
        batch --> per --> caps
    end

    caps --> journal["Journal decision"]
    journal --> mode{"Decision mode"}
    mode -- blocked --> blocked(["Stop: caller exits 2"])
    mode -- "dry-run" --> dry(["Stop: nothing sent"])
    mode -- "paper / live" --> loop["Next allowed intent"]

    loop --> whatif{"What-if check passes?<br/>(if enabled)"}
    whatif -- no --> wiRef["Journal what_if_refused<br/>status Refused"]
    whatif -- yes --> sent["Journal sent (idem_key)"]
    sent --> place["placeOrder"]
    wiRef --> more
    place --> more{"More intents?"}
    more -- yes --> loop
    more -- no --> await["Await status:<br/>working, done or refused"]

    await --> rest{"rest_seconds set?"}
    rest -- yes --> wait["Wait, then cancel<br/>unfilled quantity"] --> await2["Await status"]
    rest -- no --> final
    await2 --> final["Resolve status per order:<br/>321 / 201 before going live = Rejected"]
    final --> jstatus["Journal status"]
    jstatus --> failedQ{"Any refused, rejected,<br/>unacknowledged or unfilled?"}
    failedQ -- yes --> stepFail(["Step failed: exit 4"])
    failedQ -- no --> okEnd(["Exit 0"])
```
