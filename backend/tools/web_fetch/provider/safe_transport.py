"""
Connection-level SSRF protection for the web fetch tool.

`HTTPFetchProvider.validate_url_and_ip` checks a URL before each request, but httpx
would resolve the hostname again when connecting. A DNS-rebinding host can answer the
first lookup with a public IP and the second with 127.0.0.1 / 169.254.169.254.

This module closes that gap by resolving and validating inside the connection itself
and connecting to the validated IP literal (TLS still uses the original hostname for
SNI and certificate verification). It also enforces the response byte budget while
data is being read off the socket, so oversized bodies are aborted mid-download
instead of being buffered fully in memory first.
"""

import asyncio
import ipaddress
import socket
import ssl
import typing
from typing import Callable, Optional

import httpcore
import httpx

from ..models import FetchSSRFError, FetchSizeLimitError

# Allowance for status line + headers on top of the body budget.
HEADER_ALLOWANCE_BYTES: int = 64 * 1024

NAT64_WELL_KNOWN_PREFIX = ipaddress.ip_network("64:ff9b::/96")


def check_ip_safety(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> None:
    """
    Raise FetchSSRFError unless `ip` is a publicly routable unicast address.

    Covers loopback, RFC1918, link-local (cloud metadata), multicast, reserved,
    unspecified, carrier-grade NAT (100.64.0.0/10) and any other non-global range,
    plus IPv6 forms that embed an IPv4 address (IPv4-mapped, 6to4, Teredo, NAT64).
    """
    if isinstance(ip, ipaddress.IPv6Address):
        # An IPv4-mapped address (::ffff:a.b.c.d) IS its embedded IPv4 address: the
        # kernel connects to the IPv4 host. Validate the inner address and stop there.
        # The outer IPv6 wrapper's own flags are not a property of the destination and
        # are not stable across Python versions -- ::ffff:0:0/96 was classified as
        # `is_reserved` before 3.11.10 / 3.12.6 and is not after, so checking the
        # wrapper would reject every public IPv4 host on an older interpreter.
        if ip.ipv4_mapped is not None:
            check_ip_safety(ip.ipv4_mapped)
            return

        embedded = []
        if ip.sixtofour is not None:
            embedded.append(ip.sixtofour)
        if ip.teredo is not None:
            embedded.extend(ip.teredo)
        if ip in NAT64_WELL_KNOWN_PREFIX:
            embedded.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        for inner in embedded:
            check_ip_safety(inner)

    checks = (
        ("loopback", ip.is_loopback),
        ("private", ip.is_private),
        ("link-local", ip.is_link_local),
        ("multicast", ip.is_multicast),
        ("reserved", ip.is_reserved),
        ("unspecified", ip.is_unspecified),
    )
    for label, flagged in checks:
        if flagged:
            raise FetchSSRFError(f"Access to {label} IP '{ip}' is forbidden (SSRF protection).")
    if not ip.is_global:
        raise FetchSSRFError(f"Access to non-public IP '{ip}' is forbidden (SSRF protection).")


class _BudgetedStream(httpcore.AsyncNetworkStream):
    """Network stream wrapper that aborts once more than `budget` bytes were read."""

    def __init__(self, inner: httpcore.AsyncNetworkStream, budget: int, counter: Optional[list] = None):
        self._inner = inner
        self._budget = budget
        self._counter = counter if counter is not None else [0]

    async def read(self, max_bytes: int, timeout: Optional[float] = None) -> bytes:
        data = await self._inner.read(max_bytes, timeout=timeout)
        self._counter[0] += len(data)
        if self._counter[0] > self._budget:
            await self._inner.aclose()
            raise FetchSizeLimitError(
                f"Response exceeded maximum limit of {self._budget - HEADER_ALLOWANCE_BYTES} bytes "
                "while downloading (aborted)."
            )
        return data

    async def write(self, buffer: bytes, timeout: Optional[float] = None) -> None:
        await self._inner.write(buffer, timeout=timeout)

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> httpcore.AsyncNetworkStream:
        tls = await self._inner.start_tls(ssl_context, server_hostname=server_hostname, timeout=timeout)
        return _BudgetedStream(tls, self._budget, self._counter)

    def get_extra_info(self, info: str) -> typing.Any:
        return self._inner.get_extra_info(info)


class SSRFSafeNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolves, validates and pins every outbound TCP connection."""

    def __init__(
        self,
        max_response_bytes: int,
        ip_validator: Callable[[typing.Any], None] = check_ip_safety,
        inner: Optional[httpcore.AsyncNetworkBackend] = None,
    ):
        self._inner = inner or httpcore.AnyIOBackend()
        self._budget = max_response_bytes + HEADER_ALLOWANCE_BYTES
        self._validate = ip_validator

    async def _resolve(self, host: str, port: int) -> str:
        try:
            literal = ipaddress.ip_address(host.strip("[]"))
            self._validate(literal)
            return str(literal)
        except ValueError:
            pass

        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
        except socket.gaierror as e:
            raise httpcore.ConnectError(f"DNS resolution failed for host '{host}': {e}") from e
        if not infos:
            raise httpcore.ConnectError(f"No IP addresses found for host '{host}'.")

        # Every answer must be safe; otherwise a mixed answer set could be abused.
        addresses = []
        for info in infos:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
            self._validate(ip)
            addresses.append(str(ip))
        return addresses[0]

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: Optional[float] = None,
        local_address: Optional[str] = None,
        socket_options: typing.Optional[typing.Iterable] = None,
    ) -> httpcore.AsyncNetworkStream:
        pinned_ip = await self._resolve(host, port)
        stream = await self._inner.connect_tcp(
            pinned_ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )
        return _BudgetedStream(stream, self._budget)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):  # pragma: no cover
        raise FetchSSRFError("Unix socket connections are not permitted.")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class SSRFSafeTransport(httpx.AsyncHTTPTransport):
    """httpx transport that routes all connections through SSRFSafeNetworkBackend."""

    def __init__(self, max_response_bytes: int, network_backend: Optional[httpcore.AsyncNetworkBackend] = None):
        # trust_env=False: never tunnel fetches through environment proxies, because the
        # proxy (not us) would resolve the target and bypass the IP checks.
        super().__init__(trust_env=False, http1=True, http2=False)
        ssl_context = httpx.create_ssl_context(trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            http1=True,
            http2=False,
            max_connections=4,
            network_backend=network_backend or SSRFSafeNetworkBackend(max_response_bytes),
        )


def build_safe_client(timeout: float, max_response_bytes: int, **transport_kwargs: typing.Any) -> httpx.AsyncClient:
    """Create the AsyncClient used for real (non-injected) fetches."""
    return httpx.AsyncClient(
        transport=SSRFSafeTransport(max_response_bytes, **transport_kwargs),
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    )
