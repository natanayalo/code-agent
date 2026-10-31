"""Webhook URL, network, and payload security tests."""

import http.client
import socket
import ssl
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from apps.api.webhook_alerts import payload, public_addresses, webhook_configuration


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user@example.com",
        "https://example.com:444/",
        "https://example.com/?a=1",
        "https://example.com/#x",
        "https://example.com/?",
        "https://example.com/#",
    ],
)
def test_bad_urls(url):
    assert webhook_configuration({"CODE_AGENT_READINESS_ALERT_WEBHOOK_URL": url}) is None


def test_normalization_and_credentials():
    config = webhook_configuration(
        {
            "CODE_AGENT_READINESS_ALERT_WEBHOOK_URL": "https://EXAMPLE.com:443",
            "CODE_AGENT_READINESS_ALERT_WEBHOOK_TOKEN": "secret",
        }
    )
    assert config.url == "https://example.com/"
    assert config.token == "secret"
    assert (
        webhook_configuration(
            {
                "CODE_AGENT_READINESS_ALERT_WEBHOOK_URL": "https://example.com",
                "CODE_AGENT_READINESS_ALERT_WEBHOOK_TOKEN": "bad\rheader",
            }
        )
        is None
    )


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "224.0.0.1"]
)
def test_reject_nonpublic_addresses(address):
    with patch(
        "socket.getaddrinfo",
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))],
    ):
        with pytest.raises(ValueError):
            public_addresses("example.com")


def test_payload_stable_and_safe():
    item = {
        "id": "event",
        "reason": "temporal_unavailable",
        "transition": "degraded",
        "transition_number": 1,
        "observed_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    result = payload(item)
    assert result == payload(item)
    assert result["event_id"] == "event" and result["version"] == 1
    assert set(result) == {
        "event_id",
        "version",
        "observed_at",
        "transition_number",
        "transition",
        "reason",
        "next_action",
    }


def test_configuration_edge_cases():
    assert webhook_configuration({}) is None
    assert (
        webhook_configuration({"CODE_AGENT_READINESS_ALERT_WEBHOOK_URL": "https://example.com:bad"})
        is None
    )
    assert (
        webhook_configuration({"CODE_AGENT_READINESS_ALERT_WEBHOOK_URL": "https://exa mple.com"})
        is None
    )
    assert (
        webhook_configuration(
            {"CODE_AGENT_READINESS_ALERT_WEBHOOK_URL": "https://[2606:4700:4700::1111]/"}
        ).host
        == "2606:4700:4700::1111"
    )


def test_mixed_dns_is_rejected_and_public_is_accepted():
    def entry(ip):
        return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))

    with patch("socket.getaddrinfo", return_value=[entry("8.8.8.8"), entry("10.0.0.1")]):
        with pytest.raises(ValueError):
            public_addresses("example.com")
    with patch("socket.getaddrinfo", return_value=[entry("8.8.8.8")]):
        assert public_addresses("example.com") == ["8.8.8.8"]


def test_payload_task_identity_and_unknown_reason():
    item = {
        "id": "id",
        "reason": "interaction_wait_stuck|12345678-1234-1234-1234-123456789abc",
        "transition": "degraded",
        "transition_number": 1,
        "observed_at": datetime.now(UTC),
    }
    assert payload(item)["task_id"] == "12345678-1234-1234-1234-123456789abc"
    item["reason"] = "sensitive private detail"
    with pytest.raises(ValueError):
        payload(item)


def test_pinned_connection_and_webhook_headers():
    from unittest.mock import MagicMock

    from apps.api.webhook_alerts import PinnedConnection, send_webhook

    sock = MagicMock()
    context = MagicMock()
    with (
        patch("ssl.create_default_context", return_value=context),
        patch("time.monotonic", return_value=100.0),
        patch("socket.socket", return_value=sock),
    ):
        connection = PinnedConnection("example.com", "8.8.8.8", 105.0)
        connection.connect()
        assert context.minimum_version == ssl.TLSVersion.TLSv1_2
        sock.connect.assert_called_once_with(("8.8.8.8", 443))
        context.wrap_socket.assert_called_once_with(
            sock, server_hostname="example.com", do_handshake_on_connect=False
        )
    context.wrap_socket.side_effect = OSError("sensitive")
    with (
        patch("ssl.create_default_context", return_value=context),
        patch("time.monotonic", return_value=100.0),
        patch("socket.socket", return_value=sock),
    ):
        with pytest.raises(OSError):
            PinnedConnection("example.com", "8.8.8.8", 105.0).connect()
        sock.close.assert_called()
    target = webhook_configuration(
        {
            "CODE_AGENT_READINESS_ALERT_WEBHOOK_URL": "https://example.com/path",
            "CODE_AGENT_READINESS_ALERT_WEBHOOK_TOKEN": "secret",
        }
    )
    item = {
        "id": "event",
        "reason": "temporal_unavailable",
        "transition": "degraded",
        "transition_number": 1,
        "observed_at": datetime.now(UTC),
    }
    fake = MagicMock()
    fake.getresponse.return_value.status = 302
    with (
        patch("apps.api.webhook_alerts.public_addresses", return_value=["8.8.8.8"]),
        patch("apps.api.webhook_alerts.PinnedConnection", return_value=fake) as constructor,
    ):
        assert send_webhook(target, item) == 302
        assert constructor.call_args.args[:2] == ("example.com", "8.8.8.8")
        assert fake.request.call_args.kwargs["headers"]["Authorization"] == "Bearer secret"
        fake.close.assert_called_once()


def test_dns_timeout_prevents_late_connection():
    from apps.api.webhook_alerts import send_webhook

    target = webhook_configuration(
        {"CODE_AGENT_READINESS_ALERT_WEBHOOK_URL": "https://example.com/"}
    )
    with (
        patch("time.monotonic", side_effect=[100.0, 106.0]),
        patch("apps.api.webhook_alerts.public_addresses", return_value=["8.8.8.8"]),
        patch("apps.api.webhook_alerts.PinnedConnection") as connection,
        pytest.raises(TimeoutError),
    ):
        send_webhook(target, {})
    connection.assert_not_called()


def test_webhook_tls_requires_modern_protocol_and_verifies_peer():
    from apps.api.webhook_alerts import PinnedConnection

    context = ssl.create_default_context()
    with (
        patch("ssl.create_default_context", return_value=context),
        patch("socket.socket"),
        patch.object(ssl.SSLContext, "wrap_socket"),
        patch("time.monotonic", return_value=100.0),
    ):
        PinnedConnection("example.com", "8.8.8.8", 105.0).connect()
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname


def test_trickled_headers_are_interrupted_at_absolute_deadline(monkeypatch):
    import threading
    import time

    from apps.api.webhook_alerts import PinnedConnection, send_webhook

    client, server = socket.socketpair()
    stopped = threading.Event()

    def trickle():
        try:
            server.recv(4096)
            for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n":
                server.send(bytes([byte]))
                time.sleep(0.01)
        except OSError:
            pass
        finally:
            server.close()
            stopped.set()

    worker = threading.Thread(target=trickle, daemon=True)
    worker.start()
    monkeypatch.setattr("apps.api.webhook_alerts.TIMEOUT", 0.05)
    monkeypatch.setattr("apps.api.webhook_alerts.public_addresses", lambda *_: ["8.8.8.8"])
    monkeypatch.setattr(PinnedConnection, "connect", lambda self: self._set_socket(client))
    target = webhook_configuration(
        {"CODE_AGENT_READINESS_ALERT_WEBHOOK_URL": "https://example.com/"}
    )
    item = {
        "id": "event",
        "reason": "temporal_unavailable",
        "transition": "degraded",
        "transition_number": 1,
        "observed_at": datetime.now(UTC),
    }
    started = time.monotonic()
    with pytest.raises((OSError, TimeoutError, http.client.HTTPException)):
        send_webhook(target, item)
    assert time.monotonic() - started < 0.4
    assert stopped.wait(1)
    assert client.fileno() == -1


def test_stalled_resolver_has_bounded_workers(monkeypatch):
    import threading
    import time

    from apps.api.webhook_alerts import public_addresses

    entered, release = threading.Event(), threading.Event()

    def stall(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", stall)
    try:
        with pytest.raises(TimeoutError):
            public_addresses("example.com", time.monotonic() + 0.05)
        assert entered.is_set()
        with pytest.raises(TimeoutError, match="busy"):
            public_addresses("example.com", time.monotonic() + 0.05)
    finally:
        release.set()
