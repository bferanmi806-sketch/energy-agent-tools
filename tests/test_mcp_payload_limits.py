from __future__ import annotations

import asyncio

import httpcore
import httpx
import pytest

import energy_agent_tools.connectors.mcp_network as mcp_network


@pytest.mark.parametrize(
    ("declared_length", "expected"),
    [
        (b"8", None),
        (b"9", mcp_network._RESPONSE_LIMIT_ERROR),
        (b"0000000008", None),
        (b"0000000009", mcp_network._RESPONSE_LIMIT_ERROR),
        (b"9" * 5000, mcp_network._RESPONSE_LIMIT_ERROR),
        (b"0" * 5000 + b"8", None),
    ],
)
def test_declared_content_length_uses_bounded_string_comparison(
    monkeypatch, declared_length: bytes, expected: str | None
):
    monkeypatch.setattr(mcp_network, "MAX_MCP_RESPONSE_BYTES", 8)
    response = httpcore.Response(
        200,
        headers=[(b"content-length", declared_length)],
    )

    assert mcp_network._response_rejection(response) == expected


@pytest.mark.asyncio
async def test_response_limit_rejects_large_and_encoded_bodies_and_closes_streams(monkeypatch):
    monkeypatch.setattr(mcp_network, "MAX_MCP_RESPONSE_BYTES", 8)
    requests: list[tuple[str, str]] = []
    rejected_connections_closed: dict[str, asyncio.Event] = {}
    connection_closed: list[asyncio.Event] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        closed = asyncio.Event()
        connection_closed.append(closed)
        try:
            while True:
                request_line = await reader.readline()
                if not request_line:
                    return
                request = request_line.decode("ascii").rstrip("\r\n")
                headers: dict[str, str] = {}
                while row := await reader.readline():
                    if row in (b"\r\n", b"\n"):
                        break
                    name, value = row.decode("latin-1").split(":", maxsplit=1)
                    headers[name.lower()] = value.strip()
                content_length = int(headers.get("content-length", "0"))
                if content_length:
                    await reader.readexactly(content_length)
                _, path, _ = request.split(" ", maxsplit=2)
                requests.append((path, headers.get("accept-encoding", "")))

                if path == "/declared":
                    rejected_connections_closed[path] = closed
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\nConnection: keep-alive\r\n\r\n"
                    )
                    await writer.drain()
                    await reader.read()
                    return
                if path == "/streamed":
                    rejected_connections_closed[path] = closed
                    writer.write(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Transfer-Encoding: chunked\r\n"
                        b"Connection: keep-alive\r\n\r\n"
                        b"5\r\n12345\r\n"
                    )
                    await writer.drain()
                    writer.write(b"4\r\n6789\r\n")
                    await writer.drain()
                    await reader.read()
                    return
                if path == "/compressed":
                    rejected_connections_closed[path] = closed
                    writer.write(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Encoding: gzip\r\n"
                        b"Content-Length: 8\r\n"
                        b"Connection: keep-alive\r\n\r\n"
                        b"not gzip"
                    )
                    await writer.drain()
                    await reader.read()
                    return

                body = b"safe"
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    + f"Content-Length: {len(body)}\r\n".encode("ascii")
                    + b"Connection: keep-alive\r\n\r\n"
                    + body
                )
                await writer.drain()
        finally:
            closed.set()
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    target = await mcp_network.approve_mcp_target(f"{base_url}/mcp", allow_private=True)
    try:
        async with mcp_network.approved_mcp_client(target) as client:
            normal = await client.get(f"{base_url}/normal")
            assert normal.text == "safe"
            assert requests[-1] == ("/normal", "identity")

            with pytest.raises(httpx.RequestError) as declared_error:
                await client.get(f"{base_url}/declared")
            assert str(declared_error.value) == mcp_network._RESPONSE_LIMIT_ERROR
            await asyncio.wait_for(rejected_connections_closed["/declared"].wait(), timeout=1)

            safe_after_declared = await client.get(f"{base_url}/safe-after-declared")
            assert safe_after_declared.text == "safe"

            with pytest.raises(httpx.RequestError) as streamed_error:
                async with client.stream("GET", f"{base_url}/streamed") as response:
                    await response.aread()
            assert str(streamed_error.value) == mcp_network._RESPONSE_LIMIT_ERROR
            await asyncio.wait_for(rejected_connections_closed["/streamed"].wait(), timeout=1)

            safe_after_streamed = await client.get(f"{base_url}/safe-after-streamed")
            assert safe_after_streamed.text == "safe"

            with pytest.raises(httpx.RequestError) as compressed_error:
                await client.get(f"{base_url}/compressed")
            assert str(compressed_error.value) == mcp_network._RESPONSE_ENCODING_ERROR
            assert "not gzip" not in str(compressed_error.value)
            await asyncio.wait_for(rejected_connections_closed["/compressed"].wait(), timeout=1)

            safe_after_compressed = await client.get(f"{base_url}/safe-after-compressed")
            assert safe_after_compressed.text == "safe"
        assert client.is_closed
        assert all(encoding == "identity" for _, encoding in requests)
    finally:
        server.close()
        await server.wait_closed()
        for event in connection_closed:
            if not event.is_set():
                await asyncio.wait_for(event.wait(), timeout=1)
