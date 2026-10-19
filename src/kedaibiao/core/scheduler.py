"""并发调度与重试。

教学平台的共同特点：接口慢、爱限流、偶发 502/验证码。所以调度层必须
自带三件套 —— 全局并发上限、单平台限速、指数退避重试。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

#: 这些异常值得重试；其余直接抛给上层
RETRYABLE = (TimeoutError, asyncio.TimeoutError, ConnectionError, OSError)


@dataclass(slots=True, frozen=True)
class RetryPolicy:
    attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    jitter: bool = True

    def delay_for(self, attempt: int) -> float:
        """attempt 从 1 开始。指数退避 + full jitter，避免整批任务同时重试。"""
        raw = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        return random.uniform(0, raw) if self.jitter else raw


class RateLimiter:
    """按 key（一般用平台名）限制最小请求间隔。"""

    def __init__(self, min_interval: float = 0.0) -> None:
        self._min_interval = min_interval
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def acquire(self, key: str) -> None:
        if self._min_interval <= 0:
            return
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            elapsed = time.monotonic() - self._last.get(key, 0.0)
            wait = self._min_interval - elapsed
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[key] = time.monotonic()


class Scheduler:
    """全局并发闸门 + 限速器。"""

    def __init__(self, max_concurrency: int = 3, min_interval: float = 0.0) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency 必须 >= 1")
        self._sem = asyncio.Semaphore(max_concurrency)
        self.rate_limiter = RateLimiter(min_interval)
        self.max_concurrency = max_concurrency

    async def run(
        self,
        fn: Callable[[], Awaitable[T]],
        *,
        key: str = "default",
        policy: RetryPolicy | None = None,
        on_retry: Callable[[int, BaseException], None] | None = None,
    ) -> T:
        """在并发闸门内执行 ``fn``，按 ``policy`` 重试。"""
        policy = policy or RetryPolicy()
        last_exc: BaseException | None = None

        for attempt in range(1, policy.attempts + 1):
            try:
                async with self._sem:
                    await self.rate_limiter.acquire(key)
                    return await fn()
            except RETRYABLE as exc:
                last_exc = exc
                if attempt >= policy.attempts:
                    break
                delay = policy.delay_for(attempt)
                log.warning(
                    "第 %d/%d 次失败(%s: %s)，%.1fs 后重试",
                    attempt, policy.attempts, type(exc).__name__, exc, delay,
                )
                if on_retry is not None:
                    on_retry(attempt, exc)
                await asyncio.sleep(delay)

        assert last_exc is not None
        raise last_exc

    async def map(
        self,
        items: list[T],
        fn: Callable[[T], Awaitable[object]],
        *,
        key: str = "default",
    ) -> list[object | BaseException]:
        """并发跑一批任务，返回与输入等长的结果，异常被就地捕获。

        刻意不用 ``gather`` 的默认行为：一个任务挂掉不应该带走整批。
        """

        async def guarded(item: T) -> object | BaseException:
            try:
                return await self.run(lambda: fn(item), key=key)
            except BaseException as exc:  # noqa: BLE001 - 就地收集，交给调用方决策
                return exc

        return list(await asyncio.gather(*(guarded(i) for i in items)))
