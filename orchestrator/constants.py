"""Shared constants for task classification and routing."""

# Task Type Markers
DOCS_MARKERS = ("docs", "documentation", "readme", "runbook")
BUGFIX_MARKERS = ("fix", "bug", "failing", "failure", "regression", "error")
REFACTOR_MARKERS = ("refactor", "architecture", "redesign", "restructure")
INVESTIGATION_MARKERS = ("investigate", "analyze", "debug", "inspect", "diagnose")
REVIEW_FIX_MARKERS = ("review comment", "requested changes", "review feedback")
MAINTENANCE_MARKERS = ("ci", "lint", "pre-commit", "dependency", "maintenance")

# Risk and Safety Markers
DESTRUCTIVE_TASK_MARKERS = (
    "delete file",
    "delete files",
    "delete all",
    "destroy workspace",
    "drop database",
    "drop table",
    "git clean",
    "git reset",
    "purge data",
    "rm -rf",
    "wipe data",
)

CRITICAL_MARKERS = (
    "auth",
    "authentication",
    "authorization",
    "billing",
    "secret",
    "secrets",
    "production deploy",
    "deploy to production",
    "sandbox policy",
    "deployment permissions",
)

HIGH_RISK_MARKERS = ("schema", "migration", "security", "permission", "destructive")

# Complexity and Quality Markers
AMBIGUOUS_ASKS = (
    "fix it",
    "make it better",
    "improve this",
    "do the thing",
    "analyze",
    "debug this",
)

COMPLEX_TASK_MARKERS = (
    "across files",
    "multiple files",
    "multi-file",
    "multi file",
    "multifile",
    "multi-module",
    "multi module",
    "several modules",
)

HIGH_QUALITY_REQUEST_MARKERS = (
    "highest quality",
    "high quality",
    "best quality",
)

LOW_COST_REQUEST_MARKERS = (
    "lower cost",
    "low cost",
    "cheapest",
    "cheaper",
)

# Validation and Ordering
RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
VALID_DELIVERY_MODES = {"summary", "workspace", "branch", "draft_pr"}
