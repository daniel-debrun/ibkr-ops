"""Keep IB Gateway running and authenticated using ib_async's IBC controller and Watchdog.

Reference configuration; not covered by the test suite. Requirements: IBC (https://github.com/IbcAlpha/IBC,
default ``/opt/ibc``) with credentials in its ``config.ini`` (mode 600, outside version control), and a
display (desktop session or Xvfb). Intended to run under ``deploy/systemd/ibgateway.service``.

Behaviour: starts the Gateway through IBC and connects a read-only watcher (client id 999). When API
traffic is idle, the watcher issues a minimal historical request; if that fails, or on error 1100, the
Gateway is restarted and the watcher reconnects. Two-factor authentication prompts still require manual
approval; scheduled jobs report the resulting outage through exit code 3 and heartbeats.
"""
import logging
import os

from ib_async import IB, IBC, Watchdog

from ibkr_ops.cron import alert, setup_logging

GATEWAY_MAJOR = int(os.environ.get("IBKR_GATEWAY_VERSION", "1045"))   # ~/Jts/ibgateway/1045
MODE = os.environ.get("IBKR_TRADING_MODE", "paper")
PORT = int(os.environ.get("IB_PORT", "4002" if MODE == "paper" else "4001"))


def main() -> None:
    setup_logging("gateway-keepalive")
    log = logging.getLogger("ibkr_ops")
    ibc = IBC(GATEWAY_MAJOR, gateway=True, tradingMode=MODE,
              ibcPath=os.environ.get("IBC_PATH", "/opt/ibc"),
              ibcIni=os.environ.get("IBC_INI", os.path.expanduser("~/ibc/config.ini")))
    ib = IB()
    dog = Watchdog(ibc, ib, port=PORT, clientId=999, readonly=True, connectTimeout=15,
                   appStartupTime=90, appTimeout=60, retryDelay=60, probeTimeout=20)
    dog.startedEvent += lambda _: log.info("gateway up on %s (%s)", PORT, MODE)
    dog.hardTimeoutEvent += lambda _: alert("IB Gateway unresponsive: restarting it")
    dog.stoppedEvent += lambda _: log.warning("gateway stopped")
    dog.start()
    ib.run()


if __name__ == "__main__":
    main()
