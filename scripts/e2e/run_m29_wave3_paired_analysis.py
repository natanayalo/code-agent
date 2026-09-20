#!/usr/bin/env python3
"""Render the sanitized paired supplement for M29 Wave 3."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evaluation.m29_wave3_paired import (
    PairedAnalysisReport,
    Wave3Manifest,
    assert_sanitized_paired_report,
    build_paired_report,
    manifest_sha256,
    render_paired_markdown,
)


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=29)
    return parser


def _write_report(path: Path, content: str) -> None:
    """Write generated output and create parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Generate deterministic paired JSON and Markdown outputs."""
    args = build_parser().parse_args(argv)
    if args.iterations < 1:
        raise SystemExit("--iterations must be >= 1")
    manifest = Wave3Manifest.model_validate(json.loads(args.manifest.read_text(encoding="utf-8")))
    report: PairedAnalysisReport = build_paired_report(
        manifest,
        manifest_sha256=manifest_sha256(manifest),
        iterations=args.iterations,
        seed=args.seed,
    )
    json_payload = report.model_dump(mode="json")
    assert_sanitized_paired_report(json_payload)
    _write_report(args.json_output, json.dumps(json_payload, indent=2, sort_keys=True) + "\n")
    _write_report(args.markdown_output, render_paired_markdown(report) + "\n")
    print(f"Wrote paired analysis JSON to {args.json_output}")
    print(f"Wrote paired analysis Markdown to {args.markdown_output}")
    print(f"Manifest SHA-256: {manifest_sha256(manifest)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
