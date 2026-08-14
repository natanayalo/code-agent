"""Minimal audited HTTPS CONNECT proxy for native-agent executor containers."""

from __future__ import annotations

import ipaddress
import json
import os
import select
import socket
import socketserver
from datetime import UTC, datetime
from pathlib import Path

_AUDIT_PATH = Path(os.environ.get("CODE_AGENT_PROXY_AUDIT_PATH", "/tmp/egress-audit.jsonl"))


def _audit(*, host: str, addresses: list[str], method: str, outcome: str) -> None:
    _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "task_id": os.environ.get("CODE_AGENT_PROXY_TASK_ID", "unknown"),
        "timestamp": datetime.now(UTC).isoformat(),
        "destination_host": host,
        "destination_ips": addresses,
        "method": method,
        "outcome": outcome,
    }
    with _AUDIT_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _resolved_public_addresses(host: str) -> list[str]:
    try:
        records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return sorted({item[4][0] for item in records})
    except OSError:
        return []


def _is_public_egress_host(host: str, resolved_addresses: list[str]) -> bool:
    """Validate destination resolution without importing application packages."""
    blocked_hosts = {"localhost", "host.docker.internal", "metadata.google.internal"}
    if not host or host.lower() in blocked_hosts:
        return False
    try:
        values = [ipaddress.ip_address(value) for value in resolved_addresses]
    except ValueError:
        return False
    return bool(values) and all(value.is_global for value in values)


class _ProxyHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        request_line = self.rfile.readline(8192).decode("iso-8859-1", errors="replace").strip()
        parts = request_line.split()
        if len(parts) != 3 or parts[0].upper() != "CONNECT":
            self.wfile.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return
        authority = parts[1]
        host, separator, port_text = authority.rpartition(":")
        if not separator or not host or port_text != "443":
            self.wfile.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return
        while self.rfile.readline(8192) not in {b"\r\n", b"\n", b""}:
            pass
        addresses = _resolved_public_addresses(host)
        if not _is_public_egress_host(host, addresses):
            _audit(host=host, addresses=addresses, method="CONNECT", outcome="blocked")
            self.wfile.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return
        try:
            remote = socket.create_connection((addresses[0], 443), timeout=15)
        except OSError:
            _audit(host=host, addresses=addresses, method="CONNECT", outcome="connect_failed")
            self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        _audit(host=host, addresses=addresses, method="CONNECT", outcome="allowed")
        self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        self.wfile.flush()
        with remote:
            sockets = (self.connection, remote)
            while True:
                ready, _, _ = select.select(sockets, (), (), 30)
                if not ready:
                    continue
                for source in ready:
                    target = remote if source is self.connection else self.connection
                    try:
                        data = source.recv(65536)
                    except OSError:
                        return
                    if not data:
                        return
                    target.sendall(data)


def main() -> None:
    with socketserver.ThreadingTCPServer(("0.0.0.0", 8080), _ProxyHandler) as server:
        server.daemon_threads = True
        server.serve_forever()


if __name__ == "__main__":
    main()
