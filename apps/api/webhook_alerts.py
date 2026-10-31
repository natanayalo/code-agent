"""Public HTTPS transport and sanitized webhook alert payloads."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import queue
import socket
import ssl
import threading
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
_DNS_SLOT = threading.BoundedSemaphore(1)


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


def public_addresses(host: str, deadline: float | None = None) -> list[str]:
    """Bound stalled resolver work to one daemon thread and no pending queue."""
    deadline = deadline if deadline is not None else time.monotonic() + TIMEOUT
    if not _DNS_SLOT.acquire(blocking=False):
        raise TimeoutError("webhook resolver busy")
    result: queue.Queue[tuple[list[str] | None, Exception | None]] = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            result.put((_resolve_public_addresses(host), None))
        except Exception as exc:
            result.put((None, exc))
        finally:
            _DNS_SLOT.release()

    try:
        threading.Thread(target=resolve, name="webhook-dns", daemon=True).start()
    except BaseException:
        _DNS_SLOT.release()
        raise
    try:
        addresses, error = result.get(timeout=remaining(deadline))
    except queue.Empty as exc:
        raise TimeoutError("webhook resolver timed out") from exc
    if error is not None:
        raise error
    assert addresses is not None
    return addresses


def _resolve_public_addresses(host: str) -> list[str]:
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
        super().__init__(host, port=443, timeout=TIMEOUT)
        self.address = address
        self._socket_lock = threading.Lock()
        self._aborted = False

    def abort(self) -> None:
        """Interrupt a blocked socket operation when the absolute deadline expires."""
        with self._socket_lock:
            self._aborted = True
            if self.sock is not None:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.sock.close()

    def _set_socket(self, sock: socket.socket) -> None:
        with self._socket_lock:
            if self._aborted:
                sock.close()
                raise TimeoutError("webhook deadline exceeded")
            self.sock = sock

    def connect(self) -> None:
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        address = ipaddress.ip_address(self.address)
        sock = socket.socket(socket.AF_INET6 if address.version == 6 else socket.AF_INET)
        self._set_socket(sock)
        try:
            sock.settimeout(remaining(self.deadline))
            sock.connect((self.address, 443))
            with self._socket_lock:
                if self._aborted:
                    raise TimeoutError("webhook deadline exceeded")
                self.sock = context.wrap_socket(
                    sock, server_hostname=self.host, do_handshake_on_connect=False
                )
            self.sock.settimeout(remaining(self.deadline))
            self.sock.do_handshake()
        except BaseException:
            sock.close()
            if self.sock is not None:
                self.sock.close()
            raise


def remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("webhook deadline exceeded")
    return seconds


def send_webhook(destination: WebhookDestination, item: dict) -> int:
    deadline = time.monotonic() + TIMEOUT
    address = public_addresses(destination.host, deadline)[0]
    remaining(deadline)
    connection = PinnedConnection(destination.host, address, deadline)
    headers = {"Content-Type": "application/json"}
    if destination.token:
        headers["Authorization"] = f"Bearer {destination.token}"
    timer = threading.Timer(remaining(deadline), connection.abort)
    timer.daemon = True
    timer.start()
    try:
        connection.connect()
        assert connection.sock is not None
        connection.sock.settimeout(remaining(deadline))
        connection.request(
            "POST", destination.path, body=json.dumps(payload(item)), headers=headers
        )
        connection.sock.settimeout(remaining(deadline))
        status = connection.getresponse().status
        remaining(deadline)
        return status
    finally:
        timer.cancel()
        timer.join()
        connection.close()
