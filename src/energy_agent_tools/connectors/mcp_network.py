"""Pinned, origin-scoped HTTP transport for approved MCP endpoints."""

from __future__ import annotations

import asyncio
import ipaddress
import math
import socket
import unicodedata
from collections.abc import AsyncIterable, AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit
from weakref import WeakSet

import httpcore
import httpx

_MAX_URL_BYTES = 2048
_MAX_DNS_ADDRESSES = 16
_DNS_TIMEOUT_SECONDS = 5.0
MAX_MCP_RESPONSE_BYTES = 32 * 1024 * 1024
_APPROVAL_SEAL = object()
_SAFE_REQUEST_EXTENSIONS = frozenset({"timeout", "trace"})
_SAFE_RESPONSE_EXTENSIONS = frozenset({"http_version", "reason_phrase", "trailers"})
_RESPONSE_LIMIT_ERROR = "MCP response exceeds the configured byte limit."
_RESPONSE_ENCODING_ERROR = "MCP response content encoding is unsupported."
_RESPONSE_LENGTH_ERROR = "MCP response Content-Length is invalid."
_NEVER_ROUTABLE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/23",
        "2001:db8::/32",
        "2002::/16",
        "3fff::/20",
        "fec0::/10",
    )
)


class MCPTargetError(ValueError):
    """The configured MCP endpoint cannot be safely approved."""


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class ApprovedMCPTarget:
    """A validated MCP origin with its DNS answers pinned at approval time."""

    url: str = field(repr=False)
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...]
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _APPROVAL_SEAL:
            raise ValueError("Approved MCP targets must be created by approve_mcp_target().")


_APPROVED_TARGETS: WeakSet[ApprovedMCPTarget] = WeakSet()


def _canonical_host(host: str) -> str:
    if "%" in host:
        raise MCPTargetError("The MCP target hostname is invalid.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            normalized = host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise MCPTargetError("The MCP target hostname is invalid.") from exc
        if (
            not normalized
            or len(normalized) > 253
            or any(not label or len(label) > 63 for label in normalized.rstrip(".").split("."))
            or any(
                label.startswith("-")
                or label.endswith("-")
                or any(not (character.isalnum() or character == "-") for character in label)
                for label in normalized.rstrip(".").split(".")
            )
        ):
            raise MCPTargetError("The MCP target hostname is invalid.") from None
        return normalized
    return address.compressed.lower()


def _parse_target_url(url: str, *, allow_private: bool) -> tuple[str, str, int, str]:
    if not isinstance(url, str) or not url:
        raise MCPTargetError("Provide a valid absolute MCP URL.")
    try:
        encoded = url.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise MCPTargetError("The MCP URL contains invalid characters.") from exc
    if len(encoded) > _MAX_URL_BYTES:
        raise MCPTargetError("The MCP URL is too long.")
    if any(character.isspace() or unicodedata.category(character) == "Cc" for character in url):
        raise MCPTargetError("The MCP URL contains invalid characters.")
    if "\\" in url:
        raise MCPTargetError("The MCP URL contains invalid characters.")
    if "?" in url or "#" in url:
        raise MCPTargetError("MCP URLs must not contain a query or fragment.")

    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme != "https" and not (allow_private and scheme == "http"):
            raise MCPTargetError("MCP targets must use HTTPS unless private access is approved.")
        if not parts.netloc or "@" in parts.netloc or parts.username is not None:
            raise MCPTargetError("MCP URLs must not contain user information.")
        if parts.netloc.endswith(":"):
            raise MCPTargetError("The MCP target port is invalid.")
        raw_host = parts.hostname
        if raw_host is None:
            raise MCPTargetError("The MCP target hostname is missing.")
        host = _canonical_host(raw_host)
        explicit_port = parts.port
    except ValueError as exc:
        raise MCPTargetError("The MCP URL is invalid.") from exc

    default_port = 443 if scheme == "https" else 80
    port = default_port if explicit_port is None else explicit_port
    if port < 1 or port > 65535:
        raise MCPTargetError("The MCP target port is unsupported.")

    try:
        parsed_ip = ipaddress.ip_address(host)
    except ValueError:
        netloc_host = host
    else:
        netloc_host = f"[{host}]" if parsed_ip.version == 6 else host
    netloc = netloc_host if explicit_port is None else f"{netloc_host}:{port}"
    canonical_url = urlunsplit((scheme, netloc, parts.path or "/", "", ""))
    return scheme, host, port, canonical_url


def _approved_ip(value: str, *, allow_private: bool) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise MCPTargetError("The MCP target resolved to an invalid address.") from exc
    if isinstance(address, ipaddress.IPv6Address):
        if address.scope_id is not None:
            raise MCPTargetError("Scoped IPv6 MCP targets are not supported.")
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped

    if (
        address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or any(
            address.version == network.version and address in network
            for network in _NEVER_ROUTABLE_NETWORKS
        )
    ):
        raise MCPTargetError("The MCP target resolved to a prohibited address.")
    if not allow_private and (
        not address.is_global or address.is_private or address.is_loopback or address.is_link_local
    ):
        raise MCPTargetError("MCP targets must resolve only to globally routable addresses.")
    return address.compressed.lower()


async def approve_mcp_target(url: str, *, allow_private: bool = False) -> ApprovedMCPTarget:
    """Validate an MCP URL and pin its complete DNS answer set for later requests."""
    if type(allow_private) is not bool:
        raise MCPTargetError("Private MCP approval must be an explicit boolean.")
    scheme, host, port, canonical_url = _parse_target_url(url, allow_private=allow_private)
    try:
        async with asyncio.timeout(_DNS_TIMEOUT_SECONDS):
            answers = await asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
            )
    except (OSError, TimeoutError, UnicodeError) as exc:
        raise MCPTargetError("The MCP target could not be resolved.") from exc
    if not answers:
        raise MCPTargetError("The MCP target did not resolve to an address.")
    if len(answers) > _MAX_DNS_ADDRESSES:
        raise MCPTargetError("The MCP target resolved to too many addresses.")

    addresses: list[str] = []
    seen: set[str] = set()
    for answer in answers:
        try:
            raw_address = answer[4][0]
            if not isinstance(raw_address, str):
                raise TypeError("getaddrinfo returned a non-text address")
            address = _approved_ip(raw_address, allow_private=allow_private)
        except (IndexError, TypeError) as exc:
            raise MCPTargetError("The MCP target resolved to an invalid address.") from exc
        if address not in seen:
            addresses.append(address)
            seen.add(address)

    target = ApprovedMCPTarget(
        url=canonical_url,
        scheme=scheme,
        host=host,
        port=port,
        addresses=tuple(addresses),
        _seal=_APPROVAL_SEAL,
    )
    _APPROVED_TARGETS.add(target)
    return target


def _request_origin(request: httpx.Request) -> tuple[str, str, int]:
    try:
        host = _canonical_host(request.url.host)
        port = request.url.port
    except (MCPTargetError, ValueError) as exc:
        raise httpx.InvalidURL("The request URL is invalid.") from exc
    if port is None:
        port = 443 if request.url.scheme == "https" else 80
    return request.url.scheme.lower(), host, port


def _map_httpcore_error(exc: Exception, request: httpx.Request) -> httpx.RequestError | None:
    mappings: tuple[tuple[type[Exception], type[httpx.RequestError], str], ...] = (
        (httpcore.ConnectTimeout, httpx.ConnectTimeout, "MCP target connection timed out."),
        (httpcore.ReadTimeout, httpx.ReadTimeout, "MCP target read timed out."),
        (httpcore.WriteTimeout, httpx.WriteTimeout, "MCP target write timed out."),
        (httpcore.PoolTimeout, httpx.PoolTimeout, "MCP target connection timed out."),
        (
            httpcore.ConnectError,
            httpx.ConnectError,
            "Unable to connect to the approved MCP target.",
        ),
        (httpcore.ReadError, httpx.ReadError, "Unable to read from the approved MCP target."),
        (httpcore.WriteError, httpx.WriteError, "Unable to write to the approved MCP target."),
        (httpcore.LocalProtocolError, httpx.LocalProtocolError, "MCP request protocol failed."),
        (httpcore.RemoteProtocolError, httpx.RemoteProtocolError, "MCP response protocol failed."),
        (httpcore.ProtocolError, httpx.ProtocolError, "MCP protocol failed."),
        (httpcore.ProxyError, httpx.ProxyError, "MCP proxy transport failed."),
        (httpcore.UnsupportedProtocol, httpx.UnsupportedProtocol, "MCP protocol is unsupported."),
        (httpcore.ConnectionNotAvailable, httpx.ConnectError, "MCP connection is unavailable."),
        (httpcore.NetworkError, httpx.NetworkError, "MCP network request failed."),
        (httpcore.TimeoutException, httpx.TimeoutException, "MCP request timed out."),
    )
    for core_type, httpx_type, message in mappings:
        if isinstance(exc, core_type):
            return httpx_type(message, request=request)
    return None


def _response_rejection(response: httpcore.Response) -> str | None:
    for raw_value in _response_header_values(response, b"content-encoding"):
        try:
            encodings = raw_value.decode("ascii").split(",")
        except UnicodeDecodeError:
            return _RESPONSE_ENCODING_ERROR
        if any(value.strip() and value.strip().lower() != "identity" for value in encodings):
            return _RESPONSE_ENCODING_ERROR

    for raw_value in _response_header_values(response, b"content-length"):
        try:
            declared_lengths = raw_value.decode("ascii").split(",")
        except UnicodeDecodeError:
            return _RESPONSE_LENGTH_ERROR
        for declared_length in declared_lengths:
            value = declared_length.strip()
            if not value or not value.isdecimal():
                return _RESPONSE_LENGTH_ERROR
            normalized = value.lstrip("0") or "0"
            limit = str(MAX_MCP_RESPONSE_BYTES)
            if len(normalized) > len(limit) or (
                len(normalized) == len(limit) and normalized > limit
            ):
                return _RESPONSE_LIMIT_ERROR
    return None


def _response_header_values(response: httpcore.Response, name: bytes) -> list[bytes]:
    return [value for header, value in response.headers if header.lower() == name]


async def _close_rejected_response(response: httpcore.Response) -> None:
    try:
        await response.aclose()
    except Exception:
        pass


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(
        self,
        target: ApprovedMCPTarget,
        backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._target = target
        self._backend = backend or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            requested_host = _canonical_host(host)
        except MCPTargetError as exc:
            raise httpcore.ConnectError("Request did not match the approved MCP origin.") from exc
        if (requested_host, port) != (self._target.host, self._target.port):
            raise httpcore.ConnectError("Request did not match the approved MCP origin.")

        last_error: httpcore.ConnectError | None = None
        for address in self._target.addresses:
            try:
                return await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except httpcore.ConnectError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("The approved MCP target has no pinned addresses.")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("Unix-domain MCP transports are not supported.")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _HTTPcoreResponseStream(httpx.AsyncByteStream):
    def __init__(self, response: httpcore.Response, request: httpx.Request) -> None:
        self._response = response
        self._request = request
        self._received_bytes = 0
        self._closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in cast(AsyncIterable[bytes], self._response.stream):
                self._received_bytes += len(chunk)
                if self._received_bytes > MAX_MCP_RESPONSE_BYTES:
                    try:
                        await self.aclose()
                    except Exception:
                        pass
                    raise httpx.RequestError(_RESPONSE_LIMIT_ERROR, request=self._request)
                yield chunk
        except Exception as exc:
            mapped = _map_httpcore_error(exc, self._request)
            if mapped is None:
                raise
            raise mapped from exc

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._response.aclose()
        except Exception as exc:
            mapped = _map_httpcore_error(exc, self._request)
            if mapped is None:
                raise
            raise mapped from exc


class _ApprovedHTTPTransport(httpx.AsyncBaseTransport):
    def __init__(self, target: ApprovedMCPTarget) -> None:
        self._target = target
        self._pool = httpcore.AsyncConnectionPool(
            network_backend=_PinnedNetworkBackend(target),
            max_connections=10,
            max_keepalive_connections=10,
            retries=0,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if _request_origin(request) != (
            self._target.scheme,
            self._target.host,
            self._target.port,
        ):
            raise httpx.InvalidURL("Request origin does not match the approved MCP target.")
        expected_host = httpx.URL(self._target.url).netloc.decode("ascii")
        if request.headers.get("host", "").lower() != expected_host.lower():
            raise httpx.InvalidURL("Request Host does not match the approved MCP target.")

        extensions = {
            name: value
            for name, value in request.extensions.items()
            if name in _SAFE_REQUEST_EXTENSIONS
        }
        core_request = httpcore.Request(
            method=request.method,
            url=str(request.url),
            headers=request.headers.raw,
            content=request.stream,
            extensions=extensions,
        )
        try:
            response = await self._pool.handle_async_request(core_request)
        except Exception as exc:
            mapped = _map_httpcore_error(exc, request)
            if mapped is None:
                raise
            raise mapped from exc

        rejection = _response_rejection(response)
        if rejection is not None:
            await _close_rejected_response(response)
            raise httpx.RequestError(rejection, request=request)

        response_extensions = {
            name: value
            for name, value in response.extensions.items()
            if name in _SAFE_RESPONSE_EXTENSIONS
        }
        return httpx.Response(
            response.status,
            headers=response.headers,
            stream=_HTTPcoreResponseStream(response, request),
            extensions=response_extensions,
            request=request,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


def approved_mcp_client(target: ApprovedMCPTarget, *, timeout: float = 30.0) -> httpx.AsyncClient:
    """Create an HTTPX client restricted to one previously approved MCP origin."""
    if (
        not isinstance(target, ApprovedMCPTarget)
        or target._seal is not _APPROVAL_SEAL
        or target not in _APPROVED_TARGETS
    ):
        raise ValueError("Use an ApprovedMCPTarget returned by approve_mcp_target().")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
        raise ValueError("The MCP request timeout must be a positive finite number.")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("The MCP request timeout must be a positive finite number.")

    return httpx.AsyncClient(
        base_url=target.url,
        transport=_ApprovedHTTPTransport(target),
        timeout=float(timeout),
        headers={"Accept-Encoding": "identity"},
        follow_redirects=False,
        trust_env=False,
    )
