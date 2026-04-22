"""Unit tests for structured worker failure taxonomy classification."""

from workers import WorkerCommand
from workers.failure_taxonomy import classify_failure_kind


def test_classify_failure_kind_compile_error() -> None:
    failure_kind = classify_failure_kind(
        status="failure",
        summary="Build failed with compilation failed in src/main.ts",
        commands_run=[WorkerCommand(command="npm run build", exit_code=2)],
    )
    assert failure_kind == "compile"


def test_classify_failure_kind_test_failure() -> None:
    failure_kind = classify_failure_kind(
        status="failure",
        summary="tests failed in targeted suite",
        commands_run=[WorkerCommand(command="pytest -q", exit_code=1)],
    )
    assert failure_kind == "test"


def test_classify_failure_kind_timeout() -> None:
    failure_kind = classify_failure_kind(
        status="failure",
        stop_reason="worker_timeout",
        summary="runtime hit timeout budget",
    )
    assert failure_kind == "timeout"


def test_classify_failure_kind_auth_error() -> None:
    failure_kind = classify_failure_kind(
        status="error",
        stop_reason="adapter_error",
        summary="Provider returned unauthorized: invalid credentials",
    )
    assert failure_kind == "provider_auth"


def test_classify_failure_kind_matches_python3_unittest_command() -> None:
    failure_kind = classify_failure_kind(
        status="failure",
        commands_run=[WorkerCommand(command="python3 -m unittest", exit_code=1)],
    )
    assert failure_kind == "test"


def test_classify_failure_kind_matches_python3_compile_command() -> None:
    failure_kind = classify_failure_kind(
        status="failure",
        commands_run=[WorkerCommand(command="python3 -m py_compile app.py", exit_code=1)],
    )
    assert failure_kind == "compile"


def test_classify_failure_kind_matches_typeerror_summary() -> None:
    failure_kind = classify_failure_kind(
        status="failure",
        summary="TypeError: unsupported operand type(s)",
    )
    assert failure_kind == "compile"
