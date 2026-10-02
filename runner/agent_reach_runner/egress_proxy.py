"""Allowlisted HTTPS CONNECT egress for the isolated runner container."""

from __future__ import annotations

import ipaddress
import select
import socket
import socketserver
import threading
import time

ALLOWED_HOST = "www.googleapis.com"
ALLOWED_PORT = 443
MAX_HEADER_BYTES = 8192
MAX_TUNNEL_SECONDS = 45
MAX_IDLE_SECONDS = 20


def _public_addresses(host: str, port: int) -> list[tuple[int, int, int, tuple]]:
    if host.lower() != ALLOWED_HOST or port != ALLOWED_PORT:
        raise ValueError("egress destination is not approved")
    results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses: list[tuple[int, int, int, tuple]] = []
    for family, socktype, proto, _, sockaddr in results:
        address = ipaddress.ip_address(sockaddr[0])
        if address.is_global:
            addresses.append((family, socktype, proto, sockaddr))
    if not addresses:
        raise OSError("approved host did not resolve to a public address")
    return addresses


def _parse_connect(header: bytes) -> bool:
    try:
        lines = header.decode("ascii", errors="strict").split("\r\n")
    except UnicodeDecodeError:
        return False
    if not lines or lines[0].split() != ["CONNECT", f"{ALLOWED_HOST}:{ALLOWED_PORT}", "HTTP/1.1"]:
        return False
    for line in lines[1:]:
        if not line:
            continue
        name, separator, value = line.partition(":")
        if not separator:
            return False
        if (
            name.strip().lower() == "host"
            and value.strip().lower() != f"{ALLOWED_HOST}:{ALLOWED_PORT}"
        ):
            return False
    return True


class _BoundedThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, *args, **kwargs) -> None:
        self._slots = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class _ConnectHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        client = self.request
        client.settimeout(5)
        request = bytearray()
        try:
            while b"\r\n\r\n" not in request:
                chunk = client.recv(1024)
                if not chunk:
                    return
                request.extend(chunk)
                if len(request) > MAX_HEADER_BYTES:
                    client.sendall(
                        b"HTTP/1.1 431 Request Header Fields Too Large\r\nConnection: close\r\n\r\n"
                    )
                    return
            header, separator, early_data = bytes(request).partition(b"\r\n\r\n")
            if not separator or not _parse_connect(header):
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                return
            upstream = self._connect_public()
        except (OSError, ValueError):
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            return
        try:
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if early_data:
                upstream.sendall(early_data)
            self._tunnel(client, upstream)
        except OSError:
            return
        finally:
            upstream.close()

    @staticmethod
    def _connect_public() -> socket.socket:
        last_error: OSError | None = None
        for family, socktype, proto, sockaddr in _public_addresses(ALLOWED_HOST, ALLOWED_PORT):
            upstream = socket.socket(family, socktype, proto)
            upstream.settimeout(5)
            try:
                # Dial the already-validated address to avoid a second DNS lookup/rebinding window.
                upstream.connect(sockaddr)
                upstream.settimeout(None)
                return upstream
            except OSError as exc:
                last_error = exc
                upstream.close()
        raise last_error or OSError("approved host could not be reached")

    @staticmethod
    def _tunnel(client: socket.socket, upstream: socket.socket) -> None:
        end = time.monotonic() + MAX_TUNNEL_SECONDS
        peers = (client, upstream)
        while time.monotonic() < end:
            readable, _, _ = select.select(peers, [], [], MAX_IDLE_SECONDS)
            if not readable:
                return
            for source in readable:
                data = source.recv(65_536)
                if not data:
                    destination = upstream if source is client else client
                    try:
                        destination.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    return
                destination = upstream if source is client else client
                destination.sendall(data)


def main() -> None:
    server = _BoundedThreadingTCPServer(("0.0.0.0", 3128), _ConnectHandler)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
