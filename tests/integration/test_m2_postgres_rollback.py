"""Opt-in full-schema synthetic PostgreSQL backup/restore and delivery continuity."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from apps.api.main import create_app
from db.base import Base
from orchestrator.execution import TaskExecutionService
from repositories import create_engine_from_url, create_session_factory
from tests.integration.task_endpoints_support import _default_worker
from tests.integration.test_m2_docker_security import docker
from tests.integration.test_telegram_endpoints import _text_update
from tests.unit.test_telegram_security import operator_config

pytestmark = pytest.mark.skipif(
    os.environ.get("CODE_AGENT_M2_DOCKER_TESTS") != "1",
    reason="explicit synthetic Docker opt-in required",
)


@pytest.fixture
def postgres():
    name = "code-agent-m2-rollback-" + uuid4().hex[:12]
    docker(
        "run",
        "-d",
        "--name",
        name,
        "--publish",
        "127.0.0.1::5432",
        "--memory",
        "512m",
        "--cpus",
        "1",
        "--pids-limit",
        "128",
        "--tmpfs",
        "/var/lib/postgresql/data:rw,nosuid,size=256m",
        "--env",
        "POSTGRES_PASSWORD=m2-synthetic-only",
        "postgres:16",
    )
    try:
        for _ in range(100):
            ready = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
                capture_output=True,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("synthetic PostgreSQL did not become ready")
        port = json.loads(docker("inspect", name).stdout)[0]["NetworkSettings"]["Ports"][
            "5432/tcp"
        ][0]["HostPort"]
        yield name, f"postgresql+psycopg://postgres:m2-synthetic-only@127.0.0.1:{port}"
    finally:
        docker("rm", "-f", name)


def test_backup_restore_preserves_task_session_delivery_and_command(postgres, tmp_path):
    name, base = postgres
    for database in ("m2_source", "m2_restored"):
        docker("exec", name, "createdb", "-U", "postgres", database)
    engine = create_engine_from_url(base + "/m2_source")
    subprocess.run(
        [str(Path(sys.executable).with_name("alembic")), "upgrade", "head"],
        env={**os.environ, "DATABASE_URL": base + "/m2_source"},
        check=True,
        capture_output=True,
    )
    service = TaskExecutionService(
        session_factory=create_session_factory(engine), worker=_default_worker()
    )
    headers = {"X-Telegram-Bot-Api-Secret-Token": "t" * 32}
    update = _text_update("Synthetic rollback task", update_id=701)
    with TestClient(create_app(task_service=service, auth_config=operator_config())) as client:
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(
                pool.map(
                    lambda _: client.post("/telegram/webhook", json=update, headers=headers),
                    range(2),
                )
            )
        first = responses[0]
    assert first.status_code == 200
    assert responses[1].status_code == 200
    assert responses[1].json()["task_id"] == first.json()["task_id"]
    with engine.connect() as connection:
        before = {
            table.name: connection.scalar(select(func.count()).select_from(table))
            for table in Base.metadata.sorted_tables
        }
    dump = subprocess.run(
        ["docker", "exec", name, "pg_dump", "-U", "postgres", "-Fc", "m2_source"],
        check=True,
        capture_output=True,
    ).stdout
    (tmp_path / "synthetic-state.dump").write_bytes(dump)
    subprocess.run(
        ["docker", "exec", "-i", name, "pg_restore", "-U", "postgres", "-d", "m2_restored"],
        input=dump,
        check=True,
        capture_output=True,
    )
    restored_engine = create_engine_from_url(base + "/m2_restored")
    try:
        with restored_engine.connect() as connection:
            after = {
                table.name: connection.scalar(select(func.count()).select_from(table))
                for table in Base.metadata.sorted_tables
            }
        assert before == after
        restored = TaskExecutionService(
            session_factory=create_session_factory(restored_engine), worker=_default_worker()
        )
        with TestClient(create_app(task_service=restored, auth_config=operator_config())) as client:
            duplicate = client.post("/telegram/webhook", json=update, headers=headers)
        assert duplicate.json()["task_id"] == first.json()["task_id"]
        assert duplicate.json()["session_id"] == first.json()["session_id"]
        assert duplicate.json()["detail"] == "duplicate_delivery"
    finally:
        restored_engine.dispose()
        engine.dispose()
