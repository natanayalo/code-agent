"""Unit tests for run_m29_mutation_wave CLI options, preflight readiness, and branch wiring."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy import create_engine

from db.base import Base
from evaluation.m29_mutation_fixture import (
    PINNED_TASKBOARD_FIXTURE_REVISION,
    create_taskboard_fixture_repo,
)
from evaluation.m29_mutation_runner_models import TaskCohortSnapshot
from scripts.e2e.run_m29_mutation_wave import (
    _verify_pre_run_readiness,
    build_parser,
    check_api_readiness,
    handle_preflight,
    handle_run,
)


class TestApiReadinessChecks:
    """Tests for API stack /health and /ready verification."""

    def test_check_api_readiness_happy_path(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok"})
            if request.url.path == "/ready":
                return httpx.Response(200, json={"status": "ready"})
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        check_api_readiness("http://test-api:8000", client=client)

    def test_check_api_readiness_health_failures(self) -> None:
        def handler_500(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="Internal Server Error")

        client_500 = httpx.Client(transport=httpx.MockTransport(handler_500))
        with pytest.raises(RuntimeError, match="API health check failed: status 500"):
            check_api_readiness("http://test-api:8000", client=client_500)

        def handler_non_ok(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "unhealthy"})

        client_non_ok = httpx.Client(transport=httpx.MockTransport(handler_non_ok))
        with pytest.raises(RuntimeError, match="API health check returned non-ok status"):
            check_api_readiness("http://test-api:8000", client=client_non_ok)

        def handler_invalid_json(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="not-json")

        client_invalid = httpx.Client(transport=httpx.MockTransport(handler_invalid_json))
        with pytest.raises(RuntimeError, match="API health response is not valid JSON"):
            check_api_readiness("http://test-api:8000", client=client_invalid)

    def test_check_api_readiness_ready_failures(self) -> None:
        def handler_503(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok"})
            return httpx.Response(503, json={"status": "not_ready"})

        client_503 = httpx.Client(transport=httpx.MockTransport(handler_503))
        with pytest.raises(RuntimeError, match="API readiness check failed: status 503"):
            check_api_readiness("http://test-api:8000", client=client_503)

        def handler_not_ready(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok"})
            return httpx.Response(200, json={"status": "degraded"})

        client_not_ready = httpx.Client(transport=httpx.MockTransport(handler_not_ready))
        with pytest.raises(RuntimeError, match="API readiness check returned non-ready status"):
            check_api_readiness("http://test-api:8000", client=client_not_ready)

        def handler_invalid_ready_json(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok"})
            return httpx.Response(200, text="corrupt")

        client_invalid_ready = httpx.Client(
            transport=httpx.MockTransport(handler_invalid_ready_json)
        )
        with pytest.raises(RuntimeError, match="API readiness response is not valid JSON"):
            check_api_readiness("http://test-api:8000", client=client_invalid_ready)

    def test_verify_pre_run_readiness_helper(self) -> None:
        assert _verify_pre_run_readiness(None) is True

        with patch("scripts.e2e.run_m29_mutation_wave.check_api_readiness") as mock_check:
            assert _verify_pre_run_readiness("http://127.0.0.1:8000") is True
            mock_check.assert_called_once_with("http://127.0.0.1:8000")

        with patch(
            "scripts.e2e.run_m29_mutation_wave.check_api_readiness",
            side_effect=RuntimeError("connection refused"),
        ):
            assert _verify_pre_run_readiness("http://127.0.0.1:8000") is False


class TestCliSubcommandsAndFlags:
    """Tests for CLI arguments: --branch and --api-base-url."""

    def test_parser_branch_and_api_base_url_defaults(self) -> None:
        parser = build_parser()

        pref_args = parser.parse_args(
            [
                "preflight",
                "--bundle-dir",
                "/tmp/bundle",
                "--db-url",
                "sqlite:///tmp/db.sqlite",
                "--workspace-root",
                "/tmp/ws",
                "--fixture-repo-path",
                "/tmp/fixture",
            ]
        )
        assert pref_args.api_base_url is None

        pref_custom = parser.parse_args(
            [
                "preflight",
                "--bundle-dir",
                "/tmp/bundle",
                "--db-url",
                "sqlite:///tmp/db.sqlite",
                "--workspace-root",
                "/tmp/ws",
                "--fixture-repo-path",
                "/tmp/fixture",
                "--api-base-url",
                "http://disposable-api:8000",
            ]
        )
        assert pref_custom.api_base_url == "http://disposable-api:8000"

        run_args = parser.parse_args(
            [
                "run",
                "--bundle-dir",
                "/tmp/bundle",
                "--db-url",
                "sqlite:///tmp/db.sqlite",
                "--workspace-root",
                "/tmp/ws",
                "--fixture-repo-path",
                "/tmp/fixture",
            ]
        )
        assert run_args.branch == "master"

        run_custom = parser.parse_args(
            [
                "run",
                "--bundle-dir",
                "/tmp/bundle",
                "--db-url",
                "sqlite:///tmp/db.sqlite",
                "--workspace-root",
                "/tmp/ws",
                "--fixture-repo-path",
                "/tmp/fixture",
                "--branch",
                "custom-branch",
            ]
        )
        assert run_custom.branch == "custom-branch"

    def test_handle_preflight_verifies_api_readiness_when_configured(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir(parents=True)
        (bundle_dir / "bundle.json").write_text("{}", encoding="utf-8")
        ws_root = tmp_path / "ws"
        ws_root.mkdir(parents=True)
        fixture_dir = tmp_path / "fixture"
        fixture_dir.mkdir(parents=True)

        args = MagicMock()
        args.ack_disposable_mutation_stack = True
        args.bundle_dir = str(bundle_dir)
        args.workspace_root = str(ws_root)
        args.fixture_repo_path = str(fixture_dir)
        args.db_url = f"sqlite:///{tmp_path / 'db.sqlite'}"
        args.api_base_url = "http://mock-api:8000"
        args.as_of = None
        args.as_of = None

        mock_snap = TaskCohortSnapshot(
            as_of=datetime.now(UTC),
            window_start_at=datetime.now(UTC),
            window_end_at=datetime.now(UTC),
            baseline_memory_context_hash="abc",
            baseline_ids_by_profile={},
            frozen_baseline_ids_sha256_by_profile={},
        )

        engine = create_engine(args.db_url)
        Base.metadata.create_all(engine)

        with patch("scripts.e2e.run_m29_mutation_wave.check_api_readiness") as mock_check:
            with patch(
                "scripts.e2e.run_m29_mutation_wave.run_mutation_preflight_checks",
                return_value=mock_snap,
            ):
                with patch(
                    "scripts.e2e.run_m29_mutation_wave.extract_mutation_baseline_snapshot",
                    return_value=({}, {}),
                ):
                    rc = handle_preflight(args)
                    assert rc == 0
                    mock_check.assert_called_once_with("http://mock-api:8000")

    def test_handle_preflight_fails_when_api_readiness_fails(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir(parents=True)
        (bundle_dir / "bundle.json").write_text("{}", encoding="utf-8")
        ws_root = tmp_path / "ws"
        ws_root.mkdir(parents=True)
        fixture_dir = tmp_path / "fixture"
        fixture_dir.mkdir(parents=True)

        args = MagicMock()
        args.ack_disposable_mutation_stack = True
        args.bundle_dir = str(bundle_dir)
        args.workspace_root = str(ws_root)
        args.fixture_repo_path = str(fixture_dir)
        args.db_url = f"sqlite:///{tmp_path / 'db.sqlite'}"
        args.api_base_url = "http://mock-api:8000"
        args.as_of = None

        engine = create_engine(args.db_url)
        Base.metadata.create_all(engine)

        with patch(
            "scripts.e2e.run_m29_mutation_wave.check_api_readiness",
            side_effect=RuntimeError("API not ready"),
        ):
            rc = handle_preflight(args)
            assert rc == 1

    def test_handle_run_aborts_when_pre_run_readiness_fails(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir(parents=True)
        (bundle_dir / "bundle.json").write_text("{}", encoding="utf-8")

        args = MagicMock()
        args.ack_disposable_mutation_stack = True
        args.bundle_dir = str(bundle_dir)
        args.workspace_root = str(tmp_path / "ws")
        args.fixture_repo_path = str(tmp_path / "fixture")
        args.db_url = f"sqlite:///{tmp_path / 'db.sqlite'}"
        args.api_base_url = "http://failing-api:8000"

        with patch(
            "scripts.e2e.run_m29_mutation_wave._verify_pre_run_readiness",
            return_value=False,
        ):
            rc = handle_run(args)
            assert rc == 1

    def test_fixture_repo_supports_master_and_main_clones(self, tmp_path: Path) -> None:
        fixture_repo = tmp_path / "fixture"
        create_taskboard_fixture_repo(fixture_repo)

        ws_master = tmp_path / "clone_master"
        subprocess.run(
            [
                "git",
                "clone",
                "--branch",
                "master",
                "--single-branch",
                str(fixture_repo),
                str(ws_master),
            ],
            check=True,
            capture_output=True,
        )
        rev_master = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ws_master, text=True
        ).strip()
        assert rev_master == PINNED_TASKBOARD_FIXTURE_REVISION

        ws_main = tmp_path / "clone_main"
        subprocess.run(
            [
                "git",
                "clone",
                "--branch",
                "main",
                "--single-branch",
                str(fixture_repo),
                str(ws_main),
            ],
            check=True,
            capture_output=True,
        )
        rev_main = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ws_main, text=True
        ).strip()
        assert rev_main == PINNED_TASKBOARD_FIXTURE_REVISION
