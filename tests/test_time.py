from datetime import date

from energy_agent_tools.time import day_window


def test_dst_calendar_days_are_not_fixed_24_hours():
    for day, hours in [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25), (date(2026, 9, 30), 24)]:
        start, end = day_window(day, "Europe/London")
        assert (end - start).total_seconds() / 3600 == hours
        assert start.utcoffset().total_seconds() == 0
