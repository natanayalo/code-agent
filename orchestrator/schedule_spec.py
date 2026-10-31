"""Validated, versioned configuration for weekly dependency reviews."""

from __future__ import annotations

from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from temporalio.client import ScheduleCalendarSpec, ScheduleRange
from temporalio.client import ScheduleSpec as TemporalScheduleSpec

Weekday = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_DAYS = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")


class EvidenceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["operational_incidents"]
    lookback_hours: int = Field(strict=True, ge=1, le=168)


class ScheduleSpec(BaseModel):
    """The complete public configuration; execution options are intentionally fixed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    purpose: Literal["dependency_review", "health_summary", "evidence_collection"]
    evidence: EvidenceConfig | None = Field(default=None, exclude_if=lambda value: value is None)
    repo_key: str = Field(min_length=1, max_length=255, pattern=r"^\S+$")
    branch: str | None = Field(default=None, min_length=1, max_length=255, pattern=r"^\S+$")
    weekdays: tuple[Weekday, ...] = Field(min_length=1, max_length=7)
    time: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    timezone: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_evidence(self) -> ScheduleSpec:
        if (self.purpose == "evidence_collection") != (self.evidence is not None):
            raise ValueError("evidence configuration is required only for evidence collection")
        return self

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("version must be an integer")
        return value

    @field_validator("weekdays")
    @classmethod
    def normalize_weekdays(cls, value: tuple[Weekday, ...]) -> tuple[Weekday, ...]:
        if len(set(value)) != len(value):
            raise ValueError("weekdays must not contain duplicates")
        return tuple(sorted(value, key=_DAYS.index))

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

    def temporal_spec(self) -> TemporalScheduleSpec:
        """Keep wall-clock and DST interpretation in Temporal's calendar engine."""
        hour, minute = (int(part) for part in self.time.split(":"))
        return TemporalScheduleSpec(
            calendars=[
                ScheduleCalendarSpec(
                    hour=[ScheduleRange(hour)],
                    minute=[ScheduleRange(minute)],
                    day_of_week=[ScheduleRange(_DAYS.index(day)) for day in self.weekdays],
                )
            ],
            time_zone_name=self.timezone,
        )
