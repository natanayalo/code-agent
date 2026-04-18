"""Shared numeric coercion helpers for config and budget parsing."""

from __future__ import annotations


def coerce_int_like(value: object) -> int | None:
    """Parse integer-like values while rejecting booleans and invalid numerics."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            return int(value)
        except (OverflowError, ValueError):
            return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return int(float(stripped))
        except (OverflowError, ValueError):
            return None
    return None


def coerce_positive_int_like(value: object) -> int | None:
    """Return a positive integer-like value when present."""
    parsed = coerce_int_like(value)
    return parsed if parsed is not None and parsed > 0 else None


def coerce_non_negative_int_like(value: object) -> int | None:
    """Return a non-negative integer-like value when present."""
    parsed = coerce_int_like(value)
    return parsed if parsed is not None and parsed >= 0 else None
