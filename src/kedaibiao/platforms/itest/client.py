"""iTEST 智能测评云平台（外研社 Unipus）客户端 —— 只读链路。

## 平台定位

iTEST 是**考试系统**，不是作业平台：教师围绕**班级**发布「班级测试」，学生凭
班级授权码加入后，考试出现在考试列表里。所以它的「作业」实际是**考试**。

## 本模块只做只读的三步

1. **SSO 登录**：复用 Unipus SSO 的 AES 表单加密（与 U校园 同一套 SSO）；
2. **考试列表**：`/utest/itest-mobile-api/student/exam/list`；
3. **换答题窗口令牌**：`/utest/itest/s/exam/judgeEntry` —— **不会开始考试**
   （调研已核验：调用前后 `ansBeginTime` 仍为空、`finish` 仍为 0）。

## 刻意不做的事

- **不进答题窗口、不取卷、不交卷**。取卷（`answer/load`）需要浏览器里的设备检测
  产生的两个不透明 cookie（`envTest*` / `iTMExam`），而且一旦进入答题页就开始
  计时；交卷是不可逆的写操作。这两件都不在本适配器范围内。
- 所以 ``capabilities.fetch_questions = False``、``submit = False``。

## 依据

`docs/research/itest.md`（已核验：登录链路、考试列表、judgeEntry 不发卷）。
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from dataclasses import dataclass
from typing import Any

from kedaibiao.net.http import HttpClient
from kedaibiao.platforms.unipus.crypto import encrypt_login_field

log = logging.getLogger(__name__)

#: iTEST 云平台基址（校内端是另一套，未接入）
BASE = "https://itestcloud.unipus.cn"
#: Unipus 统一登录
SSO = "https://sso.unipus.cn"

LOGIN_PATH = "/utest/itest/login"
EXAM_PAGE_PATH = "/utest/itest/s/exam"
EXAM_LIST_PATH = "/utest/itest-mobile-api/student/exam/list"
JUDGE_ENTRY_PATH = "/utest/itest/s/exam/judgeEntry"
ANSWER_WINDOW_PATH = "/itest-api/itest/s/answer/index"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

#: 登录后会出现的会话 cookie（用于判断「是否需要重新登录」）
SESSION_COOKIES = ("jwt", "iTSsiD")


class ItestError(RuntimeError):
    pass


class ItestAuthError(ItestError):
    pass


class CaptchaRequired(ItestAuthError):
    """SSO 要求验证码（code 1506）。

    这不是「密码错」，而是风控要求人机验证 —— 调用方可以退回浏览器求解，
    不要把这种情况报成凭据问题。
    """

    def __init__(self, login_url: str = "") -> None:
        super().__init__(
            "SSO 要求图片/滑块验证码（code 1506）。需要走浏览器求解后重试。"
        )
        self.login_url = login_url


@dataclass(slots=True)
class ExamEntry:
    """考试列表里的一条。"""

    exam_id: str
    title: str = ""
    code: str = ""
    start_at: str = ""
    end_at: str = ""
    duration: str = ""
    raw: dict[str, Any] | None = None

    def to_assignment_fields(self) -> dict[str, str]:
        return {
            "exam_id": self.exam_id,
            "title": self.title,
            "code": self.code,
            "start_at": self.start_at,
            "end_at": self.end_at,
            "duration": self.duration,
        }


def _pick(raw: dict[str, Any], *names: str) -> str:
    """从平台字段里挑第一个非空值（字段名在不同版本之间有差异）。"""
    for name in names:
        value = raw.get(name)
        if value not in (None, ""):
            return str(value)
    return ""


def parse_exam_list(payload: Any) -> list[ExamEntry]:
    """解析考试列表响应。

    平台结构是 ``{"code":1,"rs":{"data":[...]}}``；字段名按调研记录取，
    取不到就留空 —— **不猜同义字段之外的语义**。
    """
    if not isinstance(payload, dict):
        raise ItestError("考试列表响应不是对象")
    rs = payload.get("rs")
    rows = None
    if isinstance(rs, dict):
        rows = rs.get("data")
    elif isinstance(rs, list):
        rows = rs
    if rows is None:
        rows = payload.get("data")
    if not isinstance(rows, list):
        raise ItestError("考试列表响应里没有 data 数组")

    out: list[ExamEntry] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        exam_id = _pick(item, "ksdId", "examId", "id")
        if not exam_id:
            continue
        out.append(
            ExamEntry(
                exam_id=exam_id,
                title=_pick(item, "examName", "name", "title", "ksName"),
                code=_pick(item, "examCode", "code"),
                start_at=_pick(item, "beginTime", "startTime", "beginDate"),
                end_at=_pick(item, "endTime", "stopTime", "endDate"),
                duration=_pick(item, "limitTime", "duration", "examTime"),
                raw=item,
            )
        )
    return out


class ItestClient:
    """只读客户端。会话靠 cookie（``jwt`` / ``iTSsiD``），可跨进程复用。"""

    def __init__(self, http: HttpClient, *, logger: logging.Logger | None = None) -> None:
        self.http = http
        self.log = logger or log
        self._user_id = ""

    # -- 会话 -------------------------------------------------------------

    def _headers(self, *, json_body: bool = False, referer: str = "") -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept": "application/json, text/plain, */*",
        }
        if json_body:
            headers["Content-Type"] = "application/json; charset=UTF-8"
        if referer:
            headers["Referer"] = referer
        return headers

    def session_cookie_names(self) -> list[str]:
        jar = getattr(getattr(self.http, "transport", None), "cookies", None)
        if jar is None:
            return []
        return [c.name for c in jar]

    async def is_logged_in(self) -> bool:
        """有会话 cookie 就认为登录态还在（真正的失效会由具体接口报错暴露）。"""
        names = set(self.session_cookie_names())
        return any(name in names for name in SESSION_COOKIES)

    async def login(self, username: str, password: str) -> bool:
        """Unipus SSO 登录 → 拿 serviceTicket → 换 iTEST 会话 cookie。

        两步都必须是**同一个 cookie 会话**（登录页会先种下跳转所需的 cookie）。
        """
        if not username or not password:
            raise ItestAuthError(
                "iTEST 登录需要账号密码（该平台没有别的登录方式）。"
                "请在控制台「账号」页点「重新登录」并输入密码。"
            )

        # 1) 打开登录入口，从跳转里取 service
        resp = await self.http.get(
            f"{BASE}{LOGIN_PATH}", headers=self._headers(referer=f"{BASE}/")
        )
        service = _extract_service(resp.url or "")
        if not service:
            # 没有跳 SSO，说明**已经是登录态**（旧 cookie 还有效）——
            # 这不是错误，直接当成登录成功返回。
            if await self.is_logged_in():
                self.log.info("登录入口直接进了业务页 —— 会话仍然有效，跳过登录")
                return True
            raise ItestAuthError(
                f"登录页没有跳转到 SSO（拿到的是 {resp.url!r}）—— "
                "平台改版或网络异常，无法继续。"
            )

        # 2) SSO 表单登录（用户名/密码各自 AES 加密）
        payload = {
            "service": service,
            "username": encrypt_login_field(username),
            "password": encrypt_login_field(password),
        }
        body = json.dumps(payload).encode("utf-8")
        sso_resp = await self.http.request(
            "POST",
            f"{SSO}/sso/0.1/sso/cip/login",
            headers=self._headers(json_body=True, referer=resp.url or ""),
            body=body,
        )
        data = sso_resp.json() if sso_resp.body else {}
        rs = data.get("rs") or {}
        ticket = rs.get("serviceTicket") if isinstance(rs, dict) else None
        if str(data.get("code")) == "1506":
            # 风控要验证码：交回上层用浏览器求解，不当作凭据错误
            raise CaptchaRequired(resp.url or "")
        if str(data.get("code")) != "0" or not ticket:
            raise ItestAuthError(
                f"SSO 登录失败：code={data.get('code')} msg={data.get('msg')!r}"
            )

        # 3) 带 ticket 回登录页 → 种下 iTEST 会话
        query = urllib.parse.urlencode({"ticket": ticket})
        await self.http.get(
            f"{BASE}{LOGIN_PATH}?{query}", headers=self._headers(referer=resp.url or "")
        )
        await self.http.get(
            f"{BASE}{EXAM_PAGE_PATH}", headers=self._headers(referer=f"{BASE}/")
        )
        return await self.is_logged_in()

    # -- 只读接口 ---------------------------------------------------------

    async def list_exams(self) -> list[ExamEntry]:
        resp = await self.http.post_json(
            f"{BASE}{EXAM_LIST_PATH}",
            {},
            headers=self._headers(json_body=True, referer=f"{BASE}{EXAM_PAGE_PATH}"),
        )
        if not resp.ok:
            raise ItestError(f"考试列表 HTTP {resp.status}")
        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise ItestError(f"考试列表返回的不是 JSON（可能登录态失效）：{exc}") from exc
        if str(payload.get("code")) not in ("1", "0") and payload.get("rs") is None:
            raise ItestAuthError(
                f"考试列表被拒：code={payload.get('code')} msg={payload.get('msg')!r}"
            )
        return parse_exam_list(payload)

    async def judge_entry(self, exam_id: str, exam_code: str = "") -> str:
        """换答题窗口 URL。**只签发令牌，不会开始考试**（调研已核验）。"""
        body = urllib.parse.urlencode(
            {"examId": exam_id, "examCode": exam_code}
        ).encode("utf-8")
        resp = await self.http.request(
            "POST",
            f"{BASE}{JUDGE_ENTRY_PATH}",
            headers={
                **self._headers(referer=f"{BASE}{EXAM_PAGE_PATH}"),
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
            body=body,
        )
        if not resp.ok:
            raise ItestError(f"judgeEntry HTTP {resp.status}")
        data = resp.json()
        if data.get("code") != 1:
            raise ItestError(
                f"judgeEntry 失败：code={data.get('code')} msg={data.get('msg')!r}"
            )
        answer_url = (data.get("data") or {}).get("url")
        if not answer_url:
            raise ItestError("judgeEntry 没返回答题窗口 URL")
        return answer_url

    @staticmethod
    def window_url(answer_url: str) -> str:
        """弹窗实际 URL：必须补 ``returnUrl``，否则平台报「没有权限」。"""
        return answer_url + "&returnUrl=" + urllib.parse.quote(
            f"{BASE}{EXAM_PAGE_PATH}", safe=""
        )


def _extract_service(url: str) -> str:
    from urllib.parse import parse_qs

    query = urllib.parse.urlparse(url).query
    values = parse_qs(query).get("service")
    return values[0] if values else ""
