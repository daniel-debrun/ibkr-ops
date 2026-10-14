"""Client-id allocation without collisions.

Each API connection to a Gateway requires a distinct client id; a duplicate is refused with error 326,
reported with ``reqId == -1``. ``lease`` holds an exclusive file lock on ``<state>/clientid-<n>.lock`` for
the first free id in ``range(base, base + span)``, so concurrent local processes never share an id. Ids held
elsewhere (another host, a stale socket) are handled by ``Session``, which retries with the next id.

``ROLES`` assigns a distinct id band to each job type so connections are identifiable in Gateway logs.
"""
from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path

#: Suggested id bands per job kind; ``0`` is reserved for the TWS/Gateway "master" client, don't use it.
ROLES = {"probe": 900, "cron-quotes": 100, "cron-history": 200, "cron-orders": 300, "interactive": 500}


def state_dir() -> Path:
    d = Path(os.environ.get("IBKR_OPS_STATE") or
             Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "ibkr-ops")
    d.mkdir(parents=True, exist_ok=True)
    return d


class NoFreeClientId(RuntimeError):
    pass


@contextmanager
def lease(base: int, span: int = 20, skip: frozenset = frozenset(), directory: Path | None = None):
    """Yield the first clientId in ``[base, base+span)`` not locked by another local process (and not in
    ``skip``); the lock is held until the block exits. Raises ``NoFreeClientId`` when all are taken."""
    if base <= 0:
        raise ValueError("clientId 0 is the Gateway's master client; use a positive base")
    directory = directory or state_dir()
    for cid in range(base, base + span):
        if cid in skip:
            continue
        fh = open(directory / f"clientid-{cid}.lock", "a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            continue
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()}\n")
            fh.flush()
            yield cid
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()
        return
    raise NoFreeClientId(f"all clientIds {base}..{base + span - 1} are leased")
