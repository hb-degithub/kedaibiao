"""Web 控制台的服务层 —— 与 HTTP 框架无关。

分成两层的理由：本机装不上 FastAPI（没有 pip），所以 HTTP 外壳用标准库写；
但**数据与动作的逻辑不该和 HTTP 绑死**。将来换 FastAPI / 加 CLI 子命令，
这里一行不用改。

本模块位于应用层，可以依赖 ``runtime`` 及以下所有层。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from enum import Enum
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from kedaibiao import __version__, adapters
from kedaibiao.api.actions import (
    PLATFORM_ERROR,
    ActionInputError,
    ensure_generic_actions,
    get_handler,
    has_action,
    load_builtin_actions,
    register_platform,
)
from kedaibiao.bank.store import ProgressStore, QuestionBank, record_to_dict
from kedaibiao.config.overrides import OverrideStore
from kedaibiao.config.settings import (
    ENV_PREFIX,
    Settings,
    load_settings,
    resolve_environ,
)
from kedaibiao.core.models import Credentials, TaskKind
from kedaibiao.platforms import load_builtin_adapters
from kedaibiao.runtime import (
    build_runtime,
    persist_http_cookies,
    session_path,
    restore_adapter_session,
    save_adapter_session,
)
from kedaibiao.security.accounts import AccountError, AccountStore, PlatformAccount
from kedaibiao.security.auth import (
    ROLE_SUPERADMIN,
    ROLE_USER,
    AuthError,
    AuthStore,
    Session,
    User,
)
from kedaibiao.security.credentials import env_key
from kedaibiao.tasks.llm import LLMConfig
from kedaibiao.tasks.pipeline import PipelineConfig

log = logging.getLogger(__name__)

JOB_HISTORY = 50
EVENT_HISTORY = 500
#: 每个平台保留多少行「详细日志」
PLATFORM_LOG_HISTORY = 400
OPTIONAL_DEPS = ("playwright", "fastapi", "uvicorn", "faster_whisper")


class ConsoleError(Exception):
    """带机器可读 ``kind`` 与 HTTP 状态码的受控错误。"""

    def __init__(self, message: str, *, kind: str = "bad_request", status: int = 400) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


@dataclass(frozen=True, slots=True)
class Viewer:
    """当前请求是谁在看。

    ``user_id=None`` 表示**未登录**，只允许内部/测试使用；HTTP 层在进入任何
    数据方法之前就会拦掉未登录请求（见 ``api/server.py`` 的鉴权门），
    所以生产路径上 viewer 一定是真实用户。
    """

    user_id: int | None = None
    is_superadmin: bool = False
    username: str = ""

    @property
    def authenticated(self) -> bool:
        return self.user_id is not None

    def owner_filter(self, requested: int | None = None) -> int | None:
        """把请求里的 ``?owner_id=`` 解析成实际用于过滤的归属。

        - 普通用户：只能是自己。指定别人 → 403（**不是**静默当成自己）；
        - 超级管理员：指定谁就是谁；不指定 = ``None`` = 不过滤（全量视图）；
        - 未登录：不过滤（内部/测试路径）。
        """
        if not self.authenticated:
            return requested
        if self.is_superadmin:
            return requested
        if requested is not None and int(requested) != self.user_id:
            raise ConsoleError(
                "无权查看其他用户的数据", kind="forbidden", status=403
            )
        return self.user_id

    def require_superadmin(self) -> None:
        if self.authenticated and not self.is_superadmin:
            raise ConsoleError("需要超级管理员权限", kind="forbidden", status=403)


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


def _friendly_platform_error(platform: str, exc: BaseException) -> str:
    """把平台异常翻成用户能照做的中文，并指出环境变量那条路已经不存在了。

    背景：适配器里的报错文案还停留在「设置 KEDAIBIAO_<平台>_USER/_PASS」，
    但凭据现在只从控制台账号库来 —— 照那句话做是不会生效的。
    """
    message = str(exc).strip() or type(exc).__name__
    if "KEDAIBIAO_" in message:
        message += (
            "\n注意：`.env` 里的平台账号已不再被读取。"
            "请在控制台「账号」页绑定该平台的账号。"
        )
    if "csrf" in message.lower() or "会话" in message:
        message += (
            "\n该平台的业务接口要求**浏览器会话 cookie**（csrf 与浏览器上下文绑定，"
            "密码登录只解决身份层）。需要用浏览器导出 cookie 的方式接入。"
        )
    return message


def _clean_essay(text: str) -> str:
    """去掉模型爱加的围栏/引号/标题行，只留正文。"""
    body = (text or "").strip()
    fence = re.search(r"```(?:[a-zA-Z]*)\s*(.+?)\s*```", body, re.S)
    if fence:
        body = fence.group(1).strip()
    lines = [ln for ln in body.splitlines() if not ln.strip().startswith("#")]
    body = "\n".join(lines).strip().strip('"').strip("“”").strip()
    return body


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ConsoleService:
    """控制台的全部数据与动作。线程安全（Web 服务是多线程的）。"""

    def __init__(
        self, *, root: str | Path = ".", settings: Settings | None = None,
        environ: dict[str, str] | None = None,
    ) -> None:
        self.root = Path(root)
        self._settings = settings  # 懒加载；每次跑批都会重新读配置
        #: 显式注入的环境变量（测试用）。``None`` 表示每次从 .env + os.environ 现算。
        self._environ: dict[str, str] | None = dict(environ) if environ is not None else None
        self._lock = threading.RLock()
        self._jobs: list[dict[str, Any]] = []
        self._jobs_by_id: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        #: 按平台分开的执行日志（终端式面板用）。每个平台独立一份，互不串味。
        self._platform_logs: dict[str, list[dict[str, Any]]] = {}
        self._seq = 0
        self._active_job_id: str | None = None
        #: 用户点了「停止」的任务 id（后台线程每个循环点检查一次）
        self._cancel_requested: set[str] = set()
        # 动作与适配器注册表都是进程级的，重复登记会覆盖，这里调用是幂等的。
        # 必须显式调用：程序化使用 ConsoleService 时不会经过 CLI 的 main()，
        # 否则会出现「平台数 0」这种看起来像坏了的结果。
        load_builtin_adapters()
        load_builtin_actions()

    # -- 配置与存储 -------------------------------------------------------

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            self._settings = load_settings(root=self.root, environ=self.environ)
        return self._settings

    @property
    def environ(self) -> dict[str, str]:
        """本次运行认的环境变量（``.env`` + 真实环境变量）。

        凭据与平台扫描都要走这一份，不能各自去读 ``os.environ`` ——
        否则 ``.env`` 里配的凭据在控制台上会显示成「未配置」。

        **每次现算**（除非构造时显式注入过）：用户改了 ``.env`` 之后刷新页面
        就该生效，不该要求重启服务。解析一个几百字节的文件，开销可以忽略。
        """
        if self._environ is not None:
            return self._environ
        return resolve_environ(self.root)

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

    # -- 大模型状态 -------------------------------------------------------

    #: 大模型字段 → 环境变量名（控制台覆盖值与 .env 走同一套键）
    LLM_FIELDS: dict[str, str] = {
        "base_url": "KEDAIBIAO_LLM_BASE_URL",
        "api_key": "KEDAIBIAO_LLM_API_KEY",
        "model": "KEDAIBIAO_LLM_MODEL",
        "timeout": "KEDAIBIAO_LLM_TIMEOUT",
        "temperature": "KEDAIBIAO_LLM_TEMPERATURE",
        "user_agent": "KEDAIBIAO_LLM_USER_AGENT",
    }

    def _llm_overrides(self) -> dict[str, str]:
        """控制台里保存的大模型覆盖值（键形如 ``llm.model``）。"""
        try:
            return OverrideStore(self._db_path()).all("llm.")
        except Exception:  # noqa: BLE001 - 读不到就当没覆盖
            log.debug("读取大模型覆盖配置失败", exc_info=True)
            return {}

    def _llm_environ(self) -> dict[str, str]:
        """把控制台覆盖值叠到环境变量上，得到**本次实际生效**的那一份。"""
        merged = dict(self.environ)
        for name, env_name in self.LLM_FIELDS.items():
            value = self._llm_overrides().get(f"llm.{name}")
            if value is not None and value != "":
                merged[env_name] = value
        return merged

    def llm_status(self) -> dict[str, Any]:
        """大模型答题的配置状态。**绝不返回 api_key 的值**，只说有没有设。"""
        cfg = LLMConfig.from_env(self._llm_environ())
        overrides = self._llm_overrides()
        from_console = sorted(k.split(".", 1)[1] for k in overrides if k.startswith("llm."))
        return {
            "enabled": cfg.enabled,
            "configured": cfg.enabled,
            "provider": "openai-compatible",
            "model": cfg.model,
            "api_base": cfg.base_url,
            # 只报告「有没有设」，不返回内容
            "api_key_set": bool(cfg.api_key),
            "timeout": cfg.timeout,
            "temperature": cfg.temperature,
            # 控制台可改：这些字段被界面覆盖了（其余取 .env）
            "editable": True,
            "override_fields": from_console,
            "source": "console" if from_console else ("env" if cfg.enabled else "none"),
            "configured_via": (
                "控制台「大模型」面板" if from_console
                else ("环境变量 KEDAIBIAO_LLM_*" if cfg.enabled else "")
            ),
        }

    def update_llm_config(self, viewer: Viewer | None, payload: dict[str, Any]) -> dict[str, Any]:
        """保存控制台里改的大模型配置。只有超管能改。

        只写 ``app_settings``，**不回写 .env**；字段留空 = 删掉覆盖项、回落到 .env。
        ``api_key`` 只进不出：界面永远拿不到明文，只看到「已设置 / 未设置」。
        """
        if not (viewer and viewer.is_superadmin):
            raise ConsoleError("只有超级管理员能改大模型配置", kind="forbidden", status=403)
        if not isinstance(payload, dict):
            raise ConsoleError("请求体必须是 JSON 对象", kind="bad_request", status=400)

        store = OverrideStore(self._db_path())
        updates: dict[str, str] = {}
        removals: list[str] = []

        base_url = str(payload.get("base_url") or "").strip()
        if "base_url" in payload:
            if base_url and not re.match(r"^https?://", base_url):
                raise ConsoleError(
                    "接口地址必须 http:// 或 https:// 开头",
                    kind="bad_request", status=400,
                )
            if base_url:
                updates["llm.base_url"] = base_url.rstrip("/")
            else:
                removals.append("llm.base_url")

        model = str(payload.get("model") or "").strip()
        if "model" in payload:
            if model:
                updates["llm.model"] = model
            else:
                removals.append("llm.model")

        if "api_key" in payload:
            key = str(payload.get("api_key") or "").strip()
            if key:
                updates["llm.api_key"] = key
        if payload.get("clear_api_key"):
            removals.append("llm.api_key")

        for name, low, high in (("timeout", 1.0, 600.0), ("temperature", 0.0, 2.0)):
            if name not in payload:
                continue
            raw = payload.get(name)
            try:
                value = float(raw)
            except (TypeError, ValueError):
                raise ConsoleError(f"{name} 必须是数字", kind="bad_request", status=400) from None
            if not (low <= value <= high):
                raise ConsoleError(
                    f"{name} 要在 {low:g}~{high:g} 之间", kind="bad_request", status=400
                )
            updates[f"llm.{name}"] = f"{value:g}"

        if "user_agent" in payload:
            ua = str(payload.get("user_agent") or "").strip()
            if ua:
                updates["llm.user_agent"] = ua
            else:
                removals.append("llm.user_agent")

        try:
            store.put_many(updates)
            store.delete([k for k in removals if k not in updates])
        except Exception as exc:  # noqa: BLE001
            raise ConsoleError(f"保存失败：{exc}", kind="internal", status=500) from exc
        return self.llm_status()

    def test_llm_connection(self, viewer: Viewer | None) -> dict[str, Any]:
        """用当前生效的配置真发一次最小请求，验证 key / 地址 / 模型是否可用。"""
        from kedaibiao.tasks.llm import LLMAnswerer, LLMError

        cfg = LLMConfig.from_env(self._llm_environ())
        if not cfg.enabled:
            raise ConsoleError(
                "大模型还没配好：至少要填接口地址和模型名", kind="bad_request", status=400
            )
        llm = LLMAnswerer(cfg)
        started = time.monotonic()
        try:
            reply = asyncio.run(llm.complete(
                "你是连通性测试助手。", "只回复两个字母：OK", max_tokens=64
            ))
        except LLMError as exc:
            raise ConsoleError(f"调用失败：{exc}", kind="platform_error", status=502) from exc
        except Exception as exc:  # noqa: BLE001
            raise ConsoleError(f"调用失败：{type(exc).__name__}: {exc}",
                               kind="platform_error", status=502) from exc
        return {
            "ok": True,
            "model": cfg.model,
            "api_base": cfg.base_url,
            "reply": (reply or "").strip()[:200],
            "latency_ms": int((time.monotonic() - started) * 1000),
        }

    # -- 只读接口 ---------------------------------------------------------

    def overview(
        self, platform: str | None = None, *, owner_id: int | None = None,
        viewer: Viewer | None = None,
    ) -> dict[str, Any]:
        scope = self._check_platform(platform)
        owner = (viewer or Viewer()).owner_filter(owner_id)
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
                    handle.bank.stats(scope, owner_id=owner),
                    handle.store.status_counts(platform=scope, owner_id=owner),
                )

            bank_stats, run_counts = self._read(collect)
            for key in ("total", "succeeded", "failed", "skipped", "running"):
                run_counts.setdefault(key, 0)

        bound = self._with_accounts(
            lambda st: {a.platform for a in st.list_for(owner)}
        )
        credentialed = [name for name in adapters.names() if name in bound]

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
            "llm": self.llm_status(),
            "bank": bank_stats,
            "runs": run_counts,
        }

    def platforms(self) -> dict[str, Any]:
        bound = self._with_accounts(lambda st: {a.platform for a in st.list_for()})
        items = []
        for name in adapters.names():
            cls = adapters.get(name)
            caps = getattr(cls, "capabilities", None)
            items.append({
                "name": name,
                "display_name": getattr(cls, "display_name", "") or name,
                "capabilities": _jsonable(caps) if caps is not None else {},
                "has_credentials": name in bound,
            })
        return {"platforms": items}

    def runs(
        self, limit: int = 50, platform: str | None = None, *,
        owner_id: int | None = None, viewer: Viewer | None = None,
    ) -> dict[str, Any]:
        scope = self._check_platform(platform)
        owner = (viewer or Viewer()).owner_filter(owner_id)
        if not self._db_path().is_file():
            return {"platform": scope, "runs": []}
        rows = self._read(
            lambda h: h.store.recent_runs(_clamp(limit, 1, 500), platform=scope, owner_id=owner)
        )
        return {"platform": scope, "runs": [_jsonable(r) for r in rows]}

    def submissions(
        self, limit: int = 50, platform: str | None = None, *,
        owner_id: int | None = None, viewer: Viewer | None = None,
    ) -> dict[str, Any]:
        scope = self._check_platform(platform)
        owner = (viewer or Viewer()).owner_filter(owner_id)
        if not self._db_path().is_file():
            return {"platform": scope, "submissions": []}
        rows = self._read(
            lambda h: h.store.recent_submissions(_clamp(limit, 1, 500), platform=scope, owner_id=owner)
        )
        return {"platform": scope, "submissions": [_jsonable(r) for r in rows]}

    def bank(
        self, platform: str | None = None, *, owner_id: int | None = None,
        viewer: Viewer | None = None,
    ) -> dict[str, Any]:
        scope = self._check_platform(platform)
        owner = (viewer or Viewer()).owner_filter(owner_id)
        empty: dict[str, Any] = {
            "total": 0, "graded": 0, "correct": 0, "accuracy": None,
            "ungraded": 0, "by_source": {}, "by_platform": {},
        }
        if not self._db_path().is_file():
            return {**empty, "platform": scope, "db_path": str(self._db_path())}
        stats = self._read(lambda h: h.bank.stats(scope, owner_id=owner))
        return {**stats, "platform": scope, "db_path": str(self._db_path())}

    def statistics(
        self, *, owner_id: int | None = None, viewer: Viewer | None = None
    ) -> dict[str, Any]:
        """跨平台对比。**刻意不接受 platform 参数** —— 它本身就是横向视图。

        归属仍然隔离：普通用户只统计自己的数据。
        """
        owner = (viewer or Viewer()).owner_filter(owner_id)
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
                handle.bank.stats(owner_id=owner),
                handle.bank.platform_stats(owner_id=owner),
                handle.store.platform_stats(owner_id=owner),
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
                "has_credentials": self._with_accounts(
                    lambda st, n=name: len(st.list_for(None, n)) > 0
                ),
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
            "llm": self.llm_status(),
            "platforms": platforms,
        }

    def _plog(self, platform: str, level: str, text: str) -> None:
        """追加一行「某平台的执行日志」。

        和 job 的 logs 分开存：job 属于**一次**任务，这里属于**一个平台**的历史。
        控制台的「详细日志」面板看的是这一份（切平台就换一份，互不干扰）。
        """
        name = str(platform or "")
        if not name:
            return
        with self._lock:
            lines = self._platform_logs.setdefault(name, [])
            lines.append({"at": _now(), "level": str(level), "text": str(text)[:600]})
            del lines[:-PLATFORM_LOG_HISTORY]

    def _plog_long(self, platform: str, level: str, text: str, *, max_chars: int = 6000) -> None:
        """把可能很长的内容（提示词 / 作文正文 / 批注）分行写进平台日志。

        终端面板里要能直接读到「我让它怎么改」「它改成了什么」，
        所以这里按行切、每行限长，而不是整段截断成一行。
        """
        body = str(text or "")[:max_chars]
        for raw in body.splitlines() or [""]:
            line = raw.rstrip()
            while len(line) > 300:
                self._plog(platform, level, line[:300])
                line = "    " + line[300:]
            self._plog(platform, level, line)

    def platform_logs(self, platform: str, limit: int = 200) -> dict[str, Any]:
        scope = self._check_platform(platform)
        with self._lock:
            lines = list(self._platform_logs.get(scope or "", []))
        return {
            "platform": scope,
            "logs": lines[-_clamp(limit, 1, 400):],
            "total": len(lines),
        }

    def events(self, limit: int = 100) -> dict[str, Any]:
        with self._lock:
            return {"events": list(self._events[-_clamp(limit, 1, EVENT_HISTORY):])}

    def jobs(
        self, platform: str | None = None, *, viewer: Viewer | None = None
    ) -> dict[str, Any]:
        scope = self._check_platform(platform)
        viewer = viewer or Viewer()
        with self._lock:
            items = [_jsonable(j) for j in reversed(self._jobs)]
        if scope:
            items = [j for j in items if j.get("platform") == scope]
        if viewer.authenticated and not viewer.is_superadmin:
            items = [j for j in items if j.get("owner_id") == viewer.user_id]
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
        ensure_generic_actions(scope)  # 课程/作业是所有平台都有的通用动作
        return {
            "platform": scope,
            "actions": [spec.to_dict() for spec in register_platform(scope)],
        }

    def run_action(
        self, platform: str, action: str, params: dict[str, Any] | None = None,
        *, viewer: Viewer | None = None,
    ) -> dict[str, Any]:
        scope = self._check_platform(platform)
        assert scope is not None
        ensure_generic_actions(scope)
        if not has_action(scope, action):
            available = ", ".join(s.name for s in register_platform(scope)) or "<无>"
            raise ConsoleError(
                f"{scope} 没有这个动作: {action!r}；可用: {available}",
                kind="unknown_action", status=404,
            )
        spec = next(s for s in register_platform(scope) if s.name == action)
        cleaned = self._validate_params(spec, params or {})
        detail = json.dumps(cleaned, ensure_ascii=False) if cleaned else ""
        self._plog(scope, "info", f"$ 动作 {action} {detail}".rstrip())
        try:
            result = asyncio.run(self._execute_action(scope, action, cleaned, viewer))
        except ConsoleError as exc:
            self._plog(scope, "error", f"✗ {action}：{exc}")
            raise
        except Exception as exc:  # noqa: BLE001
            self._plog(scope, "error", f"✗ {action}：{type(exc).__name__}: {exc}")
            raise
        envelope_kind = (result or {}).get("kind")
        title = (result or {}).get("title") or (result or {}).get("label") or ""
        self._plog(scope, "ok", f"→ {action} {envelope_kind or ''} {title}".rstrip())
        return result

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
        self, platform: str, action: str, params: dict[str, Any],
        viewer: Viewer | None = None,
    ) -> dict[str, Any]:
        """跑一个动作。每次都新建 Runtime —— 它们各自持有 sqlite 连接。

        ``local`` 动作（只读本地缓存 / 调外部模型）不连平台，跳过登录；
        其余动作登录用「当前账号」的 cookie 文件。
        """
        spec = next((s for s in register_platform(platform) if s.name == action), None)
        local = bool(getattr(spec, "local", False))
        creds, cookie_file = (None, None) if local else self._resolve_login(platform, viewer)

        runtime = build_runtime(
            # 大模型这类「控制台可改」的配置要叠进来，动作层才能看到界面上的值
            root=self.root, environ=self._llm_environ(), cookie_file=cookie_file,
            owner_id=viewer.user_id if (viewer and viewer.authenticated) else 0,
        )
        adapter: Any = None
        try:
            adapter = runtime.make_adapter(platform)

            if not local:
                # 有落盘的会话就先恢复：账号库不存密码，没有会话就只能要求重新登录
                restore_adapter_session(adapter, cookie_file)
                try:
                    logged_in = await adapter.refresh_session(creds)
                except Exception as exc:  # noqa: BLE001 - 平台异常一律翻成 502
                    raise ConsoleError(
                        f"{platform} 登录失败：{_friendly_platform_error(platform, exc)}",
                        kind=PLATFORM_ERROR, status=502,
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
            except ActionInputError as exc:
                # 参数不合法是 400，不是 502 —— 别把用户的输入错误说成平台故障
                raise ConsoleError(str(exc), kind="bad_request", status=400) from exc
            except Exception as exc:  # noqa: BLE001
                log.exception("平台动作失败: %s/%s", platform, action)
                raise ConsoleError(
                    f"{platform} 接口调用失败：{_friendly_platform_error(platform, exc)}",
                    kind=PLATFORM_ERROR, status=502,
                ) from exc
        finally:
            if adapter is not None and not local:
                # 落盘 cookie：下次点「课程/作业」时 refresh_session 能直接跳过登录，
                # 不必每次都重新登录（对平台礼貌，也快得多）
                try:
                    persist_http_cookies(adapter, runtime.settings)
                except Exception:  # noqa: BLE001 - 落盘失败不该影响已拿到的结果
                    log.debug("cookie 落盘失败", exc_info=True)
                await adapter.close()
            runtime.close()

    def recent_questions(
        self, limit: int = 50, *, platform: str | None = None,
        owner_id: int | None = None, viewer: Viewer | None = None,
    ) -> list[dict[str, Any]]:
        """最近的题目与作答（「详细作答信息」用）。"""
        scope = self._check_platform(platform)
        owner = (viewer or Viewer()).owner_filter(owner_id)
        if not self._db_path().is_file():
            return []
        return self._read(
            lambda h: h.bank.recent_questions(
                _clamp(limit, 1, 500), platform=scope, owner_id=owner
            )
        )

    # -- 认证与用户 -------------------------------------------------------

    def _with_auth(self, fn: Any) -> Any:
        store = AuthStore(self._db_path())
        try:
            return fn(store)
        finally:
            store.close()

    def _with_accounts(self, fn: Any) -> Any:
        store = AccountStore(self._db_path())
        try:
            return fn(store)
        finally:
            store.close()

    def bootstrap_admin(self) -> tuple[dict[str, Any] | None, str | None]:
        """首次启动创建超级管理员，并把「无归属」的历史数据认领给它。

        认领这一步很关键：升级前所有数据 ``owner_id=0``，如果不认领，
        登录后就"什么都看不见"，用户会以为数据丢了。
        """
        user, generated = self._with_auth(
            lambda st: st.ensure_bootstrap_admin()
        )
        if user is None:
            return None, None
        # 历史数据认领给首个超管
        def claim(handle: Any) -> None:
            handle.bank.claim_ownerless(user.id)

        if self._db_path().is_file():
            self._read(claim)
        return user.public(), generated

    def login(self, username: str, password: str) -> tuple[dict[str, Any], Session]:
        user = self._with_auth(lambda st: st.verify_login(username, password))
        if user is None:
            raise ConsoleError("账号或密码不正确", kind="unauthorized", status=401)
        if user.disabled:
            raise ConsoleError("该账号已被停用", kind="unauthorized", status=401)
        session = self._with_auth(lambda st: st.create_session(user.id))
        return user.public(), session

    def logout(self, token: str) -> None:
        if token:
            self._with_auth(lambda st: st.delete_session(token))

    def user_for_token(self, token: str) -> dict[str, Any] | None:
        session = self._with_auth(lambda st: st.get_session(token))
        if session is None:
            return None
        user = self._with_auth(lambda st: st.get_user_by_id(session.user_id))
        if user is None or user.disabled:
            return None
        return user.public()

    # -- 用户管理（仅超管）------------------------------------------------

    def users(self, viewer: Viewer) -> dict[str, Any]:
        viewer.require_superadmin()

        users = self._with_auth(lambda st: st.list_users())
        counts = self._with_accounts(lambda st: st.counts_by_owner())
        out = []
        for user in users:
            row = user.public()
            row["account_count"] = counts.get(user.id, 0)
            out.append(row)
        return {"users": out, "account_counts": counts}

    def create_user(
        self, viewer: Viewer, username: str, password: str, *,
        role: str = ROLE_USER, note: str = "",
    ) -> dict[str, Any]:
        viewer.require_superadmin()
        try:
            user = self._with_auth(
                lambda st: st.create_user(username, password, role=role, note=note)
            )
        except AuthError as exc:
            raise ConsoleError(str(exc), kind=exc.kind, status=exc.status) from exc
        return user.public()

    def update_user(self, viewer: Viewer, user_id: int, **fields: Any) -> dict[str, Any]:
        viewer.require_superadmin()

        def apply(st: AuthStore) -> User:
            if st.get_user_by_id(user_id) is None:
                raise ConsoleError("用户不存在", kind="not_found", status=404)
            if fields.get("password"):
                st.set_password(user_id, fields["password"])
            if fields.get("username"):
                st.rename_user(user_id, fields["username"])
            if fields.get("role"):
                st.set_role(user_id, fields["role"])
            if fields.get("disabled") is not None:
                # 不允许把自己停用（会把管理入口锁死）
                if int(user_id) == viewer.user_id and fields["disabled"]:
                    raise ConsoleError(
                        "不能停用当前登录的账号", kind="bad_request", status=400
                    )
                st.set_disabled(user_id, bool(fields["disabled"]))
            user = st.get_user_by_id(user_id)
            assert user is not None
            return user

        try:
            user = self._with_auth(apply)
        except AuthError as exc:
            raise ConsoleError(str(exc), kind=exc.kind, status=exc.status) from exc
        return user.public()

    def delete_user(self, viewer: Viewer, user_id: int) -> None:
        viewer.require_superadmin()
        if int(user_id) == viewer.user_id:
            raise ConsoleError("不能删除当前登录的账号", kind="bad_request", status=400)

        def apply(st: AuthStore) -> None:
            if st.get_user_by_id(user_id) is None:
                raise ConsoleError("用户不存在", kind="not_found", status=404)
            st.delete_user(user_id)

        try:
            self._with_auth(apply)
        except AuthError as exc:
            raise ConsoleError(str(exc), kind=exc.kind, status=exc.status) from exc
        # 连带清掉他的平台账号（数据行本身留着，便于超管追溯）
        self._with_accounts(lambda st: st.delete_for_owner(user_id))

    # -- 平台账号（多号切换）----------------------------------------------

    def _require_account(self, viewer: Viewer, account_id: int) -> PlatformAccount:
        account = self._with_accounts(lambda st: st.get(account_id))
        if account is None:
            raise ConsoleError("账号不存在", kind="not_found", status=404)
        if (
            viewer.authenticated
            and not viewer.is_superadmin
            and account.owner_id != viewer.user_id
        ):
            raise ConsoleError("无权操作其他用户的账号", kind="forbidden", status=403)
        return account

    def accounts(
        self, viewer: Viewer, *, platform: str | None = None,
        owner_id: int | None = None,
    ) -> dict[str, Any]:
        """账号列表。普通用户只看自己；超管可用 ``owner_id`` 指定某人。"""
        if platform:
            self._check_platform(platform)
        if not viewer.authenticated:
            target = owner_id
        elif viewer.is_superadmin:
            target = owner_id
        else:
            if owner_id is not None and int(owner_id) != viewer.user_id:
                raise ConsoleError("无权查看其他用户的账号", kind="forbidden", status=403)
            target = viewer.user_id
        rows = self._with_accounts(lambda st: st.list_for(target, platform))
        return {
            "platform": platform,
            "owner_id": target,
            "accounts": [a.public() for a in rows],
        }

    def add_account(
        self, viewer: Viewer, platform: str, *,
        label: str = "", username: str = "", password: str = "",
        login_now: bool = True, owner_id: int | None = None,
    ) -> dict[str, Any]:
        self._check_platform(platform)
        if not viewer.authenticated:
            raise ConsoleError("未登录", kind="unauthorized", status=401)
        if owner_id is not None and not viewer.is_superadmin and int(owner_id) != viewer.user_id:
            raise ConsoleError("无权操作其他用户的账号", kind="forbidden", status=403)
        target = owner_id if (viewer.is_superadmin and owner_id is not None) else viewer.user_id
        if not username.strip():
            raise ConsoleError("请填写平台用户名", kind="bad_request", status=400)

        account = self._with_accounts(
            lambda st: st.add(target, platform, label=label, username=username)
        )
        if login_now and password:
            ok, note = self._login_account(account, password)
            self._with_accounts(lambda st: st.record_login(account.id, ok=ok, note=note))
            if not ok:
                raise ConsoleError(
                    f"{platform} 登录失败：{note}", kind=PLATFORM_ERROR, status=502
                )
        fresh = self._with_accounts(lambda st: st.get(account.id))
        return (fresh or account).public()

    def activate_account(self, viewer: Viewer, account_id: int) -> dict[str, Any]:
        account = self._require_account(viewer, account_id)
        self._with_accounts(
            lambda st: st.set_active(account.owner_id, account.platform, account.id)
        )
        fresh = self._with_accounts(lambda st: st.get(account.id))
        return (fresh or account).public()

    def login_account(self, viewer: Viewer, account_id: int, password: str) -> dict[str, Any]:
        account = self._require_account(viewer, account_id)
        ok, note = self._login_account(account, password)
        self._with_accounts(lambda st: st.record_login(account.id, ok=ok, note=note))
        if not ok:
            raise ConsoleError(
                f"{account.platform} 登录失败：{note}", kind=PLATFORM_ERROR, status=502
            )
        fresh = self._with_accounts(lambda st: st.get(account.id))
        return (fresh or account).public()

    def update_account(
        self, viewer: Viewer, account_id: int, *,
        label: str | None = None, username: str | None = None,
    ) -> dict[str, Any]:
        account = self._require_account(viewer, account_id)
        try:
            updated = self._with_accounts(
                lambda st: st.update(account.id, label=label, username=username)
            )
        except AccountError as exc:
            raise ConsoleError(str(exc), kind=exc.kind, status=exc.status) from exc
        return updated.public()

    def delete_account(self, viewer: Viewer, account_id: int) -> None:
        account = self._require_account(viewer, account_id)
        self._with_accounts(lambda st: st.delete(account.id))
        try:
            settings = load_settings(root=self.root, environ=self.environ)
            cookie = account.cookie_path(settings.session_dir)
            cookie.unlink(missing_ok=True)
            # 会话快照也是凭据，一并删掉
            session_path(cookie).unlink(missing_ok=True)
        except OSError:
            log.debug("删除会话文件失败", exc_info=True)

    def _login_account(self, account: PlatformAccount, password: str) -> tuple[bool, str]:
        """用给定密码登录该账号，成功则把 cookie 落到**它自己的文件**里。

        密码只在这里用一次，不落盘。
        """
        settings = load_settings(root=self.root, environ=self.environ)
        cookie_file = account.cookie_path(settings.session_dir)
        runtime = build_runtime(
            root=self.root, environ=self.environ, cookie_file=str(cookie_file)
        )
        adapter: Any = None
        try:
            adapter = runtime.make_adapter(account.platform)
            creds = Credentials(username=account.username, password=password)
            ok = asyncio.run(adapter.login(creds))
            if ok:
                persist_http_cookies(adapter, settings)
                # 有些平台的会话不只在 cookie 里（bearer token 等），
                # 不把会话本身存下来，下次调用就只剩空密码 → 报「需要账号密码」
                save_adapter_session(adapter, cookie_file)
                return True, ""
            return False, "平台拒绝了这次登录（账号或密码不对？）"
        except Exception as exc:  # noqa: BLE001 - 平台异常统一成失败原因
            log.warning("账号 %s 登录异常: %s", account.id, exc)
            return False, f"{type(exc).__name__}: {exc}"
        finally:
            if adapter is not None:
                try:
                    asyncio.run(adapter.close())
                except Exception:  # noqa: BLE001
                    log.debug("关闭适配器失败", exc_info=True)
            runtime.close()

    def _resolve_login(
        self, platform: str, viewer: Viewer | None
    ) -> tuple[Credentials, str]:
        """这次请求用哪个号、哪个 cookie 文件。

        **只认账号库里绑定过的账号**，不再回退到 ``.env``。

        为什么去掉 .env 回退：两套来源并存时，用户永远搞不清「底部用的是哪个
        号」，而且 .env 里是明文密码。现在只有一条路 —— 在控制台「账号」页绑定，
        密码只在登录那一刻用一次，之后靠该账号自己的 cookie 文件。
        """
        settings = load_settings(root=self.root, environ=self.environ)
        owner = viewer.user_id if (viewer and viewer.authenticated) else None
        try:
            try:
                return self._with_accounts(
                    lambda st: st.resolve_credentials(
                        platform, owner_id=owner, session_dir=settings.session_dir
                    )
                )
            except AccountError:
                # 超级管理员没给自己绑号时，退回「全量视角」：命令行/早期绑定过的
                # 账号 owner_id=0，管理员在控制台里也应该能用（否则页面上看得到
                # 账号，点动作却报「还没有绑定账号」）。
                if not (viewer and viewer.is_superadmin):
                    raise
                return self._with_accounts(
                    lambda st: st.resolve_credentials(
                        platform, owner_id=None, session_dir=settings.session_dir
                    )
                )
        except AccountError as exc:
            raise ConsoleError(str(exc), kind=exc.kind, status=exc.status) from exc

    # -- 作文：评分循环 / 手动取分 / 历史 ---------------------------------

    def _essay_http(self, platform: str, viewer: Viewer | None) -> tuple[Any, Any, Any]:
        """返回 (adapter, http, cookie_file)，并已恢复会话。"""
        creds, cookie_file = self._resolve_login(platform, viewer)
        settings = load_settings(root=self.root, environ=self.environ)
        runtime = build_runtime(root=self.root, environ=self.environ,
                                cookie_file=cookie_file)
        adapter = runtime.make_adapter(platform)
        restore_adapter_session(adapter, cookie_file)
        return adapter, runtime, cookie_file

    def start_grade_loop(
        self,
        platform: str,
        rid: str,
        *,
        target_score: float,
        max_attempts: int = 3,
        initial_essay: str = "",
        viewer: Viewer | None = None,
        analysis_timeout: float = 180.0,
        resubmit_without_score: bool = False,
    ) -> dict[str, Any]:
        """启动「写→交→看批注→改写→再交」的后台任务。

        ``analysis_timeout`` 是**等批改出分**的上限（秒）。超时不算失败：稿子已经交了，
        任务会以 ``stop_reason=timeout`` 停下，前端提供「继续等待 N 秒 / 手动获取」。
        """
        scope = self._check_platform(platform)
        assert scope is not None
        if scope != "pigai":
            raise ConsoleError(
                "评分循环目前只实现了 pigai（其他平台的提交还没落地）",
                kind="not_supported", status=501,
            )
        if not rid.strip():
            raise ConsoleError("缺少作业号 rid", kind="bad_request", status=400)
        if not (0 < float(target_score) <= 100):
            raise ConsoleError("目标分数要在 0~100 之间", kind="bad_request", status=400)
        if int(max_attempts) < 1:
            raise ConsoleError("轮次至少 1 轮", kind="bad_request", status=400)
        analysis_timeout = float(analysis_timeout or 180.0)
        if not (20.0 <= analysis_timeout <= 1800.0):
            raise ConsoleError("等分上限要在 20~1800 秒之间", kind="bad_request", status=400)

        with self._lock:
            self._seq += 1
            job_id = f"g-{self._seq}"
            job: dict[str, Any] = {
                "job_id": job_id, "platform": scope, "rid": rid.strip(),
                "target_score": float(target_score), "max_attempts": int(max_attempts),
                "analysis_timeout": analysis_timeout,
                "resubmit_without_score": bool(resubmit_without_score),
                "status": "running", "started_at": _now(), "finished_at": None,
                "events": [], "attempts": [], "stop_reason": "", "message": "",
                "best_score": None, "initial_essay": initial_essay,
                "owner_id": viewer.user_id if (viewer and viewer.authenticated) else 0,
            }
            self._jobs.append(job)
            self._jobs_by_id[job_id] = job
            del self._jobs[:-JOB_HISTORY]
            for stale in list(self._jobs_by_id):
                if stale not in {j["job_id"] for j in self._jobs}:
                    del self._jobs_by_id[stale]

        threading.Thread(
            target=self._run_grade_loop, args=(job_id, scope, rid.strip(),
                                               float(target_score), int(max_attempts),
                                               initial_essay, viewer, analysis_timeout,
                                               bool(resubmit_without_score)),
            name=f"kedaibiao-grade-{job_id}", daemon=True,
        ).start()
        return _jsonable(job)

    # -- iTEST：一键自动化（取卷 → 转写答题 → 填 → 核验 → 复核 → 待人工/交卷）--

    def start_itest_auto(
        self, exam_id: str, *, viewer: Viewer | None = None,
        autosave_wait: float | None = None,
    ) -> dict[str, Any]:
        """启动「一键自动化」后台任务，返回 job（内含 logs 供控制台实时滚动）。"""
        scope = self._check_platform("itest")
        assert scope is not None
        exam_id = str(exam_id or "").strip()
        if not exam_id:
            raise ConsoleError("缺少考试号（exam_id）", kind="bad_request", status=400)

        with self._lock:
            self._seq += 1
            job_id = f"a-{self._seq}"
            job: dict[str, Any] = {
                "job_id": job_id, "kind": "itest-auto", "platform": "itest",
                "exam_id": exam_id, "status": "running",
                "started_at": _now(), "finished_at": None,
                "logs": [], "steps": [], "verify": None, "manual": [],
                "paper": None, "error": "", "owner_id":
                    viewer.user_id if (viewer and viewer.authenticated) else 0,
            }
            self._jobs.append(job)
            self._jobs_by_id[job_id] = job
            del self._jobs[:-JOB_HISTORY]
            for stale in list(self._jobs_by_id):
                if stale not in {j["job_id"] for j in self._jobs}:
                    del self._jobs_by_id[stale]

        threading.Thread(
            target=self._run_itest_auto,
            args=(job_id, exam_id, viewer, autosave_wait),
            name=f"kedaibiao-itest-auto-{job_id}", daemon=True,
        ).start()
        return _jsonable(job)

    def itest_job(self, job_id: str) -> dict[str, Any]:
        return self.grade_job(job_id)  # 共用同一份任务存储

    def _job_log(self, job_id: str, level: str, text: str) -> None:
        """追加一行日志（控制台按行滚动显示，格式像命令输出）。"""
        with self._lock:
            job = self._jobs_by_id.get(job_id)
            if job is None:
                return
            job["logs"].append({"at": _now(), "level": level, "text": str(text)})
            del job["logs"][:-800]
            # 同一行也进「平台日志」—— 控制台切到该平台就能翻到这次任务的输出
            name = str(job.get("platform") or "")
            if name:
                lines = self._platform_logs.setdefault(name, [])
                lines.append({"at": _now(), "level": str(level), "text": str(text)[:600]})
                del lines[:-PLATFORM_LOG_HISTORY]

    def _job_step(self, job_id: str, name: str, ok: bool, detail: str = "") -> None:
        with self._lock:
            job = self._jobs_by_id.get(job_id)
            if job is None:
                return
            job["steps"].append({
                "name": name, "ok": bool(ok), "detail": str(detail),
                "at": _now(),
            })

    def _run_itest_auto(
        self, job_id: str, exam_id: str, viewer: Viewer | None,
        autosave_wait: float | None,
    ) -> None:
        import time as _time

        from kedaibiao.platforms.itest import cache as itest_cache
        from kedaibiao.platforms.itest.answer import build_sheet
        from kedaibiao.platforms.itest.exam import ExamRunner, cookies_from_jar
        from kedaibiao.platforms.itest.paper import KIND_RADIO
        from kedaibiao.tasks.llm import LLMAnswerer, LLMConfig

        runtime = None
        adapter = None
        try:
            # 实测：iTEST 的自动保存只认页面里「真实交互」产生的答案模型，
            # 我们用脚本设值 + 派发事件填进去的值**不会**被它缓存（重进页面读回是空）。
            # 所以默认不等自动保存；需要观察持久化的可以显式传 autosave_wait。
            wait = autosave_wait
            if wait is None:
                try:
                    wait = float(self.environ.get("KEDAIBIAO_ITEST_AUTOSAVE_WAIT", "0"))
                except (TypeError, ValueError):
                    wait = 0.0

            self._job_log(job_id, "info", f"$ kedaibiao itest auto --exam {exam_id}")
            creds, cookie_file = self._resolve_login("itest", viewer)
            runtime = build_runtime(
                root=self.root, environ=self._llm_environ(), cookie_file=cookie_file,
                owner_id=viewer.user_id if (viewer and viewer.authenticated) else 0,
            )
            adapter = runtime.make_adapter("itest")
            restore_adapter_session(adapter, cookie_file)
            self._job_log(job_id, "info", "[1/6] 登录 iTEST…")
            if not asyncio.run(adapter.refresh_session(creds)):
                raise ConsoleError("登录失败（会话已失效，请到「账号」页重新登录）",
                                   kind=PLATFORM_ERROR, status=502)
            self._job_log(job_id, "ok", "[1/6] 登录成功")
            self._job_step(job_id, "登录", True)

            client = adapter.client()
            jar = getattr(getattr(client, "http", None), "transport", None)
            cookies = cookies_from_jar(getattr(jar, "cookies", None))
            if not cookies:
                raise ConsoleError("没有可用的会话 cookie", kind=PLATFORM_ERROR, status=502)

            entry = asyncio.run(adapter.exam_meta(exam_id))
            ks_id = str(((getattr(entry, "raw", None) or {}).get("ksId")) or "")
            settings = getattr(runtime, "settings", None)
            cache_dir = itest_cache.cache_dir(settings)

            # ① 取卷（会开始/继续计时）
            self._job_log(job_id, "info", "[2/6] 进入考场取卷（★ 计时开始）…")
            # with 里才会启动浏览器并注入 cookie
            with ExamRunner(cookies) as runner:
                entered = runner.enter(exam_id, ks_id=ks_id)
                for step in entered.steps:
                    self._job_log(job_id, "ok" if step.ok else "warn",
                                  f"       · {step.name} {step.detail}".rstrip())
                if not entered.ok:
                    raise ConsoleError(f"没能进入作答页：{entered.message}",
                                       kind=PLATFORM_ERROR, status=502)
                paper = runner.read_paper()
            itest_cache.write_json(
                itest_cache.paper_path(settings, exam_id), paper.to_dict()
            )
            self._job_log(
                job_id, "ok",
                f"[2/6] 取卷完成：{len(paper.sections)} 大题 / {paper.question_count} 题 / "
                f"{paper.blank_count} 槽位",
            )
            self._job_step(job_id, "取卷", True, f"{paper.question_count} 题")

            # ② 转写 + AI 答题
            self._job_log(job_id, "info", "[3/6] 音频转写 + 大模型答题…")
            config = LLMConfig.from_env(self._llm_environ())
            if not config.enabled:
                raise ConsoleError("大模型未配置 —— 先在设置页填接口地址/模型/Key",
                                   kind="bad_request", status=400)
            self._job_log(job_id, "info", f"       模型：{config.model}")
            sheet, transcripts, stt_note = asyncio.run(build_sheet(
                paper, LLMAnswerer(config), cache_dir,
                environ=self._llm_environ(),
            ))
            if stt_note:
                self._job_log(job_id, "info", f"       {stt_note}")
            self._job_log(
                job_id, "ok",
                f"[3/6] 答题完成：{sheet.answered} 题有答案 / 跳过 {len(sheet.skipped)} 题",
            )
            for item in sheet.skipped:
                self._job_log(job_id, "warn",
                              f"       · 跳过 {item.get('qid')}（{item.get('kind')}）："
                              f"{item.get('reason', '')[:60]}")
            itest_cache.write_json(
                itest_cache.answers_path(settings, exam_id),
                {"answers": sheet.answers, "skipped": sheet.skipped,
                 "transcripts": {str(k): v for k, v in transcripts.items()}},
            )
            self._job_step(job_id, "AI 答题", True,
                           f"{sheet.answered} 题，跳过 {len(sheet.skipped)}")

            answers = dict(sheet.answers)

            # ③ 填答 + 同会话核验
            missing = [q.qid for s in paper.sections for q in s.questions
                       if not str(answers.get(q.qid) or "").strip()]
            filled: dict[str, Any] = {}
            checked: dict[str, Any] = {}
            with ExamRunner(cookies) as runner:
                self._job_log(job_id, "info", "[4/6] 进入作答页填答案…")
                entered = runner.enter(exam_id, ks_id=ks_id)
                if not entered.ok:
                    raise ConsoleError(f"没能进入作答页：{entered.message}",
                                       kind=PLATFORM_ERROR, status=502)
                if answers:
                    filled = runner.fill(answers)
                    checked = runner.verify(answers)
                self._job_log(
                    job_id, "ok",
                    f"[4/6] 填表：填空 {filled.get('blank', 0)} / 单选 {filled.get('radio', 0)} / "
                    f"翻译 {filled.get('textarea', 0)}；核验不符 {checked.get('total', 0)} 处",
                )
                self._job_step(
                    job_id, "填答案", True, json.dumps(filled, ensure_ascii=False)
                )
                self._job_step(job_id, "逐项核验", not checked.get("total"),
                               f"不符 {checked.get('total', 0)} 处")
                for bad in (checked.get("bad") or [])[:10]:
                    self._job_log(job_id, "warn", f"       · 核验不符：{bad}")
                if wait > 0:
                    self._job_log(
                        job_id, "info",
                        f"[5/6] 等平台自动保存（{wait:.0f}s，iTEST 每分钟存一次）…",
                    )
                    _time.sleep(wait)

            # ④ 重新抓一遍复核（新会话，验证真的落到服务端）
            self._job_log(job_id, "info", "[5/6] 复核：重新进考场抓一遍页面…")
            with ExamRunner(cookies) as runner:
                entered = runner.enter(exam_id, ks_id=ks_id)
                if not entered.ok:
                    raise ConsoleError(f"复核时进不去作答页：{entered.message}",
                                       kind=PLATFORM_ERROR, status=502)
                current = runner.read_answers()

            matched = sum(
                1 for qid, want in answers.items()
                if str(current.get(qid) or "").strip() == str(want).strip()
            )
            mismatched = [
                {"qid": qid, "want": want, "got": current.get(qid, "")}
                for qid, want in answers.items()
                if str(current.get(qid) or "").strip() != str(want).strip()
            ]
            persisted = matched > 0
            if mismatched and not persisted:
                # 全是空 → 平台没把脚本填的值缓存下来（实测如此），不是填错
                self._job_log(
                    job_id, "info",
                    f"[5/6] 重进考场读回：页面是空的（{matched}/{len(answers)}）—— "
                    "iTEST 的自动保存不认脚本填值；交卷时会重新填写并核验，不影响结果。",
                )
                self._job_step(job_id, "复核", True,
                               "服务端未落库（交卷时重填并核验）")
            else:
                self._job_log(
                    job_id, "ok" if not mismatched else "warn",
                    f"[5/6] 复核完成：{matched}/{len(answers)} 题与答案单一致"
                    + ("" if not mismatched else f"，{len(mismatched)} 题不一致"),
                )
                self._job_step(job_id, "复核", not mismatched,
                               f"{matched}/{len(answers)} 一致")

            # ⑤ 需要人工的题
            manual_rows: list[dict[str, Any]] = []
            for section in paper.sections:
                for question in section.questions:
                    if str(answers.get(question.qid) or "").strip():
                        continue
                    manual_rows.append({
                        "qid": question.qid,
                        "kind": question.kind,
                        "kind_label": {"blank": "填空", "radio": "单选",
                                       "write": "翻译"}.get(question.kind, question.kind),
                        "stem": (question.text or "")[:160],
                        "sentences": question.sentences[:6],
                        "slots": question.slot_count,
                        "options": (
                            [{"letter": c.letter, "text": c.text, "sub": c.sub}
                             for c in question.choices]
                            if question.kind == KIND_RADIO else []
                        ),
                        "answer": "",
                    })
            with self._lock:
                job = self._jobs_by_id.get(job_id)
                if job is not None:
                    job["manual"] = manual_rows
                    job["verify"] = {
                        "total": len(answers), "matched": matched,
                        "mismatched": mismatched, "missing": missing,
                        "filled": filled, "checked": checked,
                        "persisted": persisted,
                    }
                    job["paper"] = {
                        "title": next((s.part1 for s in paper.sections if s.part1), paper.title),
                        "question_count": paper.question_count,
                        "slot_count": paper.blank_count,
                    }
            if manual_rows:
                self._job_log(
                    job_id, "warn",
                    f"[6/6] 还有 {len(manual_rows)} 题没有答案 —— "
                    "请在下方手动选择后点「保存并复核」",
                )
            else:
                self._job_log(job_id, "ok", "[6/6] 全部题目都有答案，可以确认交卷")

            self._job_step(job_id, "完成", True)
            self._job_log(job_id, "ok", "自动化流程结束（未交卷）。")
            with self._lock:
                job = self._jobs_by_id.get(job_id)
                if job is not None:
                    job["status"] = "succeeded"
        except ConsoleError as exc:
            self._job_log(job_id, "error", f"✗ {exc}")
            with self._lock:
                job = self._jobs_by_id.get(job_id)
                if job is not None:
                    job["status"] = "failed"
                    job["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001 - 后台任务必须自己收尾
            log.exception("iTEST 自动化失败")
            self._job_log(job_id, "error", f"✗ {type(exc).__name__}: {exc}")
            with self._lock:
                job = self._jobs_by_id.get(job_id)
                if job is not None:
                    job["status"] = "failed"
                    job["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if adapter is not None:
                with contextlib.suppress(Exception):
                    asyncio.run(adapter.close())
            if runtime is not None:
                with contextlib.suppress(Exception):
                    runtime.close()
            with self._lock:
                job = self._jobs_by_id.get(job_id)
                if job is not None:
                    job["finished_at"] = _now()

    def save_itest_manual(
        self, exam_id: str, answers: dict[str, str], *, viewer: Viewer | None = None
    ) -> dict[str, Any]:
        """把手动补的答案并进答案单缓存，返回最新的缺项清单。"""
        from kedaibiao.platforms.itest import cache as itest_cache
        from kedaibiao.platforms.itest.paper import Paper

        settings = self.settings
        exam_id = str(exam_id or "").strip()
        if not exam_id:
            raise ConsoleError("缺少考试号（exam_id）", kind="bad_request", status=400)
        if not isinstance(answers, dict) or not answers:
            raise ConsoleError("没有要保存的答案", kind="bad_request", status=400)

        paper_raw = itest_cache.read_json(itest_cache.paper_path(settings, exam_id))
        if paper_raw is None:
            raise ConsoleError("还没有这场考试的试卷缓存，先跑一次「一键自动化」",
                               kind="bad_request", status=400)
        paper = Paper.from_dict(paper_raw)
        sheet = itest_cache.read_json(itest_cache.answers_path(settings, exam_id)) or {}
        merged = {str(k): str(v) for k, v in (sheet.get("answers") or {}).items()}
        for qid, value in answers.items():
            text = str(value or "").strip()
            if text:
                merged[str(qid)] = text
        skipped = [s for s in (sheet.get("skipped") or [])
                   if str(s.get("qid")) not in merged]
        itest_cache.write_json(
            itest_cache.answers_path(settings, exam_id),
            {"answers": merged, "skipped": skipped,
             "transcripts": sheet.get("transcripts") or {}},
        )
        missing = [q.qid for s in paper.sections for q in s.questions
                   if not str(merged.get(q.qid) or "").strip()]
        return {
            "exam_id": exam_id,
            "answered": len(merged),
            "missing": missing,
            "can_submit": not missing,
            "answers": merged,
        }

    def grade_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            found = self._jobs_by_id.get(job_id)
        if found is None:
            raise ConsoleError(f"没有这个任务: {job_id}", kind="not_found", status=404)
        return _jsonable(found)

    def _run_grade_loop(self, job_id: str, platform: str, rid: str,
                        target: float, attempts: int, initial_essay: str,
                        viewer: Viewer | None, analysis_timeout: float = 180.0,
                        resubmit_without_score: bool = False) -> None:
        from kedaibiao.platforms.pigai.grade_loop import GradeLoop
        from kedaibiao.tasks.llm import LLMConfig, LLMAnswerer

        runtime = None
        adapter = None
        try:
            adapter, runtime, cookie_file = self._essay_http(platform, viewer)
            settings = runtime.settings

            page = None
            try:
                page = asyncio.run(adapter.fetch_assignment_by_rid(rid))
            except Exception:  # noqa: BLE001 - 拿不到题目信息也能继续
                page = None
            topic = (page.title if page else "") or ""
            word_range = (page.word_range if page else "") or ""

            essay = initial_essay.strip()
            if not essay:
                essay = self._write_initial_essay(settings, topic, word_range, page)
            if not essay.strip():
                raise ConsoleError(
                    "没有初始正文，且大模型未配置无法起草。"
                    "请配置 KEDAIBIAO_LLM_* 或在请求里带上 initial_essay。",
                    kind="bad_request", status=400,
                )

            rewrite = None
            cfg = LLMConfig.from_env(runtime.environ)
            if cfg.enabled:
                llm = LLMAnswerer(cfg)
                from kedaibiao.platforms.pigai.grade_loop import REWRITE_SYSTEM, build_rewrite_prompt

                async def rewrite(snapshot, current, next_attempt):  # noqa: ANN001
                    prompt = build_rewrite_prompt(
                        topic=topic, word_range=word_range, essay=current,
                        snapshot=snapshot, attempt=next_attempt,
                    )
                    # 提示词、改后正文都进日志 —— 用户要能在这里直接看到「让它怎么改」和「改成了什么」
                    self._plog(platform, "info",
                               f"— 第 {next_attempt} 轮改写提示词（{len(prompt)} 字）—")
                    self._plog_long(platform, "info", prompt, max_chars=6000)
                    text = await llm.complete(REWRITE_SYSTEM, prompt)
                    cleaned = _clean_essay(text)
                    self._plog(platform, "ok",
                               f"— 第 {next_attempt} 轮改写完成：{len(cleaned.split())} 词 —")
                    self._plog_long(platform, "ok", cleaned, max_chars=6000)
                    return cleaned

            def on_event(event: dict[str, Any]) -> None:
                with self._lock:
                    job = self._jobs_by_id.get(job_id)
                    if job is None:
                        return
                    job["events"].append({"at": _now(), **_jsonable(event)})
                    del job["events"][:-400]
                    kind = str(event.get("kind") or "")
                    if kind:
                        extra = ""
                        if event.get("score") is not None:
                            extra = f" {event.get('score')} 分"
                        elif event.get("message"):
                            extra = f" {event.get('message')}"
                        lines = self._platform_logs.setdefault("pigai", [])
                        lines.append({
                            "at": _now(),
                            "level": "error" if "fail" in kind else "info",
                            "text": f"· {kind}{extra}"[:600],
                        })
                        del lines[:-PLATFORM_LOG_HISTORY]
                    if event.get("kind") == "attempt.graded":
                        score = event.get("score")
                        if isinstance(score, (int, float)):
                            current = job.get("best_score")
                            job["best_score"] = (
                                score if not isinstance(current, (int, float))
                                else max(current, score)
                            )

            loop = GradeLoop(
                adapter=adapter,
                http=adapter.ctx.client(),
                base="https://www.pigai.org",
                target_score=target, max_attempts=attempts,
                on_event=on_event, rewrite=rewrite,
                word_range=word_range,
                analysis_timeout=analysis_timeout,
                resubmit_without_score=resubmit_without_score,
            )
            result = asyncio.run(loop.run(rid=rid, topic=topic, initial_essay=essay))

            # 落库每一轮
            if runtime.store is not None:
                owner = viewer.user_id if (viewer and viewer.authenticated) else 0
                for rec in result.attempts:
                    try:
                        runtime.store.record_attempt(
                            owner_id=owner, platform=platform, rid=rid,
                            eid=rec.eid, attempt=rec.attempt, snapshot=rec,
                        )
                    except Exception:  # noqa: BLE001
                        log.debug("记录尝试失败", exc_info=True)

            with self._lock:
                job = self._jobs_by_id[job_id]
                job["status"] = "succeeded" if result.stop_reason == "reached" else "failed"
                job["stop_reason"] = result.stop_reason
                job["message"] = result.message
                job["attempts"] = [a.to_dict() for a in result.attempts]
                job["best_score"] = result.best_score
        except Exception as exc:  # noqa: BLE001
            log.exception("评分循环异常")
            with self._lock:
                job = self._jobs_by_id.get(job_id)
                if job is not None:
                    job["status"] = "failed"
                    job["message"] = f"{type(exc).__name__}: {exc}"
        finally:
            if adapter is not None:
                try:
                    asyncio.run(adapter.close())
                except Exception:  # noqa: BLE001
                    pass
            if runtime is not None:
                runtime.close()
            with self._lock:
                job = self._jobs_by_id.get(job_id)
                if job is not None:
                    job["finished_at"] = _now()

    def _write_initial_essay(self, settings: Any, topic: str, word_range: str,
                             page: Any) -> str:
        """没给初始正文时，用大模型按题目要求起草。"""
        from kedaibiao.tasks.llm import LLMAnswerer, LLMConfig

        cfg = LLMConfig.from_env(self._llm_environ())
        if not cfg.enabled:
            return ""
        requirement = getattr(page, "requirement", "") or topic
        prompt = (
            f"题目/要求：{requirement}\n"
            f"字数要求：{word_range or '120~150'}\n"
            "请写一篇符合要求的英语作文，只输出正文。"
        )
        try:
            return _clean_essay(asyncio.run(LLMAnswerer(cfg).complete(
                "你是英语写作助手，只输出英文正文，不要标题与解释。", prompt)))
        except Exception:  # noqa: BLE001
            log.warning("起草失败", exc_info=True)
            return ""

    @staticmethod
    def _latest_eid(adapter: Any, rid: str) -> str:
        """按作业号找最新一篇的稿件号。

        控制台「只取分」按钮只知道 rid（作业号），不知道 eid（稿件号）；
        拿空 eid 去请求会得到「没找到作文」。所以缺 eid 时就地查一次列表。
        """
        import asyncio

        try:
            rows = asyncio.run(adapter.list_finished_essays())
        except Exception:  # noqa: BLE001 - 查不到就交给调用方报错
            return ""
        for row in rows:
            if str(getattr(row, "rid", "")) == str(rid):
                return str(getattr(row, "eid", "") or "")
        return ""

    def _log_snapshot(self, platform: str, rid: str, eid: str, snap: Any) -> None:
        """把「分数 + 四维 + 总评 + 逐条批注 + 正文」写进平台日志。

        控制台的执行日志面板要能直接看到平台给了什么评价、作文长什么样 ——
        这是判断「按什么改写」的依据，不该只留在界面卡片里。
        """
        data = snap.to_dict() if hasattr(snap, "to_dict") else dict(snap or {})
        score = data.get("score")
        if score is None:
            self._plog(platform, "warn",
                       f"· 取分 {rid}/{eid}：暂无分数（{data.get('raw_state') or '待分析'}）")
            return
        self._plog(platform, "ok",
                   f"· 取分 {rid}/{eid}：{score} / {data.get('full_marks') or '—'} 分"
                   f"（{data.get('raw_state') or '—'}）")
        dims = data.get("dimensions") or []
        if dims:
            self._plog(platform, "info", "  维度：" + "，".join(
                f"{d.get('name')} {round(float(d.get('ratio') or 0) * 100)}%"
                for d in dims
            ))
        overall = str(data.get("overall_comment") or "").strip()
        if overall:
            self._plog_long(platform, "info", "  总评：" + overall)
        feedback = data.get("feedback_lines") or []
        if feedback:
            self._plog(platform, "info", f"  批注 {len(feedback)} 条：")
            for item in feedback[:20]:
                self._plog_long(platform, "info", "    - " + str(item), max_chars=600)
        essay = str(data.get("essay_text") or "").strip()
        if essay:
            self._plog(platform, "info",
                       f"  作文正文（{len(essay.split())} 词）：")
            self._plog_long(platform, "info", essay, max_chars=4000)

    def fetch_essay_grade(self, platform: str, rid: str, eid: str, *,
                          viewer: Viewer | None = None,
                          trigger_analysis: bool = True) -> dict[str, Any]:
        """手动取一次分数与评价（不提交任何东西）。"""
        scope = self._check_platform(platform)
        assert scope is not None
        if scope != "pigai":
            raise ConsoleError("取分目前只实现了 pigai", kind="not_supported", status=501)
        adapter, runtime, _ = self._essay_http(scope, viewer)
        try:
            from kedaibiao.platforms.pigai import grading

            eid = str(eid or "").strip()
            if not eid:
                eid = self._latest_eid(adapter, rid)
                if eid:
                    self._plog(scope, "info", f"· 未给稿件号，自动取最新：{eid}")
            http = adapter.ctx.client()
            if trigger_analysis:
                snap = asyncio.run(grading.wait_for_grade(
                    adapter, http, base="https://www.pigai.org", rid=rid, eid=eid))
            else:
                snap = asyncio.run(grading.fetch_snapshot(adapter, rid=rid, eid=eid))
            self._log_snapshot(scope, rid, eid, snap)
            return snap.to_dict()
        finally:
            asyncio.run(adapter.close())
            runtime.close()

    def wait_essay_grade(
        self, rid: str, eid: str = "", *, platform: str = "pigai",
        timeout: float = 180.0, viewer: Viewer | None = None,
    ) -> dict[str, Any]:
        """**再等一段时间的批改结果**（不重新提交）。

        评分循环以 ``stop_reason=timeout`` 停下后，用户可以带着稿件号再等 N 秒，
        或者直接取一次分。超时**不抛 502** —— 返回 ``timed_out=True`` 让前端继续问用户。
        """
        scope = self._check_platform(platform)
        assert scope is not None
        if scope != "pigai":
            raise ConsoleError("等分目前只实现了 pigai", kind="not_supported", status=501)
        rid = str(rid or "").strip()
        if not rid:
            raise ConsoleError("缺少作业号 rid", kind="bad_request", status=400)
        timeout = float(timeout or 180.0)
        if not (5.0 <= timeout <= 1800.0):
            raise ConsoleError("等待时长要在 5~1800 秒之间", kind="bad_request", status=400)

        from kedaibiao.platforms.pigai import grading

        adapter, runtime, _ = self._essay_http(scope, viewer)
        try:
            eid = str(eid or "").strip()
            if not eid:
                eid = self._latest_eid(adapter, rid)
            http = adapter.ctx.client()
            self._plog(scope, "info", f"$ 等批改 {rid}/{eid or '—'} 最多 {timeout:.0f}s")
            try:
                snap = asyncio.run(grading.wait_for_grade(
                    adapter, http, base="https://www.pigai.org",
                    rid=rid, eid=eid, timeout=timeout,
                ))
            except grading.AnalysisTimeout as exc:
                self._plog(scope, "warn", f"· 仍未出分（{exc}），可以继续等或手动获取")
                return {
                    "rid": rid, "eid": eid, "graded": False, "timed_out": True,
                    "message": f"{exc}。稿子已经交了，只是还没批完；可以再等一段或稍后手动获取。",
                }
            out = snap.to_dict()
            out.update({"timed_out": False})
            self._log_snapshot(scope, rid, eid, snap)
            self._plog(scope, "ok",
                       f"→ 出分 {out.get('score') if out.get('graded') else '未出分'}"
                       f"（{out.get('raw_state') or '—'}）")
            return out
        finally:
            with contextlib.suppress(Exception):
                asyncio.run(adapter.close())
            runtime.close()

    def essay_history(self, rid: str, *, platform: str = "pigai",
                      viewer: Viewer | None = None) -> dict[str, Any]:
        """某作业的历史提交记录（每一轮的分数与批注）。"""
        owner = (viewer or Viewer()).owner_filter(None)
        if not self._db_path().is_file():
            return {"rid": rid, "attempts": []}
        rows = self._read(lambda h: h.store.attempts_for(
            rid, owner_id=owner, platform=platform))
        return {"rid": rid, "platform": platform, "attempts": rows}

    # -- 单个作业：详情与提交 ---------------------------------------------

    def assignment_detail(
        self, platform: str, assignment_id: str, *, viewer: Viewer | None = None
    ) -> dict[str, Any]:
        """作业详情（控制台双击作业行时调用）。走通用动作，返回展示信封。"""
        if not str(assignment_id or "").strip():
            raise ConsoleError("缺少任务号", kind="bad_request", status=400)
        return self.run_action(
            platform, "detail", {"assignment_id": str(assignment_id).strip()}, viewer=viewer
        )

    def submit_assignment(
        self,
        platform: str,
        assignment_id: str,
        *,
        viewer: Viewer | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """针对**单个作业**作答并提交。

        ``confirm=False``（默认）= 只规划不提交，返回「打算交什么」。
        ``confirm=True`` 才会真的调平台的写接口。

        平台没实现提交时**明确拒绝**（501 not_supported），而不是假装成功 ——
        现在 pigai 仍是 ``capabilities.submit = False``；unipus 已实现但不能真交
        （默认干跑，真发要 ``submit_live``）。
        """
        scope = self._check_platform(platform)
        assert scope is not None
        cls: Any = adapters.get(scope)
        caps = getattr(cls, "capabilities", None)
        display = getattr(cls, "display_name", None) or scope
        if caps is not None and not getattr(caps, "submit", False):
            raise ConsoleError(
                f"{display} 尚未实现提交（capabilities.submit = False）。"
                f"当前平台只能读数据；提交需要先按真实抓包实现该平台的写接口。",
                kind="not_supported", status=501,
            )

        assignment_id = str(assignment_id or "").strip()
        if not assignment_id:
            raise ConsoleError("缺少任务号", kind="bad_request", status=400)

        self._plog(scope, "info",
                   f"$ 提交 {assignment_id}（{'真交，不可逆' if confirm else '干跑，不提交'}）")
        try:
            result = asyncio.run(
                self._submit_one(scope, assignment_id, viewer=viewer, confirm=confirm)
            )
        except ConsoleError as exc:
            self._plog(scope, "error", f"✗ 提交 {assignment_id}：{exc}")
            raise
        except Exception as exc:  # noqa: BLE001
            self._plog(scope, "error",
                       f"✗ 提交 {assignment_id}：{type(exc).__name__}: {exc}")
            raise
        if isinstance(result, dict):
            if result.get("dry_run", True) is False or result.get("submitted") is not None:
                self._plog(scope, "ok",
                           f"→ 提交完成 submitted={result.get('submitted')} "
                           f"finish={result.get('server_finish')}")
            else:
                self._plog(scope, "ok",
                           f"→ 干跑 {result.get('question_count', '?')} 题，"
                           f"有答案 {result.get('answered', '?')} 题")
        return result

    async def _submit_one(
        self, platform: str, assignment_id: str, *, viewer: Viewer | None, confirm: bool
    ) -> dict[str, Any]:
        creds, cookie_file = self._resolve_login(platform, viewer)
        settings = load_settings(root=self.root, environ=self.environ)
        owner_id = viewer.user_id if (viewer and viewer.authenticated) else 0
        runtime = build_runtime(
            # 大模型这类「控制台可改」的配置要叠进来，动作层才能看到界面上的值
            root=self.root, environ=self._llm_environ(), cookie_file=cookie_file,
            owner_id=owner_id,
        )
        adapter: Any = None
        try:
            adapter = runtime.make_adapter(platform)
            restore_adapter_session(adapter, cookie_file)
            if not await adapter.refresh_session(creds):
                raise ConsoleError(
                    f"{platform} 登录失败（会话可能已失效，请到「账号」页重新登录）",
                    kind=PLATFORM_ERROR, status=502,
                )

            target = None
            for course in await adapter.list_courses():
                for item in await adapter.list_assignments(course):
                    if item.remote_id == assignment_id:
                        target = item
                        break
                if target is not None:
                    break
            if target is None:
                raise ConsoleError(
                    f"在 {platform} 里找不到作业 {assignment_id!r}",
                    kind="not_found", status=404,
                )

            # 原生作答链路的平台（iTEST）：取题/交卷都在浏览器里做，
            # 不走通用的 fetch_questions → answerer → submit 三段式。
            if getattr(adapter, "native_submit_flow", False):
                if not confirm:
                    plan = await adapter.native_plan(assignment_id)
                    plan.setdefault("title", target.title)
                    return plan
                result = await adapter.native_submit(assignment_id)
                result.setdefault("title", target.title)
                return result

            questions = list(target.questions)
            if not questions and getattr(adapter.capabilities, "fetch_questions", False):
                questions = await adapter.fetch_questions(target)
            if not questions:
                raise ConsoleError(
                    "没有取到题目，无法作答", kind="bad_request", status=400
                )

            answers = await runtime.pipeline.answerer.answer(questions)
            answerable = [a for a in answers if not a.is_empty()]
            weakest = min((a.confidence for a in answerable), default=0.0)
            plan = {
                "platform": platform,
                "assignment_id": assignment_id,
                "title": target.title,
                "question_count": len(questions),
                "answered": len(answerable),
                "min_confidence": round(weakest, 4),
                "threshold": runtime.settings.min_confidence,
                "dry_run": not confirm,
                "answers": [
                    {
                        "index": i + 1,
                        "stem": (a.question.stem or "")[:120],
                        "choices": a.choices,
                        "text": a.text,
                        "source": a.source.value,
                        "confidence": a.confidence,
                        "needs_human": a.confidence < runtime.settings.min_confidence,
                    }
                    for i, a in enumerate(answers)
                ],
            }

            if not confirm:
                plan["submitted"] = False
                plan["note"] = "这是干跑结果，没有向平台提交任何内容。确认无误后带 confirm=true 再调用。"
                return plan

            if not answerable:
                raise ConsoleError("一题都没答出来，拒绝提交", kind="bad_request", status=400)
            if weakest < runtime.settings.min_confidence:
                raise ConsoleError(
                    f"最低置信度 {weakest:.2f} 低于阈值 {runtime.settings.min_confidence:.2f}，"
                    f"拒绝提交（宁可交白卷也不交错答案）",
                    kind="bad_request", status=400,
                )

            result = await adapter.submit(target, answers)
            if runtime.store is not None:
                runtime.store.record_submission(
                    platform, result, owner_id=owner_id
                )
            plan["submitted"] = True
            plan["ok"] = result.ok
            plan["score"] = result.score
            plan["message"] = result.message
            return plan
        finally:
            if adapter is not None:
                try:
                    persist_http_cookies(adapter, settings)
                    save_adapter_session(adapter, cookie_file)
                except Exception:  # noqa: BLE001
                    log.debug("保存会话失败", exc_info=True)
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
        viewer: Viewer | None = None,
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
        creds, cookie_file = self._resolve_login(platform, viewer)
        owner_id = viewer.user_id if (viewer and viewer.authenticated) else 0

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
                "owner_id": owner_id,
            }
            self._jobs.append(job)
            self._jobs_by_id[job_id] = job
            del self._jobs[:-JOB_HISTORY]
            for stale in list(self._jobs_by_id):
                if stale not in {j["job_id"] for j in self._jobs}:
                    del self._jobs_by_id[stale]
            self._active_job_id = job_id

        self._plog(platform, "info",
                   f"$ 跑批 {job_id}（{'live 真跑' if live else '干跑'}，"
                   f"kinds={sorted(k.value for k in parsed_kinds) or '全部'}，"
                   f"limit={limit or '不限'}）")
        thread = threading.Thread(
            target=self._execute,
            args=(job_id, platform, bool(live), limit, parsed_kinds, owner_id,
                  cookie_file, creds),
            name=f"kedaibiao-job-{job_id}",
            daemon=True,
        )
        thread.start()
        return _jsonable(job)

    # -- 刷课（按教材组织 + 任务模式时长 + 未完成任务提交）------------------

    def course_tree(
        self, platform: str, course_remote_id: str, *, viewer: Viewer | None = None,
        with_state: bool = True, state_scope: str = "required",
    ) -> dict[str, Any]:
        """一本书的单元/任务树（只读）。控制台「刷课」页点进教材时调用。"""
        scope = self._check_platform(platform)
        assert scope is not None
        course_remote_id = str(course_remote_id or "").strip()
        if not course_remote_id:
            raise ConsoleError("缺少课程号（course）", kind="bad_request", status=400)
        creds, cookie_file = self._resolve_login(scope, viewer)
        runtime = build_runtime(
            root=self.root, environ=self._llm_environ(), cookie_file=cookie_file,
            owner_id=viewer.user_id if (viewer and viewer.authenticated) else 0,
        )
        adapter: Any = None
        try:
            adapter = runtime.make_adapter(scope)
            restore_adapter_session(adapter, cookie_file)
            if not asyncio.run(adapter.refresh_session(creds)):
                raise ConsoleError(
                    f"{scope} 登录失败（会话可能已失效，请到「账号」页重新登录）",
                    kind=PLATFORM_ERROR, status=502,
                )
            if not hasattr(adapter, "native_course_tree"):
                raise ConsoleError(
                    f"{scope} 没有教材树能力（native_course_tree）",
                    kind="not_supported", status=501,
                )
            target = None
            for course in asyncio.run(adapter.list_courses()):
                if str(course.remote_id) == course_remote_id:
                    target = course
                    break
            if target is None:
                raise ConsoleError(
                    f"在 {scope} 里找不到课程 {course_remote_id!r}",
                    kind="not_found", status=404,
                )
            tree = asyncio.run(
                adapter.native_course_tree(
                    target, with_state=bool(with_state), state_scope=state_scope
                )
            )
            return _jsonable(tree)
        finally:
            if adapter is not None:
                with contextlib.suppress(Exception):
                    asyncio.run(adapter.close())
            runtime.close()

    def start_brush(
        self,
        platform: str,
        *,
        course: str,
        minutes: float = 10.0,
        confirm: bool = False,
        submit_pending: bool = False,
        only_required: bool = True,
        interval: float = 20.0,
        max_groups: int = 60,
        subjective_placeholder: str = "",
        llm_subjective: bool = False,
        mode: str = "",
        viewer: Viewer | None = None,
    ) -> dict[str, Any]:
        """刷课：一本书 →（可选）提交未完成任务 + 任务模式挂时长。

        ``mode`` 明确只做哪一半：``"submit"``（只提交未完成）、``"duration"``
        （只挂时长）、``"both"``；留空时按 ``submit_pending`` 推断。

        安全默认：``confirm=False`` 只出计划（不提交、不建连接）。
        ``submit_pending=True`` 才会尝试提交「没做过」的任务组；
        已做过的组**不会重交**（平台留存首条记录，实测重交不改分）。
        """
        scope = self._check_platform(platform)
        assert scope is not None
        course = str(course or "").strip()
        if not course:
            raise ConsoleError("缺少课程号（course）", kind="bad_request", status=400)
        cls: Any = adapters.get(scope)
        if not getattr(cls, "native_submit_flow", False):
            raise ConsoleError(
                f"{getattr(cls, 'display_name', scope)} 没有原生刷课链路",
                kind="not_supported", status=501,
            )
        try:
            minutes = float(minutes)
        except (TypeError, ValueError) as exc:
            raise ConsoleError("minutes 必须是数字", kind="bad_request", status=400) from exc
        if not (0.5 <= minutes <= 240):
            raise ConsoleError("minutes 要在 0.5~240 之间", kind="bad_request", status=400)
        interval = max(5.0, min(float(interval or 20.0), 120.0))
        try:
            max_groups = int(max_groups or 60)
        except (TypeError, ValueError):
            max_groups = 60
        max_groups = max(1, min(max_groups, 500))
        mode = str(mode or "").strip().lower()
        if mode and mode not in ("submit", "duration", "both"):
            raise ConsoleError("mode 只能是 submit / duration / both", kind="bad_request", status=400)
        if not mode:
            mode = "both" if submit_pending else "duration"
        submit_pending = mode in ("submit", "both")
        do_duration = mode in ("duration", "both")

        creds, cookie_file = self._resolve_login(scope, viewer)
        owner_id = viewer.user_id if (viewer and viewer.authenticated) else 0

        with self._lock:
            if self._active_job_id is not None:
                raise ConsoleError(
                    f"已有任务在跑（{self._active_job_id}），同一时刻只允许一个。",
                    kind="busy", status=409,
                )
            self._seq += 1
            job_id = f"r-{self._seq}"
            job: dict[str, Any] = {
                "job_id": job_id, "kind": "brush", "platform": scope,
                "course": course, "minutes": minutes, "confirm": bool(confirm),
                "submit_pending": bool(submit_pending), "only_required": bool(only_required),
                "mode": mode, "max_groups": max_groups,
                "status": "running", "started_at": _now(), "finished_at": None,
                "summary": "", "error": "", "logs": [],
                "counts": {"submitted": 0, "skipped": 0, "failed": 0,
                           "targets": 0, "credited_seconds": 0},
                "owner_id": owner_id,
            }
            self._jobs.append(job)
            self._jobs_by_id[job_id] = job
            del self._jobs[:-JOB_HISTORY]
            for stale in list(self._jobs_by_id):
                if stale not in {j["job_id"] for j in self._jobs}:
                    del self._jobs_by_id[stale]
            self._active_job_id = job_id

        self._plog(
            scope, "info",
            f"$ 刷课 {job_id}（{'真跑' if confirm else '干跑'}；{minutes:g} 分钟；"
            f"course={course}；提交未完成={submit_pending}）",
        )
        threading.Thread(
            target=self._run_brush,
            args=(job_id, scope, course, minutes, bool(confirm), bool(submit_pending),
                  bool(only_required), interval, owner_id, cookie_file, creds,
                  do_duration, mode, max_groups, str(subjective_placeholder or ""),
                  bool(llm_subjective)),
            name=f"kedaibiao-brush-{job_id}", daemon=True,
        ).start()
        return _jsonable(job)

    async def _llm_fill_group(
        self, job_id: str, adapter: Any, group: dict[str, Any]
    ) -> dict[str, list[str]]:
        """对「服务端没有标准答案」的题组用大模型生成内容。

        只处理翻译/写作/主观类（``needs_answer``），返回
        ``{instanceId: [子题文本...]}`` 供提交时覆盖。
        生成内容会写进日志（截断）便于人工复查。
        """
        from kedaibiao.tasks.llm import LLMAnswerer, LLMConfig

        config = LLMConfig.from_env(self._llm_environ())
        if not config.enabled:
            raise ConsoleError(
                "大模型未配置（控制台「设置」页填 BASE_URL 与 MODEL 后再试）",
                kind="bad_request", status=400,
            )
        plan = await adapter.native_plan(group["assignment_id"])
        rows = plan.get("questions") or []
        answerer = LLMAnswerer(config)
        overrides: dict[str, list[str]] = {}
        for row in rows:
            # 服务端有标准答案就不用模型：标准答案更准（翻译/阅读常带答案）
            if row.get("has_standard_answer"):
                continue
            if not (row.get("needs_answer") or row.get("wants_text")):
                continue
            instance_id = str(row.get("instanceId") or "")
            stem = str(row.get("stem") or "").strip()
            if not instance_id or not stem:
                continue
            kind = str(row.get("kind") or "")
            children = max(int(row.get("child_count") or 1), 1)
            # 判断题面语言：含中文才走「翻译」，英文题（阅读理解/简答）走「作答」。
            # 之前按题型标签一律当翻译，英文题会被喂「翻译助手」的提示，
            # 网关对此常返回空内容（实测）。
            has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in stem)
            if (kind == "translation" or "翻译" in str(group.get("type_label") or "")) and has_cjk:
                system = (
                    "你是英语翻译助手。把用户给的汉语逐句翻译成自然、准确的英语；"
                    "只输出译文，不要解释。"
                )
                if children > 1:
                    user = (
                        f"下面共有 {children} 句中文。请逐句翻译，**每句一行、共 {children} 行**，"
                        f"行首不要加编号或符号：\n{stem}"
                    )
                else:
                    user = f"请把下面的内容翻译成英语：\n{stem}"
            elif kind == "writing" or "写作" in str(group.get("type_label") or ""):
                system = (
                    "你是大学英语写作助手。按题目要求写一篇结构完整、"
                    "语言自然的英语短文，只输出正文。"
                )
                user = f"题目要求：\n{stem}"
            else:
                system = (
                    "你是大学英语答题助手。用英语简明作答，只输出答案正文。"
                )
                user = f"题目：\n{stem}"
            text = ""
            last_error = ""
            for attempt in range(1, 4):     # 网关会偶发返回空串/超时，重试 2 次
                try:
                    text = (await answerer.complete(system, user, max_tokens=800) or "").strip()
                except Exception as exc:  # noqa: BLE001
                    last_error = f"{type(exc).__name__}: {exc}"
                    text = ""
                if text:
                    break
                last_error = last_error or "网关返回空内容"
                self._job_log(
                    job_id, "warn",
                    f"  · 生成失败第 {attempt}/3 次（{last_error[:60]}）：{stem[:36]}…",
                )
                await asyncio.sleep(2.0 * attempt)
            if not text:
                self._job_log(
                    job_id, "error",
                    f"  ✗ 大模型未生成内容，该题跳过（原因：{last_error or '空响应'}）",
                )
                continue
            text = (text or "").strip()
            if not text:
                continue
            import re as _re

            parts = [
                _re.sub(r"^\s*\d+[.、)]\s*", "", part).strip()
                for part in text.split("\n")
                if part.strip()
            ]
            if children == 1:
                values = [text]
            elif len(parts) >= children:
                values = parts[:children]
            else:
                values = parts + [""] * (children - len(parts))
            overrides[instance_id] = values
            self._job_log(
                job_id, "info",
                f"  · 大模型作答（{instance_id}）：{text[:80].replace(chr(10), ' ')}…",
            )
        return overrides

    def _run_brush(
        self, job_id: str, platform: str, course_id: str, minutes: float,
        confirm: bool, submit_pending: bool, only_required: bool, interval: float,
        owner_id: int, cookie_file: str | None, credentials: Any,
        do_duration: bool = True, mode: str = "both", max_groups: int = 60,
        subjective_placeholder: str = "",
        llm_subjective: bool = False,
    ) -> None:
        from kedaibiao.platforms.unipus import duration as duration_mod

        runtime = None
        adapter: Any = None
        counts = {"submitted": 0, "skipped": 0, "failed": 0, "targets": 0,
                  "credited_seconds": 0}
        try:
            runtime = build_runtime(
                root=self.root, environ=self._llm_environ(),
                cookie_file=cookie_file, owner_id=owner_id,
            )
            adapter = runtime.make_adapter(platform)
            if subjective_placeholder and hasattr(adapter, "subjective_placeholder"):
                adapter.subjective_placeholder = str(subjective_placeholder)
                self._job_log(
                    job_id, "warn",
                    f"· 翻译/写作将用占位文本「{subjective_placeholder}」提交"
                    f"（服务端没有标准答案；能得完成分但内容不是人写的）",
                )
            restore_adapter_session(adapter, cookie_file)
            self._job_log(job_id, "info", "$ 恢复登录会话…")
            if not asyncio.run(adapter.refresh_session(credentials)):
                raise RuntimeError("登录失败：请到「账号」页重新登录一次")
            self._job_log(job_id, "ok", "✓ 会话就绪")

            course = None
            for item in asyncio.run(adapter.list_courses()):
                if str(item.remote_id) == course_id:
                    course = item
                    break
            if course is None:
                raise RuntimeError(f"找不到课程 {course_id}")
            self._job_log(job_id, "info", f"→ {course.name}")
            tree = asyncio.run(adapter.native_course_tree(course, with_state=True))
            groups = [g for unit in tree["units"] for g in unit["groups"]]
            required = [g for g in groups if g["required"]]
            pending = [
                g for g in groups
                if not g.get("finished") and (g["required"] or not only_required)
            ]
            self._job_log(
                job_id, "info",
                f"  {tree['group_count']} 个任务组，必修 {tree['required_count']}；"
                f"未完成 {len(pending)}（范围：{'仅必修' if only_required else '全部'}）",
            )

            stopped_any = False
            if len(pending) > max_groups:
                self._job_log(
                    job_id, "warn",
                    f"· 本轮最多提交 {max_groups} 组（剩余 {len(pending) - max_groups} 组未处理，"
                    f"可再跑一次；勾选「只处理必修」能少交很多）",
                )
                pending = pending[:max_groups]
            if not only_required:
                non_required = [g for g in pending if not g.get("required")]
                if non_required:
                    self._job_log(
                        job_id, "warn",
                        f"· 未勾选「只处理必修」：这 {len(non_required)} 组不在班级必修里，"
                        f"提交不会计入门户进度",
                    )
            if submit_pending:
                rate_limited = 0
                permission_denied = 0
                aborted = False
                stopped = False
                estimated_minutes = (len(pending) * (interval + 1.5)) / 60 if confirm else 0
                if confirm and pending:
                    self._job_log(
                        job_id, "info",
                        f"· 预计耗时约 {estimated_minutes:.0f} 分钟"
                        f"（{len(pending)} 组 × 约 {interval + 1.5:.0f}s）；"
                        f"随时可点「停止任务」",
                    )
                for index, group in enumerate(pending):
                    if self._cancelled(job_id):
                        stopped = True
                        stopped_any = True
                    if stopped or aborted:
                        counts["skipped"] += 1
                        continue
                    if not confirm:
                        counts["skipped"] += 1
                        self._job_log(
                            job_id, "info",
                            f"· 干跑：将提交 {group['name']}（{group['unit_name']}）",
                        )
                        continue
                    overrides: dict[str, list[str]] = {}
                    if llm_subjective and group.get("needs_answer"):
                        try:
                            overrides = asyncio.run(
                                self._llm_fill_group(job_id, adapter, group)
                            )
                        except Exception as exc:  # noqa: BLE001 - 生成失败就退回无覆盖
                            self._job_log(
                                job_id, "warn",
                                f"  · 大模型作答失败（{type(exc).__name__}: {exc}），"
                                f"该组退回占位/空答案",
                            )
                    if group.get("needs_answer") and not overrides and not subjective_placeholder:
                        counts["failed"] += 1
                        self._job_log(
                            job_id, "error",
                            f"✗ {group['name']} 跳过提交：服务端无标准答案，"
                            f"且大模型没有生成内容（空提交会记 0 分，而平台留存首条记录、"
                            f"之后改不了）",
                        )
                        continue
                    try:
                        result = asyncio.run(
                            adapter.native_submit(
                                group["assignment_id"], answer_overrides=overrides or None
                            )
                        )
                        counts["submitted"] += 1
                        rate_limited = 0
                        self._job_log(
                            job_id, "ok",
                            f"✓ 已提交 {group['name']}：code={result.get('code')!r}",
                        )
                        if index < len(pending) - 1:
                            if self._sleep_interruptible(job_id, interval):
                                stopped = True
                                stopped_any = True
                    except Exception as exc:  # noqa: BLE001
                        counts["failed"] += 1
                        code = getattr(exc, "code", None)
                        is_limit = type(exc).__name__ == "UnipusRateLimited"
                        if is_limit:
                            rate_limited += 1
                        elif code == 100002:
                            permission_denied += 1
                        hint = ""
                        if code == 100002:
                            activated = tree.get("activated")
                            state_text = {True: "已激活", False: "**未激活**"}.get(
                                activated, "激活状态未知"
                            )
                            hint = (
                                f"（没有权限；教材{state_text}：未激活的教材无法提交，"
                                "需要先在网页端输入激活码；也可能是任务在本班还没打开过）"
                            )
                        self._job_log(
                            job_id, "error",
                            f"✗ {group['name']} 提交失败：{type(exc).__name__}: {exc}{hint}",
                        )
                        if is_limit:
                            # 平台明确说「休息几分钟」：继续打只会把号打进更长的冷却
                            wait = float(getattr(exc, "retry_after", 0) or 0)
                            self._job_log(
                                job_id, "warn",
                                f"  ⚠️ 平台限流（{code}）：停止本轮提交，剩余 "
                                f"{len(pending) - index - 1} 个未尝试。建议等 "
                                f"{max(wait, 180):.0f} 秒以上再跑。",
                            )
                            aborted = True
                        elif permission_denied >= 2:
                            self._job_log(
                                job_id, "warn",
                                "  ⚠️ 连续 2 个任务「没有权限」（100002）：停止本轮提交。"
                                "常见原因是这本教材还没激活（网页端需输入激活码）"
                                "或任务在本班还没打开过。",
                            )
                            aborted = True
                        elif rate_limited >= 2:
                            aborted = True
            else:
                self._job_log(job_id, "info", "· 未勾选「提交未完成后任务」，只刷时长")

            if not do_duration:
                counts["targets"] = 0
                summary = (
                    f"完成：提交 {counts['submitted']}，跳过 {counts['skipped']}，"
                    f"失败 {counts['failed']}（未刷时长）"
                )
                with self._lock:
                    job = self._jobs_by_id.get(job_id)
                    if job is not None:
                        job["counts"] = counts
                self._finish_batch_job(job_id, "done", summary=summary)
                self._job_log(job_id, "ok", f"■ {summary}")
                self._plog(platform, "ok", f"■ 刷课 {job_id} {summary}")
                return

            # 任务模式时长：目标 = 必修组（没有必修就用全部）
            targets_pool = required or groups
            targets = [
                duration_mod.TaskTarget(
                    instance=tree["instance_id"],
                    unit=g["unit_id"],
                    group=g["id"],
                    name=g["name"],
                )
                for g in targets_pool
            ][:40]
            counts["targets"] = len(targets)
            report = duration_mod.run_task_duration(
                adapter.client().session.open_id,
                adapter.client().auth_headers(
                    __import__("kedaibiao.platforms.unipus.endpoints", fromlist=["UnipusApp"]).UnipusApp.UCONTENT
                )["x-annotator-auth-token"],
                targets,
                seconds=minutes * 60,
                page_base=tree["page_base"],
                confirm=confirm,
            )
            counts["credited_seconds"] = int(report.credited_seconds)
            for line in report.plan.warnings:
                self._job_log(job_id, "warn", f"⚠️ {line}")
            if not confirm:
                self._job_log(job_id, "info", f"· 干跑计划：{report.plan.summary()}")
                self._job_log(job_id, "info", f"· 将连：{duration_mod.redact_duration_url(report.plan.url)}")
            else:
                self._job_log(
                    job_id, "info",
                    f"· 任务模式：start {report.starts_sent} 次 / stop {report.stops_sent} 次 / "
                    f"轮换 {report.rotations}；记账 {report.credited_seconds:.0f} 秒；"
                    f"错误：{report.error or '无'}",
                )
            prefix = "已停止" if stopped_any else "完成"
            summary = (
                f"{prefix}：提交 {counts['submitted']}，跳过 {counts['skipped']}，"
                f"失败 {counts['failed']}，时长目标 {counts['targets']} 个，"
                f"记账 {counts['credited_seconds']} 秒"
            )
            with self._lock:
                job = self._jobs_by_id.get(job_id)
                if job is not None:
                    job["counts"] = counts
            self._finish_batch_job(job_id, "done", summary=summary)
            self._job_log(job_id, "ok", f"■ {summary}")
            self._plog(platform, "ok", f"■ 刷课 {job_id} {summary}")
        except Exception as exc:  # noqa: BLE001
            message = f"{type(exc).__name__}: {exc}"
            self._finish_batch_job(job_id, "failed", error=message)
            self._job_log(job_id, "error", f"✗ {message}")
            self._plog(platform, "error", f"✗ 刷课 {job_id}：{message}")
            log.exception("刷课失败: %s", job_id)
        finally:
            if adapter is not None:
                with contextlib.suppress(Exception):
                    asyncio.run(adapter.close())
            if runtime is not None:
                with contextlib.suppress(Exception):
                    runtime.close()
            with self._lock:
                if self._active_job_id == job_id:
                    self._active_job_id = None

    # -- 批量提交（原生提交链路平台，例如 U校园）---------------------------

    def start_batch_submit(
        self,
        platform: str,
        *,
        course: str = "",
        confirm: bool = False,
        limit: int = 0,
        interval: float = 8.0,
        skip_done: bool = True,
        viewer: Viewer | None = None,
    ) -> dict[str, Any]:
        """一键全部提交：后台任务，逐组「读状态 → 干跑计划 →（确认后）真交」。

        - ``confirm=False``（默认）= **只规划不提交**，回报「哪些会交、哪些跳过/被拦」；
        - ``confirm=True`` 才逐组真的写平台（不可逆），组间默认停 ``interval`` 秒；
        - ``skip_done=True``（默认）跳过**所有已完成**（``first_pass`` 有值）的组。
          为什么连"没全对"也跳过：2026-10-20 实测，平台的留存策略
          ``record_every_submit=false`` —— 一组只保留首条记录，重交**不会改分**
          （提交前后 ``state.score`` 完全一致）。想覆盖旧记录只能人工在网页端重做；
        - ``course`` 可填课程号或课程名片段，留空 = 全部课程；
        - ``limit`` 最多处理多少个任务（0 = 不限），给首次试跑限流用。

        任务进度写进 ``job.logs``（同时进平台日志），前端按 ``job_id`` 轮询
        ``GET /api/jobs/<id>``。同一时刻只允许一个任务（与跑批共用闸门）。
        """
        scope = self._check_platform(platform)
        assert scope is not None
        cls: Any = adapters.get(scope)
        caps = getattr(cls, "capabilities", None)
        if caps is not None and not getattr(caps, "submit", False):
            raise ConsoleError(
                f"{getattr(cls, 'display_name', scope)} 尚未实现提交"
                f"（capabilities.submit = False），不能批量提交。",
                kind="not_supported", status=501,
            )
        if not getattr(cls, "native_submit_flow", False):
            raise ConsoleError(
                f"{getattr(cls, 'display_name', scope)} 没有原生提交链路"
                f"（native_plan/native_submit），暂不支持一键批量提交。",
                kind="not_supported", status=501,
            )

        course = str(course or "").strip()
        limit = _clamp(int(limit or 0), 0, 10_000)
        try:
            interval = float(interval)
        except (TypeError, ValueError):
            interval = 8.0
        interval = max(0.5, min(interval, 120.0))

        # 先校验账号存在（批量任务里再登录），这样 HTTP 层能立刻回 400/404
        creds, cookie_file = self._resolve_login(scope, viewer)
        owner_id = viewer.user_id if (viewer and viewer.authenticated) else 0

        with self._lock:
            if self._active_job_id is not None:
                raise ConsoleError(
                    f"已有任务在跑（{self._active_job_id}），同一时刻只允许一个。",
                    kind="busy", status=409,
                )
            self._seq += 1
            job_id = f"b-{self._seq}"
            job: dict[str, Any] = {
                "job_id": job_id,
                "kind": "batch-submit",
                "platform": scope,
                "course": course,
                "confirm": bool(confirm),
                "limit": limit,
                "interval": interval,
                "skip_done": bool(skip_done),
                "status": "running",
                "started_at": _now(),
                "finished_at": None,
                "summary": "",
                "error": "",
                "logs": [],
                "items": [],
                "counts": {
                    "planned": 0, "submitted": 0, "skipped": 0,
                    "blocked": 0, "failed": 0,
                },
                "owner_id": owner_id,
            }
            self._jobs.append(job)
            self._jobs_by_id[job_id] = job
            del self._jobs[:-JOB_HISTORY]
            for stale in list(self._jobs_by_id):
                if stale not in {j["job_id"] for j in self._jobs}:
                    del self._jobs_by_id[stale]
            self._active_job_id = job_id

        self._plog(
            scope, "info",
            f"$ 批量提交 {job_id}（{'真交，不可逆' if confirm else '干跑，不提交'}；"
            f"course={course or '全部课程'}，limit={limit or '不限'}）",
        )
        threading.Thread(
            target=self._run_batch_submit,
            args=(job_id, scope, course, bool(confirm), limit, interval,
                  bool(skip_done), owner_id, cookie_file, creds),
            name=f"kedaibiao-batch-{job_id}",
            daemon=True,
        ).start()
        return _jsonable(job)

    def _run_batch_submit(
        self,
        job_id: str,
        platform: str,
        course: str,
        confirm: bool,
        limit: int,
        interval: float,
        skip_done: bool,
        owner_id: int,
        cookie_file: str | None,
        credentials: Any,
    ) -> None:
        """批量提交的后台线程：逐课程 → 逐任务，单组失败不拖垮整批。"""
        runtime = None
        adapter: Any = None
        counts: dict[str, int] = {}
        try:
            runtime = build_runtime(
                root=self.root, environ=self._llm_environ(),
                cookie_file=cookie_file, owner_id=owner_id,
            )
            adapter = runtime.make_adapter(platform)
            restore_adapter_session(adapter, cookie_file)
            self._job_log(job_id, "info", "$ 恢复登录会话…")
            if not asyncio.run(adapter.refresh_session(credentials)):
                raise RuntimeError(
                    "登录失败：会话无效且无法自动登录，请到「账号」页重新登录一次"
                )
            self._job_log(job_id, "ok", "✓ 会话就绪")

            courses = asyncio.run(adapter.list_courses())
            if course:
                needle = course.lower()
                courses = [
                    c for c in courses
                    if str(c.remote_id) == course
                    or needle in (c.name or "").lower()
                    or needle in str(c.remote_id).lower()
                ]
            if not courses:
                raise RuntimeError(f"没有匹配的课程（course={course!r}）")
            self._job_log(job_id, "info", f"· 课程 {len(courses)} 门")

            processed = 0
            for target in courses:
                if limit and processed >= limit:
                    break
                self._job_log(
                    job_id, "info", f"→ {target.name}（{target.remote_id}）"
                )
                assignments = asyncio.run(adapter.list_assignments(target))
                self._job_log(job_id, "info", f"  {len(assignments)} 个任务")
                for assignment in assignments:
                    if limit and processed >= limit:
                        break
                    processed += 1
                    self._batch_one(
                        job_id, adapter, assignment,
                        confirm=confirm, skip_done=skip_done, interval=interval,
                    )
                    if limit and processed >= limit:
                        break
            counts = self._batch_counts(job_id)
            summary = (
                f"完成：可提交 {counts['planned']}，真交 {counts['submitted']}，"
                f"跳过 {counts['skipped']}，被拦 {counts['blocked']}，"
                f"失败 {counts['failed']}"
            )
            self._finish_batch_job(job_id, "done", summary=summary)
            self._job_log(job_id, "ok", f"■ {summary}")
            self._plog(platform, "ok", f"■ 批量提交 {job_id} {summary}")
        except Exception as exc:  # noqa: BLE001 - 后台任务必须落成失败状态
            counts = self._batch_counts(job_id)
            message = f"{type(exc).__name__}: {exc}"
            self._finish_batch_job(job_id, "failed", error=message)
            self._job_log(job_id, "error", f"✗ {message}")
            self._plog(platform, "error", f"✗ 批量提交 {job_id}：{message}")
            log.exception("批量提交失败: %s", job_id)
        finally:
            if adapter is not None:
                with contextlib.suppress(Exception):
                    asyncio.run(adapter.close())
            if runtime is not None:
                with contextlib.suppress(Exception):
                    runtime.close()
            with self._lock:
                if self._active_job_id == job_id:
                    self._active_job_id = None

    def _batch_one(
        self,
        job_id: str,
        adapter: Any,
        assignment: Any,
        *,
        confirm: bool,
        skip_done: bool,
        interval: float,
    ) -> None:
        """处理一个任务组：读状态 → 干跑计划 →（确认后）真交。"""
        assignment_id = str(getattr(assignment, "remote_id", "") or "")
        title = str(getattr(assignment, "title", "") or assignment_id)
        if assignment_id:
            # 同名的任务很多（每单元都有 Exercise 1），日志里带上分组号便于对照
            title = f"{title}·{assignment_id.rsplit('/', 1)[-1][:8]}"
        if not assignment_id:
            self._bump(job_id, "failed")
            self._job_log(job_id, "warn", "· 跳过：任务号为空")
            return

        state: dict[str, Any] = {}
        if hasattr(adapter, "native_assignment_state"):
            try:
                state = asyncio.run(adapter.native_assignment_state(assignment_id)) or {}
            except Exception as exc:  # noqa: BLE001 - 状态读不到就当作未知，不拦
                self._job_log(job_id, "warn", f"· {title}：状态读取失败（{type(exc).__name__}），按未完成处理")

        # 「要不要跳过」只看**逐项得分**：平台的 score_pct 不是正确率
        # （实测：score=[1,1,1,1,1,0,0,0,0,0] 也回 score_pct=1）。
        if skip_done and state.get("finished"):
            total_items = int(state.get("total_items") or 0)
            correct_items = int(state.get("correct_items") or 0)
            if not total_items:
                reason = "已完成（无计分点：纯学习/内容组）"
            elif correct_items == total_items:
                reason = f"已满分（{correct_items}/{total_items} 项全对）"
            else:
                # 没全对也跳过：平台留存首条记录，重交不改分（实测）
                reason = (f"已完成但未全对（{correct_items}/{total_items} 项）；"
                          f"平台留存首条记录，重交不改分")
            self._bump(job_id, "skipped")
            self._job_log(job_id, "info", f"· 跳过 {title}（{reason}）")
            return

        try:
            plan = asyncio.run(adapter.native_plan(assignment_id))
        except Exception as exc:  # noqa: BLE001
            self._bump(job_id, "failed")
            self._job_log(job_id, "error", f"✗ {title}：取计划失败 {type(exc).__name__}: {exc}")
            return

        blockers = [str(item) for item in (plan.get("blockers") or [])]
        if not plan.get("can_submit", True):
            self._bump(job_id, "blocked")
            self._job_log(
                job_id, "warn",
                f"· 被拦 {title}：" + ("；".join(blockers) or "本地校验未通过"),
            )
            return

        self._bump(job_id, "planned")
        detail = (
            f"题目 {plan.get('question_count')} · 计分点 {plan.get('item_count')}"
            f" · submitType {plan.get('submit_type')}"
        )
        if not confirm:
            self._job_log(job_id, "ok", f"✓ 可提交 {title}（{detail}）")
            return

        try:
            result = asyncio.run(adapter.native_submit(assignment_id))
        except Exception as exc:  # noqa: BLE001
            retry_after = getattr(exc, "retry_after", None)
            self._bump(job_id, "failed")
            self._job_log(job_id, "error", f"✗ {title}：提交失败 {type(exc).__name__}: {exc}")
            if isinstance(retry_after, (int, float)) and retry_after > 0:
                wait = min(float(retry_after), 120.0)
                self._job_log(job_id, "warn", f"   撞上限流，等待 {wait:.0f}s 再继续")
                time.sleep(wait)
            return

        self._bump(job_id, "submitted")
        self._job_log(
            job_id, "ok",
            f"✓ 已提交 {title}：code={result.get('code')!r} "
            f"得分率={result.get('score_pct')} "
            f"finish={result.get('server_finish')}",
        )
        if interval:
            time.sleep(interval)

    def _batch_counts(self, job_id: str) -> dict[str, int]:
        with self._lock:
            job = self._jobs_by_id.get(job_id)
            return dict(job.get("counts") or {}) if job else {}

    def _bump(self, job_id: str, key: str) -> None:
        with self._lock:
            job = self._jobs_by_id.get(job_id)
            if job is None:
                return
            counts = job.setdefault("counts", {})
            counts[key] = int(counts.get(key) or 0) + 1

    def _finish_batch_job(
        self,
        job_id: str,
        status: str,
        *,
        summary: str = "",
        error: str = "",
    ) -> None:
        with self._lock:
            job = self._jobs_by_id.get(job_id)
            if job is None:
                return
            job["status"] = status
            job["finished_at"] = _now()
            if summary:
                job["summary"] = summary
            if error:
                job["error"] = error

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        """请求停止一个后台任务（刷课 / 批量提交）。

        只置标记，真正的停在后台线程的下一个检查点生效 —— 正在飞的那一个
        请求会照常完成，不会留下半提交状态。
        """
        job_id = str(job_id or "").strip()
        with self._lock:
            job = self._jobs_by_id.get(job_id)
            if job is None:
                raise ConsoleError(
                    f"任务 {job_id!r} 不存在或已被清理", kind="not_found", status=404
                )
            if job.get("status") != "running":
                return _jsonable(job)
            self._cancel_requested.add(job_id)
            job["cancel_requested"] = True
        self._job_log(job_id, "warn", "· 收到停止请求：处理完当前这一组就停")
        return _jsonable(self._jobs_by_id.get(job_id) or job)

    def _cancelled(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._cancel_requested

    def _sleep_interruptible(self, job_id: str, seconds: float) -> bool:
        """分片睡；返回 True 表示期间收到了停止请求。"""
        remaining = float(seconds)
        while remaining > 0:
            if self._cancelled(job_id):
                return True
            step = min(1.0, remaining)
            time.sleep(step)
            remaining -= step
        return self._cancelled(job_id)

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
        owner_id: int = 0,
        cookie_file: str | None = None,
        credentials: Any = None,
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
                owner_id=owner_id,
                cookie_file=cookie_file,
            )
            runtime.bus.on("*", self._record_event)
            if cookie_file:
                # 跑批同样使用「恢复会话」这条路，避免明明登录过却报缺凭据
                pass

            report = asyncio.run(runtime.run(platform, credentials=credentials))

            self._plog(platform, "ok" if report.failed == 0 else "warn",
                       f"→ 跑批 {job_id} 结束：{report.summary()}")
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
            self._plog(platform, "error", f"✗ 跑批 {job_id}：{type(exc).__name__}: {exc}")
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
