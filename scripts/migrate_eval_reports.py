#!/usr/bin/env python3
"""Migration script for M20.0 to M20.7 evaluation reports."""

import argparse
import json
import logging
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def _migrate_metrics(metrics: dict[str, Any]) -> None:
    if "repair_loops_count" not in metrics:
        metrics["repair_loops_count"] = 0
    if "time_to_pr_seconds" not in metrics:
        metrics["time_to_pr_seconds"] = None
    if "ci_rejection_count" not in metrics:
        metrics["ci_rejection_count"] = 0
    if "review_rejection_count" not in metrics:
        metrics["review_rejection_count"] = 0
    if "validation_failure_category" not in metrics:
        metrics["validation_failure_category"] = None
    if "worker_profile" not in metrics:
        metrics["worker_profile"] = None
    if "provider_failure_cause" not in metrics:
        metrics["provider_failure_cause"] = None


def _migrate_report(report: dict[str, Any]) -> None:
    if "repair_loops_total" not in report:
        report["repair_loops_total"] = 0
    if "mean_time_to_pr_seconds" not in report:
        report["mean_time_to_pr_seconds"] = None
    if "ci_rejection_total" not in report:
        report["ci_rejection_total"] = 0
    if "review_rejection_total" not in report:
        report["review_rejection_total"] = 0
    if "validation_failure_category_counts" not in report:
        report["validation_failure_category_counts"] = {}
    if "worker_profile_success_rates" not in report:
        report["worker_profile_success_rates"] = {}
    if "provider_failure_cause_counts" not in report:
        report["provider_failure_cause_counts"] = {}


def _migrate_comparison(comp: dict[str, Any]) -> None:
    if "delta_cases_with_validation_evidence" not in comp:
        comp["delta_cases_with_validation_evidence"] = 0
    if "delta_cases_needing_approval" not in comp:
        comp["delta_cases_needing_approval"] = 0
    if "delta_cases_needing_manual_log_inspection" not in comp:
        comp["delta_cases_needing_manual_log_inspection"] = 0
    if "delta_cases_with_worker_failure" not in comp:
        comp["delta_cases_with_worker_failure"] = 0
    if "delta_mean_commands_run" not in comp:
        comp["delta_mean_commands_run"] = None
    if "delta_mean_files_changed" not in comp:
        comp["delta_mean_files_changed"] = None
    if "delta_mean_friction_reports" not in comp:
        comp["delta_mean_friction_reports"] = None
    if "delta_repair_loops_total" not in comp:
        comp["delta_repair_loops_total"] = 0
    if "delta_mean_time_to_pr_seconds" not in comp:
        comp["delta_mean_time_to_pr_seconds"] = None
    if "delta_ci_rejection_total" not in comp:
        comp["delta_ci_rejection_total"] = 0
    if "delta_review_rejection_total" not in comp:
        comp["delta_review_rejection_total"] = 0


def migrate_file(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logging.error(f"Failed to read {path}: {e}")
        return False

    migrated = False

    results = data.get("results", [])
    for result in results:
        rel = result.get("reliability")
        if rel:
            _migrate_metrics(rel)
            migrated = True

    rel_report = data.get("reliability_report")
    if rel_report:
        _migrate_report(rel_report)
        migrated = True

    comp = data.get("comparison")
    if comp:
        _migrate_comparison(comp)
        migrated = True

    if migrated:
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        logging.info(f"Migrated {path}")

    return migrated


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate old evaluation reports to M20.7 schema.")
    parser.add_argument(
        "directory",
        nargs="?",
        default="artifacts/evaluations",
        help="Directory containing JSON reports",
    )
    args = parser.parse_args()

    target_dir = Path(args.directory)
    if not target_dir.exists() or not target_dir.is_dir():
        logging.warning(f"Target directory {target_dir} does not exist or is not a directory.")
        return

    migrated_count = 0
    for file_path in target_dir.glob("**/*.json"):
        if migrate_file(file_path):
            migrated_count += 1

    logging.info(f"Migration complete. {migrated_count} files migrated.")


if __name__ == "__main__":
    main()
