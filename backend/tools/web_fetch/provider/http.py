import re
import time
import socket
import asyncio
import logging
import ipaddress
import urllib.parse
from typing import Optional, Tuple, Dict, Any, List, Set

import httpx

from .base import FetchProvider
from ..models import (
    FetchResult,
    FetchResponse,
    FetchError,
    FetchSSRFError,
    FetchSecurityError,
    FetchConnectionError,
    FetchTimeoutError,
    FetchResponseError,
    FetchContentError,
    FetchSizeLimitError,
)
from ..extractor import ContentExtractor, DEFAULT_MAX_CHARS
from .safe_transport import build_safe_client, check_ip_safety

logger = logging.getLogger("tora.web_fetch.http")

DEFAULT_FETCH_TIMEOUT: float = 10.0
MAX_FETCH_TIMEOUT: float = 30.0
MAX_REDIRECTS: int = 5
MAX_RESPONSE_BYTES: int = 2 * 1024 * 1024  # 2MB response limit

ALLOWED_SCHEMES: Set[str] = {"http", "https"}

ALLOWED_CONTENT_TYPES: Set[str] = {
    "text/html",
    "application/xhtml+xml",
}

BLOCKED_HOSTNAMES: Set[str] = {
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "::1",
    "169.254.169.254",       # AWS/GCP/Azure link-local instance metadata
    "metadata.google.internal",
    "instance-data",
}

DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36 (TORA/1.0; +https://spendsy.app)"
)


class HTTPFetchProvider(FetchProvider):
    """
    Standard HTTP/HTTPS Fetch Provider with comprehensive security controls:
    1. Strict scheme enforcement (HTTP/HTTPS only).
    2. Deep SSRF protection with async DNS resolution and IP address validation
       (rejects loopback, private, link-local, multicast, and reserved addresses).
    3. Step-by-step redirect inspection ensuring every redirect target is SSRF-safe.
    4. Bounded response streaming (2MB hard limit) to prevent memory exhaustion.
    5. Content-Type verification (HTML/XHTML only; rejects binaries, PDFs, images).
    6. Plain text extraction and whitespace normalization via ContentExtractor.
    """

    def __init__(
        self,
        timeout_seconds: float = DEFAULT_FETCH_TIMEOUT,
        user_agent: str = DEFAULT_USER_AGENT,
        client: Optional[httpx.AsyncClient] = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ):
        self.timeout_seconds = min(timeout_seconds, MAX_FETCH_TIMEOUT)
        self.user_agent = user_agent
        self._client = client
        self.max_response_bytes = max_response_bytes

    @property
    def name(self) -> str:
        return "http"

    async def validate_url_and_ip(self, url: str) -> Tuple[urllib.parse.ParseResult, str]:
        """
        Validate URL scheme, hostname, and resolve DNS to ensure target IP is not private or loopback.

        :param url: The URL to validate.
        :return: Tuple of (parsed_url, canonical_hostname).
        :raises FetchSSRFError: On scheme, hostname, or IP address security violations.
        :raises FetchConnectionError: On DNS resolution failure.
        """
        if not url or not isinstance(url, str) or not url.strip():
            raise FetchSSRFError("URL must be a non-empty string.")

        clean_url = url.strip()

        try:
            parsed = urllib.parse.urlparse(clean_url)
        except Exception as e:
            raise FetchSSRFError(f"Malformed URL '{clean_url}': {e}") from e

        # 1. Scheme Check
        scheme = parsed.scheme.lower()
        if scheme not in ALLOWED_SCHEMES:
            raise FetchSSRFError(
                f"Unsupported URL scheme '{parsed.scheme}'. Only 'http' and 'https' are permitted."
            )

        hostname = parsed.hostname
        if not hostname:
            raise FetchSSRFError("URL must include a valid hostname.")

        hostname_clean = hostname.strip().lower()

        # 2. Hostname Blacklist Check
        if hostname_clean in BLOCKED_HOSTNAMES or hostname_clean.endswith(".localhost"):
            raise FetchSSRFError(f"Access to blocked hostname '{hostname}' is forbidden (SSRF protection).")

        # 3. Direct IP Address Check (if hostname is an IP literal)
        try:
            ip = ipaddress.ip_address(hostname_clean)
            self._check_ip_safety(ip)
            return parsed, hostname_clean
        except ValueError:
            # Not an IP literal — proceed to DNS resolution
            pass

        # 4. Asynchronous DNS Resolution & IP Check
        try:
            port = parsed.port or (443 if scheme == "https" else 80)
        except ValueError as e:
            raise FetchSSRFError(f"Invalid port in URL '{clean_url}': {e}") from e
        try:
            loop = asyncio.get_running_loop()
            addrinfo = await loop.getaddrinfo(
                hostname_clean,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as e:
            raise FetchConnectionError(f"DNS resolution failed for host '{hostname}': {e}") from e
        except Exception as e:
            raise FetchConnectionError(f"Could not resolve host '{hostname}': {e}") from e

        if not addrinfo:
            raise FetchConnectionError(f"No IP addresses found for host '{hostname}'.")

        # Check every resolved IP address
        for addr in addrinfo:
            ip_str = addr[4][0]
            try:
                ip = ipaddress.ip_address(ip_str)
                self._check_ip_safety(ip)
            except ValueError:
                raise FetchSSRFError(f"Invalid resolved IP address '{ip_str}' for host '{hostname}'.")

        return parsed, hostname_clean

    def _check_ip_safety(self, ip: ipaddress._BaseAddress) -> None:
        """
        Verify that an IP address is public and safe to contact.
        Rejects loopback, private, link-local, multicast, reserved, unspecified,
        carrier-grade NAT and IPv6 forms embedding unsafe IPv4 addresses.
        """
        check_ip_safety(ip)

    def _validate_content_type(self, content_type_header: Optional[str]) -> str:
        """
        Verify that the HTTP response Content-Type is supported HTML/XHTML.
        """
        if not content_type_header:
            return "text/html"

        # Content-Type may be formatted as 'text/html; charset=UTF-8'
        raw_mime = content_type_header.split(";")[0].strip().lower()

        if raw_mime in ALLOWED_CONTENT_TYPES:
            return raw_mime

        # If MIME is completely outside supported types (e.g. PDF, image, octet-stream, audio)
        raise FetchContentError(
            f"Unsupported content type '{raw_mime}'. Only HTML and XHTML pages can be read."
        )

    async def fetch(
        self,
        url: str,
        max_chars: int = DEFAULT_MAX_CHARS,
        timeout_seconds: Optional[float] = None,
        **kwargs: Any,
    ) -> FetchResponse:
        """
        Execute safe HTTP fetch with redirect tracking, SSRF validation, and content extraction.
        """
        effective_timeout = min(
            timeout_seconds or self.timeout_seconds,
            MAX_FETCH_TIMEOUT,
        )

        current_url = url.strip()
        redirect_count = 0
        start_time = time.monotonic()

        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            # Avoid compressed bodies so the on-the-wire byte budget also bounds
            # the decoded size (mitigates decompression bombs).
            "Accept-Encoding": "identity",
        }

        # Step 1: Initial SSRF Validation
        await self.validate_url_and_ip(current_url)

        # Step 2: Request Execution with Manual Redirect Loop
        try:
            client_to_use = self._client
            owns_client = False
            if client_to_use is None:
                # Connections are re-resolved, validated and pinned per connect
                # (DNS-rebinding safe) and the byte budget is enforced on the wire.
                client_to_use = build_safe_client(
                    timeout=effective_timeout,
                    max_response_bytes=self.max_response_bytes,
                )
                owns_client = True

            try:
                while True:
                    logger.info("Fetching URL (redirect %d): %s", redirect_count, current_url)

                    # Send request (do NOT follow redirects automatically)
                    response = await client_to_use.get(
                        current_url,
                        headers=headers,
                        follow_redirects=False,
                    )

                    # Handle Redirects (3xx)
                    if response.status_code in (301, 302, 303, 307, 308):
                        redirect_count += 1
                        if redirect_count > MAX_REDIRECTS:
                            raise FetchSecurityError(
                                f"Too many redirects (exceeded limit of {MAX_REDIRECTS})."
                            )

                        location = response.headers.get("Location")
                        if not location:
                            raise FetchResponseError(
                                message=f"Redirect status {response.status_code} received without Location header.",
                                status_code=response.status_code,
                            )

                        # Resolve relative redirect URLs safely
                        next_url = urllib.parse.urljoin(current_url, location)

                        # SSRF-validate the redirect destination before proceeding
                        await self.validate_url_and_ip(next_url)
                        current_url = next_url
                        continue

                    # Handle HTTP Errors (4xx, 5xx)
                    if response.status_code != 200:
                        raise FetchResponseError(
                            message=f"HTTP request to '{current_url}' failed with status {response.status_code}.",
                            status_code=response.status_code,
                            detail=response.text[:300] if response.text else None,
                        )

                    # Check Content-Type
                    content_type = self._validate_content_type(response.headers.get("Content-Type"))

                    # Check Content-Length if present
                    content_length_str = response.headers.get("Content-Length")
                    if content_length_str:
                        try:
                            content_length = int(content_length_str)
                            if content_length > self.max_response_bytes:
                                raise FetchSizeLimitError(
                                    f"Response size ({content_length} bytes) exceeds maximum limit of {self.max_response_bytes} bytes."
                                )
                        except ValueError:
                            pass

                    # Read body with size bounding
                    raw_bytes = response.content
                    if len(raw_bytes) > self.max_response_bytes:
                        raise FetchSizeLimitError(
                            f"Response body ({len(raw_bytes)} bytes) exceeds maximum limit of {self.max_response_bytes} bytes."
                        )

                    # Decode HTML text
                    encoding = response.encoding or "utf-8"
                    try:
                        html_text = raw_bytes.decode(encoding, errors="replace")
                    except Exception:
                        html_text = raw_bytes.decode("utf-8", errors="replace")

                    break

            finally:
                if owns_client:
                    await client_to_use.aclose()

        except httpx.TimeoutException as e:
            duration_ms = round((time.monotonic() - start_time) * 1000, 2)
            raise FetchTimeoutError(
                f"Web fetch request to '{url}' timed out after {effective_timeout} seconds."
            ) from e

        except httpx.ConnectError as e:
            raise FetchConnectionError(
                f"Could not establish connection to '{current_url}': {e}"
            ) from e

        except (FetchError, ValueError):
            raise

        except Exception as e:
            raise FetchError(f"Unexpected error fetching '{current_url}': {str(e)}") from e

        # Step 3: Extract Clean Plain Text
        title, clean_content, truncated = ContentExtractor.extract(
            raw_html=html_text,
            max_chars=max_chars,
        )

        duration_ms = round((time.monotonic() - start_time) * 1000, 2)
        parsed_final = urllib.parse.urlparse(current_url)
        domain = parsed_final.netloc

        result = FetchResult(
            url=url,
            final_url=current_url,
            domain=domain,
            title=title,
            content=clean_content,
            content_type=content_type,
            status_code=response.status_code,
            truncated=truncated,
            metadata={
                "duration_ms": duration_ms,
                "redirects": redirect_count,
                "raw_bytes": len(raw_bytes),
            },
        )

        return FetchResponse(
            url=url,
            result=result,
            provider=self.name,
            metadata={"duration_ms": duration_ms},
        )
