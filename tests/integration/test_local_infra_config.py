"""Integration-style checks for the local Docker infrastructure config."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def _load_compose_config() -> dict[str, Any]:
    """Load the docker-compose configuration for structural assertions."""
    with Path("docker-compose.yml").open("r", encoding="utf-8") as compose_file:
        config = yaml.safe_load(compose_file)

    assert isinstance(config, dict)
    return config


def test_docker_compose_defines_api_and_postgres_services() -> None:
    """The local stack exposes the API and Postgres services."""
    compose_config = _load_compose_config()
    services = compose_config["services"]

    assert "api" in services
    assert "postgres" in services


def test_api_service_waits_for_healthy_postgres() -> None:
    """The API service depends on a healthy Postgres container and exposes its port."""
    compose_config = _load_compose_config()
    api_service = compose_config["services"]["api"]

    assert api_service["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert "8000:8000" in api_service["ports"]


def test_api_service_has_healthcheck() -> None:
    """The API service defines a healthcheck against the local health endpoint."""
    compose_config = _load_compose_config()
    api_service = compose_config["services"]["api"]

    assert api_service["healthcheck"]["test"][0] == "CMD"
    assert (
        "urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).read()"
        in (api_service["healthcheck"]["test"][3])
    )


def test_postgres_service_has_healthcheck_and_port_mapping() -> None:
    """Postgres exposes a local port and a healthcheck for Compose gating."""
    compose_config = _load_compose_config()
    postgres_service = compose_config["services"]["postgres"]

    assert "5432:5432" in postgres_service["ports"]
    assert postgres_service["healthcheck"]["test"][0] == "CMD-SHELL"
