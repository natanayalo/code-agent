import json
import tempfile
from pathlib import Path

from scripts.migrate_eval_reports import migrate_file


def test_migrate_file_adds_new_metrics():
    # Legacy M20.0 mock report
    legacy_report = {
        "suite_name": "frozen-v1",
        "results": [
            {
                "case_id": "frozen-001",
                "reliability": {
                    "human_interaction_count": 0,
                    "repeated_question_count": 0,
                    "validation_evidence_present": True,
                    "manual_log_inspection_needed": False,
                    "worker_status": "success",
                    "worker_failure_kind": None,
                    "next_action_hint": "summarize_result",
                    "friction_report_count": 0,
                    "files_changed_count": 2,
                    "commands_run_count": 0,
                    "test_results_count": 1,
                    "approval_required": False,
                    "approval_status": None,
                    "stage_latency_seconds": {},
                    "stage_latency_available": False,
                    "attempt_count": 1,
                },
            }
        ],
        "reliability_report": {
            "total_cases": 1,
            "cases_needing_approval": 0,
            "cases_with_validation_evidence": 1,
            "cases_needing_manual_log_inspection": 0,
            "cases_with_worker_failure": 0,
            "worker_failure_kind_counts": {},
            "mean_commands_run": 0.0,
            "mean_files_changed": 2.0,
            "mean_friction_reports": 0.0,
            "stage_latency_available": False,
            "mean_stage_latency_seconds": {},
        },
        "comparison": {
            "baseline_variant_label": None,
            "candidate_variant_label": None,
            "delta_passed_cases": 0,
            "delta_total_score": 0,
            "delta_reviewed_cases": 0,
            "delta_precision": None,
            "delta_actionable_rate": None,
            "delta_false_discovery_rate": None,
            "delta_false_positive_rate": None,
            "delta_fix_after_review_success": None,
            "delta_empty_review_correctness": None,
        },
    }

    with tempfile.TemporaryDirectory() as tmp_dir:
        report_path = Path(tmp_dir) / "report.json"
        with report_path.open("w", encoding="utf-8") as f:
            json.dump(legacy_report, f)

        # Run migration
        assert migrate_file(report_path) is True

        # Verify output
        with report_path.open("r", encoding="utf-8") as f:
            migrated_report = json.load(f)

        # 1. ReliabilityMetrics
        rel_metrics = migrated_report["results"][0]["reliability"]
        assert rel_metrics["repair_loops_count"] == 0
        assert rel_metrics["time_to_pr_seconds"] is None
        assert rel_metrics["ci_rejection_count"] == 0
        assert rel_metrics["review_rejection_count"] == 0
        assert rel_metrics["validation_failure_category"] is None
        assert rel_metrics["worker_profile"] is None
        assert rel_metrics["provider_failure_cause"] is None

        # 2. ReliabilityReport
        rel_report = migrated_report["reliability_report"]
        assert rel_report["repair_loops_total"] == 0
        assert rel_report["mean_time_to_pr_seconds"] is None
        assert rel_report["ci_rejection_total"] == 0
        assert rel_report["review_rejection_total"] == 0
        assert rel_report["validation_failure_category_counts"] == {}
        assert rel_report["worker_profile_success_rates"] == {}
        assert rel_report["provider_failure_cause_counts"] == {}

        # 3. EvaluationComparison
        comp = migrated_report["comparison"]
        assert comp["delta_cases_with_validation_evidence"] == 0
        assert comp["delta_cases_needing_approval"] == 0
        assert comp["delta_cases_needing_manual_log_inspection"] == 0
        assert comp["delta_cases_with_worker_failure"] == 0
        assert comp["delta_mean_commands_run"] is None
        assert comp["delta_mean_files_changed"] is None
        assert comp["delta_mean_friction_reports"] is None
        assert comp["delta_repair_loops_total"] == 0
        assert comp["delta_mean_time_to_pr_seconds"] is None
        assert comp["delta_ci_rejection_total"] == 0
        assert comp["delta_review_rejection_total"] == 0
