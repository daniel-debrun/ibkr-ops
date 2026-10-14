"""Command-line interface: ``python -m ibkr_ops <command>``. No command places orders.

    probe [--handshake]               Gateway availability; exit 0 if up, 3 if down
    heartbeats JOB=HOURS ...          report stale, failed, misfired or missing jobs; exit 1 if any
    code CODE [MESSAGE]               classification and handling of an IB error code
    holidays YEAR                     NYSE holidays for YEAR
"""
from __future__ import annotations

import argparse
import json
import sys

from . import cron, errors, probe, tradingdays
from .config import Endpoint


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ibkr_ops")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--handshake", action="store_true", help="also connect (read-only probe clientId)")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    h = sub.add_parser("heartbeats")
    h.add_argument("expect", nargs="+", metavar="JOB=HOURS")
    h.add_argument("--alert", action="store_true", help="run $IBKR_OPS_ALERT_CMD on problems")
    c = sub.add_parser("code")
    c.add_argument("code", type=int)
    c.add_argument("message", nargs="?", default="")
    y = sub.add_parser("holidays")
    y.add_argument("year", type=int)
    a = ap.parse_args(argv)

    if a.cmd == "probe":
        ep = Endpoint.resolve({"host": a.host, "port": a.port})
        if a.handshake:
            out = probe.handshake(ep)
        else:
            out = {"host": ep.host, "port": ep.port, "port_mode": ep.mode, "ok": probe.port_open(ep.host, ep.port)}
        print(json.dumps(out, indent=1))
        return cron.OK if out["ok"] else cron.GATEWAY_DOWN
    if a.cmd == "heartbeats":
        expect = {}
        for spec in a.expect:
            job, _, hours = spec.partition("=")
            expect[job] = float(hours or 24)
        problems = cron.check_heartbeats(expect)
        for line in problems:
            print(line)
        if problems and a.alert:
            cron.alert("ibkr-ops heartbeats: " + " | ".join(problems))
        return 1 if problems else 0
    if a.cmd == "code":
        print(errors.describe(a.code, a.message))
        return 0
    if a.cmd == "holidays":
        for d in sorted(tradingdays.nyse_holidays(a.year)):
            print(d.isoformat(), d.strftime("%a"))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
