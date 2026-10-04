from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
from dataclasses import FrozenInstanceError, replace
from typing import Any

import httpcore
import httpx
import pytest

import energy_agent_tools.connectors.mcp_network as mcp_network
from energy_agent_tools.connectors.mcp_network import (
    ApprovedMCPTarget,
    MCPTargetError,
    approve_mcp_target,
    approved_mcp_client,
)


def _addrinfo(address: str, port: int) -> tuple[Any, ...]:
    parsed = ipaddress.ip_address(address)
    if parsed.version == 4:
        sockaddr: tuple[Any, ...] = (str(parsed), port)
        family = socket.AF_INET
    else:
        sockaddr = (str(parsed), port, 0, 0)
        family = socket.AF_INET6
    return family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr


async def _set_dns(monkeypatch, addresses: list[str]):
    loop = asyncio.get_running_loop()
    calls: list[tuple[str, int]] = []

    async def getaddrinfo(host: str, port: int, *, type: int, proto: int):
        calls.append((host, port))
        return [_addrinfo(address, port) for address in addresses]

    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.2.3.4",
        "169.254.169.254",
        "224.0.0.1",
        "240.0.0.1",
        "255.255.255.255",
        "192.0.0.9",
        "198.18.0.1",
        "::1",
        "fe80::1",
        "fec0::1",
        "ff02::1",
        "::ffff:192.168.1.8",
        "64:ff9b::c0a8:1",
        "64:ff9b:1::c0a8:1",
        "2002:c0a8:101::",
        "2001:db8::1",
    ],
)
async def test_default_approval_rejects_non_global_and_special_addresses(monkeypatch, address: str):
    calls = await _set_dns(monkeypatch, [address])

    with pytest.raises(MCPTargetError):
        await approve_mcp_target("https://mcp.example/mcp")

    assert calls == [("mcp.example", 443)]


@pytest.mark.asyncio
async def test_default_approval_rejects_mixed_public_and_private_dns(monkeypatch):
    calls = await _set_dns(monkeypatch, ["9.9.9.9", "192.168.1.4"])

    with pytest.raises(MCPTargetError, match="globally routable"):
        await approve_mcp_target("https://mcp.example/mcp")

    assert calls == [("mcp.example", 443)]


@pytest.mark.asyncio
async def test_default_approval_accepts_only_public_answers_and_pins_them(monkeypatch):
    calls = await _set_dns(monkeypatch, ["9.9.9.9", "2001:4860:4860::8888"])

    target = await approve_mcp_target("HTTPS://MCP.EXAMPLE:443/mcp")

    assert isinstance(target, ApprovedMCPTarget)
    assert target.url == "https://mcp.example:443/mcp"
    assert target.host == "mcp.example"
    assert target.port == 443
    assert target.addresses == ("9.9.9.9", "2001:4860:4860::8888")
    assert calls == [("mcp.example", 443)]
    with pytest.raises(FrozenInstanceError):
        target.port = 444  # type: ignore[misc]
    with pytest.raises(ValueError, match="approve_mcp_target"):
        approved_mcp_client(replace(target, addresses=("127.0.0.1",)))


@pytest.mark.asyncio
async def test_dns_resolution_has_a_fixed_deadline(monkeypatch):
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(mcp_network, "_DNS_TIMEOUT_SECONDS", 0.01)

    async def slow_getaddrinfo(host: str, port: int, *, type: int, proto: int):
        await asyncio.sleep(1)
        return [_addrinfo("9.9.9.9", port)]

    monkeypatch.setattr(loop, "getaddrinfo", slow_getaddrinfo)

    with pytest.raises(MCPTargetError, match="could not be resolved"):
        await approve_mcp_target("https://mcp.example/mcp")


@pytest.mark.asyncio
async def test_dns_answer_count_is_capped(monkeypatch):
    calls = await _set_dns(
        monkeypatch,
        [f"9.9.9.{index}" for index in range(1, mcp_network._MAX_DNS_ADDRESSES + 2)],
    )

    with pytest.raises(MCPTargetError, match="too many addresses"):
        await approve_mcp_target("https://mcp.example/mcp")

    assert calls == [("mcp.example", 443)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://mcp.example/mcp",
        "ftp://mcp.example/mcp",
        "https://user:secret@mcp.example/mcp",
        "https://@mcp.example/mcp",
        "https://mcp.example/mcp?token=secret",
        "https://mcp.example/mcp?",
        "https://mcp.example/mcp#fragment",
        "https://mcp.example/mcp#",
        "https://mcp.example:0/mcp",
        "https://mcp.example:65536/mcp",
        "https://mcp.example:notaport/mcp",
        "https://[fe80::1%25en0]/mcp",
        "https://mcp.example/mcp\r\nHost: attacker.example",
        "https://mcp.example/" + "a" * 2048,
    ],
)
async def test_url_policy_rejects_unsafe_or_unsupported_urls(monkeypatch, url: str):
    calls = await _set_dns(monkeypatch, ["9.9.9.9"])

    with pytest.raises(MCPTargetError):
        await approve_mcp_target(url)

    assert calls == []


@pytest.mark.asyncio
async def test_private_http_approval_reaches_owned_local_http_fixture(monkeypatch):
    requests: list[tuple[str, dict[str, str], bytes]] = []
    connection_closed = asyncio.Event()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = (await reader.readline()).decode("ascii").rstrip("\r\n")
        headers: dict[str, str] = {}
        while row := await reader.readline():
            if row in (b"\r\n", b"\n"):
                break
            name, value = row.decode("latin-1").split(":", maxsplit=1)
            headers[name.lower()] = value.strip()
        body = await reader.readexactly(int(headers.get("content-length", "0")))
        requests.append((line, headers, body))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: keep-alive\r\n\r\nok")
        await writer.drain()
        await reader.read()
        connection_closed.set()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/mcp"
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    try:
        with pytest.raises(MCPTargetError):
            await approve_mcp_target(url)

        target = await approve_mcp_target(url, allow_private=True)
        assert target.addresses == ("127.0.0.1",)
        async with approved_mcp_client(target) as client:
            response = await client.post(target.url, json={"jsonrpc": "2.0"})
            assert response.status_code == 200
            assert response.text == "ok"
        assert client.is_closed
        assert len(requests) == 1
        request_line, headers, body = requests[0]
        assert request_line == "POST /mcp HTTP/1.1"
        assert headers["host"] == f"127.0.0.1:{port}"
        assert headers["content-type"] == "application/json"
        assert body == b'{"jsonrpc":"2.0"}'
        await asyncio.wait_for(connection_closed.wait(), timeout=1)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_foreign_request_origin_and_redirect_are_blocked_before_dial():
    primary_requests: list[str] = []
    foreign_requests: list[str] = []

    async def serve(requests: list[str], response: bytes):
        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            requests.append((await reader.readline()).decode("ascii").rstrip("\r\n"))
            while row := await reader.readline():
                if row in (b"\r\n", b"\n"):
                    break
            writer.write(response)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        return server, server.sockets[0].getsockname()[1]

    foreign_server, foreign_port = await serve(
        foreign_requests,
        b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
    )
    foreign_url = f"http://127.0.0.1:{foreign_port}/alternate"
    redirect_server, redirect_port = await serve(
        primary_requests,
        (
            "HTTP/1.1 302 Found\r\n"
            f"Location: {foreign_url}\r\n"
            "Content-Length: 0\r\nConnection: close\r\n\r\n"
        ).encode("ascii"),
    )
    target_url = f"http://127.0.0.1:{redirect_port}/mcp"
    try:
        target = await approve_mcp_target(target_url, allow_private=True)
        async with approved_mcp_client(target) as client:
            with pytest.raises(httpx.InvalidURL, match="origin"):
                await client.get(foreign_url)
            assert primary_requests == []
            assert foreign_requests == []

            with pytest.raises(httpx.InvalidURL, match="origin"):
                await client.get(target_url, follow_redirects=True)

        assert primary_requests == ["GET /mcp HTTP/1.1"]
        assert foreign_requests == []
    finally:
        redirect_server.close()
        foreign_server.close()
        await redirect_server.wait_closed()
        await foreign_server.wait_closed()


@pytest.mark.asyncio
async def test_dns_changes_cannot_change_dialed_ip_or_tls_hostname(monkeypatch):
    dns_calls = await _set_dns(monkeypatch, ["9.9.9.9"])
    target = await approve_mcp_target("https://mcp.example/mcp")
    dialed: list[tuple[str, int]] = []
    tls: list[tuple[str | None, bool, ssl.VerifyMode]] = []

    class TLSProbeStream:
        async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
            raise AssertionError("TLS probe must fail before reading.")

        async def write(self, buffer: bytes, timeout: float | None = None) -> None:
            raise AssertionError("TLS probe must fail before writing.")

        async def start_tls(
            self,
            ssl_context: ssl.SSLContext,
            server_hostname: str | None = None,
            timeout: float | None = None,
        ):
            tls.append((server_hostname, ssl_context.check_hostname, ssl_context.verify_mode))
            raise httpcore.ConnectError("controlled TLS probe")

        async def aclose(self) -> None:
            return None

    async def changed_dns(host: str, port: int, *, type: int, proto: int):
        dns_calls.append((host, port))
        return [_addrinfo("127.0.0.1", port)]

    async def fake_connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ):
        dialed.append((host, port))
        return TLSProbeStream()

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", changed_dns)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", fake_connect_tcp)

    client = approved_mcp_client(target)
    try:
        request = client.build_request(
            "GET",
            target.url,
            extensions={"sni_hostname": "attacker.example"},
        )
        with pytest.raises(httpx.ConnectError):
            await client.send(request)
    finally:
        await client.aclose()

    assert dialed == [("9.9.9.9", 443)]
    assert dns_calls == [("mcp.example", 443)]
    assert tls == [("mcp.example", True, ssl.CERT_REQUIRED)]


@pytest.mark.asyncio
async def test_httpcore_errors_are_mapped_to_httpx_errors(monkeypatch):
    await _set_dns(monkeypatch, ["9.9.9.9"])
    target = await approve_mcp_target("https://mcp.example/mcp")

    async def timeout_connect(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ):
        raise httpcore.ConnectTimeout("private backend diagnostic")

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", timeout_connect)
    async with approved_mcp_client(target) as client:
        with pytest.raises(httpx.ConnectTimeout) as caught:
            await client.get(target.url)
        assert str(caught.value) == "MCP target connection timed out."


async def test_request_host_override_is_rejected_before_connection(monkeypatch):
    await _set_dns(monkeypatch, ["9.9.9.9"])
    target = await approve_mcp_target("https://mcp.example/mcp")
    dialed = []

    async def forbidden_dial(*args, **kwargs):
        dialed.append(args)
        raise AssertionError("Host override must be rejected before network I/O")

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", forbidden_dial)
    async with approved_mcp_client(target) as client:
        with pytest.raises(httpx.InvalidURL, match="Host"):
            await client.get(target.url, headers={"Host": "other.example"})
    assert dialed == []
