"""Validation and calendar translation for weekly schedules."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from temporalio.client import ScheduleRange

from orchestrator.schedule_spec import ScheduleSpec


def valid_spec(**changes):
    values = {
        "version": 1,
        "purpose": "dependency_review",
        "repo_key": "project",
        "weekdays": ["monday", "sunday"],
        "time": "09:05",
        "timezone": "America/New_York",
    }
    return ScheduleSpec.model_validate(values | changes)


def test_calendar_preserves_local_timezone_and_translates_weekdays():
    spec = valid_spec()
    calendar = spec.temporal_spec()
    assert spec.weekdays == ("sunday", "monday")
    assert calendar.time_zone_name == "America/New_York"
    assert calendar.calendars[0].hour == [ScheduleRange(9)]
    assert calendar.calendars[0].minute == [ScheduleRange(5)]
    assert calendar.calendars[0].day_of_week == [ScheduleRange(0), ScheduleRange(1)]


def test_timezone_tracks_spring_and_fall_dst_transitions():
    zone = ZoneInfo(valid_spec().temporal_spec().time_zone_name)
    before_spring = datetime(2026, 3, 8, 6, 59, tzinfo=UTC).astimezone(zone)
    after_spring = datetime(2026, 3, 8, 7, 0, tzinfo=UTC).astimezone(zone)
    first_fall = datetime(2026, 11, 1, 5, 30, tzinfo=UTC).astimezone(zone)
    second_fall = datetime(2026, 11, 1, 6, 30, tzinfo=UTC).astimezone(zone)
    assert (before_spring.hour, after_spring.hour) == (1, 3)
    assert (first_fall.hour, second_fall.hour) == (1, 1)
    assert (first_fall.fold, second_fall.fold) == (0, 1)


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 2},
        {"version": True},
        {"purpose": "mutation"},
        {"repo_key": ""},
        {"branch": "invalid branch"},
        {"weekdays": []},
        {"weekdays": ["monday", "monday"]},
        {"weekdays": ["funday"]},
        {"time": "24:00"},
        {"time": "9:05"},
        {"timezone": "Not/A_Zone"},
        {"timezone": "../UTC"},
        {"task_text": "change files"},
        {"secrets": {"token": "value"}},
    ],
)
def test_invalid_or_unsupported_fields_are_rejected(changes):
    with pytest.raises(ValidationError):
        valid_spec(**changes)
