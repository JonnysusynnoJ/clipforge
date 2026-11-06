"""Slot planning per account: daily limit, minimum gap and a local posting window."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


def next_slot(
    *,
    now: datetime,
    taken: Iterable[datetime],
    daily_limit: int,
    min_gap: timedelta,
    tz: str,
    window_start: time,
    window_end: time,
    lead: timedelta = timedelta(minutes=30),
    preferred_hours: list[int] | None = None,
    horizon_days: int = 14,
) -> datetime:
    """Earliest allowed publish time (timezone-aware, in `tz`).

    Rules: at most `daily_limit` posts per local calendar day, at least `min_gap` between any
    two posts of the account, inside [window_start, window_end), and not before now + lead.
    `preferred_hours` (e.g. Metricool best times) are tried first on each day.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    zone = ZoneInfo(tz)
    local_now = now.astimezone(zone)
    earliest = local_now + lead
    times = sorted(t.astimezone(zone) for t in taken)

    def ok(candidate: datetime) -> bool:
        if candidate < earliest:
            return False
        if not (window_start <= candidate.time() < window_end):
            return False
        same_day = [t for t in times if t.date() == candidate.date()]
        if len(same_day) >= daily_limit:
            return False
        return all(abs(candidate - t) >= min_gap for t in times)

    for day in range(horizon_days):
        date = (local_now + timedelta(days=day)).date()
        start = datetime.combine(date, window_start, tzinfo=zone)
        end = datetime.combine(date, window_end, tzinfo=zone)
        candidates = [
            datetime.combine(date, time(h, 0), tzinfo=zone)
            for h in preferred_hours or []
            if window_start.hour <= h < window_end.hour
        ]
        t = max(start, earliest.replace(second=0, microsecond=0))
        if t.minute % 15:  # round up to the next quarter hour
            t += timedelta(minutes=15 - t.minute % 15)
        while t < end:
            candidates.append(t)
            t += timedelta(minutes=15)
        for c in candidates:
            if ok(c):
                return c
    raise RuntimeError(f"no free slot within {horizon_days} days")
