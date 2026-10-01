"""Bounded retries for reads and temporary circuit breaking without credential logs."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx


@dataclass
class ProviderState:
    failures: int = 0
    open_until: float = 0
    requests: int = 0
    retries: int = 0
    status: int | None = None


class ReadTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        retries: int = 2,
        failure_threshold: int = 3,
        cooldown: float = 30,
        budget: float = 30,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        if not 0 <= retries <= 3 or failure_threshold < 1 or cooldown <= 0 or budget <= 0:
            raise ValueError("Invalid retry limits")
        self.transport = transport or httpx.AsyncHTTPTransport()
        self.retries = retries
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown
        self.budget = budget
        self.clock = clock
        self.sleep = sleep
        self.states: dict[str, ProviderState] = {}

    @staticmethod
    def provider_id(request: httpx.Request) -> str:
        origin = f"{request.url.scheme}://{request.url.host}:{request.url.port}"
        return hashlib.sha256(origin.encode()).hexdigest()[:16]

    def health(self) -> list[dict[str, str | int | float | None]]:
        return [
            {
                "provider_id": key,
                "status": state.status,
                "failures": state.failures,
                "requests": state.requests,
                "retries": state.retries,
                "cooldown_remaining_seconds": max(0, state.open_until - self.clock()),
            }
            for key, state in self.states.items()
        ]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method not in {"GET", "HEAD"}:
            return await self.transport.handle_async_request(request)
        key = self.provider_id(request)
        if key not in self.states and len(self.states) >= 1000:
            self.states.pop(next(iter(self.states)))
        state = self.states.setdefault(key, ProviderState())
        if state.open_until > self.clock():
            raise httpx.ConnectError("Provider circuit is temporarily open.", request=request)
        state.requests += 1
        try:
            async with asyncio.timeout(self.budget):
                return await self._attempt(request, state)
        except TimeoutError:
            self._failure(state)
            raise httpx.ReadTimeout("Provider read budget exhausted.", request=request) from None

    def _failure(self, state: ProviderState) -> None:
        state.failures += 1
        if state.failures >= self.failure_threshold:
            state.open_until = self.clock() + self.cooldown

    async def _attempt(self, request: httpx.Request, state: ProviderState) -> httpx.Response:
        for attempt in range(self.retries + 1):
            try:
                response = await self.transport.handle_async_request(request)
            except httpx.TransportError:
                if attempt == self.retries:
                    self._failure(state)
                    raise
                state.retries += 1
                await self.sleep(0.25 * 2**attempt)
                continue
            state.status = response.status_code
            if response.status_code not in {408, 429, 500, 502, 503, 504}:
                state.failures = 0
                state.open_until = 0
                return response
            delay = 0.25 * 2**attempt
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = self.budget
            if attempt == self.retries or not 0 <= delay <= 2:
                self._failure(state)
                return response
            await response.aclose()
            state.retries += 1
            await self.sleep(delay)
        raise AssertionError("Retry loop exhausted")

    async def aclose(self) -> None:
        await self.transport.aclose()
