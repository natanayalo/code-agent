"""Common serialization and conversion utilities."""

from typing import Any


def to_dict(value: Any) -> dict[str, Any]:
    """Normalize serialized dictionaries and Pydantic models defensively."""
    if isinstance(value, dict):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
            return dict(dumped) if isinstance(dumped, dict) else {}
        except Exception:
            pass
    legacy_dict = getattr(value, "dict", None)
    if callable(legacy_dict):
        try:
            dumped = legacy_dict()
            return dict(dumped) if isinstance(dumped, dict) else {}
        except Exception:
            pass
    return {}
