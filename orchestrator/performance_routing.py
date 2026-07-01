"""Dynamic worker profile routing based on historical performance metrics."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orchestrator.state import RouteDecision
from workers.base import WorkerProfile

logger = logging.getLogger(__name__)

DEFAULT_METRICS_PATH = Path(__file__).resolve().parents[1] / "evaluation" / "routing_metrics.json"

_METRICS_CACHE: dict[Path, dict[str, Any]] = {}


class PerformanceRoutingPolicy:
    """Policy helper to dynamically choose worker profiles based on success rates and latencies."""

    def __init__(self, metrics_path: Path | None = None) -> None:
        self.metrics_path = metrics_path or DEFAULT_METRICS_PATH
        self.metrics_data: dict[str, Any] = {}
        self._load_metrics()

    def _load_metrics(self) -> None:
        if self.metrics_path in _METRICS_CACHE:
            self.metrics_data = _METRICS_CACHE[self.metrics_path]
            return

        if not (self.metrics_path.exists() or self.metrics_path.is_symlink()):
            logger.warning("Routing metrics file not found at: %s", self.metrics_path)
            return
        try:
            with self.metrics_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
                self.metrics_data = data if isinstance(data, dict) else {}
                _METRICS_CACHE[self.metrics_path] = self.metrics_data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load/parse routing metrics JSON: %s", e)
            self.metrics_data = {}

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
        source = str(self.metrics_data.get("source", "evaluation/routing_metrics.json"))

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
            # Normalize profile name (e.g., strip read-only suffixes)
            normalized_profile_name = profile_name
            if profile_name.endswith("-read-only"):
                normalized_profile_name = profile_name.removesuffix("-read-only")
            elif profile_name.endswith("-read-only-executor"):
                normalized_profile_name = profile_name.removesuffix("-read-only-executor")

            profile_metric = profiles_metrics.get(normalized_profile_name)
            if not isinstance(profile_metric, dict):
                candidate_metrics_meta[profile_name] = "no_metrics"
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
