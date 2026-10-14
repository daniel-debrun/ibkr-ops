"""Gated order execution.

    decision, results = execute(session, intents, Policy.from_dict(cfg["policy"]), ledger="state/orders.jsonl")

Sequence:

1. The account is taken from the Gateway; the day's placed value and previously sent keys from the ledger.
   ``gates.check`` produces a decision; anything other than ``paper`` or ``live`` stops here.
2. The decision is journalled to an append-only JSONL ledger, and each order's ``idem_key`` is journalled as
   ``sent`` before the order is transmitted, so an interrupted run cannot resend it.
3. Optionally, IB's what-if check is run per order; a failure refuses that order.
4. Order status is awaited: rejections are only reported asynchronously.
5. Optionally (``rest_seconds``), unfilled quantity is cancelled after a resting period.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from . import gates
from .config import redact
from .contracts import to_contract
from .io import append_jsonl, latest_by, read_jsonl
from .session import DONE, ReadOnlySession

log = logging.getLogger("ibkr_ops")


def ledger_state(ledger: Path, today: str) -> tuple[float, frozenset]:
    """(value sent today, every idem_key ever sent) from the ledger."""
    recs = read_jsonl(ledger)
    sent = [r for r in recs if r.get("event") == "sent"]
    today_value = sum(float(r.get("value") or 0) for r in sent if str(r.get("at", "")).startswith(today))
    return today_value, frozenset(r["idem_key"] for r in sent if r.get("idem_key"))


def build_order(intent: gates.OrderIntent, account: str, tif: str = "DAY"):
    """An ib_async ``LimitOrder``/``MarketOrder`` for a gated intent, pinned to ``account``."""
    from ib_async import LimitOrder, MarketOrder

    action, qty = ("BUY" if intent.qty > 0 else "SELL"), abs(intent.qty)
    common = dict(tif=tif, account=account, orderRef=intent.idem_key[:40] if intent.idem_key else "")
    if intent.limit is not None:
        return LimitOrder(action, qty, float(intent.limit), **common)
    return MarketOrder(action, qty, **common)


def execute(session, intents: list[gates.OrderIntent], policy: gates.Policy, ledger: str | Path,
            what_if: bool = True, confirm_timeout: float = 15.0, rest_seconds: float | None = None,
            now: datetime | None = None) -> tuple[gates.Decision, dict]:
    """Gate, journal, preview, place, confirm. Returns (decision, {idem_key or key: result dict})."""
    if policy.place and getattr(session, "readonly", False):
        raise ReadOnlySession("policy says place but the session is read-only; open Session(readonly=False)")
    ledger = Path(ledger)
    now = now or datetime.now(timezone.utc)
    account = session.account
    today_value, sent_keys = ledger_state(ledger, now.strftime("%Y-%m-%d"))
    ctx = gates.Context(account=account, port=session.endpoint.port, now=now,
                        placed_today_value=today_value, sent_keys=sent_keys)
    decision = gates.check(intents, policy, ctx)
    append_jsonl(ledger, {"event": "decision", "at": now.isoformat(), "mode": decision.mode,
                          "account": redact(account), "blocked": decision.blocked,
                          "allowed": [i.idem_key or i.key for i in decision.allowed],
                          "refused": [[i.idem_key or i.key, why] for i, why in decision.refused]})
    log.info("order decision:\n%s", decision.summary())
    if not decision.will_place:
        return decision, {}

    results: dict = {}
    trades: dict = {}
    for intent in decision.allowed:
        contract, order = to_contract(intent.key), build_order(intent, account)
        tag = intent.idem_key or intent.key
        if what_if:
            try:
                st = session.what_if(contract, order)
                if not st:
                    raise RuntimeError("empty what-if answer")
            except Exception as e:  # noqa: BLE001 - a failed preview refuses the order
                results[tag] = {"status": "Refused", "error": f"what-if: {e}"}
                append_jsonl(ledger, {"event": "what_if_refused", "at": _now(), "idem_key": tag, "error": str(e)})
                continue
            append_jsonl(ledger, {"event": "what_if", "at": _now(), "idem_key": tag,
                                  "init_margin_change": getattr(st, "initMarginChange", ""),
                                  "commission": _num(getattr(st, "commissionAndFees", getattr(st, "commission", None))),
                                  "warning": getattr(st, "warningText", "")})
        append_jsonl(ledger, {"event": "sent", "at": _now(), "idem_key": tag, "key": intent.key,
                              "qty": intent.qty, "limit": intent.limit, "value": intent.value,
                              "mode": decision.mode})
        trade = session.place(contract, order)
        trades[tag] = trade
        results[tag] = {"order_id": trade.order.orderId}

    live = list(trades.values())
    session.await_orders(live, timeout=confirm_timeout)
    if rest_seconds and live:
        deadline = time.monotonic() + rest_seconds
        while time.monotonic() < deadline and not all(t.orderStatus.status in DONE for t in live):
            session.ib.sleep(0.5)
        for t in live:
            if t.orderStatus.status not in DONE:
                session.cancel(t)
        session.await_orders(live, timeout=confirm_timeout)
    for tag, t in trades.items():
        st = session.order_state(t)
        results[tag].update(st)
        append_jsonl(ledger, {"event": "status", "at": _now(), "idem_key": tag,
                              "order_id": t.order.orderId, **st})
    return decision, results


def failed(results: dict) -> list[str]:
    """Tags of orders that were refused, rejected, never acknowledged, or cancelled with no fill. An unfilled
    order is reported as failed so that a run which executed nothing exits non-zero."""
    bad = []
    for tag, r in results.items():
        st = r.get("status", "")
        if st in ("Refused", "Rejected", "Inactive", "Unacknowledged", "") or (
                st in ("Cancelled", "ApiCancelled") and not r.get("filled")):
            bad.append(tag)
    return bad


def order_history(ledger: str | Path) -> dict:
    """Latest status per order id (the Gateway reuses a rejected order's id, so later records win)."""
    return latest_by((r for r in read_jsonl(ledger) if r.get("event") == "status"), "order_id")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if abs(f) > 1e300 else f
