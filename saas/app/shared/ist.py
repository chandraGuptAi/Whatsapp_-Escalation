"""India Standard Time helpers. Store UTC (timestamptz); convert only for display and windows."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


def day_window(day: datetime) -> tuple[datetime, datetime]:
    """Start (inclusive) and end (exclusive) of the IST calendar day containing `day`."""
    start = datetime.combine(day.astimezone(IST).date(), time.min, IST)
    return start, start + timedelta(days=1)
