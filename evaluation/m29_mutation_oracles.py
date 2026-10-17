"""Runner-owned read-only acceptance oracles and baseline regression verifier."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import importlib.util
import io
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def _import_from_workspace(workspace_dir: Path, module_name: str) -> Any:
    ws_str = str(workspace_dir.resolve())
    if ws_str not in sys.path:
        sys.path.insert(0, ws_str)
    for mod in list(sys.modules.keys()):
        if mod == "taskboard" or mod.startswith("taskboard."):
            del sys.modules[mod]
    return importlib.import_module(module_name)


def _verify_pair_01_label_normalization(workspace_dir: Path) -> None:
    labels_mod = _import_from_workspace(workspace_dir, "taskboard.labels")
    normalize_label = getattr(labels_mod, "normalize_label", None)
    if not callable(normalize_label):
        raise AssertionError("taskboard.labels does not export callable normalize_label")

    s1 = "  Quick   Fix! "
    if normalize_label(s1) != "quick-fix":
        raise AssertionError(f"Expected 'quick-fix', got {normalize_label(s1)!r}")
    if s1 != "  Quick   Fix! ":
        raise AssertionError("Input string was mutated")

    if normalize_label("café & TEA") != "café-tea":
        raise AssertionError(f"Expected 'café-tea', got {normalize_label('café & TEA')!r}")
    if normalize_label("ＦＯＯ") != "foo":
        raise AssertionError(f"Expected 'foo', got {normalize_label('ＦＯＯ')!r}")
    if normalize_label("!@#$$%^&*()") != "":
        raise AssertionError(f"Expected empty string, got {normalize_label('!@#$$%^&*()')!r}")
    if normalize_label("") != "":
        raise AssertionError("Expected empty string for empty input")
    if normalize_label("---hello---world---") != "hello-world":
        raise AssertionError("Expected leading/trailing hyphens stripped and runs collapsed")
    if normalize_label("hello_world") != "hello-world":
        raise AssertionError("Expected non-alphanumeric '_' converted to '-'")


def _verify_pair_02_task_ordering(workspace_dir: Path) -> None:
    models_mod = _import_from_workspace(workspace_dir, "taskboard.models")
    ordering_mod = _import_from_workspace(workspace_dir, "taskboard.ordering")
    task_cls = getattr(models_mod, "Task")
    sort_tasks = getattr(ordering_mod, "sort_tasks", None)
    if not callable(sort_tasks):
        raise AssertionError("taskboard.ordering does not export callable sort_tasks")

    t0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    t_earlier = t0 - timedelta(hours=1)
    t_later = t0 + timedelta(hours=1)

    b = task_cls(id="b", title="B", priority=2, created_at=t0)
    a = task_cls(id="a", title="A", priority=2, created_at=t0)
    c = task_cls(id="c", title="C", priority=3, created_at=t_later)
    d = task_cls(id="d", title="D", priority=2, created_at=t_earlier)

    inp = [b, a, c, d]
    out = sort_tasks(inp)

    if [t.id for t in out] != ["c", "d", "a", "b"]:
        raise AssertionError(f"Expected order ['c', 'd', 'a', 'b'], got {[t.id for t in out]}")
    if [t.id for t in inp] != ["b", "a", "c", "d"]:
        raise AssertionError("Input list order was mutated")
    if out is inp:
        raise AssertionError("sort_tasks did not return a new list")
    if sort_tasks([]) != []:
        raise AssertionError("Expected empty list for empty input")


def _verify_pair_03_task_filters(workspace_dir: Path) -> None:
    models_mod = _import_from_workspace(workspace_dir, "taskboard.models")
    filters_mod = _import_from_workspace(workspace_dir, "taskboard.filters")
    task_cls = getattr(models_mod, "Task")
    filter_tasks = getattr(filters_mod, "filter_tasks", None)
    if not callable(filter_tasks):
        raise AssertionError("taskboard.filters does not export callable filter_tasks")

    a = task_cls(id="a", title="A", status="queued", owner="Sam", tags=["red"])
    b = task_cls(id="b", title="B", status="done", owner="sam", tags=["red"])
    c = task_cls(id="c", title="C", status="queued", owner="Lee", tags=["red", "blue"])
    d = task_cls(id="d", title="D", status="in_progress", owner="SAM", tags=["blue"])
    tasks = [a, b, c, d]

    if filter_tasks(tasks, status="queued") != [a, c]:
        raise AssertionError("filter_tasks status='queued' failed")
    if filter_tasks(tasks, owner="SAM") != [a, b, d]:
        raise AssertionError("filter_tasks owner='SAM' case-insensitive match failed")
    if filter_tasks(tasks, tag="red") != [a, b, c]:
        raise AssertionError("filter_tasks tag='red' failed")
    if filter_tasks(tasks, status="queued", owner="sAm", tag="red") != [a]:
        raise AssertionError("filter_tasks conjunction failed")
    if filter_tasks(tasks, status="done", owner="Lee") != []:
        raise AssertionError("filter_tasks disjoint match failed")

    all_res = filter_tasks(tasks)
    if all_res != [a, b, c, d]:
        raise AssertionError("filter_tasks with no filters failed")
    if all_res is tasks:
        raise AssertionError("filter_tasks must return a new list")
    if tasks != [a, b, c, d]:
        raise AssertionError("Input list was mutated")


def _verify_pair_04_cursor_pagination(workspace_dir: Path) -> None:
    models_mod = _import_from_workspace(workspace_dir, "taskboard.models")
    pagination_mod = _import_from_workspace(workspace_dir, "taskboard.pagination")
    task_cls = getattr(models_mod, "Task")
    paginate_tasks = getattr(pagination_mod, "paginate_tasks", None)
    if not callable(paginate_tasks):
        raise AssertionError("taskboard.pagination does not export callable paginate_tasks")

    tasks = [task_cls(id=x, title=x) for x in ["a", "b", "c", "d"]]

    p1 = paginate_tasks(tasks, limit=2)
    if [t.id for t in p1.items] != ["a", "b"] or p1.next_cursor != "b":
        raise AssertionError(f"First page failed: {p1}")

    p2 = paginate_tasks(tasks, limit=2, after_id="b")
    if [t.id for t in p2.items] != ["c", "d"] or p2.next_cursor is not None:
        raise AssertionError(f"Second page failed: {p2}")

    p3 = paginate_tasks(tasks, limit=4)
    if [t.id for t in p3.items] != ["a", "b", "c", "d"] or p3.next_cursor is not None:
        raise AssertionError(f"Single full page failed: {p3}")

    p4 = paginate_tasks(tasks, limit=2, after_id="d")
    if p4.items != [] or p4.next_cursor is not None:
        raise AssertionError(f"Past-the-end page failed: {p4}")

    try:
        paginate_tasks(tasks, limit=0)
        raise AssertionError("limit=0 did not raise ValueError")
    except ValueError:
        pass

    try:
        paginate_tasks(tasks, limit=2, after_id="unknown")
        raise AssertionError("unknown after_id did not raise ValueError")
    except ValueError:
        pass


def _verify_pair_05_csv_export(workspace_dir: Path) -> None:
    models_mod = _import_from_workspace(workspace_dir, "taskboard.models")
    export_mod = _import_from_workspace(workspace_dir, "taskboard.export")
    task_cls = getattr(models_mod, "Task")
    export_tasks_csv = getattr(export_mod, "export_tasks_csv", None)
    if not callable(export_tasks_csv):
        raise AssertionError("taskboard.export does not export callable export_tasks_csv")

    t1 = task_cls(id="1", title="Simple task", status="queued", owner="alice", priority=1)
    t2 = task_cls(id="2", title="Task, with comma", status="in_progress", owner=None, priority=2)
    t3 = task_cls(
        id="3", title='Task with "quotes"\nand newline', status="done", owner="bob", priority=3
    )

    csv_data = export_tasks_csv([t1, t2, t3])
    if not isinstance(csv_data, str):
        raise AssertionError("export_tasks_csv did not return a string")
    if "\r" in csv_data:
        raise AssertionError("export_tasks_csv output contains carriage return (CR)")
    if not csv_data.startswith("id,title,status,owner,priority\n"):
        raise AssertionError(f"Header line mismatch in csv: {csv_data[:40]!r}")

    rows = list(csv.reader(io.StringIO(csv_data)))
    if len(rows) != 4:
        raise AssertionError(f"Expected 4 rows, parsed {len(rows)}")
    if rows[1] != ["1", "Simple task", "queued", "alice", "1"]:
        raise AssertionError(f"Row 1 mismatch: {rows[1]}")
    if rows[2] != ["2", "Task, with comma", "in_progress", "", "2"]:
        raise AssertionError(f"Row 2 (comma title / missing owner) mismatch: {rows[2]}")
    if rows[3] != ["3", 'Task with "quotes"\nand newline', "done", "bob", "3"]:
        raise AssertionError(f"Row 3 (quote and newline) mismatch: {rows[3]}")

    empty_csv = export_tasks_csv([])
    if empty_csv != "id,title,status,owner,priority\n":
        raise AssertionError("export_tasks_csv([]) did not return header-only string")


def _verify_pair_06_status_transitions(workspace_dir: Path) -> None:
    status_mod = _import_from_workspace(workspace_dir, "taskboard.status")
    transition_status = getattr(status_mod, "transition_status", None)
    if not callable(transition_status):
        raise AssertionError("taskboard.status does not export callable transition_status")

    allowed_moves = [
        ("queued", "in_progress"),
        ("queued", "cancelled"),
        ("in_progress", "done"),
        ("in_progress", "cancelled"),
    ]
    for cur, req in allowed_moves:
        if transition_status(cur, req) != req:
            raise AssertionError(f"Transition {cur} -> {req} failed")

    for st in ["queued", "in_progress", "done", "cancelled"]:
        if transition_status(st, st) != st:
            raise AssertionError(f"Idempotent transition for {st} failed")

    disallowed_moves = [
        ("queued", "done"),
        ("in_progress", "queued"),
        ("done", "in_progress"),
        ("done", "queued"),
        ("done", "cancelled"),
        ("cancelled", "in_progress"),
        ("cancelled", "done"),
        ("cancelled", "queued"),
        ("unknown", "queued"),
        ("queued", "unknown"),
    ]
    for cur, req in disallowed_moves:
        try:
            transition_status(cur, req)
            raise AssertionError(f"Disallowed transition {cur} -> {req} did not raise ValueError")
        except ValueError:
            pass


def _verify_pair_07_capped_retry_delay(workspace_dir: Path) -> None:
    retry_mod = _import_from_workspace(workspace_dir, "taskboard.retry")
    retry_delay_seconds = getattr(retry_mod, "retry_delay_seconds", None)
    if not callable(retry_delay_seconds):
        raise AssertionError("taskboard.retry does not export callable retry_delay_seconds")

    if retry_delay_seconds(1) != 1:
        raise AssertionError(f"attempt 1 default failed: {retry_delay_seconds(1)}")
    if retry_delay_seconds(2) != 2:
        raise AssertionError(f"attempt 2 default failed: {retry_delay_seconds(2)}")
    if retry_delay_seconds(3) != 4:
        raise AssertionError(f"attempt 3 default failed: {retry_delay_seconds(3)}")
    if retry_delay_seconds(6) != 30:
        raise AssertionError(f"attempt 6 default failed: {retry_delay_seconds(6)}")

    if retry_delay_seconds(3, base_seconds=5, max_seconds=50) != 20:
        raise AssertionError("Custom base/cap failed")
    if retry_delay_seconds(1, base_seconds=0, max_seconds=30) != 0:
        raise AssertionError("Zero base_seconds failed")
    if retry_delay_seconds(5, base_seconds=2, max_seconds=0) != 0:
        raise AssertionError("Zero max_seconds failed")

    big_delay = retry_delay_seconds(10**9, base_seconds=1, max_seconds=30)
    if big_delay != 30:
        raise AssertionError(f"Large attempt delay cap failed: {big_delay}")

    for bad_attempt in [0, -1, -5]:
        try:
            retry_delay_seconds(bad_attempt)
            raise AssertionError(f"Attempt {bad_attempt} did not raise ValueError")
        except ValueError:
            pass

    for bad_base in [-1, -10]:
        try:
            retry_delay_seconds(1, base_seconds=bad_base)
            raise AssertionError("Negative base_seconds did not raise ValueError")
        except ValueError:
            pass


def _verify_pair_08_event_deduplication(workspace_dir: Path) -> None:
    models_mod = _import_from_workspace(workspace_dir, "taskboard.models")
    events_mod = _import_from_workspace(workspace_dir, "taskboard.events")
    event_cls = getattr(models_mod, "Event")
    deduplicate_events = getattr(events_mod, "deduplicate_events", None)
    if not callable(deduplicate_events):
        raise AssertionError("taskboard.events does not export callable deduplicate_events")

    e1 = event_cls(id="1", event_type="create", payload={"v": 1})
    e2 = event_cls(id="2", event_type="update", payload={"v": 2})
    e3 = event_cls(id="1", event_type="create_dup", payload={"v": 3})
    e4 = event_cls(id="3", event_type="delete", payload=None)
    events = [e1, e2, e3, e4]

    out = deduplicate_events(events)
    if out != [e1, e2, e4]:
        raise AssertionError(f"Deduplicated events mismatch: {out}")
    if events != [e1, e2, e3, e4]:
        raise AssertionError("Input events list was mutated")
    if out is events:
        raise AssertionError("deduplicate_events must return a new list")
    if deduplicate_events([]) != []:
        raise AssertionError("deduplicate_events([]) failed")


def _verify_pair_09_event_counts(workspace_dir: Path) -> None:
    models_mod = _import_from_workspace(workspace_dir, "taskboard.models")
    events_mod = _import_from_workspace(workspace_dir, "taskboard.events")
    event_cls = getattr(models_mod, "Event")
    count_events_by_type = getattr(events_mod, "count_events_by_type", None)
    if not callable(count_events_by_type):
        raise AssertionError("taskboard.events does not export callable count_events_by_type")

    events = [
        event_cls(id="1", event_type="create"),
        event_cls(id="2", event_type="update"),
        event_cls(id="3", event_type="create"),
        event_cls(id="4", event_type="Create"),
    ]
    counts = count_events_by_type(events)
    expected = {"Create": 1, "create": 2, "update": 1}
    if counts != expected:
        raise AssertionError(f"Expected {expected}, got {counts}")
    if list(counts.keys()) != ["Create", "create", "update"]:
        raise AssertionError(f"Keys are not in lexical order: {list(counts.keys())}")
    if count_events_by_type([]) != {}:
        raise AssertionError("count_events_by_type([]) failed")


def _verify_pair_10_task_status_summary(workspace_dir: Path) -> None:
    models_mod = _import_from_workspace(workspace_dir, "taskboard.models")
    summary_mod = _import_from_workspace(workspace_dir, "taskboard.summary")
    task_cls = getattr(models_mod, "Task")
    summarize_tasks = getattr(summary_mod, "summarize_tasks", None)
    if not callable(summarize_tasks):
        raise AssertionError("taskboard.summary does not export callable summarize_tasks")

    statuses = ["queued", "done", "done", "cancelled", "archived"]
    tasks = [task_cls(id=str(i), title=f"T{i}", status=s) for i, s in enumerate(statuses)]

    res = summarize_tasks(tasks)
    expected = {
        "total": 5,
        "queued": 1,
        "in_progress": 0,
        "done": 2,
        "cancelled": 1,
        "other": 1,
    }
    if res != expected:
        raise AssertionError(f"Expected {expected}, got {res}")
    if list(res.keys()) != ["total", "queued", "in_progress", "done", "cancelled", "other"]:
        raise AssertionError(f"Summary keys mismatch: {list(res.keys())}")

    empty_res = summarize_tasks([])
    expected_empty = {
        "total": 0,
        "queued": 0,
        "in_progress": 0,
        "done": 0,
        "cancelled": 0,
        "other": 0,
    }
    if empty_res != expected_empty:
        raise AssertionError(f"Empty tasks summary failed: {empty_res}")


TOPIC_ORACLE_MAP = {
    "label_normalization": _verify_pair_01_label_normalization,
    "task_ordering": _verify_pair_02_task_ordering,
    "task_filters": _verify_pair_03_task_filters,
    "cursor_pagination": _verify_pair_04_cursor_pagination,
    "csv_export": _verify_pair_05_csv_export,
    "status_transitions": _verify_pair_06_status_transitions,
    "capped_retry_delay": _verify_pair_07_capped_retry_delay,
    "event_deduplication": _verify_pair_08_event_deduplication,
    "event_counts": _verify_pair_09_event_counts,
    "task_status_summary": _verify_pair_10_task_status_summary,
}


def run_topic_acceptance_oracle_in_process(topic: str, workspace_snapshot_dir: Path) -> None:
    """Run topic acceptance oracle in process against workspace_snapshot_dir."""
    oracle_fn = TOPIC_ORACLE_MAP.get(topic)
    if oracle_fn is None:
        raise ValueError(f"Unknown topic for acceptance oracle: {topic}")
    oracle_fn(workspace_snapshot_dir)


def run_baseline_regression_in_process(workspace_snapshot_dir: Path) -> None:
    """Run baseline regression suite against tests/ in workspace_snapshot_dir."""
    ws_str = str(workspace_snapshot_dir.resolve())
    if ws_str not in sys.path:
        sys.path.insert(0, ws_str)

    test_file = workspace_snapshot_dir / "tests" / "test_baseline.py"
    if not test_file.is_file():
        raise AssertionError(f"Baseline test file not found in workspace: {test_file}")

    for mod in list(sys.modules.keys()):
        if mod == "taskboard" or mod.startswith("taskboard."):
            del sys.modules[mod]

    spec = importlib.util.spec_from_file_location("taskboard_test_baseline", str(test_file))
    if spec is None or spec.loader is None:
        raise AssertionError(f"Failed to create module spec for {test_file}")

    mod = importlib.util.module_from_spec(spec)
    sys.modules["taskboard_test_baseline"] = mod
    spec.loader.exec_module(mod)

    loader = unittest.defaultTestLoader
    suite = loader.loadTestsFromModule(mod)
    buffer = io.StringIO()
    runner = unittest.TextTestRunner(stream=buffer, verbosity=1)
    result = runner.run(suite)

    if not result.wasSuccessful():
        err_msg = buffer.getvalue()
        raise AssertionError(
            f"Baseline regression tests failed ({len(result.failures)} failures, "
            f"{len(result.errors)} errors):\n{err_msg}"
        )


def compute_verifier_source_sha256() -> str:
    """Compute deterministic SHA-256 of runner-owned oracle and verifier modules."""
    files = [
        Path(__file__).resolve(),
        Path(__file__).resolve().parent / "m29_mutation_verifier.py",
    ]
    hasher = hashlib.sha256()
    for f in files:
        if f.is_file():
            content = f.read_text(encoding="utf-8").replace("\r\n", "\n")
            hasher.update(f.name.encode("utf-8"))
            hasher.update(b":")
            hasher.update(content.encode("utf-8"))
            hasher.update(b"\n---\n")
    return hasher.hexdigest()


def main() -> None:
    """Subprocess CLI entrypoint for runner-owned verifier checks."""
    parser = argparse.ArgumentParser(description="M29 Runner-Owned Verifier")
    parser.add_argument("--workspace", required=True, type=Path, help="Workspace directory")
    parser.add_argument("--topic", type=str, help="Topic acceptance oracle to run")
    parser.add_argument("--check-baseline", action="store_true", help="Run baseline regression")

    args = parser.parse_args()
    ws_dir = args.workspace.resolve()

    if args.check_baseline:
        run_baseline_regression_in_process(ws_dir)
    if args.topic:
        run_topic_acceptance_oracle_in_process(args.topic, ws_dir)


if __name__ == "__main__":
    main()
