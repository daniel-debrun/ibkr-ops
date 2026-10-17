"""Guard for scheduled jobs: locking, UTC logging, pre-flight checks, schedule validation, step isolation,
exit codes, heartbeats and alerting.

    def main(job):
        with Session(role="cron-quotes") as s:
            job.step("quotes", lambda: sweep(s))
            job.step("bars", lambda: bars(s))

    sys.exit(cron.run("ib-quotes", main, window=("04:30Z", 30)))

* **Lock**: a non-blocking flock per job name; an overlapping run exits 5.
* **Logging**: every line is timestamped in UTC; IB account ids in any record (including ib_async's own INFO
  lines, which print positions with the full account) are redacted.
* **Gateway pre-flight**: a closed API port exits 3 before any work is attempted.
* **Schedule window**: Debian/Ubuntu cron ignores ``CRON_TZ`` and fires in local time. A job given its
  intended UTC window exits 6 (misfired) and alerts when started outside it.
* **Trading day**: judged on the New York calendar date, so a US job running after 20:00 ET (already the next
  day in UTC) checks the right session.
* **Steps**: a failing step does not stop subsequent steps, but the run exits 4.
* **Heartbeat**: each run writes ``<state>/heartbeat/<job>.json``. Runs that never start leave no record, so
  ``check_heartbeats`` must be scheduled independently to detect them.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from . import probe, tradingdays
from .client_ids import state_dir
from .config import Endpoint, redact
from .errors import GatewayDown
from .io import atomic_write_text

OK, ERROR, REFUSED, GATEWAY_DOWN, PARTIAL, SKIPPED, MISFIRED = 0, 1, 2, 3, 4, 5, 6
EXIT_NAMES = {OK: "ok", ERROR: "error", REFUSED: "refused", GATEWAY_DOWN: "gateway_down", PARTIAL: "partial",
              SKIPPED: "skipped", MISFIRED: "misfired"}

log = logging.getLogger("ibkr_ops")


class Refused(Exception):
    """Raise from a job to exit 2 (kill switch, gates, bad signed input)."""


class Skip(Exception):
    """Raise from a job to exit 5 with a reason (nothing to do today)."""


class Misfired(Exception):
    pass


class _UTCFormatter(logging.Formatter):
    converter = time.gmtime

    def formatTime(self, record, datefmt=None):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", self.converter(record.created))


#: IB account ids: paper DU/DF (sometimes with a letter, e.g. DUP), live U, advisor F, I
ACCOUNT_RE = re.compile(r"\b(?:D[UF][A-Z]?|U|F|I)\d{5,9}\b")
NEW_YORK = ZoneInfo("America/New_York")


class RedactAccounts(logging.Filter):
    """Replace IB account ids in a record's final message with ``redact`` (``DU1234567`` -> ``DU*******``).
    ib_async logs positions and account values at INFO with the full account id."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        red = ACCOUNT_RE.sub(lambda m: redact(m.group(0)), msg)
        if red != msg:
            record.msg, record.args = red, None
        return True


def setup_logging(job: str, level=logging.INFO) -> None:
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(_UTCFormatter(f"%(asctime)s [{job}] %(levelname)s %(message)s"))
    h.addFilter(RedactAccounts())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(level)


@dataclass
class Job:
    name: str
    started: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    results: dict = field(default_factory=dict)
    failures: dict = field(default_factory=dict)

    def step(self, name: str, fn: Callable, *args, **kwargs):
        """Run one step; record and log a failure instead of raising. Returns the result or None.
        ``GatewayDown`` is re-raised: later steps can't succeed without it."""
        t0 = time.monotonic()
        try:
            out = fn(*args, **kwargs)
        except (GatewayDown, Refused, Skip):
            raise
        except Exception as e:  # noqa: BLE001
            self.failures[name] = f"{type(e).__name__}: {e}"
            log.error("step %s failed after %.1fs: %s\n%s", name, time.monotonic() - t0, e, traceback.format_exc())
            return None
        self.results[name] = "ok"
        log.info("step %s ok in %.1fs", name, time.monotonic() - t0)
        return out


def parse_window(spec: tuple[str, float] | None):
    """``("04:30Z", 30)`` -> (time of day UTC, tolerance minutes)."""
    if spec is None:
        return None
    hhmm, tol = spec
    hhmm = hhmm.rstrip("Zz")
    h, m = (int(x) for x in hhmm.split(":"))
    return h * 60 + m, float(tol)


def in_window(now: datetime, window) -> bool:
    if window is None:
        return True
    target, tol = window
    now = now.astimezone(timezone.utc)
    minute = now.hour * 60 + now.minute + now.second / 60
    diff = abs(minute - target)
    return min(diff, 1440 - diff) <= tol


def heartbeat_path(job: str) -> Path:
    d = state_dir() / "heartbeat"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{job}.json"


def alert(message: str) -> None:
    """Run ``$IBKR_OPS_ALERT_CMD <message>`` (e.g. ``notify-send ibkr-ops``, a webhook script); always log."""
    log.error("ALERT %s", message)
    cmd = os.environ.get("IBKR_OPS_ALERT_CMD")
    if cmd:
        try:
            subprocess.run([*shlex.split(cmd), message], timeout=30, check=False)
        except Exception as e:  # noqa: BLE001
            log.error("alert command failed: %s", e)


def run(name: str, main: Callable[[Job], int | None], *, require_gateway: bool = True,
        endpoint: Endpoint | None = None, window: tuple[str, float] | None = None,
        trading_day: bool = False, now: datetime | None = None,
        alert_on=(ERROR, GATEWAY_DOWN, PARTIAL, MISFIRED)) -> int:
    """Run ``main(job)`` under the cron guard and return the exit code (pass it to ``sys.exit``)."""
    if not name.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise ValueError(f"bad job name {name!r}")
    setup_logging(name)
    job = Job(name)
    now = now or job.started
    lock_dir = state_dir() / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = open(lock_dir / f"{name}.lock", "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.warning("skipped: the previous run still holds the lock")
        _beat(job, SKIPPED, "previous run still running")
        alert(f"{name}: skipped, previous run still running")
        return SKIPPED
    try:
        return _guarded(job, main, require_gateway, endpoint, window, trading_day, now, alert_on)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _guarded(job, main, require_gateway, endpoint, window, trading_day, now, alert_on) -> int:
    detail = ""
    try:
        w = parse_window(window)
        if not in_window(now, w):
            raise Misfired(f"fired at {now.astimezone(timezone.utc):%H:%MZ}, outside its window {window[0]}±"
                       f"{window[1]:g}min (is cron running in local time? Debian cron ignores CRON_TZ)")
        if trading_day and not tradingdays.is_trading_day(now.astimezone(NEW_YORK).date()):
            raise Skip("not a US trading day")
        if require_gateway:
            ep = endpoint or Endpoint.resolve()
            if not probe.port_open(ep.host, ep.port):
                raise GatewayDown(f"nothing listening on {ep.host}:{ep.port}")
        rc = main(job)
        if rc not in (None, OK):
            code = int(rc)
        elif job.failures:
            code, detail = PARTIAL, "; ".join(f"{k}: {v}" for k, v in job.failures.items())
        else:
            code = OK
    except Misfired as e:
        code, detail = MISFIRED, str(e)
        log.error("misfired: %s", e)
    except Skip as e:
        code, detail = SKIPPED, str(e)
        log.warning("skipped: %s", e)
    except Refused as e:
        code, detail = REFUSED, str(e)
        log.error("refused: %s", e)
    except GatewayDown as e:
        code, detail = GATEWAY_DOWN, f"gateway down: {e} (log in to IB Gateway)"
        log.error(detail)
    except Exception as e:  # noqa: BLE001
        code, detail = ERROR, f"{type(e).__name__}: {e}"
        log.error("failed: %s\n%s", e, traceback.format_exc())
    _beat(job, code, detail)
    log.info("exit %d (%s) after %.0fs", code, EXIT_NAMES.get(code, "?"),
             (datetime.now(timezone.utc) - job.started).total_seconds())
    if code in alert_on:
        alert(f"{job.name}: {EXIT_NAMES.get(code, code)}: {detail}"[:500])
    return code


def _beat(job: Job, code: int, detail: str) -> None:
    atomic_write_text(heartbeat_path(job.name), json.dumps({
        "job": job.name, "started": job.started.isoformat(timespec="seconds"),
        "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "exit": code, "status": EXIT_NAMES.get(code, str(code)), "detail": detail,
        "steps": {**job.results, **job.failures}}, indent=1))


def check_heartbeats(expect: dict[str, float], now: datetime | None = None,
                     directory: Path | None = None) -> list[str]:
    """``{job: max hours since its last run}`` -> problems (missing, stale, or last run not ok/skipped).
    Run this from its own cron entry, and alert on a non-empty result."""
    now = now or datetime.now(timezone.utc)
    directory = directory or state_dir() / "heartbeat"
    problems = []
    for job, hours in expect.items():
        p = directory / f"{job}.json"
        if not p.exists():
            problems.append(f"{job}: never ran")
            continue
        try:
            hb = json.loads(p.read_text())
            finished = datetime.fromisoformat(hb["finished"])
        except (ValueError, KeyError) as e:
            problems.append(f"{job}: unreadable heartbeat ({e})")
            continue
        age = now - finished
        if age > timedelta(hours=hours):
            problems.append(f"{job}: last ran {age.total_seconds() / 3600:.1f}h ago (expected every {hours:g}h)")
        elif hb.get("exit") not in (OK, SKIPPED):
            problems.append(f"{job}: last run {hb.get('status')}: {hb.get('detail', '')}"[:300])
    return problems
