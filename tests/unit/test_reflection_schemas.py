"""Unit tests for the Reflection and Improvement Pydantic schemas."""

import pytest
from pydantic import ValidationError

from orchestrator.reflection import FrictionReport, ImprovementSuggestion


def test_friction_report_defaults():
    """Verify that FrictionReport initializes with correct defaults."""
    report = FrictionReport(description="Command failed")
    assert report.task_id is None
    assert report.worker_run_id is None
    assert report.source == "other"
    assert report.description == "Command failed"
    assert report.impact == "unknown"
    assert report.context is None


def test_friction_report_validation():
    """Verify that FrictionReport enforces constraints."""
    with pytest.raises(ValidationError):
        # description cannot be empty
        FrictionReport(description="")

    with pytest.raises(ValidationError):
        # invalid source
        FrictionReport(description="test", source="invalid_source")  # type: ignore


def test_improvement_suggestion_defaults():
    """Verify that ImprovementSuggestion initializes with correct defaults."""
    suggestion = ImprovementSuggestion(
        title="Improve parsing",
        description="Add a fallback parser for JSON errors.",
        validation_path="Run tests and verify fallback",
    )
    assert suggestion.title == "Improve parsing"
    assert suggestion.description == "Add a fallback parser for JSON errors."
    assert suggestion.value == "medium"
    assert suggestion.effort == "medium"
    assert suggestion.risk == "medium"
    assert suggestion.layer_impact == "other"
    assert suggestion.hitl_need == "optional"
    assert suggestion.validation_path == "Run tests and verify fallback"


def test_improvement_suggestion_validation():
    """Verify that ImprovementSuggestion enforces constraints."""
    with pytest.raises(ValidationError):
        # missing required validation_path
        ImprovementSuggestion(title="Title", description="Desc")

    with pytest.raises(ValidationError):
        # empty title
        ImprovementSuggestion(title="", description="Desc", validation_path="Test")
