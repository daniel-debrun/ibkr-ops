"""Pure modules: errors, config, client ids, pacing, clean, history, contracts, io, trading days."""
import math
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from ibkr_ops import clean, client_ids, config, errors, history, io, pacing, tradingdays
from ibkr_ops.contracts import ContractCache, Instrument, Resolved, parse_fcx_local_symbol, pick_listing

UNSET_DOUBLE = 1.7976931348623157e308


# --- errors -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("code,msg,kind", [
    (162, "Historical Market Data Service error message:API historical data query pacing violation", errors.Kind.PACING),
    (162, "Historical Market Data Service error message:HMDS query returned no data: UHLAX", errors.Kind.NO_DATA),
    (162, "Historical Market Data Service error message:API historical data query cancelled: 4", errors.Kind.INFO),
    (2104, "Market data farm connection is OK", errors.Kind.INFO),
    (2119, "some new farm notice", errors.Kind.INFO),
    (10167, "Displaying delayed market data", errors.Kind.INFO),
    (2174, "Warning: ... without explicit time zone", errors.Kind.WARNING),
    (326, "client id is already in use", errors.Kind.CLIENT_ID),
    (1100, "Connectivity between IB and TWS has been lost", errors.Kind.CONNECTIVITY),
    (200, "No security definition", errors.Kind.NO_DATA),
    (10089, "requires additional subscription", errors.Kind.ENTITLEMENT),
    (201, "Order rejected", errors.Kind.REJECTED),
    (99999, "never seen", errors.Kind.FATAL),
])
def test_classify(code, msg, kind):
    assert errors.classify(code, msg) is kind


def test_notices_and_warnings_never_end_a_request():
    assert not errors.ends_request(errors.Kind.INFO) and not errors.ends_request(errors.Kind.WARNING)
    assert errors.ends_request(errors.Kind.NO_DATA) and errors.ends_request(errors.Kind.PACING)
    assert "too old" in errors.describe(10285, "fractional size rules")


# --- config -------------------------------------------------------------------------------------------------
def test_endpoint_resolution(monkeypatch):
    assert config.Endpoint.resolve() == config.Endpoint("127.0.0.1", 4002, None)
    monkeypatch.setenv("IBKR_PORT", "4001")
    monkeypatch.setenv("IB_CLIENT_ID", "42")
    ep = config.Endpoint.resolve()
    assert (ep.port, ep.client_id, ep.mode) == (4001, 42, "live")
    assert config.Endpoint.resolve({"port": 7497}).mode == "paper"


def test_accounts():
    assert config.is_paper_account("DU1234567") and config.is_paper_account("DUP0000001")
    assert not config.is_paper_account("U1234567")
    assert config.redact("DU1234567") == "DU*******"
    assert config.market_data_type(None) == 3 and config.market_data_type("delayed-frozen") == 4
    with pytest.raises(ValueError):
        config.market_data_type("realtimeish")


# --- client ids ---------------------------------------------------------------------------------------------
def test_lease_skips_held_ids(tmp_path):
    with client_ids.lease(100, 4, directory=tmp_path) as a:
        with client_ids.lease(100, 4, directory=tmp_path) as b:
            with client_ids.lease(100, 4, skip=frozenset({102}), directory=tmp_path) as c:
                assert (a, b, c) == (100, 101, 103)


def test_lease_exhausted_and_released(tmp_path):
    with client_ids.lease(100, 1, directory=tmp_path) as a:
        with pytest.raises(client_ids.NoFreeClientId):
            with client_ids.lease(100, 1, directory=tmp_path):
                pass
    with client_ids.lease(100, 1, directory=tmp_path) as again:
        assert again == a == 100
    with pytest.raises(ValueError):
        with client_ids.lease(0, 1, directory=tmp_path):
            pass


# --- pacing -------------------------------------------------------------------------------------------------
class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(round(s, 3))
        self.t += s


def test_pacer_window_identical_and_per_contract():
    c = Clock()
    p = pacing.Pacer(max_requests=3, window=10, identical_gap=15, per_contract=(2, 2.0), clock=c, sleep=c.sleep)
    p.wait(key="a", contract="X")
    p.wait(key="b", contract="X")
    assert p.delay(key="c", contract="X") == 2.0          # 2 per 2 s on one contract
    assert p.delay(key="a") == 15.0                       # identical request within 15 s
    p.wait(key="c", contract="Y")
    assert p.delay(key="d") == 10.0                       # 3 per 10 s window is full
    p.wait(key="d")
    assert c.slept == [10.0]


def test_backoff_retries_pacing_and_timeouts_then_gives_up():
    slept, calls = [], []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise errors.PacingError(162, "pacing violation")
        if len(calls) == 2:
            raise TimeoutError("no answer")
        return "bars"

    assert pacing.with_backoff(flaky, sleep=slept.append) == "bars"
    assert slept == [30.0, 60.0]
    with pytest.raises(errors.IBError):
        pacing.with_backoff(lambda: (_ for _ in ()).throw(errors.IBError(321, "read-only")), sleep=slept.append)
    with pytest.raises(TimeoutError):
        pacing.with_backoff(lambda: (_ for _ in ()).throw(TimeoutError()), delays=(1,), sleep=slept.append)


# --- clean --------------------------------------------------------------------------------------------------
def test_sentinels():
    assert clean.price(-1) is None and clean.price(0) is None and clean.price(float("nan")) is None
    assert clean.price(UNSET_DOUBLE) is None and clean.price(0.04) == 0.04
    assert clean.greek(-2) is None and clean.greek(-1, is_vol=True) is None and clean.greek(-0.4) == -0.4
    assert clean.size(0, paired_price=-1) is None and clean.size(0, paired_price=0.5) == 0
    from decimal import Decimal
    assert clean.bar_volume(Decimal(-1)) is None and clean.bar_volume(Decimal(2**127 - 1)) is None


def test_quote_live_beats_delayed_and_no_stale_fallback():
    q = clean.quote({1: 10.0, 66: 9.0, 2: 10.2, 0: 5, 3: 7, 9: 9.5})
    assert (q["bid"], q["ask"], q["price_type"]) == (10.0, 10.2, "live")
    d = clean.quote({66: -1, 67: 0.04, 69: 0, 70: 5000, 75: 0.05})
    assert d["bid"] is None and d["bid_size"] is None and d["ask"] == 0.04 and d["price_type"] == "delayed"
    none = clean.quote({9: 101.0})                         # only a close: no price_type, no bid/ask
    assert none["price_type"] is None and none["bid"] is None and none["close"] == 101.0
    assert clean.mid(q) == pytest.approx(10.1)


# --- history ------------------------------------------------------------------------------------------------
def test_bar_size_is_validated():
    assert history.bar_size("5 min") == "5 mins" and history.bar_size("1 MIN") == "1 min"
    assert history.bar_size("daily") == "1 day"
    with pytest.raises(ValueError):
        history.bar_size("5 minutes")


def test_end_anchor_is_utc_dash_form():
    assert history.end_anchor(date(2026, 10, 14)) == "20261014-23:59:59"
    toronto = datetime(2026, 10, 14, 16, 0, tzinfo=ZoneInfo("America/Toronto"))
    assert history.end_anchor(toronto) == "20261014-20:00:00"
    assert history.end_anchor("") == ""


def test_chunk_plans():
    p5 = history.chunk_plan("5 mins", date(2026, 7, 1), date(2026, 9, 30))
    assert all(d == "30 D" for _, d in p5) and len(p5) == 4 and p5[-1][0] == "20260930-23:59:59"
    p1 = history.chunk_plan("1 min", date(2026, 9, 1), date(2026, 9, 30))
    assert all(d == "7 D" for _, d in p1) and len(p1) == 5
    assert history.chunk_plan("1 day", date(2016, 1, 1), date(2026, 1, 1)) == [("20260101-23:59:59", "11 Y")]
    assert history.chunk_plan("1 day", date(2026, 1, 1), date(2026, 3, 1)) == [("20260301-23:59:59", "59 D")]


def test_parse_bar_time_keeps_zone():
    assert history.parse_bar_time("20261014") == date(2026, 10, 14)
    assert history.parse_bar_time("1760472000") == datetime(2025, 10, 14, 20, 0, tzinfo=timezone.utc)
    t = history.parse_bar_time("20261014 16:16:00 US/Central")
    assert t.tzinfo is not None and t.utcoffset() == timedelta(hours=-5)
    assert history.parse_bar_time("20261014  09:30:00").tzinfo is None


def test_bar_rows_and_dedupe():
    bars = [SimpleNamespace(date="20261013", open=1, high=1, low=1, close=1, volume=-1, wap=0, barCount=0),
            SimpleNamespace(date="20261014", open=2, high=2, low=2, close=2, volume=5, wap=2, barCount=1)]
    rows = history.bar_rows("EUR.USD", bars)
    assert rows[0]["volume"] is None and rows[0]["wap"] is None and rows[1]["volume"] == 5
    assert [r["time"] for r in history.bar_rows("X", bars, traded_only=True)] == [date(2026, 10, 14)]
    merged = history.dedupe(rows + history.bar_rows("EUR.USD", bars[1:]), start="2026-10-14", end="2026-10-14")
    assert [r["time"] for r in merged] == [date(2026, 10, 14)]


# --- contracts ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("key,sec", [("SPY", "STK"), ("EUR.USD", "CASH"), ("SPY 20261120 450 C", "OPT"),
                                     ("FCX:UHLAX 20261008 88 C", "FCX"), ("BOND:912828XX1", "BOND"),
                                     ("CON:853400777", "CON")])
def test_instrument_round_trip(key, sec):
    i = Instrument.parse(key)
    assert i.sec_type == sec and Instrument.parse(i.key) == i
    assert Instrument.parse("FCX:UHLAX 20261008 88 C").multiplier == 1.0


def test_fcx_local_symbol():
    assert parse_fcx_local_symbol("UHLAX_101126_75_YES") == {
        "product": "UHLAX", "target": "20261011", "strike": 75.0, "side": "YES", "right": "C"}


def test_pick_listing_prefers_us_then_canada():
    d = lambda ex: SimpleNamespace(contract=SimpleNamespace(primaryExchange=ex, exchange="SMART"))
    assert pick_listing([d("LSE"), d("TSE"), d("NASDAQ")]).primaryExchange == "NASDAQ"
    assert pick_listing([d("LSE"), d("TSE")]).primaryExchange == "TSE"
    assert pick_listing([d("LSE")]).primaryExchange == "LSE"


def test_contract_cache_negative_ttl(tmp_path):
    now = [1000.0]
    calls = []

    def fetch(found):
        def f():
            calls.append(1)
            return found
        return f

    cache = ContractCache(tmp_path / "c.json", negative_ttl=60, clock=lambda: now[0])
    assert cache.lookup("SHXYZ", fetch([])) == []
    assert cache.lookup("SHXYZ", fetch([])) == [] and len(calls) == 1      # negative-cached
    now[0] += 61
    assert cache.lookup("SHXYZ", fetch([Resolved(5)]))[0].con_id == 5 and len(calls) == 2
    again = ContractCache(tmp_path / "c.json", clock=lambda: now[0])
    assert again.lookup("SHXYZ", fetch([]))[0].con_id == 5 and len(calls) == 2
    (tmp_path / "torn.json").write_text("{\"found\": ")
    assert ContractCache(tmp_path / "torn.json").data == {"found": {}, "missing": {}}


# --- io -----------------------------------------------------------------------------------------------------
def test_atomic_write_and_ledger(tmp_path):
    p = io.atomic_write_csv(tmp_path / "q.csv", [{"a": 1, "b": None}], ["a", "b"])
    assert p.read_text() == "a,b\n1,\n"
    assert [x.name for x in tmp_path.iterdir()] == ["q.csv"]           # no temp files left behind
    led = tmp_path / "l.jsonl"
    io.append_jsonl(led, {"order_id": 7, "status": "Rejected"})
    io.append_jsonl(led, {"order_id": 7, "status": "Filled"})
    with led.open("a") as f:
        f.write('{"order_id": 8, "sta')                               # a killed writer's torn line
    recs = io.read_jsonl(led)
    assert len(recs) == 2 and io.latest_by(recs, "order_id")[7]["status"] == "Filled"
    assert io.run_stamp(datetime(2026, 10, 14, 2, 31, tzinfo=timezone.utc)) == "20261014T0231Z"


# --- trading days -------------------------------------------------------------------------------------------
def test_nyse_holidays():
    assert tradingdays.nyse_holidays(2026) == {
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
        date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25)}
    h22 = tradingdays.nyse_holidays(2022)
    assert date(2021, 12, 31) not in tradingdays.nyse_holidays(2021)      # Sat New Year's: not observed
    assert date(2022, 12, 26) in h22 and date(2022, 6, 20) in h22
    assert not tradingdays.is_trading_day(date(2026, 10, 17))             # Saturday
    assert tradingdays.is_trading_day(date(2026, 10, 14))
