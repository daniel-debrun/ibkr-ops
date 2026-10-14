import pytest


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Every test gets its own lock/heartbeat dir and never sees the host's IB env vars."""
    monkeypatch.setenv("IBKR_OPS_STATE", str(tmp_path / "state"))
    for k in ("IB_HOST", "IBKR_HOST", "IB_PORT", "IBKR_PORT", "IB_CLIENT_ID", "IBKR_ALLOW_LIVE", "IBKR_OPS_ALERT_CMD"):
        monkeypatch.delenv(k, raising=False)
