"""US equity trading days under NYSE holiday rules.

Covers the regular holidays and observed-date rules: a Saturday holiday is observed the preceding Friday
(except New Year's Day, which is then not observed) and a Sunday holiday the following Monday. Unscheduled
closures and early closes are not modelled; supply them via ``extra_closed``.

Not applicable to ForecastEx event contracts, which trade on most US holidays.
"""
from __future__ import annotations

from datetime import date, timedelta


def _easter(y: int) -> date:
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(y, month, day)


def _nth_weekday(y: int, month: int, weekday: int, n: int) -> date:
    d = date(y, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(y: int, month: int, weekday: int) -> date:
    d = date(y, month + 1, 1) - timedelta(days=1) if month < 12 else date(y, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date, new_years: bool = False) -> date | None:
    if d.weekday() == 5:
        return None if new_years else d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def nyse_holidays(y: int) -> set[date]:
    days = [
        _observed(date(y, 1, 1), new_years=True),
        _nth_weekday(y, 1, 0, 3),          # Martin Luther King Jr. Day
        _nth_weekday(y, 2, 0, 3),          # Washington's Birthday
        _easter(y) - timedelta(days=2),    # Good Friday
        _last_weekday(y, 5, 0),            # Memorial Day
        _observed(date(y, 6, 19)) if y >= 2022 else None,
        _observed(date(y, 7, 4)),
        _nth_weekday(y, 9, 0, 1),          # Labor Day
        _nth_weekday(y, 11, 3, 4),         # Thanksgiving
        _observed(date(y, 12, 25)),
    ]
    return {d for d in days if d is not None}


def is_trading_day(d: date, extra_closed: set[date] = frozenset()) -> bool:
    return d.weekday() < 5 and d not in nyse_holidays(d.year) and d not in extra_closed
