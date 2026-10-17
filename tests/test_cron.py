"""The cron guard: exit codes, heartbeats, lock, window, alerts."""
import fcntl
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from ibkr_ops import cron
from ibkr_ops.client_ids import state_dir
from ibkr_ops.config import Endpoint

AT = datetime(2026, 10, 14, 4, 31, tzinfo=timezone.utc)
DEAD = Endpoint("127.0.0.1", 1)                  # nothing listens on port 1


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(cron, "alert", lambda m: sent.append(m))
    return sent


def beat(job):
    return json.loads(cron.heartbeat_path(job).read_text())


def test_ok_and_heartbeat(alerts):
    assert cron.run("j-ok", lambda job: job.step("a", lambda: 1) and None, require_gateway=False, now=AT) == cron.OK
    assert beat("j-ok")["status"] == "ok" and beat("j-ok")["steps"] == {"a": "ok"} and not alerts


def test_failed_step_is_partial_not_zero(alerts):
    def main(job):
        job.step("good", lambda: 1)
        job.step("bad", lambda: 1 / 0)
    assert cron.run("j-partial", main, require_gateway=False, now=AT) == cron.PARTIAL
    assert "ZeroDivisionError" in beat("j-partial")["detail"] and alerts


def test_gateway_down_is_its_own_code(alerts):
    called = []
    assert cron.run("j-gw", lambda job: called.append(1), endpoint=DEAD, now=AT) == cron.GATEWAY_DOWN
    assert not called and "gateway down" in alerts[0]


def test_misfire_outside_window(alerts):
    local_time_fire = AT + timedelta(hours=4)    # "04:31 UTC" entry fired at 04:31 local (UTC-4)
    rc = cron.run("j-win", lambda job: None, require_gateway=False, window=("04:30Z", 30), now=local_time_fire)
    assert rc == cron.MISFIRED and "CRON_TZ" in alerts[0]
    assert cron.run("j-win", lambda job: None, require_gateway=False, window=("04:30Z", 30), now=AT) == cron.OK
    late = datetime(2026, 10, 14, 23, 50, tzinfo=timezone.utc)
    assert cron.run("j-mid", lambda job: None, require_gateway=False, window=("00:10Z", 30), now=late) == cron.OK


def test_trading_day_skip_and_refusal(alerts):
    saturday = datetime(2026, 10, 17, 20, 0, tzinfo=timezone.utc)
    assert cron.run("j-sat", lambda job: None, require_gateway=False, trading_day=True, now=saturday) == cron.SKIPPED
    def refuse(job):
        raise cron.Refused("kill switch")
    assert cron.run("j-ref", refuse, require_gateway=False, now=AT) == cron.REFUSED
    assert not alerts                            # skips and refusals are expected outcomes


def test_trading_day_uses_the_new_york_date(alerts):
    friday_evening_et = datetime(2026, 10, 17, 1, 0, tzinfo=timezone.utc)       # Fri 21:00 ET, Sat in UTC
    assert cron.run("j-fri", lambda job: None, require_gateway=False, trading_day=True,
                    now=friday_evening_et) == cron.OK
    sunday_evening_et = datetime(2026, 10, 19, 1, 0, tzinfo=timezone.utc)       # Sun 21:00 ET, Mon in UTC
    assert cron.run("j-sun", lambda job: None, require_gateway=False, trading_day=True,
                    now=sunday_evening_et) == cron.SKIPPED


def test_account_ids_are_redacted_in_job_logs(capsys):
    cron.setup_logging("j-log")
    logging.getLogger("ib_async.wrapper").info("position: Position(account='%s', position=13.0)", "DUP000000")
    logging.getLogger("x").warning("live U1234567 and paper DU7654321; order 1234567 stays")
    err = capsys.readouterr().err
    assert "DUP000000" not in err and "DUP******" in err
    assert "U1234567" not in err and "U*******" in err and "DU*******" in err and "order 1234567 stays" in err


def test_overlapping_run_is_skipped(alerts):
    d = state_dir() / "locks"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "j-lock.lock", "a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert cron.run("j-lock", lambda job: None, require_gateway=False, now=AT) == cron.SKIPPED
    assert cron.run("j-lock", lambda job: None, require_gateway=False, now=AT) == cron.OK


def test_heartbeat_watchdog(alerts):
    cron.run("j-a", lambda job: None, require_gateway=False, now=AT)
    cron.run("j-b", lambda job: 1 / 0, require_gateway=False, now=AT)
    later = datetime.now(timezone.utc) + timedelta(hours=30)
    problems = cron.check_heartbeats({"j-a": 26, "j-b": 100, "j-never": 1}, now=later)
    assert any("j-a: last ran" in p for p in problems)
    assert any(p.startswith("j-b: last run error") for p in problems)
    assert "j-never: never ran" in problems
    assert cron.check_heartbeats({"j-a": 26}) == []


def test_bad_job_name():
    with pytest.raises(ValueError):
        cron.run("../../etc", lambda job: None)
