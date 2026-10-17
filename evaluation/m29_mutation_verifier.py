"""Runner-owned read-only external quality evaluator and blinded neutral reviewer boundary."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from evaluation.m29_mutation_isolation import TerminalSnapshot
from evaluation.m29_mutation_runner_models import ExternalEvaluationResult
from evaluation.m29_mutation_suite import (
    DEFAULT_M29_NEUTRAL_EVALUATOR,
    M29MutationCase,
    NeutralEvaluatorManifest,
)
from evaluation.provider_reliability_models import ExecutionIdentity


@dataclass(frozen=True)
class BlindedNeutralReviewRequest:
    """Blinded review request containing no candidate provider identity or case order."""

    case_topic: str
    prompt: str
    acceptance_oracle: str
    allowed_source_path: str
    diff_text: str
    changed_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class NeutralReviewOutcome:
    """Outcome of neutral third-party review."""

    passed: bool
    blocking_findings: tuple[str, ...] = ()
    evaluator_identity: ExecutionIdentity | None = None
    evaluator_identity_status: Literal["verified", "mismatched", "unavailable"] = "verified"
    fallback_attempted: bool = False
    reason: str | None = None


class NeutralReviewerClient(Protocol):
    """Protocol for neutral third-party reviewer implementations."""

    def review_blinded_diff(
        self, request: BlindedNeutralReviewRequest, manifest: NeutralEvaluatorManifest
    ) -> NeutralReviewOutcome: ...


def _stage_read_only_tree(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, symlinks=False)
    for root, dirs, files in os.walk(destination):
        for f in files:
            os.chmod(os.path.join(root, f), 0o444)
        for d in dirs:
            os.chmod(os.path.join(root, d), 0o555)
    os.chmod(destination, 0o555)


def _restore_permissions_for_cleanup(path: Path) -> None:
    if not path.exists():
        return
    for root, dirs, files in os.walk(path):
        for d in dirs:
            try:
                os.chmod(os.path.join(root, d), 0o755)
            except OSError:
                pass
        for f in files:
            try:
                os.chmod(os.path.join(root, f), 0o644)
            except OSError:
                pass
    try:
        os.chmod(path, 0o755)
    except OSError:
        pass


def _run_subprocess_check(cmd: list[str], timeout: int) -> tuple[str, int, str]:
    cmd_str = " ".join(cmd)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = proc.stdout + proc.stderr
        return cmd_str, proc.returncode, output
    except subprocess.TimeoutExpired:
        return cmd_str, 124, "subprocess timed out"
    except Exception as exc:
        return cmd_str, 1, f"subprocess execution failed: {exc}"


def _compute_tree_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    for root, dirs, files in os.walk(path):
        dirs.sort()
        for f in sorted(files):
            fp = Path(root) / f
            hasher.update(str(fp.relative_to(path)).encode("utf-8"))
            try:
                hasher.update(fp.read_bytes())
            except OSError:
                pass
    return hasher.hexdigest()


def _extract_workspace_diff(workspace_path: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return proc.stdout
    except Exception:
        pass
    return ""


class ReadOnlyExternalEvaluator:
    """Runner-owned read-only external evaluator with isolated staging and blinded review."""

    def __init__(
        self,
        reviewer_client: NeutralReviewerClient | None = None,
        neutral_manifest: NeutralEvaluatorManifest | None = None,
        python_executable: str = sys.executable,
        timeout_seconds: int = 60,
    ) -> None:
        self.reviewer_client = reviewer_client
        self.neutral_manifest = neutral_manifest or DEFAULT_M29_NEUTRAL_EVALUATOR
        self.python_executable = python_executable
        self.timeout_seconds = timeout_seconds
        self.oracles_script = Path(__file__).resolve().parent / "m29_mutation_oracles.py"

    def _perform_review(
        self,
        case: M29MutationCase,
        manifest: NeutralEvaluatorManifest,
        workspace_path: Path,
        terminal_snapshot: TerminalSnapshot,
        oracles_passed: bool,
    ) -> NeutralReviewOutcome:
        if self.reviewer_client is not None:
            diff_text = _extract_workspace_diff(workspace_path)
            blinded_req = BlindedNeutralReviewRequest(
                case_topic=case.topic,
                prompt=case.prompt,
                acceptance_oracle=case.acceptance_oracle,
                allowed_source_path=case.allowed_source_path,
                diff_text=diff_text,
                changed_files=terminal_snapshot.changed_files,
            )
            return self.reviewer_client.review_blinded_diff(blinded_req, manifest)

        return NeutralReviewOutcome(
            passed=False,
            evaluator_identity=None,
            evaluator_identity_status="unavailable",
            fallback_attempted=False,
            reason="neutral_reviewer_client_unavailable",
        )

    def _run_staged_checks(
        self, staging_dir: Path, case: M29MutationCase
    ) -> tuple[bool, bool, list[str], list[int]]:
        cmds_run: list[str] = []
        exit_codes: list[int] = []

        base_cmd = [
            self.python_executable,
            "-B",
            str(self.oracles_script),
            "--workspace",
            str(staging_dir),
            "--check-baseline",
        ]
        cmd_str, rc, _ = _run_subprocess_check(base_cmd, self.timeout_seconds)
        cmds_run.append(cmd_str)
        exit_codes.append(rc)
        regression_passed = rc == 0

        oracle_cmd = [
            self.python_executable,
            "-B",
            str(self.oracles_script),
            "--workspace",
            str(staging_dir),
            "--topic",
            case.topic,
        ]
        cmd_str, rc, _ = _run_subprocess_check(oracle_cmd, self.timeout_seconds)
        cmds_run.append(cmd_str)
        exit_codes.append(rc)
        oracle_passed = rc == 0

        return regression_passed, oracle_passed, cmds_run, exit_codes

    def evaluate_case(
        self,
        case: M29MutationCase,
        workspace_path: Path,
        terminal_snapshot: TerminalSnapshot,
    ) -> ExternalEvaluationResult:
        """Run acceptance oracles, regression tests, and blinded review without mutation."""
        manifest = case.neutral_evaluator or self.neutral_manifest
        tree_before = _compute_tree_sha256(workspace_path)

        with tempfile.TemporaryDirectory() as td:
            staging_dir = Path(td) / "staged_eval"
            _stage_read_only_tree(workspace_path, staging_dir)
            try:
                reg_passed, or_passed, cmds, codes = self._run_staged_checks(staging_dir, case)
            finally:
                _restore_permissions_for_cleanup(staging_dir)

        review_outcome = self._perform_review(
            case, manifest, workspace_path, terminal_snapshot, reg_passed and or_passed
        )

        tree_after = _compute_tree_sha256(workspace_path)
        if tree_before != tree_after:
            return ExternalEvaluationResult(
                acceptance_oracle_passed=False,
                regression_suite_passed=False,
                neutral_review_passed=False,
                evaluator_identity=manifest.identity,
                quality_status="not_assessable",
                commands_run=tuple(cmds),
                exit_codes=tuple(codes),
                reason="candidate_workspace_mutated_during_external_evaluation",
            )

        identity_matches = review_outcome.evaluator_identity == manifest.identity
        if (
            review_outcome.evaluator_identity_status in ("unavailable", "mismatched")
            or review_outcome.fallback_attempted
            or not identity_matches
        ):
            quality_status = "not_assessable"
            reason = (
                review_outcome.reason or f"evaluator_{review_outcome.evaluator_identity_status}"
            )
        elif reg_passed and or_passed and review_outcome.passed:
            quality_status = "pass"
            reason = None
        else:
            quality_status = "fail"
            if not or_passed:
                reason = "acceptance_oracle_failed"
            elif not reg_passed:
                reason = "regression_suite_failed"
            else:
                reason = review_outcome.reason or "neutral_review_failed"

        return ExternalEvaluationResult(
            acceptance_oracle_passed=or_passed,
            regression_suite_passed=reg_passed,
            neutral_review_passed=review_outcome.passed,
            evaluator_identity=review_outcome.evaluator_identity,
            evaluator_identity_status=review_outcome.evaluator_identity_status,
            evaluator_identity_matches=identity_matches,
            fallback_attempted=review_outcome.fallback_attempted,
            quality_status=quality_status,
            commands_run=tuple(cmds),
            exit_codes=tuple(codes),
            reason=reason,
        )
