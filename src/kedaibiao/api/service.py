"""Web 控制台的服务层 —— 与 HTTP 框架无关。

分成两层的理由：本机装不上 FastAPI（没有 pip），所以 HTTP 外壳用标准库写；
但**数据与动作的逻辑不该和 HTTP 绑死**。将来换 FastAPI / 加 CLI 子命令，
这里一行不用改。

本模块位于应用层，可以依赖 ``runtime`` 及以下所有层。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from kedaibiao import __version__, adapters
from kedaibiao.api.actions import (
    PLATFORM_ERROR,
    get_handler,
    has_action,
    load_builtin_actions,
    register_platform,
)
from kedaibiao.bank.store import ProgressStore, QuestionBank, record_to_dict
from kedaibiao.config.settings import (
    ENV_PREFIX,
    Settings,
    load_settings,
    resolve_environ,
)
from kedaibiao.core.models import TaskKind
from kedaibiao.runtime import build_runtime
from kedaibiao.security.credentials import CredentialError, env_key, load_credentials
from kedaibiao.tasks.pipeline import PipelineConfig

log = logging.getLogger(__name__)

JOB_HISTORY = 50
EVENT_HISTORY = 500
OPTIONAL_DEPS = ("playwright", "fastapi", "uvicorn")


class ConsoleError(Exception):
    """带机器可读 ``kind`` 与 HTTP 状态码的受控错误。"""

    def __init__(self, message: str, *, kind: str = "bad_request", status: int = 400) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


def _jsonable(value: Any) -> Any:
    """把领域对象递归转成可 JSON 序列化的结构。

    事件载荷里混着 datetime / Enum / dataclass，``json.dumps`` 会直接炸。
    统一在这里收口，免得每个调用点各写一遍。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    return str(value)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ConsoleService:
    """控制台的全部数据与动作。线程安全（Web 服务是多线程的）。"""

    def __init__(self, *, root: str | Path = ".", settings: Settings | None = None) -> None:
        self.root = Path(root)
        self._settings = settings  # 懒加载；每次跑批都会重新读配置
        self._environ: dict[str, str] | None = None
        self._lock = threading.RLock()
        self._jobs: list[dict[str, Any]] = []
        self._jobs_by_id: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        self._seq = 0
        self._active_job_id: str | None = None
        # 动作注册表是进程级的，重复登记会覆盖，这里调用是幂等的
        load_builtin_actions()

    # -- 配置与存储 -------------------------------------------------------

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            self._settings = load_settings(root=self.root, environ=self.environ)
        return self._settings

    @property
    def environ(self) -> dict[str, str]:
        """本次运行认的环境变量快照（``.env`` + 真实环境变量）。

        凭据与平台扫描都要走这一份，不能各自去读 ``os.environ`` ——
        否则 ``.env`` 里配的凭据在控制台上会显示成「未配置」。
        """
        if self._environ is None:
            self._environ = resolve_environ(self.root)
        return self._environ

    def _db_path(self) -> Path:
        return self.settings.db_path

    def _check_platform(self, platform: str | None) -> str | None:
        """校验 ``?platform=`` 参数，返回规范化后的作用域。

        ``None`` / 空串 = 全部平台。传了没注册的名字**必须报错**，不能静默
        当成空结果 —— 否则用户会以为「这个平台没数据」。
        """
        if platform is None or not str(platform).strip():
            return None
        name = str(platform).strip()
        if name not in adapters:
            known = ", ".join(adapters.names()) or "<未注册任何适配器>"
            raise ConsoleError(
                f"未注册的平台: {name!r}；已注册: {known}",
                kind="unknown_platform", status=404,
            )
        return name

    def _read(self, fn: Any) -> Any:
        """开一个只读用的连接跑 ``fn``，用完即关。

        sqlite 连接不能跨线程共享，而 Web 服务是多线程的 —— 每次调用开一个
        最省心，本地文件开销可以忽略。
        """

        class _Both:
            """同时持有题库与留痕，共用一个数据库文件。"""

            def __init__(self, path: Path) -> None:
                self.bank = QuestionBank(path)
                self.store = ProgressStore(path)

            def close(self) -> None:
                self.bank.close()
                self.store.close()

        handle = _Both(self._db_path())
        try:
            return fn(handle)
        finally:
            handle.close()

    # -- 只读接口 ---------------------------------------------------------

    def overview(self, platform: str | None = None) -> dict[str, Any]:
        scope = self._check_platform(platform)
        settings = self.settings
        db = self._db_path()
        exists = db.is_file()

        bank_stats: dict[str, Any] = {
            "total": 0, "graded": 0, "correct": 0, "accuracy": None,
            "ungraded": 0, "by_source": {}, "by_platform": {},
        }
        run_counts: dict[str, int] = {
            "total": 0, "succeeded": 0, "failed": 0, "skipped": 0, "running": 0,
        }

        if exists:
            def collect(handle: Any) -> tuple[dict[str, Any], dict[str, int]]:
                return (
                    handle.bank.stats(scope),
                    handle.store.status_counts(platform=scope),
                )

            bank_stats, run_counts = self._read(collect)
            for key in ("total", "succeeded", "failed", "skipped", "running"):
                run_counts.setdefault(key, 0)

        credentialed = [
            name for name in adapters.names()
            if load_credentials(name, require=False, environ=self.environ) is not None
        ]

        return {
            "version": __version__,
            "python": ".".join(str(p) for p in __import__("sys").version_info[:3]),
            "repo_root": str(self.root.resolve()),
            "platform": scope,
            "env": settings.env,
            "dry_run": settings.dry_run,
            "headless": settings.headless,
            "max_concurrency": settings.max_concurrency,
            "min_confidence": settings.min_confidence,
            "db_path": str(db),
            "db_exists": exists,
            "platform_count": len(adapters.names()),
            "credentialed_platforms": credentialed,
            "optional_deps": {name: find_spec(name) is not None for name in OPTIONAL_DEPS},
            "bank": bank_stats,
            "runs": run_counts,
        }

    def platforms(self) -> dict[str, Any]:
        items = []
        for name in adapters.names():
            cls = adapters.get(name)
            caps = getattr(cls, "capabilities", None)
            items.append({
                "name": name,
                "display_name": getattr(cls, "display_name", "") or name,
                "capabilities": _jsonable(caps) if caps is not None else {},
                "has_credentials": load_credentials(name, require=False, environ=self.environ) is not None,
            })
        return {"platforms": items}

    def runs(self, limit: int = 50, platform: str | None = None) -> dict[str, Any]:
        scope = self._check_platform(platform)
        if not self._db_path().is_file():
            return {"platform": scope, "runs": []}
        rows = self._read(
            lambda h: h.store.recent_runs(_clamp(limit, 1, 500), platform=scope)
        )
        return {"platform": scope, "runs": [_jsonable(r) for r in rows]}

    def submissions(self, limit: int = 50, platform: str | None = None) -> dict[str, Any]:
        scope = self._check_platform(platform)
        if not self._db_path().is_file():
            return {"platform": scope, "submissions": []}
        rows = self._read(
            lambda h: h.store.recent_submissions(_clamp(limit, 1, 500), platform=scope)
        )
        return {"platform": scope, "submissions": [_jsonable(r) for r in rows]}

    def bank(self, platform: str | None = None) -> dict[str, Any]:
        scope = self._check_platform(platform)
        empty: dict[str, Any] = {
            "total": 0, "graded": 0, "correct": 0, "accuracy": None,
            "ungraded": 0, "by_source": {}, "by_platform": {},
        }
        if not self._db_path().is_file():
            return {**empty, "platform": scope, "db_path": str(self._db_path())}
        stats = self._read(lambda h: h.bank.stats(scope))
        return {**stats, "platform": scope, "db_path": str(self._db_path())}

    def statistics(self) -> dict[str, Any]:
        """跨平台对比。**刻意不接受 platform 参数** —— 它本身就是横向视图。"""
        registered = adapters.names()
        empty_bank: dict[str, Any] = {
            "total": 0, "graded": 0, "correct": 0, "accuracy": None,
            "ungraded": 0, "by_source": {}, "by_platform": {},
        }
        if not self._db_path().is_file():
            return {
                "bank": empty_bank,
                "platforms": [_blank_platform_row(name) for name in registered],
            }

        def collect(handle: Any) -> tuple[Any, Any, Any]:
            return (
                handle.bank.stats(),
                handle.bank.platform_stats(),
                handle.store.platform_stats(),
            )

        bank, question_rows, run_rows = self._read(collect)

        questions = {row["platform"]: row for row in question_rows}
        runs = {row["platform"]: row for row in run_rows}

        # 先按注册顺序铺一遍，保证「没有数据的平台」也出现在对比表里
        order = list(registered)
        for name in list(runs) + list(questions):
            if name and name not in order:
                order.append(name)

        platforms = []
        for name in order:
            row = _blank_platform_row(name)
            row.update({k: v for k, v in runs.get(name, {}).items() if k != "platform"})
            q = questions.get(name)
            if q:
                row["questions"] = q["questions"]
                row["accuracy"] = q["accuracy"]
            platforms.append(row)

        return {"bank": bank, "platforms": platforms}

    def settings_view(self) -> dict[str, Any]:
        """设置页数据。**只列环境变量的键名，绝不返回值。**"""
        settings = self.settings
        db = self._db_path()
        platforms = []
        for name in adapters.names():
            cls = adapters.get(name)
            caps = getattr(cls, "capabilities", None)
            platforms.append({
                "name": name,
                "display_name": getattr(cls, "display_name", "") or name,
                "has_credentials": load_credentials(
                    name, require=False, environ=self.environ
                ) is not None,
                "credentials_hint": (
                    f"{env_key(name, 'user')} / {env_key(name, 'pass')}"
                    f"（或 {env_key(name, 'token')}）"
                ),
                "capabilities": _jsonable(caps) if caps is not None else {},
            })

        return {
            "env": settings.env,
            "dry_run": settings.dry_run,
            "headless": settings.headless,
            "max_concurrency": settings.max_concurrency,
            "min_interval": settings.min_interval,
            "max_attempts": settings.max_attempts,
            "min_confidence": settings.min_confidence,
            "notify": settings.notify,
            "db_path": str(db),
            "db_exists": db.is_file(),
            "session_dir": settings.session_dir,
            "dotenv_present": (self.root / ".env").is_file(),
            "env_keys_seen": sorted(k for k in self.environ if k.startswith(ENV_PREFIX)),
            "optional_deps": {name: find_spec(name) is not None for name in OPTIONAL_DEPS},
            "platforms": platforms,
        }

    def events(self, limit: int = 100) -> dict[str, Any]:
        with self._lock:
            return {"events": list(self._events[-_clamp(limit, 1, EVENT_HISTORY):])}

    def jobs(self, platform: str | None = None) -> dict[str, Any]:
        scope = self._check_platform(platform)
        with self._lock:
            items = [_jsonable(j) for j in reversed(self._jobs)]
        if scope:
            items = [j for j in items if j.get("platform") == scope]
        return {"platform": scope, "jobs": items}

    def job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            found = self._jobs_by_id.get(job_id)
        if found is None:
            raise ConsoleError(f"没有这个任务: {job_id}", kind="not_found", status=404)
        return _jsonable(found)

    # -- 平台只读动作 -----------------------------------------------------

    def actions(self, platform: str) -> dict[str, Any]:
        scope = self._check_platform(platform)
        assert scope is not None
        return {
            "platform": scope,
            "actions": [spec.to_dict() for spec in register_platform(scope)],
        }

    def run_action(
        self, platform: str, action: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        scope = self._check_platform(platform)
        assert scope is not None
        if not has_action(scope, action):
            available = ", ".join(s.name for s in register_platform(scope)) or "<无>"
            raise ConsoleError(
                f"{scope} 没有这个动作: {action!r}；可用: {available}",
                kind="unknown_action", status=404,
            )
        spec = next(s for s in register_platform(scope) if s.name == action)
        cleaned = self._validate_params(spec, params or {})
        return asyncio.run(self._execute_action(scope, action, cleaned))

    @staticmethod
    def _validate_params(spec: Any, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise ConsoleError("参数必须是 JSON 对象", kind="bad_request", status=400)
        out: dict[str, Any] = {}
        for param in spec.params:
            raw = params.get(param.name)
            blank = raw is None or (isinstance(raw, str) and not raw.strip())
            if blank:
                if param.required:
                    raise ConsoleError(
                        f"缺少必填参数：{param.label}（{param.name}）",
                        kind="bad_request", status=400,
                    )
                continue
            if param.type == "int":
                try:
                    out[param.name] = int(raw)
                except (TypeError, ValueError):
                    raise ConsoleError(
                        f"参数 {param.label} 必须是整数",
                        kind="bad_request", status=400,
                    ) from None
            else:
                out[param.name] = str(raw).strip()
        return out

    async def _execute_action(
        self, platform: str, action: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """跑一个只读动作。每次都新建 Runtime —— 它们各自持有 sqlite 连接。"""
        runtime = build_runtime(root=self.root, environ=self.environ)
        adapter: Any = None
        try:
            adapter = runtime.make_adapter(platform)
            try:
                creds = load_credentials(platform, require=True, environ=self.environ)
            except CredentialError as exc:
                raise ConsoleError(str(exc), kind="no_credentials", status=400) from exc

            try:
                logged_in = await adapter.refresh_session(creds)
            except Exception as exc:  # noqa: BLE001 - 平台异常一律翻成 502
                raise ConsoleError(
                    f"{platform} 登录失败：{exc}", kind=PLATFORM_ERROR, status=502
                ) from exc
            if not logged_in:
                raise ConsoleError(
                    f"{platform} 登录失败（凭据可能不对，或会话已失效）",
                    kind=PLATFORM_ERROR, status=502,
                )

            try:
                return await get_handler(platform, action)(adapter, params)
            except ConsoleError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("平台动作失败: %s/%s", platform, action)
                raise ConsoleError(
                    f"{platform} 接口调用失败：{exc}", kind=PLATFORM_ERROR, status=502
                ) from exc
        finally:
            if adapter is not None:
                await adapter.close()
            runtime.close()

    # -- 触发跑批 ---------------------------------------------------------

    def start_run(
        self,
        platform: str,
        *,
        live: bool = False,
        limit: int = 0,
        kinds: list[str] | None = None,
    ) -> dict[str, Any]:
        """校验并启动一次跑批。校验全部前置，好让 HTTP 层返回准确的状态码。"""
        if platform not in adapters:
            known = ", ".join(adapters.names()) or "<未注册任何适配器>"
            raise ConsoleError(
                f"未注册的平台: {platform!r}；已注册: {known}",
                kind="unknown_platform", status=404,
            )

        cls: Any = adapters.get(platform)
        caps = getattr(cls, "capabilities", None)

        if live:
            if caps is not None and not getattr(caps, "submit", False):
                raise ConsoleError(
                    f"{getattr(cls, 'display_name', platform)} 本阶段不支持提交"
                    f"（capabilities.submit = False），不能以 live 模式运行。",
                    kind="bad_request", status=400,
                )

        # 干跑也要登录（要拉课程才能规划），所以凭据一律前置校验 ——
        # 放在这里比让后台线程抛异常再标 failed 更容易排查。
        try:
            load_credentials(platform, require=True, environ=self.environ)
        except CredentialError as exc:
            raise ConsoleError(str(exc), kind="no_credentials", status=400) from exc

        parsed_kinds = _parse_kinds(kinds or [])
        limit = _clamp(int(limit), 0, 10_000)

        with self._lock:
            if self._active_job_id is not None:
                raise ConsoleError(
                    f"已有任务在跑（{self._active_job_id}），同一时刻只允许一个。",
                    kind="busy", status=409,
                )
            self._seq += 1
            job_id = f"j-{self._seq}"
            job: dict[str, Any] = {
                "job_id": job_id,
                "platform": platform,
                "live": bool(live),
                "status": "running",
                "started_at": _now(),
                "finished_at": None,
                "summary": "",
                "error": "",
                "records": [],
                "live_requested": bool(live),
                "limit": limit,
                "kinds": sorted(k.value for k in parsed_kinds),
            }
            self._jobs.append(job)
            self._jobs_by_id[job_id] = job
            del self._jobs[:-JOB_HISTORY]
            for stale in list(self._jobs_by_id):
                if stale not in {j["job_id"] for j in self._jobs}:
                    del self._jobs_by_id[stale]
            self._active_job_id = job_id

        thread = threading.Thread(
            target=self._execute,
            args=(job_id, platform, bool(live), limit, parsed_kinds),
            name=f"kedaibiao-job-{job_id}",
            daemon=True,
        )
        thread.start()
        return _jsonable(job)

    def wait_for_idle(self, timeout: float = 30.0) -> bool:
        """等当前任务结束。给测试与优雅关闭用。"""
        deadline = threading.Event()
        waited = 0.0
        while waited < timeout:
            with self._lock:
                if self._active_job_id is None:
                    return True
            deadline.wait(0.05)
            waited += 0.05
        return False

    # -- 内部 -------------------------------------------------------------

    def _execute(
        self,
        job_id: str,
        platform: str,
        live: bool,
        limit: int,
        kinds: frozenset[TaskKind],
    ) -> None:
        """在后台线程里跑一次流水线。

        每个任务单独装配一个 Runtime：它持有自己的 sqlite 连接，而 sqlite
        连接不能跨线程共享。跑完必须 close，否则连接会泄漏。
        """
        runtime = None
        try:
            settings = load_settings(root=self.root).merged({"dry_run": not live})
            runtime = build_runtime(
                root=self.root,
                settings=settings,
                config=PipelineConfig(
                    dry_run=not live,
                    min_confidence=settings.min_confidence,
                    kinds=kinds,
                    max_assignments=limit,
                ),
            )
            runtime.bus.on("*", self._record_event)

            report = asyncio.run(runtime.run(platform))

            with self._lock:
                job = self._jobs_by_id[job_id]
                job["summary"] = report.summary()
                job["status"] = "succeeded" if report.failed == 0 else "failed"
                job["records"] = [
                    {
                        "assignment_id": r.assignment_id,
                        "status": r.status.value,
                        "note": r.error or str(r.detail.get("reason", "")),
                    }
                    for r in report.records
                ]
        except BaseException as exc:  # noqa: BLE001 - 后台线程里必须兜住，否则静默死掉
            log.exception("跑批任务 %s 异常结束", job_id)
            with self._lock:
                job = self._jobs_by_id[job_id]
                job["status"] = "failed"
                job["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if runtime is not None:
                runtime.close()
            with self._lock:
                job = self._jobs_by_id[job_id]
                job["finished_at"] = _now()
                if self._active_job_id == job_id:
                    self._active_job_id = None

    def _record_event(self, event: Any) -> None:
        entry = {
            "at": _jsonable(getattr(event, "at", datetime.now())),
            "name": getattr(event, "name", "?"),
            "payload": _jsonable(getattr(event, "payload", {})),
        }
        with self._lock:
            self._events.append(entry)
            del self._events[:-EVENT_HISTORY]


def _blank_platform_row(name: str) -> dict[str, Any]:
    """「统计」页对比表的一行空白模板。

    没有数据的平台也要出现 —— 缺行会让人以为那个平台不存在。
    """
    return {
        "platform": name,
        "total": 0,
        "succeeded": 0,
        "failed": 0,
        "skipped": 0,
        "submissions": 0,
        "submit_ok": 0,
        "questions": 0,
        "accuracy": None,
    }


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _parse_kinds(kinds: list[str]) -> frozenset[TaskKind]:
    parsed = set()
    for raw in kinds:
        name = str(raw).strip()
        if not name:
            continue
        try:
            parsed.add(TaskKind(name))
        except ValueError as exc:
            valid = ", ".join(k.value for k in TaskKind)
            raise ConsoleError(
                f"未知的任务类型: {name!r}；可选 {valid}", kind="bad_request", status=400
            ) from exc
    return frozenset(parsed)
