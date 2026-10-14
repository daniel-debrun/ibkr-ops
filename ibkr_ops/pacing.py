"""Request pacing for IB historical data, and retry with backoff.

IB's documented historical-data limits:

* at most 60 requests in any 10-minute window;
* no identical request within 15 seconds;
* at most 6 requests for the same contract, exchange and tick type within 2 seconds.

Violations are reported as error 162 ("pacing violation") but may also surface only as an unanswered
request; ``with_backoff`` therefore treats timeouts as possible pacing violations by default. The socket
additionally accepts roughly 50 messages per second (``min_interval``).
"""
from __future__ import annotations

import time
from collections import deque
from typing import Callable, Hashable

from .errors import IBError, Kind, PacingError


class Pacer:
    """Blocks in ``wait()`` until one more request fits every budget, then records it."""

    def __init__(self, max_requests: int = 60, window: float = 600.0, identical_gap: float = 15.0,
                 per_contract: tuple[int, float] = (6, 2.0), min_interval: float = 0.0,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.max_requests, self.window = max_requests, window
        self.identical_gap, self.per_contract, self.min_interval = identical_gap, per_contract, min_interval
        self.clock, self.sleep = clock, sleep
        self._all: deque = deque()
        self._same: dict = {}
        self._contract: dict = {}

    def delay(self, key: Hashable | None = None, contract: Hashable | None = None) -> float:
        """Seconds to wait before a request with this identity (``key``) on this ``contract`` is allowed."""
        now = self.clock()
        while self._all and now - self._all[0] >= self.window:
            self._all.popleft()
        d = 0.0
        if self._all:
            d = max(d, self.min_interval - (now - self._all[-1]))
        if len(self._all) >= self.max_requests:
            d = max(d, self._all[len(self._all) - self.max_requests] + self.window - now)
        if key is not None and key in self._same:
            d = max(d, self._same[key] + self.identical_gap - now)
        if contract is not None:
            n, span = self.per_contract
            q = self._contract.get(contract, deque())
            while q and now - q[0] >= span:
                q.popleft()
            if len(q) >= n:
                d = max(d, q[len(q) - n] + span - now)
        return max(0.0, d)

    def wait(self, key: Hashable | None = None, contract: Hashable | None = None) -> float:
        d = self.delay(key, contract)
        if d > 0:
            self.sleep(d)
        now = self.clock()
        self._all.append(now)
        if key is not None:
            self._same[key] = now
        if contract is not None:
            self._contract.setdefault(contract, deque()).append(now)
        return d


def is_pacing(exc: BaseException, timeouts_as_pacing: bool = True) -> bool:
    if isinstance(exc, PacingError):
        return True
    if isinstance(exc, IBError):
        return exc.kind is Kind.PACING
    return timeouts_as_pacing and isinstance(exc, TimeoutError)


def with_backoff(fn: Callable, delays=(30.0, 60.0, 120.0), timeouts_as_pacing: bool = True,
                 sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = lambda m: None):
    """Call ``fn()``; on a pacing error (or a timeout) sleep through ``delays`` and retry; re-raise after."""
    for i in range(len(delays) + 1):
        try:
            return fn()
        except Exception as e:
            if i == len(delays) or not is_pacing(e, timeouts_as_pacing):
                raise
            log(f"pacing/timeout ({e}); backing off {delays[i]:.0f}s")
            sleep(delays[i])
