from orchestrator.execution_policy import normalize_scout_submission


def test_deep_scout_budget():
    constraints, budget = normalize_scout_submission(
        {"task_type": "scout", "scout_mode": "deep"}, {"max_iterations": 10}
    )
    assert budget["max_iterations"] == 5  # capped by DEEP_SCOUT_BUDGET_CAPS
    assert budget["worker_timeout_seconds"] == 360


def test_normal_scout_budget():
    constraints, budget = normalize_scout_submission({"task_type": "scout"}, {"max_iterations": 10})
    assert budget["max_iterations"] == 3  # capped by SCOUT_BUDGET_CAPS
    assert budget["worker_timeout_seconds"] == 180
