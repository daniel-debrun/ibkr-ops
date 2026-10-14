"""Example: a read-only quote sweep under the scheduled-job guard.

    python3 examples/quotes_job.py SPY QQQ EUR.USD          # interactive
    cron: scripts/cron-run.sh 1200 python3 examples/quotes_job.py SPY QQQ

Exit codes: 0 ok, 3 Gateway down, 4 partial failure, 6 started outside its window (see ibkr_ops.cron).
"""
import sys
from pathlib import Path

from ibkr_ops import cron
from ibkr_ops.contracts import to_contract
from ibkr_ops.io import atomic_write_csv, run_stamp
from ibkr_ops.session import Session

OUT = Path("state/quotes")
COLS = ["symbol", "at", "bid", "ask", "last", "close", "bid_size", "ask_size", "volume", "price_type", "error"]


def main(job: cron.Job, keys: list[str]) -> None:
    with Session(role="cron-quotes", market_data="delayed") as s:          # read-only by default
        quotes = job.step("quotes", s.quotes, {k: to_contract(k) for k in keys})
    if quotes is None:
        return
    rows = [{"symbol": k, **row} for k, row in quotes.items()]
    missing = [r["symbol"] for r in rows if r["price_type"] is None]
    if missing:
        job.failures["no_quote"] = ", ".join(missing)       # loud shrink: exit 4, not a silent short file
    atomic_write_csv(OUT / f"{run_stamp()}.csv", rows, COLS)


if __name__ == "__main__":
    sys.exit(cron.run("example-quotes", lambda job: main(job, sys.argv[1:] or ["SPY"]), trading_day=True))
