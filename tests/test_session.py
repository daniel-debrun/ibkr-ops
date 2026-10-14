"""Session and gated orders on ib_async, against a scripted fake IB (no Gateway, no socket)."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("ib_async")

from fake_ib import Script, factory

from ibkr_ops import client_ids, gates, orders
from ibkr_ops.config import Endpoint
from ibkr_ops.contracts import to_contract
from ibkr_ops.errors import ClientIdInUse, GatewayDown, IBError
from ibkr_ops.pacing import Pacer
from ibkr_ops.session import ReadOnlySession, Session


def session(script, **kw):
    kw.setdefault("pacer", Pacer(sleep=lambda s: None))
    return Session(kw.pop("endpoint", Endpoint()), ib_factory=factory(script), check_port=False, **kw)


def test_connect_is_read_only_lean_and_raises_request_errors():
    from ib_async import StartupFetchNONE
    sc = Script()
    with session(sc, role="cron-quotes") as s:
        assert s.account == "DU1234567" and s.client_id == client_ids.ROLES["cron-quotes"]
        assert s.ib.RaiseRequestErrors is True
        assert ("connect", True, StartupFetchNONE) in sc.calls and ("mdtype", 3) in sc.calls
    assert "disconnect" in sc.calls


def test_client_id_in_use_moves_to_the_next_id():
    sc = Script(busy_ids={100, 101})
    with session(sc, role="cron-quotes") as s:
        assert s.client_id == 102
    assert sc.connects == [100, 101, 102]
    with pytest.raises(ClientIdInUse):
        session(Script(busy_ids={55}), endpoint=Endpoint(client_id=55)).__enter__()


def test_no_handshake_is_gateway_down():
    sc = Script(silent=True)
    with pytest.raises(GatewayDown, match="no API handshake"):
        with session(sc):
            pass
    assert "disconnect" in sc.calls
    with pytest.raises(GatewayDown, match="nothing listening"):
        Session(Endpoint("127.0.0.1", 1), ib_factory=factory(sc)).__enter__()


def test_history_backs_off_on_pacing_and_sends_a_utc_anchor():
    sc = Script(history=["pacing", "bars"])
    with session(sc) as s:
        b = s.history(to_contract("SPY"), end="2026-10-14", duration="5 D", bar_size="daily", backoff=(0,))
    assert [x.close for x in b] == [10.0, 11.0]
    hist = [c for c in sc.calls if c[0] == "history"]
    assert len(hist) == 2 and hist[-1][1:] == ("20261014-23:59:59", "5 D", "1 day", "TRADES")


def test_history_no_data_is_empty_but_a_timeout_raises():
    sc = Script(history=["nodata", "silent", "pacing", "pacing"])
    with session(sc) as s:
        assert s.history(to_contract("UHLAX"), duration="2 D", bar_size="1 hour", backoff=()) == []
        with pytest.raises(TimeoutError):
            s.history(to_contract("SPY"), duration="2 D", bar_size="1 hour", timeout=0.2, backoff=())
        with pytest.raises(IBError, match="pacing"):
            s.history(to_contract("SPY"), duration="2 D", bar_size="1 hour", backoff=(0,))


def test_fx_history_uses_midpoint():
    sc = Script()
    with session(sc) as s:
        s.history(to_contract("EUR.USD"), duration="1 M", bar_size="1 day")
    assert [c for c in sc.calls if c[0] == "history"][-1][-1] == "MIDPOINT"


def test_lookups_and_qualify():
    d = SimpleNamespace(contract=SimpleNamespace(conId=1, primaryExchange="NASDAQ", exchange="SMART"))
    tsx = SimpleNamespace(contract=SimpleNamespace(conId=2, primaryExchange="TSE", exchange="SMART"))
    sc = Script(details={"AAPL": [tsx, d]})
    with session(sc) as s:
        assert s.qualify(to_contract("AAPL")).conId == 1
        assert s.contract_details(to_contract("NOPE")) == [] and s.qualify(to_contract("NOPE")) is None


def test_quotes_stream_then_cancel_and_tag_delayed():
    sc = Script(quotes={"SPY": dict(marketDataType=3, bid=100.0, ask=100.1, bidSize=3, askSize=4),
                        "QQQ": dict(marketDataType=1, bid=400.0, ask=400.2)})
    with session(sc) as s:
        out = s.quotes({"SPY": to_contract("SPY"), "QQQ": to_contract("QQQ"), "GONE": to_contract("GONE")}, wait=1)
    assert out["SPY"]["bid"] == 100.0 and out["SPY"]["price_type"] == "delayed" and out["SPY"]["error"] is None
    assert out["QQQ"]["price_type"] == "live" and out["QQQ"]["last"] is None
    assert out["GONE"]["price_type"] is None and "[200" in out["GONE"]["error"]
    assert sorted(sc.cancelled) == ["GONE", "QQQ", "SPY"]


def test_connectivity_is_tracked():
    with session(Script()) as s:
        s.ib.errorEvent.emit(-1, 1100, "Connectivity between IB and Trader Workstation has been lost.", None)
        assert s.link_ok is False
        s.ib.errorEvent.emit(-1, 1102, "Connectivity restored - data maintained.", None)
        assert s.link_ok is True and any("1100" in n for n in s.notices)


# --- orders -------------------------------------------------------------------------------------------------
def intent(key="SPY", qty=10, limit=100.5, idem="sig1:SPY"):
    return gates.OrderIntent(key, qty, limit, 100.0, datetime.now(timezone.utc), "live", idem)


def test_read_only_session_refuses_to_send(tmp_path):
    sc = Script()
    with session(sc) as s:
        with pytest.raises(ReadOnlySession):
            orders.execute(s, [intent()], gates.Policy(place=True), tmp_path / "o.jsonl")
        with pytest.raises(ReadOnlySession):
            s.place(to_contract("SPY"), orders.build_order(intent(), s.account))
    assert sc.placed == [] and not (tmp_path / "o.jsonl").exists()


def test_dry_run_places_nothing_but_journals(tmp_path):
    sc, led = Script(), tmp_path / "orders.jsonl"
    with session(sc, readonly=False) as s:
        d, res = orders.execute(s, [intent()], gates.Policy(), led)
    assert d.mode == "dry-run" and res == {} and sc.placed == []
    assert '"event": "decision"' in led.read_text()


def test_paper_order_journals_before_sending_and_refuses_a_repeat(tmp_path):
    sc, led = Script(), tmp_path / "orders.jsonl"
    with session(sc, readonly=False) as s:
        d, res = orders.execute(s, [intent()], gates.Policy(place=True), led)
        assert d.mode == "paper" and res["sig1:SPY"]["status"] == "Filled" and res["sig1:SPY"]["filled"] == 10
        assert orders.failed(res) == []
        events = [l.split('"event": "')[1].split('"')[0] for l in led.read_text().splitlines()]
        assert events == ["decision", "what_if", "sent", "status"]
        assert sc.placed[0][1:] == ("SPY", "BUY", 10.0, "LMT")
        d2, _ = orders.execute(s, [intent()], gates.Policy(place=True), led)
    assert d2.mode == "blocked" and len(sc.placed) == 1


@pytest.mark.parametrize("outcome,code", [("reject", "201"), ("readonly", "321")])
def test_refused_orders_are_rejected_not_active(tmp_path, outcome, code):
    sc = Script(order_outcome={"QQQ": outcome})
    with session(sc, readonly=False) as s:
        _, res = orders.execute(s, [intent("QQQ", idem="sig2:QQQ")], gates.Policy(place=True),
                                tmp_path / "o.jsonl", confirm_timeout=0.5)
    assert res["sig2:QQQ"]["status"] == "Rejected" and code in res["sig2:QQQ"]["error"]
    assert orders.failed(res) == ["sig2:QQQ"]


def test_resting_order_is_cancelled_after_rest(tmp_path):
    sc = Script(order_outcome={"SPY": "rest"})
    with session(sc, readonly=False) as s:
        _, res = orders.execute(s, [intent()], gates.Policy(place=True), tmp_path / "o.jsonl",
                                confirm_timeout=0.5, rest_seconds=0.3)
    assert res["sig1:SPY"]["status"] == "Cancelled" and sc.cancelled
    assert orders.failed(res) == ["sig1:SPY"]                     # unfilled orders are reported


def test_what_if_refusal_stops_the_order(tmp_path):
    sc = Script(what_if_reject={"SPY"})
    with session(sc, readonly=False) as s:
        _, res = orders.execute(s, [intent()], gates.Policy(place=True), tmp_path / "o.jsonl")
    assert res["sig1:SPY"]["status"] == "Refused" and sc.placed == []


def test_live_account_on_paper_port_is_blocked(tmp_path):
    sc = Script(account="U7654321")
    with session(sc, readonly=False) as s:
        d, _ = orders.execute(s, [intent()], gates.Policy(place=True, allow_live=True), tmp_path / "o.jsonl")
    assert d.mode == "blocked" and sc.placed == []
