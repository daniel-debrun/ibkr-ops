"""Historical-data request planning and bar parsing (no network I/O).

TWS historical requests are defined by an ``endDateTime`` anchor and a ``durationStr`` lookback; there is no
start parameter. Conventions applied here:

* anchors are sent as ``yyyyMMdd-HH:mm:ss`` in UTC (timezone-less anchors are deprecated, warning 2174);
* bar sizes are validated against IB's accepted tokens rather than passed through;
* intraday spans are split into 30-day requests for bars of 5 minutes or more and 7-day requests below;
  daily and coarser bars are requested as a single span;
* TRADES history is split-adjusted but not dividend-adjusted (ADJUSTED_LAST includes dividends);
* cash/FX and bonds have no TRADES data and default to MIDPOINT;
* zero-volume bars repeat the previous close and can be excluded with ``traded_only``.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import clean

BAR_SIZES = ("1 secs", "5 secs", "10 secs", "15 secs", "30 secs", "1 min", "2 mins", "3 mins", "5 mins",
             "10 mins", "15 mins", "20 mins", "30 mins", "1 hour", "2 hours", "3 hours", "4 hours", "8 hours",
             "1 day", "1 week", "1 month")
_ALIASES = {"1m": "1 min", "5m": "5 mins", "15m": "15 mins", "30m": "30 mins", "1h": "1 hour", "1d": "1 day",
            "daily": "1 day", "day": "1 day", "weekly": "1 week", "monthly": "1 month", "minute": "1 min",
            "hourly": "1 hour", "5 min": "5 mins", "15 min": "15 mins", "30 min": "30 mins", "2 min": "2 mins",
            "1 mins": "1 min", "1 hours": "1 hour", "1 days": "1 day"}
WHAT_TO_SHOW = ("TRADES", "MIDPOINT", "BID", "ASK", "BID_ASK", "ADJUSTED_LAST", "HISTORICAL_VOLATILITY",
                "OPTION_IMPLIED_VOLATILITY", "FEE_RATE", "SCHEDULE")


def bar_size(spec: str) -> str:
    s = " ".join(str(spec).strip().lower().split())
    s = _ALIASES.get(s, s)
    if s not in BAR_SIZES:
        raise ValueError(f"unknown bar size {spec!r}; IB accepts {', '.join(BAR_SIZES)}")
    return s


def bar_minutes(size: str) -> float:
    n, unit = bar_size(size).split()
    return int(n) * {"secs": 1 / 60, "min": 1, "mins": 1, "hour": 60, "hours": 60,
                     "day": 1440, "week": 10080, "month": 43200}[unit]


def default_what_to_show(sec_type: str) -> str:
    return "MIDPOINT" if sec_type.upper() in ("CASH", "BOND") else "TRADES"


def _as_utc(t) -> datetime:
    if isinstance(t, datetime):
        return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)
    if isinstance(t, date):
        return datetime(t.year, t.month, t.day, 23, 59, 59, tzinfo=timezone.utc)
    return _as_utc(datetime.fromisoformat(str(t)) if ("T" in str(t) or ":" in str(t))
                   else date.fromisoformat(str(t)))


def end_anchor(t) -> str:
    """``t`` (date, datetime, ISO string; naive = UTC) -> ``yyyyMMdd-HH:mm:ss`` UTC. A bare date anchors at
    23:59:59 so that day's bar is included. ``None``/"" -> "" (now)."""
    if t in (None, ""):
        return ""
    return _as_utc(t).strftime("%Y%m%d-%H:%M:%S")


def duration_for(start, end) -> str:
    days = max(1, (_as_utc(end) - _as_utc(start)).days)
    return f"{days} D" if days <= 365 else f"{-(-days // 365)} Y"


def chunk_plan(size: str, start, end) -> list[tuple[str, str]]:
    """``[(endDateTime, durationStr), ...]`` ascending, covering ``[start, end]``. Chunks may overlap the
    window edges; dedupe on (symbol, time) and trim after fetching."""
    size = bar_size(size)
    s, e = _as_utc(start), _as_utc(end)
    if bar_minutes(size) >= 1440:
        return [(end_anchor(e), duration_for(s, e))]
    step = 30 if bar_minutes(size) >= 5 else 7
    anchors, a = [], e
    while a > s:
        anchors.append(a)
        a -= timedelta(days=step)
    return [(end_anchor(a), f"{step} D") for a in reversed(anchors)]


def parse_bar_time(raw) -> date | datetime:
    """A ``BarData.date``: ``yyyyMMdd`` (daily) -> date; epoch seconds (formatDate=2) -> UTC datetime;
    ``yyyyMMdd HH:mm:ss [Zone/Name]`` -> datetime in that zone (kept, not dropped), naive if none given."""
    s = " ".join(str(raw).split())
    if s.isdigit():
        if len(s) == 8:
            return date(int(s[:4]), int(s[4:6]), int(s[6:]))
        return datetime.fromtimestamp(int(s), tz=timezone.utc)
    parts = s.split(" ")
    dt = datetime.strptime(f"{parts[0]} {parts[1]}", "%Y%m%d %H:%M:%S")
    return dt.replace(tzinfo=ZoneInfo(parts[2])) if len(parts) > 2 else dt


def bar_rows(symbol: str, bars, traded_only: bool = False) -> list[dict]:
    """ib_async (or ibapi) ``BarData`` -> tidy dicts. ``volume``/``wap`` None where IB has none.
    ``traded_only`` drops zero-volume bars (which only repeat the last close)."""
    out = []
    for b in bars:
        vol = clean.bar_volume(getattr(b, "volume", None))
        if traded_only and not vol:
            continue
        wap = clean.num(getattr(b, "wap", getattr(b, "average", None)))
        out.append({"symbol": symbol, "time": parse_bar_time(b.date) if not isinstance(b.date, (date, datetime)) else b.date,
                    "open": float(b.open), "high": float(b.high), "low": float(b.low), "close": float(b.close),
                    "volume": vol, "wap": wap if wap and wap > 0 else None,
                    "count": getattr(b, "barCount", None)})
    return out


def dedupe(rows: list[dict], start=None, end=None) -> list[dict]:
    """Merge overlapping chunks: last write wins per (symbol, time), sorted, trimmed to [start, end]."""
    keyed = {(r["symbol"], r["time"]): r for r in rows}
    out = [keyed[k] for k in sorted(keyed, key=lambda k: (k[0], _sort_key(k[1])))]
    if start is not None or end is not None:
        lo = _lower(start) if start is not None else None
        hi = _as_utc(end) if end is not None else None
        out = [r for r in out if (lo is None or _sort_key(r["time"]) >= lo) and (hi is None or _sort_key(r["time"]) <= hi)]
    return out


def _lower(t) -> datetime:
    """A window's lower bound: a bare date means its midnight (where ``_as_utc`` means its last second)."""
    if isinstance(t, str) and "T" not in t and ":" not in t:
        t = date.fromisoformat(t)
    return _sort_key(t) if isinstance(t, date) else _as_utc(t)


def _sort_key(t) -> datetime:
    if isinstance(t, datetime):
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return datetime(t.year, t.month, t.day, tzinfo=timezone.utc)
