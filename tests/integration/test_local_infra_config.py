"""Integration-style checks for the local Docker infrastructure config."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml


@pytest.fixture(scope="module")
def compose_config() -> dict[str, Any]:
    """Load the docker-compose configuration for structural assertions."""
    with Path("docker-compose.yml").open("r", encoding="utf-8") as compose_file:
        config = yaml.safe_load(compose_file)

    assert isinstance(config, dict)
    return config


def test_docker_compose_defines_api_and_postgres_services(
    compose_config: dict[str, Any],
) -> None:
    """The local stack exposes the API and Postgres services."""
    services = compose_config["services"]

    assert "api" in services
    assert "postgres" in services


def test_api_service_waits_for_healthy_postgres(compose_config: dict[str, Any]) -> None:
    """The API service depends on a healthy Postgres container and exposes its port."""
    api_service = compose_config["services"]["api"]

    assert api_service["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert "8000:8000" in api_service["ports"]


def test_compose_requires_explicit_database_credentials(
    compose_config: dict[str, Any],
) -> None:
    """The local stack requires DB credentials from .env instead of weak defaults."""
    postgres_env = compose_config["services"]["postgres"]["environment"]
    api_env = compose_config["services"]["api"]["environment"]

    assert postgres_env["POSTGRES_DB"] == "${POSTGRES_DB?Please set POSTGRES_DB in .env}"
    assert postgres_env["POSTGRES_USER"] == "${POSTGRES_USER?Please set POSTGRES_USER in .env}"
    assert (
        postgres_env["POSTGRES_PASSWORD"]
        == "${POSTGRES_PASSWORD?Please set POSTGRES_PASSWORD in .env}"
    )
    assert api_env["POSTGRES_DB"] == "${POSTGRES_DB?Please set POSTGRES_DB in .env}"
    assert api_env["POSTGRES_USER"] == "${POSTGRES_USER?Please set POSTGRES_USER in .env}"
    assert (
        api_env["POSTGRES_PASSWORD"] == "${POSTGRES_PASSWORD?Please set POSTGRES_PASSWORD in .env}"
    )
    assert (
        api_env["DATABASE_URL"]
        == "postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${DATABASE_HOST:-postgres}:${DATABASE_PORT:-5432}/${POSTGRES_DB}"
    )


def test_api_service_has_healthcheck(compose_config: dict[str, Any]) -> None:
    """The API service defines a healthcheck against the local health endpoint."""
    api_service = compose_config["services"]["api"]
    expected_healthcheck = (
        "import urllib.request; "
        "urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).read()"
    )

    assert api_service["healthcheck"]["test"] == [
        "CMD",
        "python",
        "-c",
        expected_healthcheck,
    ]


def test_postgres_service_has_healthcheck_and_port_mapping(
    compose_config: dict[str, Any],
) -> None:
    """Postgres exposes a local port and a healthcheck for Compose gating."""
    postgres_service = compose_config["services"]["postgres"]
    expected_healthcheck = (
        "pg_isready -U ${POSTGRES_USER?Please set POSTGRES_USER in .env} "
        "-d ${POSTGRES_DB?Please set POSTGRES_DB in .env}"
    )

    assert "5432:5432" in postgres_service["ports"]
    assert postgres_service["healthcheck"]["test"] == [
        "CMD-SHELL",
        expected_healthcheck,
    ]
