"""Order gates and signed inputs."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from ibkr_ops import gates, signing

NOW = datetime(2026, 10, 14, 19, 50, tzinfo=timezone.utc)


def intent(key="SPY", qty=10, limit=100.5, ref=100.0, age_s=60, ptype="live", idem="s1:SPY"):
    return gates.OrderIntent(key, qty, limit, ref, NOW - timedelta(seconds=age_s), ptype, idem)


def ctx(account="DU1234567", port=4002, **kw):
    return gates.Context(account=account, port=port, now=NOW, env=kw.pop("env", {}), **kw)


def test_dry_run_is_the_default():
    d = gates.check([intent()], gates.Policy(), ctx())
    assert d.mode == "dry-run" and not d.will_place and len(d.allowed) == 1


def test_paper_places_with_flag():
    d = gates.check([intent()], gates.Policy(place=True), ctx())
    assert d.mode == "paper" and d.will_place


def test_live_needs_policy_and_env():
    live = ctx(account="U1234567", port=4001)
    assert gates.check([intent()], gates.Policy(place=True), live).mode == "blocked"
    assert gates.check([intent()], gates.Policy(place=True, allow_live=True), live).mode == "blocked"
    ok = ctx(account="U1234567", port=4001, env={"IBKR_ALLOW_LIVE": "1"})
    assert gates.check([intent()], gates.Policy(place=True, allow_live=True), ok).mode == "live"


def test_port_account_disagreement_blocks_everything():
    d = gates.check([intent()], gates.Policy(place=True, allow_live=True),
                    ctx(account="U1234567", port=4002, env={"IBKR_ALLOW_LIVE": "1"}))
    assert d.mode == "blocked" and "convention" in d.blocked[0] and "U*******" in d.blocked[0]
    assert gates.check([intent()], gates.Policy(place=True), ctx(account="")).mode == "blocked"


def test_kill_switch(tmp_path):
    k = tmp_path / "KILL"
    k.touch()
    d = gates.check([intent()], gates.Policy(place=True, kill_switch=str(k)), ctx())
    assert d.mode == "blocked" and "kill switch" in d.blocked[0]


@pytest.mark.parametrize("kw,why", [
    (dict(age_s=3600), "old"),
    (dict(ptype="delayed"), "not live"),
    (dict(ref=None), "no reference price"),
    (dict(limit=None), "market orders"),
    (dict(limit=110.0), "more than"),
    (dict(qty=10.5), "fractional"),
    (dict(qty=0), "zero"),
    (dict(qty=200), "max_order_value"),
    (dict(key="SPY 20261120 450 C", ref=1.0, limit=1.01, qty=1), None),
    (dict(key="FCX:UHLAX 20261008 88 C", ref=0.4, limit=1.2, qty=5), "(0, 1)"),
])
def test_order_reasons(kw, why):
    d = gates.check([intent(**kw)], gates.Policy(place=True, all_or_nothing=False), ctx())
    if why is None:
        assert d.allowed and not d.refused
    else:
        assert d.refused and why in d.refused[0][1]


def test_delayed_allowed_when_asked():
    d = gates.check([intent(ptype="delayed")], gates.Policy(place=True, allow_delayed_quotes=True), ctx())
    assert d.mode == "paper"


def test_allowlist_and_sec_types():
    p = gates.Policy.from_dict({"place": True, "allowlist": ["qqq"], "all_or_nothing": False})
    assert "allowlist" in gates.check([intent()], p, ctx()).refused[0][1]
    p = gates.Policy.from_dict({"place": True, "sec_types": ["STK"], "all_or_nothing": False})
    assert "sec type" in gates.check([intent(key="EUR.USD", qty=1000, ref=1.1, limit=1.1)], p, ctx()).refused[0][1]
    with pytest.raises(ValueError, match="unknown policy keys"):
        gates.Policy.from_dict({"max_order_valu": 1e9})


def test_idempotency():
    d = gates.check([intent()], gates.Policy(place=True), ctx(sent_keys=frozenset({"s1:SPY"})))
    assert d.mode == "blocked" and "already sent" in d.refused[0][1]
    d = gates.check([intent(), intent()], gates.Policy(place=True, all_or_nothing=False), ctx())
    assert len(d.allowed) == 1 and "duplicate" in d.refused[0][1]


def test_all_or_nothing_and_batch_caps():
    good, bad = intent(), intent(key="QQQ", idem="s1:QQQ", ptype="delayed")
    d = gates.check([good, bad], gates.Policy(place=True), ctx())
    assert d.mode == "blocked" and not d.allowed
    d = gates.check([good, bad], gates.Policy(place=True, all_or_nothing=False), ctx())
    assert d.mode == "paper" and d.allowed == [good]
    many = [intent(key=k, idem=k) for k in ("A", "B", "C")]
    assert "max_batch_value" in gates.check(many, gates.Policy(place=True, max_batch_value=2500), ctx()).blocked[0]
    d = gates.check([good], gates.Policy(place=True), ctx(placed_today_value=49_500))
    assert "max_daily_value" in d.blocked[0]
    assert "max_orders" in gates.check(many, gates.Policy(place=True, max_orders=2), ctx()).blocked[0]


def test_marketable_limit_never_exceeds_slippage():
    assert gates.marketable_limit(100.0, 5, 0.01) == 101.0
    assert gates.marketable_limit(100.003, 5, 0.01) <= 100.003 * 1.01
    assert gates.marketable_limit(100.0, -5, 0.01) == 99.0
    assert gates.PRO_FIXED.estimate(100, 50) == 1.0 and gates.PRO_FIXED.estimate(1000, 50) == 5.0


# --- signing ------------------------------------------------------------------------------------------------
@pytest.fixture
def keys():
    from nacl.encoding import HexEncoder
    from nacl.signing import SigningKey
    sk = SigningKey.generate()
    return sk.encode(encoder=HexEncoder).decode(), sk.verify_key.encode(encoder=HexEncoder).decode()


def test_signed_input(tmp_path, keys):
    sk, pk = keys
    data = tmp_path / "orders.csv"
    data.write_text("key,qty\nSPY,10\n")
    sig = signing.sign(data, sk, NOW + timedelta(minutes=30), key_id="desk")
    path, meta = signing.verify(sig, [pk], now=NOW)
    assert path == data and meta["key_id"] == "desk"
    assert signing.verify(sig, [signing.key_fingerprint(pk)], now=NOW)[0] == data
    with pytest.raises(signing.Refused, match="expired"):
        signing.verify(sig, [pk], now=NOW + timedelta(hours=1))
    with pytest.raises(signing.Refused, match="custom"):
        signing.verify(sig, [pk], now=NOW, extra_checks=[lambda m: "custom rule failed"])
    data.write_text("key,qty\nSPY,1000\n")
    with pytest.raises(signing.Refused, match="changed after"):
        signing.verify(sig, [pk], now=NOW)


def test_a_claimed_key_id_is_not_trust(tmp_path, keys):
    """An attacker signs with their own key and claims the trusted key_id: refused (trust is the key)."""
    from nacl.encoding import HexEncoder
    from nacl.signing import SigningKey
    _, trusted_pk = keys
    attacker = SigningKey.generate().encode(encoder=HexEncoder).decode()
    data = tmp_path / "orders.csv"
    data.write_text("key,qty\nSPY,10\n")
    sig = signing.sign(data, attacker, NOW + timedelta(minutes=30), key_id="desk")
    with pytest.raises(signing.Refused, match="not trusted"):
        signing.verify(sig, [trusted_pk, "desk"], now=NOW)
    m = json.loads(sig.read_text())
    m["key_id"] = "other"
    sig.write_text(json.dumps(m))
    with pytest.raises(signing.Refused, match="signature"):
        signing.verify(sig, [trusted_pk], now=NOW)
