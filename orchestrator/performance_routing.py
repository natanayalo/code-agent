"""Dynamic worker profile routing based on historical performance metrics."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orchestrator.state import RouteDecision
from workers.base import WorkerProfile
from workers.model_config import (
    resolve_antigravity_model_config,
    resolve_codex_model_config,
)

logger = logging.getLogger(__name__)

DEFAULT_METRICS_PATH = Path(__file__).resolve().parents[1] / "evaluation" / "routing_metrics.json"

_METRICS_CACHE: dict[Path, dict[str, Any]] = {}


def _expected_profile_model_config(
    profile: WorkerProfile,
    env: Mapping[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """Return the expected (model, reasoning_effort) for a worker profile."""
    resolved_env = os.environ if env is None else env
    manifest_worker = {
        "model": profile.model,
        "reasoning_effort": profile.reasoning_effort,
    }
    if profile.worker_type == "codex":
        resolved = resolve_codex_model_config(
            manifest_worker=manifest_worker,
            env=resolved_env,
        )
        return resolved.model, resolved.reasoning_effort
    elif profile.worker_type == "antigravity":
        resolved = resolve_antigravity_model_config(
            manifest_worker=manifest_worker,
            env=resolved_env,
        )
        return resolved.model, resolved.reasoning_effort
    return profile.model, profile.reasoning_effort


class PerformanceRoutingPolicy:
    """Policy helper to dynamically choose worker profiles based on success rates and latencies."""

    def __init__(
        self,
        metrics_path: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.metrics_path = (metrics_path or DEFAULT_METRICS_PATH).resolve()
        self.env: Mapping[str, str] = os.environ if env is None else env
        self.metrics_data: dict[str, Any] = {}
        self._load_metrics()

    def _load_metrics(self) -> None:
        if self.metrics_path in _METRICS_CACHE:
            self.metrics_data = _METRICS_CACHE[self.metrics_path]
            return

        if not (self.metrics_path.exists() or self.metrics_path.is_symlink()):
            logger.warning("Routing metrics file not found at: %s", self.metrics_path)
            self.metrics_data = {}
            _METRICS_CACHE[self.metrics_path] = self.metrics_data
            return
        try:
            with self.metrics_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
                raw_metrics = data if isinstance(data, dict) else {}

                # Pre-normalize/expand profiles at load time to avoid query-time string manipulation
                profiles = raw_metrics.get("profiles")
                if isinstance(profiles, dict):
                    expanded_profiles = {}
                    for k, v in profiles.items():
                        expanded_profiles[k] = v
                        expanded_profiles[f"{k}-read-only"] = v
                        expanded_profiles[f"{k}-read-only-executor"] = v
                        if "-native-executor" in k:
                            ro_key = k.replace("-native-executor", "-read-only-executor")
                            expanded_profiles[ro_key] = v
                    raw_metrics["profiles"] = expanded_profiles

                self.metrics_data = raw_metrics
                _METRICS_CACHE[self.metrics_path] = self.metrics_data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load/parse routing metrics JSON: %s", e)
            self.metrics_data = {}
            _METRICS_CACHE[self.metrics_path] = self.metrics_data

    def choose_profile(
        self,
        task_class: str | None,
        routable_profiles: Mapping[str, WorkerProfile],
    ) -> RouteDecision | None:
        """Choose the optimal profile from routable candidates using success rate and latency."""
        if not self.metrics_data or not task_class:
            return None

        # Normalize/fallback task class types:
        normalized_class = task_class
        if task_class == "investigation":
            normalized_class = "scout"
        elif task_class == "review_fix":
            normalized_class = "bugfix"

        profiles_metrics = self.metrics_data.get("profiles", {})
        if not isinstance(profiles_metrics, dict):
            return None

        version = self.metrics_data.get("version", "unknown")
        source = str(self.metrics_data.get("source") or self.metrics_path)

        candidates, candidate_metrics_meta = self._build_routing_candidates(
            normalized_class, routable_profiles, profiles_metrics
        )

        if not candidates:
            return None

        # Sort candidates: primary success rate (descending), secondary latency (ascending)
        candidates.sort(key=lambda x: (-x["success_rate"], x["latency"]))
        best = candidates[0]

        route_metadata = {
            "task_class": task_class,
            "selected_profile": best["profile_name"],
            "candidate_metrics": candidate_metrics_meta,
            "metric_source": source,
            "metric_version": version,
            "fallback_reason": None,
        }

        best_profile = best["profile"]
        return RouteDecision(
            chosen_worker=best_profile.worker_type,
            chosen_profile=best["profile_name"],
            runtime_mode=best_profile.runtime_mode,
            route_reason="dynamic_performance_routing",
            override_applied=False,
            route_metadata=route_metadata,
            model=best_profile.model,
            reasoning_effort=best_profile.reasoning_effort,
        )

    def _build_routing_candidates(
        self,
        normalized_class: str,
        routable_profiles: Mapping[str, WorkerProfile],
        profiles_metrics: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Filter and collect candidates along with metadata."""
        candidates = []
        candidate_metrics_meta: dict[str, Any] = {}

        for profile_name, profile in routable_profiles.items():
            profile_metric = profiles_metrics.get(profile_name)
            if not isinstance(profile_metric, dict):
                candidate_metrics_meta[profile_name] = "no_metrics"
                continue

            expected_model, expected_effort = _expected_profile_model_config(profile, env=self.env)
            metric_model = profile_metric.get("model")
            metric_effort = profile_metric.get("reasoning_effort")
            if metric_model != expected_model or metric_effort != expected_effort:
                candidate_metrics_meta[profile_name] = "model_mismatch"
                continue

            task_classes = profile_metric.get("task_classes")
            if not isinstance(task_classes, dict):
                candidate_metrics_meta[profile_name] = "no_metrics"
                continue

            task_class_metrics = task_classes.get(normalized_class)
            if not isinstance(task_class_metrics, dict):
                candidate_metrics_meta[profile_name] = "no_metrics"
                continue

            success_rate = task_class_metrics.get("success_rate")
            latency = task_class_metrics.get("mean_latency_seconds")

            if (
                not isinstance(success_rate, int | float)
                or isinstance(success_rate, bool)
                or not isinstance(latency, int | float)
                or isinstance(latency, bool)
            ):
                candidate_metrics_meta[profile_name] = "malformed_metrics"
                continue

            candidate_metrics_meta[profile_name] = {
                "success_rate": success_rate,
                "mean_latency_seconds": latency,
            }

            candidates.append(
                {
                    "profile_name": profile_name,
                    "profile": profile,
                    "success_rate": success_rate,
                    "latency": latency,
                }
            )
        return candidates, candidate_metrics_meta
