"""Pre-trade order gates (no network I/O).

Every order intent is checked against a ``Policy`` before it can be sent. Defaults are conservative:

* dry run unless ``Policy.place`` is set;
* paper accounts only; a live account requires ``Policy.allow_live`` and the environment variable
  ``IBKR_ALLOW_LIVE=1``; a port/account mismatch blocks the batch regardless of flags;
* a kill-switch file blocks all orders while it exists;
* limit orders only (``allow_market`` off), within ``max_slippage`` of a reference quote;
* the reference quote must be live (not delayed) and no older than ``max_quote_age_s``;
* caps per order, per batch, per day and on order count; optional instrument and security-type allowlists;
* an ``idem_key`` already sent is refused (callers journal keys before sending);
* by default, any refused order blocks the whole batch.

``check`` returns a ``Decision`` listing every reason; it does not raise on refusal.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import PORTS, is_paper_account, redact
from .contracts import Instrument


@dataclass(frozen=True)
class OrderIntent:
    key: str                          # Instrument key: "SPY", "EUR.USD", "FCX:UHLAX 20261008 88 C", ...
    qty: float                        # signed: + buy, - sell
    limit: float | None = None        # None: market order
    ref_price: float | None = None    # the quote the order was sized on
    quote_at: datetime | None = None  # when that quote was taken (tz-aware)
    price_type: str | None = None     # "live" / "delayed" (clean.quote)
    idem_key: str = ""                # unique per decision, e.g. f"{signal_id}:{key}"

    @property
    def instrument(self) -> Instrument:
        return Instrument.parse(self.key)

    @property
    def value(self) -> float | None:
        px = self.limit if self.limit is not None else self.ref_price
        return None if px is None else abs(self.qty) * px * self.instrument.multiplier


@dataclass(frozen=True)
class Policy:
    place: bool = False
    allow_live: bool = False
    kill_switch: str | None = None
    sec_types: frozenset = frozenset({"STK", "CASH", "OPT", "FCX"})
    allowlist: frozenset | None = None          # instrument keys or bare symbols; None = any
    max_order_value: float = 10_000.0
    max_batch_value: float = 25_000.0
    max_daily_value: float | None = 50_000.0
    max_orders: int = 50
    allow_market: bool = False
    max_slippage: float = 0.02                  # |limit - ref| / ref
    max_quote_age_s: float = 20 * 60
    allow_delayed_quotes: bool = False
    all_or_nothing: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "Policy":
        d = dict(d)
        for k in ("sec_types", "allowlist"):
            if d.get(k) is not None:
                d[k] = frozenset(str(x).upper() for x in d[k])
        unknown = set(d) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown policy keys: {sorted(unknown)}")   # a typo must not silently relax a cap
        return cls(**d)


@dataclass
class Context:
    account: str                                # from managedAccounts, never from config
    port: int
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    placed_today_value: float = 0.0
    sent_keys: frozenset = frozenset()
    env: dict = field(default_factory=lambda: dict(os.environ))


@dataclass
class Decision:
    mode: str                                   # "blocked" | "dry-run" | "paper" | "live"
    allowed: list = field(default_factory=list)
    refused: list = field(default_factory=list)  # [(intent, reason)]
    blocked: list = field(default_factory=list)  # batch-level reasons

    @property
    def will_place(self) -> bool:
        return self.mode in ("paper", "live") and bool(self.allowed)

    def summary(self) -> str:
        lines = [f"mode={self.mode} allowed={len(self.allowed)} refused={len(self.refused)}"]
        lines += [f"  BLOCKED: {r}" for r in self.blocked]
        lines += [f"  refused {i.key} {i.qty:+g}: {r}" for i, r in self.refused]
        return "\n".join(lines)


def account_mode(account: str, port: int) -> tuple[str, str | None]:
    """(``paper``/``live``, a blocking reason or None). The account decides; the port must not disagree."""
    acct_mode = "paper" if is_paper_account(account) else "live"
    port_mode = PORTS.get(port, ("", None))[1]
    if port_mode and port_mode != acct_mode:
        return acct_mode, (f"port {port} is the {port_mode} convention but account {redact(account)} is "
                           f"{acct_mode}: refusing everything until they agree")
    return acct_mode, None


def _order_reasons(i: OrderIntent, p: Policy, ctx: Context) -> list[str]:
    r = []
    inst = i.instrument
    if inst.sec_type not in p.sec_types:
        r.append(f"sec type {inst.sec_type} not allowed")
    if p.allowlist is not None and i.key.upper() not in p.allowlist and inst.symbol not in p.allowlist:
        r.append("not on the allowlist")
    if not i.qty or not math.isfinite(i.qty):
        r.append("zero or non-finite quantity")
    elif inst.sec_type != "CASH" and i.qty != int(i.qty):
        r.append("fractional quantity")
    if i.idem_key and i.idem_key in ctx.sent_keys:
        r.append(f"already sent ({i.idem_key})")
    if i.ref_price is None or i.ref_price <= 0:
        r.append("no reference price")
    else:
        if i.quote_at is None:
            r.append("reference quote has no timestamp")
        else:
            age = (ctx.now - i.quote_at).total_seconds()
            if age > p.max_quote_age_s or age < -60:
                r.append(f"reference quote is {age:.0f}s old (max {p.max_quote_age_s:.0f}s)")
        if i.price_type != "live" and not (p.allow_delayed_quotes and i.price_type == "delayed"):
            r.append(f"reference quote is {i.price_type or 'missing'}, not live")
    if i.limit is None:
        if not p.allow_market:
            r.append("market orders not allowed: give a limit (gates.marketable_limit)")
    else:
        if i.limit <= 0:
            r.append("non-positive limit")
        if inst.sec_type == "FCX" and not 0 < i.limit < 1:
            r.append("event-contract limit must be in (0, 1)")
        if i.ref_price and abs(i.limit - i.ref_price) / i.ref_price > p.max_slippage + 1e-12:
            r.append(f"limit {i.limit} is more than {p.max_slippage:.1%} from reference {i.ref_price}")
    v = i.value
    if v is None:
        r.append("order value unknown")
    elif v > p.max_order_value:
        r.append(f"value ${v:,.2f} exceeds max_order_value ${p.max_order_value:,.0f}")
    return r


def check(intents: list[OrderIntent], policy: Policy, ctx: Context) -> Decision:
    blocked = []
    if policy.kill_switch and Path(policy.kill_switch).expanduser().exists():
        blocked.append(f"kill switch present: {policy.kill_switch}")
    if not ctx.account:
        blocked.append("no account reported by the Gateway")
        mode = "live"
    else:
        mode, why = account_mode(ctx.account, ctx.port)
        if why:
            blocked.append(why)
    if mode == "live" and not (policy.allow_live and ctx.env.get("IBKR_ALLOW_LIVE") == "1"):
        blocked.append("live account: needs Policy.allow_live and IBKR_ALLOW_LIVE=1")
    if len(intents) > policy.max_orders:
        blocked.append(f"{len(intents)} orders exceeds max_orders {policy.max_orders}")

    d = Decision(mode=mode)
    keys = set()
    for i in intents:
        reasons = _order_reasons(i, policy, ctx)
        if i.idem_key:
            if i.idem_key in keys:
                reasons.append(f"duplicate idem_key in batch ({i.idem_key})")
            keys.add(i.idem_key)
        (d.refused.append((i, "; ".join(reasons))) if reasons else d.allowed.append(i))

    total = sum(i.value or 0 for i in d.allowed)
    if total > policy.max_batch_value:
        blocked.append(f"batch value ${total:,.2f} exceeds max_batch_value ${policy.max_batch_value:,.0f}")
    if policy.max_daily_value is not None and ctx.placed_today_value + total > policy.max_daily_value:
        blocked.append(f"today's value would reach ${ctx.placed_today_value + total:,.2f} "
                       f"(max_daily_value ${policy.max_daily_value:,.0f})")
    if policy.all_or_nothing and d.refused:
        blocked.append(f"{len(d.refused)} order(s) refused and all_or_nothing is on")

    if blocked:
        d.blocked, d.mode = blocked, "blocked"
        d.refused += [(i, "batch blocked") for i in d.allowed]
        d.allowed = []
    elif not policy.place:
        d.mode = "dry-run"
    return d


def marketable_limit(price: float, qty: float, max_slippage: float, tick: float = 0.01) -> float:
    """A limit that crosses ``price`` by at most ``max_slippage`` (a fraction): above to buy, below to sell,
    rounded *toward* ``price`` on the tick grid so the stated slippage is never exceeded."""
    if qty > 0:
        return round(math.floor(round(price * (1 + max_slippage) / tick, 6)) * tick, 10)
    return round(math.ceil(round(price * (1 - max_slippage) / tick, 6)) * tick, 10)


@dataclass(frozen=True)
class Commission:
    """US stock fee schedule: per share, a minimum, a cap as a share of value. Paper accounts are billed on
    IBKR Pro fixed (0.005/share, $1 min, 1% cap), not Lite."""
    per_share: float = 0.0
    minimum: float = 0.0
    max_pct: float = 1.0

    def estimate(self, shares: float, price: float) -> float:
        if shares == 0:
            return 0.0
        return min(max(self.minimum, abs(shares) * self.per_share), self.max_pct * abs(shares) * price)


PRO_FIXED = Commission(0.005, 1.0, 0.01)
