"""定时跑批：把「某个平台跑一次流水线」按固定间隔排进后台。

## 为什么不引 cron 库

项目核心层零第三方依赖，而这里要的能力很窄：**每隔 N 秒/分钟/小时跑一次**。
真正需要「每天 08:00」「工作日 9-17 点」这种日历语义时，用系统 cron 调
``kedaibiao run <平台>`` 更合适，两者可以并存 —— 本模块不试图取代 cron。

## 配置（``config/default.toml`` 或 ``config/local.toml``）

```toml
[schedule]
enabled = false          # 默认关闭；打开才起后台线程
tick = 30.0              # 每 30 秒检查一次有没有任务到期

[[schedule.jobs]]
name = "unipus-daily"    # 任务名，必须唯一（状态与日志都按它记）
platform = "unipus"      # 平台机器名，见 `kedaibiao platforms`
every = "24h"            # 支持 s / m / h / d 后缀；纯数字按秒
live = false             # **默认干跑**；写操作必须显式 live = true
limit = 0                # 0 = 不限
kinds = ""               # 空 = 全部；否则逗号分隔 course,homework,exam,practice
enabled = true
```

## 安全默认

- 整个功能默认关闭（``enabled = false``）；
- 每个任务默认 ``live = false``（干跑），要真写必须逐任务显式打开；
- 同一时刻只跑一个任务，不并发压平台；
- 每轮失败只记日志、不影响下一轮，也不会把上一轮的失败「攒」到最后才报。

配置解析刻意放在本模块、而不是 ``Settings``：``Settings`` 是扁平 dataclass，
而任务列表是「数组套表」，塞进去会让配置合并逻辑变复杂且容易误伤。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kedaibiao.core.models import TaskKind

log = logging.getLogger(__name__)

#: 秒级后缀。纯数字按秒处理；0 或负值直接拒绝，免得「每 0 秒跑一次」把平台打崩。
_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


class ScheduleConfigError(ValueError):
    """配置写错了 —— 调用方应当把它当成「启动前就该修好」的错误。"""


@dataclass(slots=True)
class ScheduleJob:
    """一个定时任务。"""

    name: str
    platform: str
    every_seconds: float
    live: bool = False
    limit: int = 0
    kinds: frozenset[TaskKind] = frozenset()
    enabled: bool = True


@dataclass(slots=True)
class SchedulePlan:
    """整份定时配置。"""

    enabled: bool = False
    tick: float = 30.0
    jobs: list[ScheduleJob] = field(default_factory=list)


def parse_interval(text: Any) -> float:
    """把 ``"30m"`` / ``"24h"`` / ``"900"`` 解析成秒数。

    非法值抛 :class:`ScheduleConfigError` —— 定时任务写错单位属于配置错误，
    静默按默认值跑（比如把「24h」当成 24 秒）比报错危险得多。
    """
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        seconds = float(text)
    else:
        raw = str(text).strip().lower()
        if not raw:
            raise ScheduleConfigError("every 不能为空")
        unit = raw[-1]
        if unit in _UNITS:
            number, factor = raw[:-1], _UNITS[unit]
        else:
            number, factor = raw, 1.0
        try:
            seconds = float(number) * factor
        except ValueError as exc:
            raise ScheduleConfigError(f"every 写法不认识: {text!r}（例：30m / 6h / 1d）") from exc
    if seconds <= 0:
        raise ScheduleConfigError(f"every 必须大于 0，收到 {text!r}")
    return seconds


def _parse_kinds(text: Any) -> frozenset[TaskKind]:
    raw = str(text or "").strip()
    if not raw:
        return frozenset()
    kinds: set[TaskKind] = set()
    for piece in raw.split(","):
        name = piece.strip()
        if not name:
            continue
        try:
            kinds.add(TaskKind(name))
        except ValueError as exc:
            valid = ", ".join(k.value for k in TaskKind)
            raise ScheduleConfigError(f"未知任务类型 {name!r}；可选：{valid}") from exc
    return frozenset(kinds)


def parse_job(raw: Mapping[str, Any]) -> ScheduleJob:
    name = str(raw.get("name") or "").strip()
    platform = str(raw.get("platform") or "").strip()
    if not name:
        raise ScheduleConfigError("定时任务缺少 name")
    if not platform:
        raise ScheduleConfigError(f"定时任务 {name!r} 缺少 platform")
    limit = raw.get("limit", 0)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ScheduleConfigError(f"定时任务 {name!r} 的 limit 必须是非负整数")
    return ScheduleJob(
        name=name,
        platform=platform,
        every_seconds=parse_interval(raw.get("every")),
        live=bool(raw.get("live", False)),
        limit=limit,
        kinds=_parse_kinds(raw.get("kinds", "")),
        enabled=bool(raw.get("enabled", True)),
    )


def parse_plan(raw: Mapping[str, Any] | None) -> SchedulePlan:
    """从 ``[schedule]`` 这一节的原始 mapping 构造计划。"""
    if not raw:
        return SchedulePlan()
    tick = raw.get("tick", 30.0)
    try:
        tick_seconds = float(tick)
    except (TypeError, ValueError) as exc:
        raise ScheduleConfigError(f"schedule.tick 必须是数字，收到 {tick!r}") from exc
    if tick_seconds <= 0:
        raise ScheduleConfigError("schedule.tick 必须大于 0")

    raw_jobs = raw.get("jobs") or []
    if not isinstance(raw_jobs, list):
        raise ScheduleConfigError("schedule.jobs 必须是数组（[[schedule.jobs]]）")

    jobs: list[ScheduleJob] = []
    seen: set[str] = set()
    for item in raw_jobs:
        if not isinstance(item, Mapping):
            raise ScheduleConfigError("schedule.jobs 里的每一项都必须是表")
        job = parse_job(item)
        if job.name in seen:
            raise ScheduleConfigError(f"定时任务名重复: {job.name!r}")
        seen.add(job.name)
        jobs.append(job)
    return SchedulePlan(enabled=bool(raw.get("enabled", False)), tick=tick_seconds, jobs=jobs)


def load_plan(root: str | Path = ".") -> SchedulePlan:
    """读 ``config/default.toml`` + ``config/local.toml`` 里的 ``[schedule]``。

    与 ``load_settings`` 同样的优先级：后者覆盖前者。缺失文件静默跳过。
    """
    root_path = Path(root)
    merged: dict[str, Any] = {}
    for rel in ("config/default.toml", "config/local.toml"):
        path = root_path / rel
        if not path.is_file():
            continue
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        section = data.get("schedule")
        if isinstance(section, dict):
            merged = {**merged, **section}
    return parse_plan(merged)


def due_jobs(
    jobs: Iterable[ScheduleJob], last_run: Mapping[str, float], now: float
) -> list[ScheduleJob]:
    """哪些任务现在该跑。首次运行（没有记录）立即到期。"""
    due: list[ScheduleJob] = []
    for job in jobs:
        if not job.enabled:
            continue
        last = last_run.get(job.name)
        if last is None or now - last >= job.every_seconds:
            due.append(job)
    return due


def execute_job(root: str | Path, job: ScheduleJob) -> dict[str, Any]:
    """真的跑一次任务，返回可打印的结果摘要。

    复用 CLI / 控制台同一条装配链（``build_runtime`` + 账号库），避免定时跑批
    与手动跑批出现行为分叉。**凭据只来自账号库**：没绑号就报错，不回退 ``.env``。
    """
    from kedaibiao.platforms import load_builtin_adapters
    from kedaibiao.runtime import build_runtime
    from kedaibiao.security.accounts import AccountError, AccountStore
    from kedaibiao.tasks.pipeline import PipelineConfig

    load_builtin_adapters()
    from kedaibiao.config.settings import load_settings

    settings = load_settings(root=root)
    config = PipelineConfig(
        dry_run=not job.live,
        min_confidence=settings.min_confidence,
        kinds=job.kinds,
        max_assignments=job.limit,
    )
    accounts = AccountStore(settings.db_path)
    try:
        creds, cookie_file = accounts.resolve_credentials(
            job.platform, session_dir=settings.session_dir
        )
    except AccountError as exc:
        accounts.close()
        raise RuntimeError(f"定时任务 {job.name!r} 取不到 {job.platform} 的账号：{exc}") from exc

    runtime = build_runtime(
        root=root, settings=settings, config=config, cookie_file=cookie_file
    )
    try:
        report = asyncio.run(runtime.run(job.platform, credentials=creds))
    finally:
        runtime.close()
        accounts.close()
    return {
        "name": job.name,
        "platform": job.platform,
        "live": job.live,
        "ok": report.failed == 0,
        "summary": report.summary(),
        "succeeded": report.succeeded,
        "failed": report.failed,
        "skipped": report.skipped,
    }


class ScheduleRunner:
    """后台线程跑批器。

    ``execute`` 可注入：测试里传假函数，生产用 :func:`execute_job`。
    同一时刻只允许一个任务在跑（串行），避免多平台并发把本机或平台打崩。
    """

    def __init__(
        self,
        plan: SchedulePlan,
        execute: Callable[[ScheduleJob], Any],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.plan = plan
        self.execute = execute
        self.clock = clock
        self.last_run: dict[str, float] = {}
        self.results: dict[str, dict[str, Any] | None] = {}
        self.errors: dict[str, str] = {}
        self.triggered = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- 单轮 --

    def run_pending(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """跑一轮到期的任务，返回本轮结果。不启动线程，便于 CLI ``--once``。"""
        moment = self.clock() if now is None else now
        finished: list[dict[str, Any]] = []
        for job in due_jobs(self.plan.jobs, self.last_run, moment):
            self.last_run[job.name] = moment
            self.triggered += 1
            try:
                result = self.execute(job)
            except Exception as exc:  # noqa: BLE001 - 单个任务失败不能拖垮整轮
                self.errors[job.name] = f"{type(exc).__name__}: {exc}"
                log.error("定时任务 %s 执行失败：%s", job.name, exc, exc_info=True)
                finished.append(
                    {
                        "name": job.name,
                        "platform": job.platform,
                        "ok": False,
                        "error": str(exc),
                    }
                )
                continue
            self.errors.pop(job.name, None)
            self.results[job.name] = result if isinstance(result, dict) else None
            finished.append(
                {"name": job.name, "platform": job.platform, "ok": True, "result": result}
            )
        return finished

    # -- 线程 --

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="kedaibiao-schedule", daemon=True)
        self._thread.start()
        log.info(
            "定时跑批已启动：%d 个任务，tick=%.0fs", len(self.plan.jobs), self.plan.tick
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                for item in self.run_pending():
                    if not item["ok"]:
                        log.error("定时任务 %s 失败：%s", item["name"], item.get("error"))
            except Exception:  # noqa: BLE001 - 循环本身不能死
                log.exception("定时跑批这一轮出错，继续下一轮")
            self._stop.wait(self.plan.tick)

    def status(self) -> dict[str, Any]:
        """给 CLI / 控制台看的只读快照。"""
        return {
            "enabled": self.plan.enabled,
            "running": self.is_running(),
            "tick": self.plan.tick,
            "triggered": self.triggered,
            "jobs": [
                {
                    "name": job.name,
                    "platform": job.platform,
                    "every_seconds": job.every_seconds,
                    "live": job.live,
                    "limit": job.limit,
                    "kinds": sorted(k.value for k in job.kinds),
                    "enabled": job.enabled,
                    "last_run_at": self.last_run.get(job.name),
                    "last_error": self.errors.get(job.name, ""),
                }
                for job in self.plan.jobs
            ],
        }
