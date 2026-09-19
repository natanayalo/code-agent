#!/usr/bin/env python3
"""CLI utility to run provider pre-dispatch diagnostics on the current execution host."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from orchestrator.provider_diagnostics import ProviderDiagnosticsService
from orchestrator.provider_diagnostics_types import SystemProviderDiagnosticsReport


def _parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check provider readiness (credentials, CLI binaries, Docker daemon)."
    )
    parser.add_argument(
        "--providers",
        type=str,
        default=None,
        help="Comma-separated list of providers to evaluate (e.g. codex,antigravity).",
    )
    parser.add_argument(
        "--required",
        type=str,
        default=None,
        help="Comma-separated list of providers that must be ready for exit code 0.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="output_json",
        help="Output raw JSON report.",
    )
    return parser.parse_args(args)


def _print_text_summary(report: SystemProviderDiagnosticsReport) -> None:
    print(f"Provider Diagnostics Report ({report.checked_at.isoformat()})")
    print(f"Target: {report.target} | Env: {report.execution_environment_id}")
    print(f"Overall Ready: {report.all_ready} | Required Ready: {report.required_providers_ready}")
    print("-" * 60)

    for provider, p_report in sorted(report.providers.items()):
        status_sym = "[READY]" if p_report.ready else "[UNREADY]"
        print(f"\nProvider: {provider.upper()} {status_sym}")
        print(f"  Decision: {p_report.decision}")
        if p_report.summary:
            print(f"  Summary: {p_report.summary}")
        if p_report.next_action_hint:
            print(f"  Next Action Hint: {p_report.next_action_hint}")

        for check in p_report.checks:
            check_status = check.status.upper()
            blocking_flag = " (BLOCKING)" if check.blocking else ""
            print(f"  - [{check_status}] {check.name}: {check.detail}{blocking_flag}")
            if check.remediation:
                print(f"      Remediation: {check.remediation}")


async def _async_main(args: argparse.Namespace) -> int:
    required_set = (
        {p.strip().lower() for p in args.required.split(",") if p.strip()}
        if args.required
        else None
    )
    service = ProviderDiagnosticsService()
    report = await service.evaluate_all_providers(
        required_providers=required_set,
        target="cli",
    )

    if args.output_json:
        print(json.dumps(report.model_dump(mode="json"), indent=2))
    else:
        _print_text_summary(report)

    if required_set is not None:
        return 0 if report.required_providers_ready else 1
    return 0 if report.all_ready else 1


def main() -> None:
    args = _parse_args()
    exit_code = asyncio.run(_async_main(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
