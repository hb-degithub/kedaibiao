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
        self._seq = 0
        self._active_job_id: str | None = None
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
            "platforms": platforms,
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
        return asyncio.run(self._execute_action(scope, action, cleaned, viewer))

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
        """跑一个只读动作。每次都新建 Runtime —— 它们各自持有 sqlite 连接。

        登录用「当前账号」的 cookie 文件；没有绑定账号时回退到 ``.env``。
        """
        creds, cookie_file = self._resolve_login(platform, viewer)

        runtime = build_runtime(
            root=self.root, environ=self.environ, cookie_file=cookie_file,
            owner_id=viewer.user_id if (viewer and viewer.authenticated) else 0,
        )
        adapter: Any = None
        try:
            adapter = runtime.make_adapter(platform)

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
            if adapter is not None:
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
            account.cookie_path(settings.session_dir).unlink(missing_ok=True)
        except OSError:
            log.debug("删除 cookie 文件失败", exc_info=True)

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
            return self._with_accounts(
                lambda st: st.resolve_credentials(
                    platform, owner_id=owner, session_dir=settings.session_dir
                )
            )
        except AccountError as exc:
            raise ConsoleError(str(exc), kind=exc.kind, status=exc.status) from exc

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

        thread = threading.Thread(
            target=self._execute,
            args=(job_id, platform, bool(live), limit, parsed_kinds, owner_id,
                  cookie_file, creds),
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
