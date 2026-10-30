"""Public HTTPS transport and sanitized webhook alert payloads."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import socket
import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from apps.api.readiness_alerts import format_readiness_alert
from apps.api.routes.readiness_alerts import _public_fields
from orchestrator.operational_health_types import READINESS_REASON_CODES

URL_ENV = "CODE_AGENT_READINESS_ALERT_WEBHOOK_URL"
TOKEN_ENV = "CODE_AGENT_READINESS_ALERT_WEBHOOK_TOKEN"
TIMEOUT = 5


@dataclass(frozen=True)
class WebhookDestination:
    url: str
    host: str
    path: str
    destination_id: str
    token: str


def webhook_configuration(environ: Mapping[str, str]) -> WebhookDestination | None:
    raw = environ.get(URL_ENV, "").strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
        if (
            parts.scheme.lower() != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.port not in (None, 443)
            or parts.query
            or parts.fragment
            or "?" in raw
            or "#" in raw
        ):
            return None
        host = parts.hostname.encode("idna").decode("ascii").lower()
        if any(c in host for c in "\\%") or any(ord(c) < 33 for c in raw):
            return None
        authority = f"[{host}]" if ":" in host else host
        path = parts.path or "/"
        url = urlunsplit(("https", authority, path, "", ""))
        token = environ.get(TOKEN_ENV, "").strip()
        if any(ord(c) < 32 or ord(c) == 127 for c in token):
            return None
        return WebhookDestination(url, host, path, hashlib.sha256(url.encode()).hexdigest(), token)
    except (ValueError, UnicodeError):
        return None


def public_addresses(host: str) -> list[str]:
    addresses = {
        str(entry[4][0]) for entry in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    }

    def allowed(address: str) -> bool:
        ip = ipaddress.ip_address(address)
        return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified)

    if not addresses or any(not allowed(address) for address in addresses):
        raise ValueError("destination address is not public")
    return sorted(addresses)


def payload(item: dict) -> dict:
    fields = _public_fields(item["reason"])
    reason = fields["reason"]
    if reason not in READINESS_REASON_CODES and reason not in {
        "provider_capability",
        "interaction_wait_stuck",
        "outbox_command_stale",
        "terminal_reconciliation_divergent",
    }:
        raise ValueError("unknown alert reason")
    if fields.get("task_id"):
        fields["task_id"] = str(UUID(fields["task_id"]))
    action = (
        fields.get("next_action")
        or format_readiness_alert(item["reason"], item["transition"]).split("Next action: ")[-1]
    )
    return {
        "version": 1,
        "event_id": item["id"],
        "observed_at": item["observed_at"].isoformat(),
        "transition_number": item["transition_number"],
        "transition": item["transition"],
        **{k: v for k, v in fields.items() if v is not None and k != "next_action"},
        "next_action": action,
    }


class PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, deadline: float) -> None:
        self.deadline = deadline
        self.tls_context = ssl.create_default_context()
        self.tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
        super().__init__(host, port=443, timeout=TIMEOUT, context=self.tls_context)
        self.address = address

    def connect(self) -> None:
        sock = socket.create_connection((self.address, 443), remaining(self.deadline))
        try:
            sock.settimeout(remaining(self.deadline))
            self.sock = self.tls_context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("webhook deadline exceeded")
    return seconds


def send_webhook(destination: WebhookDestination, item: dict) -> int:
    deadline = time.monotonic() + TIMEOUT
    address = public_addresses(destination.host)[0]
    remaining(deadline)
    connection = PinnedConnection(destination.host, address, deadline)
    headers = {"Content-Type": "application/json"}
    if destination.token:
        headers["Authorization"] = f"Bearer {destination.token}"
    try:
        connection.connect()
        assert connection.sock is not None
        connection.sock.settimeout(remaining(deadline))
        connection.request(
            "POST", destination.path, body=json.dumps(payload(item)), headers=headers
        )
        connection.sock.settimeout(remaining(deadline))
        return connection.getresponse().status
    finally:
        connection.close()
