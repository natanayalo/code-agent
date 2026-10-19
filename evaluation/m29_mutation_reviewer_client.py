"""Concrete OpenRouter neutral reviewer client for M29 post-terminal evaluation."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

import httpx

from evaluation.m29_mutation_suite import (
    NEUTRAL_EVALUATOR_SYSTEM_PROMPT,
    NeutralEvaluatorManifest,
)
from evaluation.m29_mutation_verifier import (
    BlindedNeutralReviewRequest,
    NeutralReviewOutcome,
)
from evaluation.provider_reliability_models import ExecutionIdentity

LOGGER = logging.getLogger("m29_mutation_reviewer_client")


def _expected_system_prompt_sha256() -> str:
    return hashlib.sha256(NEUTRAL_EVALUATOR_SYSTEM_PROMPT.encode("utf-8")).hexdigest()


def _format_blinded_prompt(request: BlindedNeutralReviewRequest) -> str:
    """Format review request containing zero candidate identity or case order leakage."""
    files_str = (
        "\n".join(f"- {f}" for f in sorted(request.changed_files))
        if request.changed_files
        else "(none)"
    )
    return (
        f"Topic: {request.case_topic}\n\n"
        f"Task Goal:\n{request.prompt}\n\n"
        f"Acceptance Criteria Oracle:\n{request.acceptance_oracle}\n\n"
        f"Allowed Source Path:\n{request.allowed_source_path}\n\n"
        f"Changed Files:\n{files_str}\n\n"
        f"Diff:\n```diff\n{request.diff_text}\n```\n\n"
        "Instructions:\n"
        "Evaluate whether the changes in the diff satisfy the acceptance criteria oracle and "
        "respect the allowed source path without regressions or changing unrelated files.\n"
        "Provide your verdict strictly as a JSON object with keys:\n"
        "- 'passed': boolean (true if all criteria pass, false if blocking issues found)\n"
        "- 'blocking_findings': list of concise strings describing each blocking finding\n"
        "Do not include any additional commentary or markdown wrappers outside the JSON."
    )


def _parse_json_verdict(raw_content: str) -> tuple[bool, tuple[str, ...], str | None]:
    """Parse neutral reviewer JSON verdict safely."""
    text = raw_content.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            return False, ("Verdict payload is not a JSON object",), "invalid_json_shape"
        if not isinstance(data.get("passed"), bool):
            err_msg = "Verdict payload missing boolean 'passed' field"
            return False, (err_msg,), "missing_boolean_passed"
        passed = data["passed"]
        raw_findings = data.get("blocking_findings") or []
        findings = tuple(str(f) for f in raw_findings) if isinstance(raw_findings, list) else ()
        return passed, findings, None
    except Exception as exc:
        return False, (f"Failed to parse verdict JSON: {exc}",), f"json_parse_error:{exc}"


class OpenRouterNeutralReviewerClient:
    """Blinded third-party neutral reviewer client calling an OpenAI-compatible / OpenRouter API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout_seconds: float = 120.0,
        httpx_client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._external_client = httpx_client

    def _get_client(self) -> httpx.Client:
        if self._external_client is not None:
            return self._external_client
        return httpx.Client(timeout=self.timeout_seconds)

    def _validate_manifest_preconditions(self, manifest: NeutralEvaluatorManifest) -> str | None:
        expected_sha = _expected_system_prompt_sha256()
        if manifest.system_prompt_sha256 != expected_sha:
            return (
                f"system_prompt_sha256_mismatch:"
                f"expected {expected_sha}, got {manifest.system_prompt_sha256}"
            )
        if manifest.fallback_policy != "none":
            return f"fallback_policy_must_be_none:{manifest.fallback_policy}"
        return None

    def _parse_response_identity(
        self, response_data: dict[str, Any], manifest: NeutralEvaluatorManifest
    ) -> ExecutionIdentity | None:
        returned_model = response_data.get("model")
        if not returned_model or not isinstance(returned_model, str):
            return None

        if "/" in returned_model:
            resp_provider, resp_model = returned_model.split("/", 1)
        elif manifest.provider:
            resp_provider, resp_model = manifest.provider, returned_model
        else:
            return None

        if not resp_provider or not resp_model:
            return None

        return ExecutionIdentity(
            provider=resp_provider,
            model=resp_model,
            reasoning_effort=manifest.reasoning_effort,
        )

    def _execute_completion_request(
        self, user_prompt: str, manifest: NeutralEvaluatorManifest
    ) -> tuple[dict[str, Any] | None, str | None]:
        model_name = (
            manifest.model if "/" in manifest.model else f"{manifest.provider}/{manifest.model}"
        )
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/natanayalo/code-agent",
            "X-Title": "code-agent-m29-evaluation",
        }
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": NEUTRAL_EVALUATOR_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.0,
            "max_tokens": manifest.budget.max_tokens,
            "provider": {"allow_fallbacks": False},
            "response_format": {"type": "json_object"},
        }
        try:
            client = self._get_client()
            response = client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
        except Exception as exc:
            LOGGER.error("OpenRouter neutral reviewer HTTP request failed: %s", exc)
            return None, f"http_error:{exc}"

        if response.status_code != 200:
            LOGGER.error(
                "OpenRouter neutral reviewer returned HTTP %d: %s",
                response.status_code,
                response.text[:200],
            )
            return None, f"http_status_{response.status_code}:{response.text[:200]}"

        return response.json(), None

    def _process_completion_response(
        self, resp_json: dict[str, Any], manifest: NeutralEvaluatorManifest
    ) -> NeutralReviewOutcome:
        actual_identity = self._parse_response_identity(resp_json, manifest)
        if actual_identity is None:
            return NeutralReviewOutcome(
                passed=False,
                evaluator_identity=None,
                evaluator_identity_status="unavailable",
                fallback_attempted=False,
                reason="missing_or_invalid_response_model_identity",
            )

        expected_identity = manifest.identity
        if (
            actual_identity.provider != expected_identity.provider
            or actual_identity.model != expected_identity.model
        ):
            return NeutralReviewOutcome(
                passed=False,
                evaluator_identity=actual_identity,
                evaluator_identity_status="mismatched",
                fallback_attempted=True,
                reason=(
                    f"evaluator_identity_mismatch:"
                    f"expected {expected_identity}, got {actual_identity}"
                ),
            )

        choices = resp_json.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return NeutralReviewOutcome(
                passed=False,
                evaluator_identity=actual_identity,
                evaluator_identity_status="unavailable",
                fallback_attempted=False,
                reason="missing_choices_in_response",
            )

        content = choices[0].get("message", {}).get("content", "")
        passed, findings, parse_err = _parse_json_verdict(content)
        if parse_err:
            return NeutralReviewOutcome(
                passed=False,
                blocking_findings=findings,
                evaluator_identity=actual_identity,
                evaluator_identity_status="unavailable",
                fallback_attempted=False,
                reason=parse_err,
            )

        return NeutralReviewOutcome(
            passed=passed,
            blocking_findings=findings,
            evaluator_identity=actual_identity,
            evaluator_identity_status="verified",
            fallback_attempted=False,
            reason=None if passed else (findings[0] if findings else "neutral_review_failed"),
        )

    def review_blinded_diff(
        self, request: BlindedNeutralReviewRequest, manifest: NeutralEvaluatorManifest
    ) -> NeutralReviewOutcome:
        """Execute blinded diff review against the neutral evaluator model."""
        pre_err = self._validate_manifest_preconditions(manifest)
        if pre_err:
            return NeutralReviewOutcome(
                passed=False,
                evaluator_identity=None,
                evaluator_identity_status="unavailable",
                fallback_attempted=False,
                reason=pre_err,
            )

        if not self.api_key:
            return NeutralReviewOutcome(
                passed=False,
                evaluator_identity=None,
                evaluator_identity_status="unavailable",
                fallback_attempted=False,
                reason="missing_openrouter_api_key",
            )

        user_prompt = _format_blinded_prompt(request)
        resp_json, req_err = self._execute_completion_request(user_prompt, manifest)
        if req_err or resp_json is None:
            return NeutralReviewOutcome(
                passed=False,
                evaluator_identity=None,
                evaluator_identity_status="unavailable",
                fallback_attempted=False,
                reason=req_err,
            )

        return self._process_completion_response(resp_json, manifest)


__all__ = [
    "OpenRouterNeutralReviewerClient",
]
