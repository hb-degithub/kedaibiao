"""调度器测试：重试、并发上限、错误隔离。"""

from __future__ import annotations

import asyncio
import time
import unittest

from kedaibiao.core.scheduler import RateLimiter, RetryPolicy, Scheduler


class TestRetryPolicy(unittest.TestCase):
    def test_delay_grows_and_caps(self) -> None:
        p = RetryPolicy(attempts=6, base_delay=1.0, max_delay=8.0, jitter=False)
        self.assertEqual([p.delay_for(i) for i in range(1, 6)], [1.0, 2.0, 4.0, 8.0, 8.0])

    def test_jitter_stays_in_range(self) -> None:
        p = RetryPolicy(base_delay=2.0, max_delay=30.0, jitter=True)
        for attempt in range(1, 6):
            delay = p.delay_for(attempt)
            ceiling = min(2.0 * 2 ** (attempt - 1), 30.0)
            self.assertGreaterEqual(delay, 0.0)
            self.assertLessEqual(delay, ceiling)


class TestSchedulerRun(unittest.IsolatedAsyncioTestCase):
    async def test_success_first_try(self) -> None:
        s = Scheduler()
        self.assertEqual(await s.run(lambda: _value(42)), 42)

    async def test_retries_then_succeeds(self) -> None:
        calls = {"n": 0}

        async def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("网络抖了")
            return "ok"

        policy = RetryPolicy(attempts=5, base_delay=0.001, max_delay=0.002, jitter=False)
        self.assertEqual(await s_run(flaky, policy), "ok")
        self.assertEqual(calls["n"], 3)

    async def test_raises_after_exhausting_attempts(self) -> None:
        calls = {"n": 0}

        async def always_fail() -> None:
            calls["n"] += 1
            raise TimeoutError("超时")

        policy = RetryPolicy(attempts=3, base_delay=0.001, jitter=False)
        with self.assertRaises(TimeoutError):
            await s_run(always_fail, policy)
        self.assertEqual(calls["n"], 3)

    async def test_non_retryable_propagates_immediately(self) -> None:
        calls = {"n": 0}

        async def boom() -> None:
            calls["n"] += 1
            raise ValueError("业务错误，重试没意义")

        with self.assertRaises(ValueError):
            await s_run(boom)
        self.assertEqual(calls["n"], 1)

    async def test_on_retry_callback(self) -> None:
        seen: list[int] = []

        async def flaky() -> str:
            if len(seen) < 2:
                raise ConnectionError("x")
            return "ok"

        s = Scheduler()
        policy = RetryPolicy(attempts=4, base_delay=0.001, jitter=False)
        await s.run(flaky, policy=policy, on_retry=lambda attempt, exc: seen.append(attempt))
        self.assertEqual(seen, [1, 2])


class TestSchedulerMap(unittest.IsolatedAsyncioTestCase):
    async def test_isolates_failures(self) -> None:
        s = Scheduler()

        async def fn(item: int) -> int:
            if item == 2:
                raise ValueError("坏元素")
            return item * 10

        results = await s.map([1, 2, 3], fn)
        self.assertEqual(results[0], 10)
        self.assertIsInstance(results[1], ValueError)
        self.assertEqual(results[2], 30)

    async def test_result_order_matches_input(self) -> None:
        s = Scheduler()

        async def slow_first(item: int) -> int:
            await asyncio.sleep(0.02 if item == 1 else 0.0)
            return item

        self.assertEqual(await s.map([1, 2, 3], slow_first), [1, 2, 3])


class TestConcurrency(unittest.IsolatedAsyncioTestCase):
    async def test_respects_max_concurrency(self) -> None:
        s = Scheduler(max_concurrency=2)
        state = {"now": 0, "peak": 0}

        async def fn(_: int) -> None:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
            await asyncio.sleep(0.02)
            state["now"] -= 1

        await s.map(list(range(8)), fn)
        self.assertLessEqual(state["peak"], 2)

    async def test_invalid_concurrency_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Scheduler(max_concurrency=0)


class TestRateLimiter(unittest.IsolatedAsyncioTestCase):
    async def test_spacing_enforced(self) -> None:
        rl = RateLimiter(min_interval=0.05)
        start = time.monotonic()
        for _ in range(3):
            await rl.acquire("k")
        self.assertGreaterEqual(time.monotonic() - start, 0.09)

    async def test_zero_interval_is_noop(self) -> None:
        rl = RateLimiter(min_interval=0.0)
        start = time.monotonic()
        for _ in range(50):
            await rl.acquire("k")
        self.assertLess(time.monotonic() - start, 0.05)

    async def test_keys_are_independent(self) -> None:
        rl = RateLimiter(min_interval=0.05)
        start = time.monotonic()
        await rl.acquire("a")
        await rl.acquire("b")
        self.assertLess(time.monotonic() - start, 0.04)


# ---- 小工具 ----------------------------------------------------------------


async def _value(v: int) -> int:
    return v


async def s_run(fn, policy: RetryPolicy | None = None):
    return await Scheduler().run(fn, policy=policy)


if __name__ == "__main__":
    unittest.main()
