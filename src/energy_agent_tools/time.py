from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def day_window(day: date, timezone: str) -> tuple[datetime, datetime]:
    """Local calendar-day boundaries, converted independently to UTC for DST."""
    zone = ZoneInfo(timezone)
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)
