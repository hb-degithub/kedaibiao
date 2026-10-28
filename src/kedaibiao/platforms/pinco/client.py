"""品课 RPC 客户端：所有请求的唯一出口。

协议事实（Lead 实测 + 静态提取，详见 :mod:`endpoints`）：

- 学生端业务 action 都 ``POST /student/api.json?actionName=<Op>``；
- 同一 action 在 App 侧还有一条 REST 落地（``/server-main/api/v1/...``），
  两者的逐条互转表还没交付，所以 :meth:`PincoClient.call` 只认已核验的映射，
  没有映射就报错；
- 请求头 ``x-csrf-token`` / ``authorization`` / ``x-req-traceid`` / ``x-language``；
- 响应信封 ``{code, msg, data, statusCode, message}``，HTTP 状态与 statusCode 一致。

四条纪律：

1. **所有请求都走这里** —— 包括危险动作的第二道闸门；
2. **不 sleep、不重试**：网络类异常（``OSError`` / 超时）原样上抛，退避交给
   ``core.scheduler``；业务错误与协议错误直接抛，重试没有意义；
3. **双层判错 + 按家族判成功**：HTTP 200 里可以套业务 ``code: 403``（实测形态），
   所以只看 ``Response.ok`` 不够。而且 RPC 家族用 ``statusCode == 0`` 判成功、
   REST 家族用 ``code == 0`` 判（REST 的 ``statusCode`` 是 HTTP 镜像 200）——
   混用会把每次 REST 成功判成失败。家族由调用点显式传入（见
   :data:`endpoints.Protocol`）；
4. **危险动作必须显式放行**：:data:`endpoints.DANGEROUS_ACTIONS` 里的 action
   只有传 ``dangerous=True`` 才发得出去，抓取路径不可能误触提交接口。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from kedaibiao.net.http import ResponseNotJson
from kedaibiao.platforms.pinco import endpoints as ep
from kedaibiao.platforms.pinco import parsing
from kedaibiao.platforms.pinco.errors import PincoError, PincoProtocolError

log = logging.getLogger(__name__)


class _SubmitGate:
    """提交前置条件：答案已保存并回读校验。

    单独一个对象而不是 client 属性，是为了在进程内能明确 `reset()`：
    一次提交成功后必须清掉凭据，避免下一个作业「借」到上一次的通行证。
    """

    answer_saved = False

    @classmethod
    def mark_answers_saved(cls) -> None:
        cls.answer_saved = True

    @classmethod
    def reset(cls) -> None:
        cls.answer_saved = False


#: 提交守卫的进程级凭据（见 :class:`_SubmitGate`）。
_SUBMIT_GATE = _SubmitGate


@contextmanager
def client_submit_gate() -> Iterator[None]:
    """在 ``with`` 块内允许提交类 action 发出（用完立刻失效）。

    用法严格限定在「答案已保存并回读校验通过」之后的一行调用 ——
    不给整段流程开闸，避免顺手把第二个作业也交出去。
    """
    _SUBMIT_GATE.mark_answers_saved()
    try:
        yield
    finally:
        _SUBMIT_GATE.reset()


class PincoClient:
    """RPC 调用 + 信封校验 + 请求头组装。"""

    def __init__(
        self,
        http: Any,
        *,
        csrf_token: str = "",
        authorization: str = "",
        language: str = ep.DEFAULT_LANGUAGE,
    ) -> None:
        #: 用 ``Any`` 声明：真实实现是 ``net.http.HttpClient``，测试里是同一个类
        #: 注入假 Transport。本层只用到 ``get`` / ``post_json`` / ``post_form``。
        self._http = http
        self.csrf_token = csrf_token
        self.authorization = authorization
        self.language = language

    # -- cookie / 请求头 ----------------------------------------------------

    def cookie_value(self, name: str) -> str:
        """从注入客户端的 cookie 罐里取一个 cookie。

        为什么不调 ``transport.cookie_header()``：这里只要一个值，直接遍历罐子
        更稳（不同 Transport 的 cookie_header 格式不保证一致）。取不到就返回
        空串 —— 后果由平台明确回一句 ``invalid csrf token``，比在这里猜一个
        默认值安全。
        """
        transport = getattr(self._http, "transport", None)
        jar = getattr(transport, "cookies", None)
        for cookie in jar or ():
            if getattr(cookie, "name", "") == name:
                return str(getattr(cookie, "value", ""))
        return ""

    def headers(self) -> dict[str, str]:
        """组装学生端请求头。空值不发，避免用空串顶掉服务端默认行为。

        ``x-server`` 是**必需**的：缺了它网关回 ``500 ERR_ASSERTION``，
        形状与「action 名写错」相同。证据见 ``endpoints.HEADER_SERVER`` 的注释
        （真实会话 A/B 对照：不带 500，带 ``main`` 200）。
        """
        csrf = self.csrf_token or self.cookie_value(ep.CSRF_COOKIE)
        headers = {
            ep.HEADER_TRACE_ID: uuid.uuid4().hex,
            ep.HEADER_LANGUAGE: self.language,
            ep.HEADER_SERVER: ep.SERVER_NAME,
        }
        if csrf:
            headers[ep.HEADER_CSRF] = csrf
        if self.authorization:
            headers[ep.HEADER_AUTHORIZATION] = self.authorization
        return headers

    # -- 业务调用 -----------------------------------------------------------

    async def call(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        protocol: ep.Protocol = ep.Protocol.WEB,
        dangerous: bool = False,
    ) -> Any:
        """按指定协议调用一个业务 action，返回信封里的 ``data``。

        ``protocol=REST`` 时必须有**核对过的**路径映射，否则报错：Web/REST
        互转规则还没交付，猜一条路径会静默打到别的接口上。
        """
        self._check_dangerous(action, dangerous)
        self._check_submit_shape(action, payload)
        if protocol is ep.Protocol.REST:
            url = ep.rest_url(ep.rest_path_for_action(action))
        else:
            url = ep.action_url(action)
        resp = await self._http.post_json(url, payload or {}, headers=self.headers())
        return self._unwrap(resp, action=action, family=protocol)

    @staticmethod
    def _check_submit_shape(action: str, payload: dict[str, Any] | None) -> None:
        """提交类 action 的**结构护栏**：没有答案体的请求一律不发。

        事故背景（2026-10-28）：为了探请求体结构，直接拿 ``{"studentHomeworkId": …}``
        打 ``PostV1StudenthomeworkByStudentHomeworkIdSubmit`` —— 平台**没有报错**，
        而是当成「空答案提交」收下了：score=0、submitted=true、canRedo=false，
        作业就此交掉且不能重做。

        所以宁可在这里拦住：提交类 action 必须带**非空的列表/对象字段**
        （答案体），否则直接拒绝发送。真正的提交走 ``adapter.submit``，
        它还有复核计划、置信度、平台状态三道守卫。
        """
        submit_actions = {
            ep.ACTION_SUBMIT_HOMEWORK,
            ep.ACTION_SUBMIT_CHAPTER_TASK,
            ep.ACTION_SUBMIT_COURSE_GROUP_TASK,
        }
        if action not in submit_actions:
            return
        # 提交体**只有 id**（官方学生端就是这么发的），所以不能靠请求体形状判断
        # 「有没有答案」—— 改用显式凭据：调用方逐题保存答案并回读校验通过后，
        # 才允许置位 `answers_ready`。
        if not getattr(_SUBMIT_GATE, "answer_saved", False):
            raise PincoError(
                f"拒绝发送：{action} 之前没有「答案已保存并回读校验」的凭据。"
                "提交接口只带 id，平台会把没有答案的提交当成空卷收下（0 分且不能重做）。"
                "正确顺序：逐题保存答案 → 回读校验 → 再提交。"
            )

    async def post_action(
        self, action: str, payload: dict[str, Any] | None = None, *, dangerous: bool = False
    ) -> Any:
        """按 Web RPC 调用一个 action（最常用的一条）。"""
        return await self.call(action, payload, protocol=ep.Protocol.WEB, dangerous=dangerous)

    async def get_rest(self, path: str) -> Any:
        """GET 一个 REST 路径（``/server-main/api/v1/...``）—— 按 **REST 家族**判成功。"""
        resp = await self._http.get(ep.rest_url(path), headers=self.headers())
        return self._unwrap(resp, action=path, family=ep.Protocol.REST)

    async def post_form_raw(self, url: str, data: dict[str, Any]) -> Any:
        """POST 一个表单到完整 URL，返回**原始** JSON（不做应用层信封校验）。

        为什么单独一条路：SSO 域（``id.seewo.com``）的响应形状与应用层不同 ——
        ``getPublicKeyV3`` 成功时只有 ``{"data": "<base64>"}``、``setToken``
        只有 ``{"message": "设置token成功"}``，都没有应用层的 ``code``。
        拿应用层信封去套，会把成功判成「鉴权失败」。
        """
        resp = await self._http.post_form(url, data)
        return self._json_body(resp, action=url)

    async def health_check(self) -> Any:
        """匿名健康检查 —— 走 REST ``/system/currentTime``（实测 200 + ``{"code":0}``）。

        存在的意义：把「网络/协议通不通」与「登录态有没有」分开。排查问题时，
        这一步能立刻指出方向 —— 所以它**必须真的可用**。

        为什么不再用 ``GetV1Pincofaq``：那个 action 实测恒回 ``500 ERR_ASSERTION``，
        于是 health_check 永远返回 False，把「网络没问题」误报成「网络有问题」。
        死因与 ``GetV1StudentChapterTaskList`` 同级，已登记进
        ``endpoints.DEAD_ACTIONS``。
        """
        return await self.current_time()

    async def current_time(self) -> Any:
        """REST 匿名探测：返回服务端当前时间（实测可用）。"""
        return await self.get_rest(ep.REST_PATH_CURRENT_TIME)

    # -- 内部 ---------------------------------------------------------------

    def _check_dangerous(self, action: str, dangerous: bool) -> None:
        if action in ep.DANGEROUS_ACTIONS and not dangerous:
            raise PincoProtocolError(
                f"{action} 是写操作（提交 / 学习进度上报），必须显式传 dangerous=True "
                "才允许发送。抓取路径不该走到这里 —— 这是防止误触写操作的第二道闸门。"
            )

    def _json_body(self, resp: Any, *, action: str) -> Any:
        try:
            return resp.json()
        except ResponseNotJson as exc:
            status = int(getattr(resp, "status", 0) or 0)
            raise PincoProtocolError(
                f"{action} 返回的不是 JSON（HTTP {status}，可能拿到了登录页/网关页）：{exc}"
            ) from exc

    def _unwrap(self, resp: Any, *, action: str, family: ep.Protocol) -> Any:
        """按**接口家族**判成功：RPC 看 ``statusCode``，REST 看 ``code``。

        两个家族的信封长得像、语义不同（REST 的 ``statusCode`` 是 HTTP 镜像 200），
        混用会把每次 REST 成功都判成失败 —— ``health_check`` 曾经因此恒假。
        """
        return parsing.unwrap_envelope(
            self._json_body(resp, action=action),
            action=action,
            http_status=int(getattr(resp, "status", 0) or 0),
            family=family,
        )
