"""Example: place orders from a signed CSV through the full gate sequence. Dry run unless --place.

    python3 examples/orders_job.py signals/today.csv            # dry run: prints the decision
    python3 examples/orders_job.py signals/today.csv --place    # paper account only, unless allow_live

Input CSV columns: key, qty, limit, idem_key. The CSV requires a valid <csv>.sig.json from a trusted key.
Configuration: config.json {"trusted_keys": [...], "policy": {...}}; unknown policy keys are rejected.
"""
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

from ibkr_ops import cron, gates, orders, signing
from ibkr_ops.contracts import to_contract
from ibkr_ops.session import Session


def main(job: cron.Job, path: str, place: bool) -> int:
    cfg = json.loads(Path("config.json").read_text())
    policy = gates.Policy.from_dict({**cfg.get("policy", {}), "place": place,
                                     "kill_switch": cfg.get("policy", {}).get("kill_switch", "state/KILL")})
    try:
        data, _ = signing.verify(path + signing.SUFFIX, cfg["trusted_keys"])
    except signing.Refused as e:
        raise cron.Refused(f"signed input: {e}") from None
    with data.open(newline="") as f:
        lines = list(csv.DictReader(f))
    with Session(role="cron-orders", market_data="live", readonly=not place) as s:
        quotes = s.quotes({r["key"]: to_contract(r["key"]) for r in lines})
        intents = []
        for r in lines:
            q = quotes[r["key"]]
            side = q["ask"] if float(r["qty"]) > 0 else q["bid"]          # never a stale close
            intents.append(gates.OrderIntent(
                key=r["key"], qty=float(r["qty"]), limit=float(r["limit"]) if r.get("limit") else None,
                ref_price=side, quote_at=datetime.fromisoformat(q["at"]),
                price_type=q["price_type"], idem_key=r["idem_key"]))
        decision, results = orders.execute(s, intents, policy, ledger="state/orders.jsonl")
    print(decision.summary())
    if decision.mode == "blocked":
        raise cron.Refused("; ".join(decision.blocked))
    bad = orders.failed(results)
    if bad:
        job.failures["orders"] = ", ".join(bad)
    return cron.OK


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--place"]
    sys.exit(cron.run("example-orders", lambda job: main(job, args[0], "--place" in sys.argv)))
