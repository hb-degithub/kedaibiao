"""网络传输层：协议型平台（非浏览器）共用的 HTTP 客户端。

框架里有两种适配器：

- **浏览器型**：走 `browser/` 的 Playwright 会话，页面即接口；
- **协议型**：直接打 HTTP 接口，需要自己管 cookie、请求头、编码。

后者重复度极高，所以抽到这里。关键设计是 `Transport` 可替换 —— 适配器
的单测必须能在**不联网**的前提下跑完，靠的就是注入假传输层。
"""

from __future__ import annotations

import asyncio
import http.cookiejar
import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------


class HttpError(Exception):
    pass


class TransportError(OSError, HttpError):
    """网络层失败（连不上、超时、连接被重置）。

    刻意继承 ``OSError``：``core.scheduler.RETRYABLE`` 里就有它，
    于是这类失败自动获得重试与退避，不需要适配器自己写重试。
    """


class HttpStatusError(HttpError):
    """服务端返回了非预期状态码。"""

    def __init__(self, response: Response) -> None:
        super().__init__(f"HTTP {response.status} {response.url}")
        self.response = response
        self.status = response.status


class ResponseNotJson(HttpError):
    pass


# --------------------------------------------------------------------------
# 响应
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Response:
    status: int
    url: str
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    encoding: str = "utf-8"

    def text(self) -> str:
        return self.body.decode(self.encoding, errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode(self.encoding))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            preview = self.body[:200].decode("utf-8", errors="replace")
            raise ResponseNotJson(f"{self.url} 返回的不是 JSON: {preview!r}") from exc

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


# --------------------------------------------------------------------------
# 传输层
# --------------------------------------------------------------------------


class Transport(Protocol):
    """传输契约。换成假实现即可让适配器单测完全离线。"""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 15.0,
    ) -> Response: ...


class UrllibTransport:
    """基于标准库 urllib 的实现。

    为什么不用 httpx/aiohttp：核心策略是「能零依赖就零依赖」。urllib 是阻塞的，
    所以丢到线程里跑；跑批本身是 IO 密集且并发只有个位数，线程池足够。

    局限（诚实说明）：urllib 的 TLS 指纹是 Python 的默认指纹，**不能伪装成
    浏览器**。若某个平台的网关按 TLS 指纹做风控，需要替换成别的 Transport
    （例如 curl_cffi 或直接走浏览器型适配器）。这属于适配器的选择，不是本层的锅。
    """

    def __init__(self, *, cookie_jar: http.cookiejar.CookieJar | None = None) -> None:
        self.cookies = cookie_jar if cookie_jar is not None else http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies)
        )
        self._lock = threading.Lock()

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 15.0,
    ) -> Response:
        return await asyncio.to_thread(self._request_sync, method, url, headers, body, timeout)

    def _request_sync(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None,
        body: bytes | None,
        timeout: float,
    ) -> Response:
        req = urllib.request.Request(url, data=body, method=method.upper())  # noqa: S310
        for key, value in (headers or {}).items():
            req.add_header(key, value)

        try:
            # opener 内含 CookieJar，非线程安全，加锁串行化
            with self._lock:
                with self._opener.open(req, timeout=timeout) as resp:  # noqa: S310
                    return Response(
                        status=resp.status,
                        url=resp.geturl(),
                        body=resp.read(),
                        headers={k: v for k, v in resp.headers.items()},
                        encoding=resp.headers.get_content_charset() or "utf-8",
                    )
        except urllib.error.HTTPError as exc:
            # 4xx/5xx 也当正常响应返回，交给调用方按状态码判断。
            # HTTPError 持有连接，必须显式关闭，否则会漏 ResourceWarning。
            try:
                body = exc.read() or b""
                raw_headers = {k: v for k, v in (exc.headers or {}).items()}
                charset = (exc.headers.get_content_charset() if exc.headers else None) or "utf-8"
                status = exc.code
            finally:
                exc.close()
            return Response(
                status=status, url=url, body=body, headers=raw_headers, encoding=charset
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(f"{method} {url} 失败: {exc}") from exc

    # -- cookie 持久化 ----------------------------------------------------

    def save_cookies(self, path: str | Path) -> int:
        """把 cookie 落盘，下次跑批免登录。注意：这是凭据，已被 .gitignore 排除。"""
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        data = [
            {
                "domain": c.domain,
                "path": c.path,
                "name": c.name,
                "value": c.value,
                "secure": c.secure,
                "expires": c.expires,
            }
            for c in self.cookies
        ]
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return len(data)

    def load_cookies(self, path: str | Path) -> int:
        src = Path(path)
        if not src.is_file():
            return 0
        data = json.loads(src.read_text(encoding="utf-8"))
        for item in data:
            self.cookies.set_cookie(
                http.cookiejar.Cookie(
                    version=0,
                    name=item["name"],
                    value=item["value"],
                    port=None,
                    port_specified=False,
                    domain=item["domain"],
                    domain_specified=bool(item["domain"]),
                    domain_initial_dot=item["domain"].startswith("."),
                    path=item.get("path", "/"),
                    path_specified=True,
                    secure=bool(item.get("secure")),
                    expires=item.get("expires"),
                    discard=False,
                    comment=None,
                    comment_url=None,
                    rest={},
                )
            )
        return len(data)

    def cookie_header(self) -> str:
        """手工拼 Cookie 头（某些网关不吃 urllib 自动带的）。"""
        return "; ".join(f"{c.name}={c.value}" for c in self.cookies)


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------


class HttpClient:
    """带默认头、超时与 JSON 便利方法的薄封装。"""

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        base_headers: dict[str, str] | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.transport: Transport = transport or UrllibTransport()
        self.base_headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            **(base_headers or {}),
        }
        self.timeout = timeout

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float | None = None,
    ) -> Response:
        merged = {**self.base_headers, **(headers or {})}
        return await self.transport.request(
            method,
            url,
            headers=merged,
            body=body,
            timeout=self.timeout if timeout is None else timeout,
        )

    async def get(self, url: str, **kw: Any) -> Response:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw: Any) -> Response:
        return await self.request("POST", url, **kw)

    async def post_form(self, url: str, data: dict[str, Any], **kw: Any) -> Response:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded", **kw.pop("headers", {})}
        return await self.request("POST", url, body=body, headers=headers, **kw)

    async def post_json(self, url: str, payload: Any, **kw: Any) -> Response:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", **kw.pop("headers", {})}
        return await self.request("POST", url, body=body, headers=headers, **kw)

    async def get_json(self, url: str, *, expect_error: bool = False, **kw: Any) -> Any:
        resp = await self.get(url, **kw)
        if not resp.ok and not expect_error:
            raise HttpStatusError(resp)
        return resp.json()

    async def post_json_data(self, url: str, payload: Any, *, expect_error: bool = False, **kw: Any) -> Any:
        resp = await self.post_json(url, payload, **kw)
        if not resp.ok and not expect_error:
            raise HttpStatusError(resp)
        return resp.json()

    def join(self, base: str, path: str) -> str:
        return urllib.parse.urljoin(base, path)
