"""Deterministic disposable taskboard fixture repository builder and verifier."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PINNED_TASKBOARD_FIXTURE_REVISION = "228071722c645e813af16053e56d007c25e9f127"

FIXED_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Code Agent Evaluator",
    "GIT_AUTHOR_EMAIL": "evaluator@code-agent.local",
    "GIT_AUTHOR_DATE": "2026-09-01T00:00:00+00:00",
    "GIT_COMMITTER_NAME": "Code Agent Evaluator",
    "GIT_COMMITTER_EMAIL": "evaluator@code-agent.local",
    "GIT_COMMITTER_DATE": "2026-09-01T00:00:00+00:00",
}

TASKBOARD_BASE_FILES: dict[str, str] = {
    "README.md": (
        "# taskboard\n\n"
        "Deterministic evaluation fixture repository for M29 feature/mutation benchmarks.\n"
    ),
    ".gitignore": "__pycache__/\n*.py[cod]\n.pytest_cache/\n",
    "taskboard/__init__.py": (
        '"""Taskboard disposable evaluation fixture package."""\n\n'
        "from .models import Event, Page, Task\n\n"
        '__all__ = ["Event", "Page", "Task"]\n'
    ),
    "taskboard/models.py": '''"""Standard library domain models for the taskboard fixture."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class Task:
    """Domain task model with timezone-aware creation timestamp."""

    id: str
    title: str
    status: str = "queued"
    owner: str | None = None
    priority: int = 0
    tags: list[str] = field(default_factory=list)
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def __post_init__(self) -> None:
        if not isinstance(self.created_at, datetime):
            raise TypeError("created_at must be a datetime instance")
        if (
            self.created_at.tzinfo is None
            or self.created_at.tzinfo.utcoffset(self.created_at) is None
        ):
            raise ValueError("created_at must be timezone-aware")
        if not isinstance(self.tags, list):
            object.__setattr__(self, "tags", list(self.tags))


@dataclass(frozen=True)
class Event:
    """Domain event model."""

    id: str
    event_type: str
    payload: Any = None


@dataclass(frozen=True)
class Page:
    """Paginated task collection."""

    items: list[Task]
    next_cursor: str | None = None
''',
    "taskboard/labels.py": '''"""Label normalization module stub."""


def normalize_label(value: str) -> str:
    """Normalize label using Unicode NFKC and case folding."""
    raise NotImplementedError("normalize_label is not implemented at baseline")
''',
    "taskboard/ordering.py": '''"""Task ordering module stub."""

from .models import Task


def sort_tasks(tasks: list[Task]) -> list[Task]:
    """Sort tasks by priority descending, created_at ascending, and id ascending."""
    raise NotImplementedError("sort_tasks is not implemented at baseline")
''',
    "taskboard/filters.py": '''"""Task filtering module stub."""

from .models import Task


def filter_tasks(
    tasks: list[Task],
    *,
    status: str | None = None,
    owner: str | None = None,
    tag: str | None = None,
) -> list[Task]:
    """Filter tasks by exact status, case-insensitive owner, and exact tag."""
    raise NotImplementedError("filter_tasks is not implemented at baseline")
''',
    "taskboard/pagination.py": '''"""Cursor pagination module stub."""

from .models import Page, Task


def paginate_tasks(
    tasks: list[Task],
    *,
    limit: int,
    after_id: str | None = None,
) -> Page:
    """Paginate tasks using exclusive cursor pagination."""
    raise NotImplementedError("paginate_tasks is not implemented at baseline")
''',
    "taskboard/export.py": '''"""CSV export module stub."""

from .models import Task


def export_tasks_csv(tasks: list[Task]) -> str:
    """Export tasks to an in-memory CSV string with LF record separators."""
    raise NotImplementedError("export_tasks_csv is not implemented at baseline")
''',
    "taskboard/status.py": '''"""Status transition module stub."""


def transition_status(current: str, requested: str) -> str:
    """Validate and execute a task status transition."""
    raise NotImplementedError("transition_status is not implemented at baseline")
''',
    "taskboard/retry.py": '''"""Retry delay calculation module stub."""


def retry_delay_seconds(
    attempt: int,
    *,
    base_seconds: int = 1,
    max_seconds: int = 30,
) -> int:
    """Calculate capped exponential retry delay."""
    raise NotImplementedError("retry_delay_seconds is not implemented at baseline")
''',
    "taskboard/events.py": '''"""Event processing module stub."""

from .models import Event


def deduplicate_events(events: list[Event]) -> list[Event]:
    """Deduplicate events preserving first occurrence order."""
    raise NotImplementedError("deduplicate_events is not implemented at baseline")


def count_events_by_type(events: list[Event]) -> dict[str, int]:
    """Count events grouped by exact event_type with keys in lexical order."""
    raise NotImplementedError("count_events_by_type is not implemented at baseline")
''',
    "taskboard/summary.py": '''"""Task summary module stub."""

from .models import Task


def summarize_tasks(tasks: list[Task]) -> dict[str, int]:
    """Summarize task counts by status category."""
    raise NotImplementedError("summarize_tasks is not implemented at baseline")
''',
    "tests/__init__.py": "",
    "tests/test_baseline.py": '''"""Baseline regression suite for the taskboard fixture."""

import unittest
from datetime import datetime, timezone

from taskboard.events import count_events_by_type, deduplicate_events
from taskboard.export import export_tasks_csv
from taskboard.filters import filter_tasks
from taskboard.labels import normalize_label
from taskboard.models import Event, Page, Task
from taskboard.ordering import sort_tasks
from taskboard.pagination import paginate_tasks
from taskboard.retry import retry_delay_seconds
from taskboard.status import transition_status
from taskboard.summary import summarize_tasks


class TestTaskboardBaseline(unittest.TestCase):
    """Baseline test cases verifying models and initial module stubs."""

    def test_task_model_defaults_and_construction(self) -> None:
        t = Task(id="t1", title="Sample Task")
        self.assertEqual(t.id, "t1")
        self.assertEqual(t.title, "Sample Task")
        self.assertEqual(t.status, "queued")
        self.assertIsNone(t.owner)
        self.assertEqual(t.priority, 0)
        self.assertEqual(t.tags, [])
        self.assertIsNotNone(t.created_at.tzinfo)

    def test_task_model_rejects_naive_datetime(self) -> None:
        naive = datetime(2026, 9, 1, 12, 0, 0)
        with self.assertRaises(ValueError):
            Task(id="t2", title="Bad Date", created_at=naive)

    def test_task_model_rejects_non_datetime(self) -> None:
        with self.assertRaises(TypeError):
            Task(id="t3", title="Bad Date", created_at="2026-09-01")  # type: ignore[arg-type]

    def test_event_model(self) -> None:
        ev = Event(id="e1", event_type="created", payload={"k": "v"})
        self.assertEqual(ev.id, "e1")
        self.assertEqual(ev.event_type, "created")
        self.assertEqual(ev.payload, {"k": "v"})

    def test_page_model(self) -> None:
        t = Task(id="t1", title="T1")
        page = Page(items=[t], next_cursor="t1")
        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.next_cursor, "t1")

    def test_module_entrypoints_are_callable(self) -> None:
        self.assertTrue(callable(normalize_label))
        self.assertTrue(callable(sort_tasks))
        self.assertTrue(callable(filter_tasks))
        self.assertTrue(callable(paginate_tasks))
        self.assertTrue(callable(export_tasks_csv))
        self.assertTrue(callable(transition_status))
        self.assertTrue(callable(retry_delay_seconds))
        self.assertTrue(callable(deduplicate_events))
        self.assertTrue(callable(count_events_by_type))
        self.assertTrue(callable(summarize_tasks))


if __name__ == "__main__":
    unittest.main()
''',
}


@dataclass(frozen=True)
class TaskboardFixtureInfo:
    """Metadata describing a created or verified taskboard fixture repository."""

    repo_path: Path
    commit_sha: str
    file_hashes: dict[str, str]
    tree_sha256: str


def _init_git_repo(repo_dir: Path) -> None:
    subprocess.run(["git", "init", "-b", "master"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", FIXED_GIT_ENV["GIT_AUTHOR_NAME"]],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", FIXED_GIT_ENV["GIT_AUTHOR_EMAIL"]],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "core.autocrlf", "false"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "core.eol", "lf"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )


def _write_fixture_files(repo_dir: Path) -> dict[str, str]:
    file_hashes: dict[str, str] = {}
    for rel_path, content in sorted(TASKBOARD_BASE_FILES.items()):
        dest = repo_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        file_hashes[rel_path] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return file_hashes


def _commit_fixture(repo_dir: Path) -> str:
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    env = os.environ.copy()
    env.update(FIXED_GIT_ENV)
    subprocess.run(
        ["git", "commit", "-m", "feat: initial taskboard baseline fixture"],
        cwd=repo_dir,
        env=env,
        check=True,
        capture_output=True,
    )
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def create_taskboard_fixture_repo(repo_dir: Path) -> TaskboardFixtureInfo:
    """Create deterministic disposable taskboard fixture repository at repo_dir."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    _init_git_repo(repo_dir)
    file_hashes = _write_fixture_files(repo_dir)
    commit_sha = _commit_fixture(repo_dir)

    tree_concat = "".join(f"{k}:{file_hashes[k]}" for k in sorted(file_hashes.keys()))
    tree_sha256 = hashlib.sha256(tree_concat.encode("utf-8")).hexdigest()

    return TaskboardFixtureInfo(
        repo_path=repo_dir.resolve(),
        commit_sha=commit_sha,
        file_hashes=file_hashes,
        tree_sha256=tree_sha256,
    )


def verify_taskboard_fixture_repo(
    repo_dir: Path, *, expected_revision: str | None = None
) -> list[str]:
    """Verify clean working tree, expected revision, and passing baseline suite."""
    violations: list[str] = []
    if not repo_dir.is_dir():
        return [f"fixture repository directory does not exist: {repo_dir}"]
    if not (repo_dir / ".git").exists():
        return [f"fixture directory is not a git repository: {repo_dir}"]

    status_out = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if status_out.returncode != 0:
        violations.append(f"git status failed with exit code {status_out.returncode}")
    elif status_out.stdout.strip():
        violations.append(f"fixture working tree is not clean:\n{status_out.stdout}")

    rev_out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if rev_out.returncode != 0:
        violations.append("failed to resolve HEAD commit")
    else:
        head_sha = rev_out.stdout.strip()
        if expected_revision is not None and head_sha != expected_revision:
            violations.append(
                f"fixture HEAD revision mismatch: expected {expected_revision}, got {head_sha}"
            )

    for rel_path in TASKBOARD_BASE_FILES:
        if not (repo_dir / rel_path).is_file():
            violations.append(f"missing baseline fixture file: {rel_path}")

    test_run = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-m",
            "unittest",
            "discover",
            "-s",
            str((repo_dir / "tests").resolve()),
            "-t",
            str(repo_dir.resolve()),
        ],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if test_run.returncode != 0:
        violations.append(f"baseline regression suite failed:\n{test_run.stderr}")

    return violations


def clone_case_workspace_from_fixture(
    fixture_repo_dir: Path,
    workspace_dir: Path,
    *,
    expected_revision: str,
) -> str:
    """Clone an isolated case workspace from fixture at expected_revision."""
    workspace_dir.parent.mkdir(parents=True, exist_ok=True)
    clone_proc = subprocess.run(
        ["git", "clone", str(fixture_repo_dir.resolve()), str(workspace_dir.resolve())],
        capture_output=True,
        text=True,
        check=False,
    )
    if clone_proc.returncode != 0:
        raise ValueError(f"git clone failed: {clone_proc.stderr.strip()}")
    chk = subprocess.run(
        ["git", "checkout", expected_revision],
        cwd=workspace_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if chk.returncode != 0:
        raise ValueError(
            f"failed to checkout expected revision {expected_revision}: {chk.stderr.strip()}"
        )
    rev_out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    head_sha = rev_out.stdout.strip()
    if head_sha != expected_revision:
        raise ValueError(
            f"cloned workspace revision mismatch: expected {expected_revision}, got {head_sha}"
        )
    return head_sha
