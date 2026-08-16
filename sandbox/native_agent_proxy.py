"""Minimal audited HTTPS CONNECT proxy for native-agent executor containers."""

from __future__ import annotations

import ipaddress
import json
import os
import select
import socket
import socketserver
import time
from datetime import UTC, datetime
from pathlib import Path

_AUDIT_PATH = Path(os.environ.get("CODE_AGENT_PROXY_AUDIT_PATH", "/tmp/egress-audit.jsonl"))
_NETWORK_POLICY = os.environ.get("CODE_AGENT_NETWORK_POLICY", "disabled")
_ALLOWED_HOSTS = {h for h in os.environ.get("CODE_AGENT_ALLOWED_HOSTS", "").split(",") if h}


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


def _extract_sni(data: bytes) -> str | None | bool:
    if len(data) < 5:
        return None
    if data[0] != 0x16:
        return False
    record_len = int.from_bytes(data[3:5], "big")
    if len(data) < 5 + record_len:
        return None
    if data[5] != 0x01:
        return False
    msg_len = int.from_bytes(data[6:9], "big")
    if len(data) < 9 + msg_len:
        return None
    pos = 43
    if pos >= 9 + msg_len:
        return False
    session_id_len = data[pos]
    pos += 1 + session_id_len
    if pos + 2 > 9 + msg_len:
        return False
    cipher_len = int.from_bytes(data[pos : pos + 2], "big")
    pos += 2 + cipher_len
    if pos + 1 > 9 + msg_len:
        return False
    comp_len = data[pos]
    pos += 1 + comp_len
    if pos + 2 > 9 + msg_len:
        return False
    ext_len = int.from_bytes(data[pos : pos + 2], "big")
    pos += 2
    end_pos = min(pos + ext_len, 9 + msg_len)
    while pos + 4 <= end_pos:
        ext_type = int.from_bytes(data[pos : pos + 2], "big")
        ext_size = int.from_bytes(data[pos + 2 : pos + 4], "big")
        pos += 4
        if ext_type == 0x0000:
            if pos + 2 > end_pos:
                return False
            pos += 2
            if pos + 1 > end_pos:
                return False
            name_type = data[pos]
            pos += 1
            if name_type == 0x00:
                if pos + 2 > end_pos:
                    return False
                name_len = int.from_bytes(data[pos : pos + 2], "big")
                pos += 2
                if pos + name_len > end_pos:
                    return False
                return data[pos : pos + name_len].decode("ascii", errors="ignore")
        elif ext_type == 0xFE0D:
            return "ECH_PRESENT"
        pos += ext_size
    return False


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
        if _NETWORK_POLICY == "disabled":
            self.wfile.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return

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

        if _NETWORK_POLICY == "allowlisted_hosts" and host.lower() not in _ALLOWED_HOSTS:
            _audit(host=host, addresses=[], method="CONNECT", outcome="blocked_by_allowlist")
            self.wfile.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return
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

        client_hello_data = bytearray()
        sni_verified = False

        self.connection.setblocking(False)
        start_time = time.monotonic()
        while time.monotonic() - start_time < 5.0 and len(client_hello_data) < 16384:
            try:
                chunk = self.connection.recv(4096)
                if not chunk:
                    break
                client_hello_data.extend(chunk)

                sni = _extract_sni(client_hello_data)
                if sni is False:
                    break
                elif isinstance(sni, str):
                    if sni == "ECH_PRESENT":
                        _audit(host=host, addresses=addresses, method="TLS", outcome="blocked_ech")
                        return
                    if sni.lower() != host.lower():
                        _audit(
                            host=host,
                            addresses=addresses,
                            method="TLS",
                            outcome="blocked_sni_mismatch",
                        )
                        return
                    sni_verified = True
                    break
            except BlockingIOError:
                time.sleep(0.01)
            except OSError:
                return

        if not sni_verified:
            _audit(host=host, addresses=addresses, method="TLS", outcome="blocked_no_valid_sni")
            return

        self.connection.setblocking(True)
        try:
            remote.sendall(client_hello_data)
        except OSError:
            return

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
