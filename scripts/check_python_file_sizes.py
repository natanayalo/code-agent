#!/usr/bin/env python3
"""Report oversized Python modules and functions as non-blocking review prompts.

Waivers (in .sizecheck-exceptions.yaml) are strongly discouraged and should only be
used for exceptional cases such as generated code, unavoidable third-party
compatibility shims, or large preformatted data/constants that cannot reasonably
be refactored. Splitting files or functions is the preferred remediation.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

DEFAULT_PRODUCTION_LIMIT = 600
DEFAULT_TEST_LIMIT = 800
DEFAULT_FUNCTION_LIMIT = 80
EXCLUDED_DIR_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "node_modules",
}


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    message: str
    is_waived: bool = False
    waiver_reason: str = ""


def _python_files(paths: list[Path]) -> list[Path]:
    files: set[Path] = set()
    for path in paths:
        if path.is_file():
            if path.suffix == ".py":
                files.add(path.resolve())
            continue

        for child in path.rglob("*.py"):
            if any(part in EXCLUDED_DIR_NAMES for part in child.parts):
                continue
            files.add(child.resolve())
    return sorted(files)


def _file_limit(path: Path, production_limit: int, test_limit: int) -> int:
    return test_limit if "tests" in path.parts else production_limit


def _iter_function_findings(
    path: Path, *, function_limit: int, repo_root: Path, exceptions: dict
) -> list[Finding]:
    source = path.read_text(encoding="utf-8")
    try:
        module = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [
            Finding(
                path=path,
                line=exc.lineno or 1,
                message=f"SyntaxError: {exc.msg}",
            )
        ]
    findings: list[Finding] = []

    try:
        rel_path = str(path.relative_to(repo_root))
    except ValueError:
        rel_path = str(path)

    for node in ast.walk(module):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.end_lineno is None:
            continue
        line_count = node.end_lineno - node.lineno + 1
        if line_count <= function_limit:
            continue

        func_exceptions = exceptions.get("function_exceptions", {}).get(rel_path, {})
        is_waived = node.name in func_exceptions
        waiver_reason = func_exceptions.get(node.name, "")

        findings.append(
            Finding(
                path=path,
                line=node.lineno,
                message=(
                    f"function `{node.name}` is {line_count} lines "
                    f"(review threshold: {function_limit})"
                ),
                is_waived=is_waived,
                waiver_reason=waiver_reason,
            )
        )
    return findings


def collect_findings(
    paths: list[Path],
    *,
    repo_root: Path,
    production_limit: int,
    test_limit: int,
    function_limit: int,
    exceptions: dict,
) -> list[Finding]:
    findings: list[Finding] = []
    for path in _python_files(paths):
        try:
            rel_path = str(path.relative_to(repo_root))
        except ValueError:
            rel_path = str(path)

        with path.open("r", encoding="utf-8") as f:
            line_count = sum(1 for _ in f)
        limit = _file_limit(path, production_limit, test_limit)

        if line_count > limit:
            file_exceptions = exceptions.get("file_exceptions", {})
            is_waived = rel_path in file_exceptions
            waiver_reason = file_exceptions.get(rel_path, "")

            findings.append(
                Finding(
                    path=path,
                    line=1,
                    message=f"file is {line_count} lines (review threshold: {limit})",
                    is_waived=is_waived,
                    waiver_reason=waiver_reason,
                )
            )
        findings.extend(
            _iter_function_findings(
                path, function_limit=function_limit, repo_root=repo_root, exceptions=exceptions
            )
        )
    return findings


def _load_exceptions(repo_root: Path) -> dict:
    exc_file = repo_root / ".sizecheck-exceptions.yaml"
    if not exc_file.exists():
        return {}
    if yaml is None:
        print(
            "Warning: PyYAML not found, but .sizecheck-exceptions.yaml exists. "
            "Note: adding waivers is discouraged; prefer refactoring over exceptions.",
            file=sys.stderr,
        )
        return {}
    try:
        with exc_file.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        print(f"Warning: Failed to parse {exc_file}: {exc}", file=sys.stderr)
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", default=["."], help="Files or directories to inspect.")
    parser.add_argument(
        "--repo-root", default=".", help="Repository root for relative path resolution."
    )
    parser.add_argument("--production-limit", type=int, default=DEFAULT_PRODUCTION_LIMIT)
    parser.add_argument("--test-limit", type=int, default=DEFAULT_TEST_LIMIT)
    parser.add_argument("--function-limit", type=int, default=DEFAULT_FUNCTION_LIMIT)
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    paths = [Path(p).resolve() for p in args.paths]

    exceptions = _load_exceptions(repo_root)

    findings = collect_findings(
        paths,
        repo_root=repo_root,
        production_limit=args.production_limit,
        test_limit=args.test_limit,
        function_limit=args.function_limit,
        exceptions=exceptions,
    )

    if not findings:
        print("python-size-check: no review prompts")
        return 0

    has_errors = False
    print("python-size-check: findings")
    for finding in findings:
        relative = finding.path.relative_to(repo_root)
        if finding.is_waived:
            print(
                f"[WAIVED] {relative}:{finding.line}: {finding.message} (Reason: {finding.waiver_reason})"  # noqa: E501
            )
        else:
            has_errors = True
            print(f"[ERROR] {relative}:{finding.line}: {finding.message}")

    if has_errors:
        print(
            "\npython-size-check:"
            "Thresholds are blocking. Splitting the file/function is the preferred fix. "
            "Adding a waiver is strongly discouraged and should be reserved for exceptional cases "
            "(generated code, unavoidable compatibility shims, or large static data). "
            "If necessary, add a waiver in .sizecheck-exceptions.yaml."
        )
        return 1

    print(
        "\npython-size-check: All findings are covered by waivers. "
        "Note: waivers are discouraged — review exceptions and prefer refactoring."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
