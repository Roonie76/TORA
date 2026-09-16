"""
Connection-level SSRF tests (Phase 2H-C re-audit):
- DNS rebinding: a host that validates as public but resolves to loopback at connect time
- IP classes missed by the original checks (CGNAT, IPv4-mapped / NAT64 / 6to4 IPv6)
- response byte budget enforced while downloading, not after buffering
"""
import asyncio
import ipaddress
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, patch

import httpcore
import pytest

from backend.tools.web_fetch.models import FetchSSRFError, FetchSizeLimitError, FetchError
from backend.tools.web_fetch.provider.http import HTTPFetchProvider
from backend.tools.web_fetch.provider.safe_transport import (
    SSRFSafeNetworkBackend,
    build_safe_client,
    check_ip_safety,
)


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254",
        "100.64.0.1", "100.127.255.254", "0.0.0.0", "224.0.0.1", "240.0.0.1",
        "::1", "fd00::1", "fe80::1", "::ffff:127.0.0.1", "::ffff:169.254.169.254",
        "64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe", "2002:7f00:1::", "198.18.0.1",
    ],
)
def test_non_public_addresses_rejected(addr):
    with pytest.raises(FetchSSRFError):
        check_ip_safety(ipaddress.ip_address(addr))


@pytest.mark.parametrize("addr", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_public_addresses_allowed(addr):
    check_ip_safety(ipaddress.ip_address(addr))


def test_provider_uses_shared_ip_policy():
    provider = HTTPFetchProvider()
    with pytest.raises(FetchSSRFError):
        provider._check_ip_safety(ipaddress.ip_address("100.64.0.1"))


class _Handler(BaseHTTPRequestHandler):
    body = b"<html><head><title>Local</title></head><body><p>ok</p></body></html>"

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        if self.path == "/huge":
            # No Content-Length: server streams until the client gives up.
            self.end_headers()
            try:
                chunk = b"A" * 65536
                for _ in range(200):  # ~12.8 MB
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


@pytest.fixture
def local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()


def _fake_getaddrinfo(ip):
    async def _inner(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
    return _inner


def test_dns_rebinding_blocked_at_connect_time(local_server):
    """validate_url_and_ip passes (first answer public) but the connect-time answer is loopback."""
    provider = HTTPFetchProvider()

    async def scenario():
        loop = asyncio.get_running_loop()
        with patch.object(provider, "validate_url_and_ip", new_callable=AsyncMock), \
             patch.object(loop, "getaddrinfo", side_effect=_fake_getaddrinfo("127.0.0.1")):
            await provider.fetch(f"http://rebind.attacker.test:{local_server}/")

    with pytest.raises(FetchSSRFError):
        asyncio.run(scenario())


def test_pinned_connection_reaches_validated_ip(local_server):
    """With a permissive validator the backend connects to the resolved IP, not the hostname."""
    backend = SSRFSafeNetworkBackend(max_response_bytes=1024 * 1024, ip_validator=lambda ip: None)

    async def scenario():
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", side_effect=_fake_getaddrinfo("127.0.0.1")):
            async with build_safe_client(timeout=5, max_response_bytes=1024 * 1024, network_backend=backend) as c:
                r = await c.get(f"http://unresolvable.invalid:{local_server}/")
                return r.status_code, r.text

    status, text = asyncio.run(scenario())
    assert status == 200 and "Local" in text


def test_body_budget_enforced_while_streaming(local_server):
    budget = 256 * 1024
    backend = SSRFSafeNetworkBackend(max_response_bytes=budget, ip_validator=lambda ip: None)

    async def scenario():
        async with build_safe_client(timeout=10, max_response_bytes=budget, network_backend=backend) as c:
            await c.get(f"http://127.0.0.1:{local_server}/huge")

    with pytest.raises(FetchSizeLimitError):
        asyncio.run(scenario())


def test_ip_literal_to_private_blocked_by_backend():
    backend = SSRFSafeNetworkBackend(max_response_bytes=1024)

    async def scenario():
        await backend.connect_tcp("169.254.169.254", 80)

    with pytest.raises(FetchSSRFError):
        asyncio.run(scenario())


def test_invalid_port_is_ssrf_error():
    provider = HTTPFetchProvider()
    with pytest.raises(FetchSSRFError):
        asyncio.run(provider.validate_url_and_ip("http://example.com:99999/"))


def test_size_limit_error_is_fetch_error():
    assert issubclass(FetchSizeLimitError, FetchError)
