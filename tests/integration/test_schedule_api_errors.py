"""Schedule endpoints fail closed for invalid input and Temporal failures."""

from types import SimpleNamespace

from temporalio.service import RPCError, RPCStatusCode

from apps.api.routes.schedules import get_schedule_client


def test_schedule_endpoints_require_existing_auth(client):
    client.headers.clear()
    assert client.get("/schedules").status_code == 401


def test_schedule_api_rejects_unsupported_fields_and_unknown_repository(client):
    spec = {
        "version": 1,
        "purpose": "dependency_review",
        "repo_key": "missing",
        "weekdays": ["monday"],
        "time": "09:00",
        "timezone": "UTC",
    }

    async def missing():
        raise RPCError("missing", RPCStatusCode.NOT_FOUND, b"")

    client.app.dependency_overrides[get_schedule_client] = lambda: SimpleNamespace(
        get_schedule_handle=lambda _: SimpleNamespace(describe=missing)
    )
    assert (
        client.put("/schedules/test", json=spec | {"task_text": "change files"}).status_code == 422
    )
    assert client.put("/schedules/test", json=spec).status_code == 400


def test_schedule_api_reports_temporal_unavailable(client):
    async def fail():
        raise RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")

    client.app.dependency_overrides[get_schedule_client] = lambda: SimpleNamespace(
        get_schedule_handle=lambda _: SimpleNamespace(describe=fail)
    )
    response = client.get("/schedules/test")
    assert response.status_code == 503
    assert response.json()["detail"] == "Temporal is unavailable"
    spec = {
        "version": 1,
        "purpose": "dependency_review",
        "repo_key": "missing",
        "weekdays": ["monday"],
        "time": "09:00",
        "timezone": "UTC",
    }
    assert client.put("/schedules/test", json=spec).status_code == 503
