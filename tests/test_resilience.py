import httpx
import pytest

from energy_agent_tools.resilience import ReadTransport


async def no_sleep(delay):
    pass


async def test_temporary_read_failure_retries_and_health_has_no_credentials():
    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(503 if len(calls) < 3 else 200, json={"ok": True})

    transport = ReadTransport(httpx.MockTransport(provider), sleep=no_sleep)
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.get(
            "https://provider.example/data?apikey=never-return",
            headers={"Authorization": "Bearer never-return"},
        )
    assert response.status_code == 200
    assert len(calls) == 3
    assert transport.health()[0]["retries"] == 2
    assert "never-return" not in str(transport.health())
    assert "provider.example" not in str(transport.health())


@pytest.mark.parametrize(
    "method,status,headers",
    [
        ("GET", 401, {}),
        ("POST", 503, {}),
        ("GET", 429, {"Retry-After": "3600"}),
    ],
)
async def test_auth_failures_writes_and_long_retry_after_are_not_retried(method, status, headers):
    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(status, headers=headers)

    async with httpx.AsyncClient(
        transport=ReadTransport(httpx.MockTransport(provider), sleep=no_sleep)
    ) as client:
        assert (await client.request(method, "https://provider.example/data")).status_code == status
    assert len(calls) == 1


async def test_circuit_blocks_repeated_outage_and_recovers_after_cooldown():
    now = [0]
    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(503 if now[0] == 0 else 200)

    transport = ReadTransport(
        httpx.MockTransport(provider),
        retries=0,
        failure_threshold=2,
        cooldown=10,
        clock=lambda: now[0],
        sleep=no_sleep,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        for _ in range(2):
            assert (await client.get("https://provider.example/data")).status_code == 503
        with pytest.raises(httpx.ConnectError, match="circuit"):
            await client.get("https://provider.example/data")
        assert len(calls) == 2
        now[0] = 11
        assert (await client.get("https://provider.example/data")).status_code == 200
    assert transport.health()[0]["failures"] == 0
