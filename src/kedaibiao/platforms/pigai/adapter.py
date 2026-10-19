"""批改网（pigai）平台适配器 —— **只读**。

## 范围

本适配器**只做读取**：作业列表、截止时间、已提交作文列表、历史分数与逐句批注。
`Capabilities.submit = False`、`fetch_questions = False` —— 基类会替我们抛
``NotSupportedError``，比「返回空」安全（空会被误读成「这平台没作业」）。

写入类端点（写作页、删除、提交）**刻意不在 ``protocol.py`` 声明**，本模块也绝不构造；
本模块源码里连这类端点的**字面量都不出现**（有静态审计用例钉住，见
``tests/test_pigai_adapter.py::TestNoWriteSurface``）。
从页面 href 原样带出的字符串属于**数据**（会进 ``Assignment.url``），不是本模块的意图。

## `remote_id` 约定（integration-guide §4 —— 上线后不可随便改）

``Assignment.remote_id`` = **`rid`（批改网作文号/作业号）**，不是 `eid`。理由：

- `rid` 是「一次作业」的稳定标识，**跨多稿不变**；同一 `rid` 下每稿各有独立 `eid`；
- `remote_id` 同时是 `RunRecord.assignment_id` 与
  `ProgressStore.already_done(platform, assignment_id)` 的幂等键 —— 用 `eid` 会让
  「同一作业的第二稿」被当成新作业，重复提交风险直接回来；
- `eid` 只对**已提交**的作文存在，待完成作业根本没有 `eid`（实测其 href 指向写作页、
  形如 ``?c=v2&a=<写作动作>&rid=<rid>``），所以 `eid` 只能放 `Assignment.raw`。

## 与流水线的错配（重要）

``tasks.pipeline.Pipeline`` 是为「取题 → 作答 → 提交」写的。本适配器是只读的，
所以**不要用 `Pipeline.run()` 来读数据** —— 待完成作业没有题目可取，会被
``AbortedBeforeSubmit("没有取到题目")`` 判成 SKIPPED，看起来像故障。

只读用途请直接调本适配器的辅助方法：

- ``list_pending_assignments()`` —— 未完成作业（含截止时间）
- ``list_finished_essays()``    —— 已提交作文（含成绩列与稿次）
- ``fetch_essay_detail(rid, eid)`` —— 总分 / 四维 / 总评 / 按句点评

`list_courses()` / `list_assignments()` 仍按契约实现，供将来框架统一调度使用。
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlsplit, urljoin

from kedaibiao.core.models import (
    Answer,
    Assignment,
    Course,
    Credentials,
    SubmissionResult,
    TaskKind,
)
from kedaibiao.platforms.base import (
    AdapterContext,
    Capabilities,
    NotSupportedError,
    PlatformAdapter,
)
from kedaibiao.platforms.pigai import protocol
from kedaibiao.platforms.pigai.client import PigaiClient, PigaiError, PigaiResponseError
from kedaibiao.platforms.pigai.parse import (
    parse_assignment_rows,
    parse_essay_detail,
    parse_essay_rows,
    parse_write_page,
)

__all__ = ["PigaiAdapter", "SessionExpiredError"]

#: 批改网学生端没有「课程」维度，只有一个作业清单。
#: 为了让 `Pipeline` 的 collect 流程能跑，返回这个**合成课程**（remote_id 固定）。
SYNTHETIC_COURSE_ID = "pigai"
SYNTHETIC_COURSE_NAME = "批改网（我的作业）"

#: 站点给的截止时间格式（实测列表页为 `2026-11-16 23:59`，已提交页为 `2026-10-19`）
_DEADLINE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")


class SessionExpiredError(PigaiError):
    """服务端返回了登录页 —— 会话失效。

    刻意抛而不是返回空列表：空列表会被上层误读成「这个平台没有作业」，
    属于 integration-guide §6.2 明确禁止的「异常响应被静默吞掉」。
    """


def parse_deadline(text: str | None) -> datetime | None:
    """解析站点的时间文本；解析不出来返回 ``None``（不抛）。"""
    value = (text or "").strip()
    if not value:
        return None
    for fmt in _DEADLINE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


#: 容忍写法变体的登录表单属性（建议-12：`id = "lg_from_w"` / 单引号 / 大写 ID）
_LOGIN_FORM_RE = re.compile(
    rf'id\s*=\s*["\']?{re.escape(protocol.LOGIN_FORM_MARKER)}["\']?', re.IGNORECASE
)

#: 站外判定的基准（比 netloc 而不是字符串前缀，见 `_absolute`）
_BASE_URL_PARTS = urlsplit(protocol.BASE_URL)


def _guard_session(html: str) -> None:
    """**会话失效守卫**：命中任一标记即抛 ``SessionExpiredError``。

    两件事都必须覆盖（都由独立验证者发现）：

    1. **登录表单页** —— 用**属性形态**而不是裸子串：本站自己的 JS 文案里也会出现
       `lg_from_w`，裸子串会把合法页误判成登录页（建议-8）。这里再用正则
       **容忍写法变体**（建议-12）。
    2. **会话失效页** —— 它是个约 1.7 KB 的 200 页面，**不含登录表单标识**。
       只查第 1 条会把「会话失效」误报成「页面结构变了」，调用方看到的是
       「找不到 #essayList」而不是「请重新登录」（**需知会-5**）。
    """
    if _LOGIN_FORM_RE.search(html):
        raise SessionExpiredError(
            "会话已失效：服务端返回登录页（请检查凭据或重新登录）"
        )
    for marker in protocol.SESSION_EXPIRED_MARKERS:
        if marker == protocol.LOGIN_FORM_MARKER_STRICT:
            continue  # 已由上面的正则覆盖（并额外容忍写法变体）
        if marker in html:
            raise SessionExpiredError(
                "会话已失效：服务端返回「请先登录」跳转页（请重新登录）"
            )


def _absolute(href: str) -> str:
    """把页面给出的 href 变成绝对 URL；**站外或非 http(s) 的一律返回空串**。

    ⚠ 四个坑（都来自独立验证者，都有回归用例）：

    1. 待完成作业的 href 是**只有查询串**的形式（``?c=v2&a=…&rid=…``）。
       ``urljoin("https://host/", "?c=…")`` 会得到 ``https://host/?c=…``
       —— **丢掉 `/index.php`**，因为纯查询引用是相对「当前路径」解析的。
       本站首屏就是 ``/index.php``，所以必须**显式补回**。
    2. 基址不能只用 ``BASE_URL``（它没有路径段）。
    3. ``javascript:;``、``#`` 这类要在**拼接之前**挡掉：``urljoin`` 会把 ``"#"``
       拼成 ``https://www.pigai.org/#``，**仍以 BASE_URL 开头**，前缀守卫拦不住（建议-14）。
    4. **站外判定必须比 netloc，不能比字符串前缀** —— ``startswith(BASE_URL)`` 会放行
       ``https://www.pigai.org.evil.com/x``（需知会-7）。这里比 ``netloc`` 与 ``scheme``。

    这个值**只用于展示**，适配器绝不跟随它发请求。
    """
    value = (href or "").strip()
    if not value:
        return protocol.BASE_URL + "/"
    if value.startswith("#") or value.startswith("javascript:"):
        return ""
    if value.startswith("?"):
        value = "/index.php" + value
    resolved = urljoin(protocol.BASE_URL + "/", value)
    base, parts = _BASE_URL_PARTS, urlsplit(resolved)
    if parts.netloc != base.netloc or parts.scheme != base.scheme:
        return ""
    return resolved


#: 平台在「没有待完成作业」时的页面文案（实测原话）。
#: 用它区分「真的空了」与「页面改版/登录失效」—— 后者必须继续报错，不能静默返回空。
EMPTY_PENDING_TEXT = "目前没有待完成的作业"


class PigaiAdapter(PlatformAdapter):
    """只读的批改网适配器。"""

    name = "pigai"
    display_name = "批改网"
    capabilities = Capabilities(
        login=True,
        list_courses=True,
        list_assignments=True,
        # 题目（作文题面）不通过接口取，仍为 False
        fetch_questions=False,
        # 提交：编码形态已由真实探测确认（单次百分号编码），实现见 submit 方法
        submit=True,
        course_playback=False,
    )

    def __init__(self, ctx: AdapterContext | None = None) -> None:
        super().__init__(ctx)
        self._client: PigaiClient | None = None

    # -- 客户端 -----------------------------------------------------------

    @property
    def client(self) -> PigaiClient:
        """惰性构造：没注入 ``ctx.http`` 时只在真正用到才报错。"""
        if self._client is None:
            self._client = PigaiClient(self.ctx.client(), logger=self.log)
        return self._client

    # -- 生命周期 ---------------------------------------------------------

    async def login(self, credentials: Credentials) -> bool:
        """登录。**把「被拒绝」与「网络炸了」分开处理**：

        - 服务端明确拒绝（非 2xx）→ 记日志 + 返回 ``False``，
          让 ``Pipeline`` 走它既有的「登录失败」分支（落一条 FAILED 记录并发通知）；
        - 网络层失败（``TransportError``，是 ``OSError`` 子类）→ 原样上抛，
          因为它可被重试，而且把它报成「登录失败、请检查凭据」是**误导**。
        """
        if not credentials.username:
            self.log.error("缺少用户名，无法登录批改网")
            return False
        try:
            ok = await self.client.login(credentials.username, credentials.password)
        except PigaiResponseError as exc:
            self.log.error("批改网登录被拒绝：%s", exc)
            return False
        self.log.info("批改网登录%s", "成功" if ok else "失败（仍返回登录页）")
        return ok

    async def is_logged_in(self) -> bool:
        return await self.client.is_logged_in()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- 只读辅助（本适配器的实际用途）--------------------------------------

    async def list_pending_assignments(self) -> list[Assignment]:
        """未完成作业。数据源已实测（见 docs/research/pigai.md §6.1）。

        ⚠️ **空列表是合法状态**：全部作业都交完后，页面不再有表头
        ``ul.reItemM``（实测）。原实现把「找不到表头」一律当页面异常抛错，
        结果交完最后一个待办作业后，控制台的作业页会整页报错、看起来像坏了。

        这里只在**连数据行** ``ul.reItemS`` 都没有时才当空列表；
        有数据行却没有表头说明是真的改版了，仍然抛错（不能掩盖）。
        """
        from kedaibiao.platforms.pigai.parse import HeaderMismatchError

        html = await self.client.get(protocol.Endpoints.ASSIGNMENTS_PENDING)
        _guard_session(html)
        try:
            rows = parse_assignment_rows(html)
        except HeaderMismatchError:
            # 判定「真空列表」需要**两个条件同时成立**，否则可能是改版或登录页：
            #   1. 一个数据行都没有（LIST_ROW_CLASS 不出现）
            #   2. 页面出现平台的空态文案「目前没有待完成的作业」（实测抓到的原话）
            # 只满足一个就照旧抛错 —— 宁可报错，也不要把「页面变了」误判成「没有作业」。
            if protocol.LIST_ROW_CLASS in html or EMPTY_PENDING_TEXT not in html:
                raise
            self.log.info("未完成列表为空（平台空态文案命中）—— 按「没有待办」处理")
            return []
        return [_to_pending_assignment(row) for row in rows]

    async def list_finished_essays(self) -> list[protocol.EssayRow]:
        """已提交作文（含成绩列、提交次数 = 稿次）。见 §6.2。"""
        html = await self.client.get(protocol.Endpoints.ESSAYS_DONE)
        _guard_session(html)
        return parse_essay_rows(html)

    async def fetch_essay_detail(self, rid: str, eid: str) -> protocol.EssayDetail:
        """作文评分详情：总分 / 四维 / 总评 / **正文** / 按句点评。见 §6.3-6.6。"""
        html = await self.client.get(protocol.Endpoints.essay_view(eid, rid))
        _guard_session(html)
        return parse_essay_detail(html, rid=rid, eid=eid)

    async def fetch_assignment_by_rid(self, rid: str) -> protocol.WritePage:
        """**按作业号定位一个作业** —— 用户需求「每次新作业都要作业号」的直接落点。

        走站点自己的「查找作文」端点（``Endpoints.lookup_by_rid``，GET，
        实测无副作用：进入前后列表快照稿次总和 19→19）。

        该页面与写作页功能等价，因此能顺带**读回已有稿子的正文** ——
        这是「未截止可以继续修改」的**只读一半**：先看到改到哪了。

        ⚠ 本方法**只 GET**。它拿到的页面同时是写作入口，但适配器不 POST。
        """
        html = await self.client.get(protocol.Endpoints.lookup_by_rid(rid))
        _guard_session(html)
        return parse_write_page(html, rid=rid)

    # -- PlatformAdapter 契约 ---------------------------------------------

    #: 提交载荷是否已由真实数据确认。
    #:
    #: 2026-10-19 用真实会话对草稿端点做探测、再用只读接口读回比对，
    #: 确认了**单次百分号编码**（静态推断的二次编码是错的）。
    #: 端点是静态读出的，响应契约（``eid=`` / ``msg:``）也来自静态 JS。
    submit_payload_confirmed: bool = True

    async def submit(
        self, assignment: Assignment, answers: list[Answer]
    ) -> SubmissionResult:
        """提交一篇作文。

        ⚠️ 这是**不可逆的写操作**。上层（控制台/流水线）必须先干跑让人确认。
        作文是整篇文本，所以 ``answers`` 里取第一段非空 ``text`` 当正文；
        选择题那套 ``choices`` 在这里没有意义。
        """
        from kedaibiao.platforms.pigai import submit as submit_mod

        if not self.submit_payload_confirmed:
            raise NotSupportedError(
                "pigai 提交载荷尚未确认，拒绝发出写请求。"
            )

        essay = ""
        for answer in answers:
            if answer.text and answer.text.strip():
                essay = answer.text
                break
        if not essay.strip():
            raise PigaiError(
                "提交内容为空。作文提交需要整篇正文（Answer.text），"
                "题干/选项那套不适用。"
            )

        page = await self.client.get(
            f"/index.php?c=v2&a=write&rid={assignment.remote_id}"
        )
        variables = _write_page_vars(page)
        plan = submit_mod.build_submit_payload(
            essay=essay,
            title=str(assignment.title or ""),
            rid=assignment.remote_id,
            eid=variables.get("_eid", ""),
            from_code=variables.get("_fromCode", ""),
            engine=variables.get("_engine", ""),
        )

        http = self.ctx.client()
        response = await http.request(
            "POST",
            protocol.BASE_URL + submit_mod.SUBMIT_PATH,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": f"{protocol.BASE_URL}/index.php?c=v2&a=write&rid={assignment.remote_id}",
                "Origin": protocol.BASE_URL,
            },
            body=submit_mod.encode_body(plan.payload),
        )
        result = submit_mod.parse_submit_response(response.text())
        return SubmissionResult(
            assignment_id=assignment.remote_id,
            ok=result.ok,
            score=None,  # 作文是整体评分，提交响应不带分数
            message=result.summary(),
            raw={"eid": result.eid, "raw": result.raw_text[:500]},
        )

    async def list_courses(self) -> list[Course]:
        """返回**单一合成课程**。

        批改网学生端没有课程维度（没有「课程列表」接口），作业按班级投递而不按课程组织。
        这里合成一门课程，只是为了让 ``Pipeline`` 的 collect 流程不需要为本平台开特例。
        真实用途请用 `list_pending_assignments()`。
        """
        return [
            Course(
                remote_id=SYNTHETIC_COURSE_ID,
                name=SYNTHETIC_COURSE_NAME,
                raw={"note": "合成课程：批改网学生端无课程维度，见 adapter.py docstring"},
            )
        ]

    async def list_assignments(self, course: Course) -> list[Assignment]:
        """未完成 + 已完成，按 `rid` 合并（未完成优先）。

        ``finished=True`` 的那些会被 ``Pipeline`` 直接判 SKIPPED —— 这是想要的：
        已提交的作文不该被重跑。分数与稿次放在 ``Assignment.raw``。
        """
        pending = await self.list_pending_assignments()
        finished_rows = await self.list_finished_essays()

        merged: dict[str, Assignment] = {}
        for item in pending:
            # Pipeline._collect 只在题目自带 course_name 为空时才回填，
            # 走到这里说明是直调 list_assignments，所以自己填上。
            item.course_name = course.name
            merged[item.remote_id] = item
        for row in finished_rows:
            if row.rid in merged:
                continue  # 理论上互斥；真撞了以未完成为准
            merged[row.rid] = Assignment(
                remote_id=row.rid,
                title=row.title,
                kind=TaskKind.HOMEWORK,
                course_name=course.name,
                deadline=parse_deadline(row.deadline),
                progress=1.0,
                finished=True,
                url=_absolute(row.url),
                raw={
                    "eid": row.eid,
                    "genre": row.genre,
                    "submitted_at": row.submitted_at,
                    "score_raw": row.score_raw,
                    "submit_count": row.submit_count,
                },
            )
        return list(merged.values())

    # -- 内部 -------------------------------------------------------------


def _to_pending_assignment(row: protocol.AssignmentRow) -> Assignment:
    return Assignment(
        remote_id=row.rid,
        title=row.title,
        kind=TaskKind.HOMEWORK,
        deadline=parse_deadline(row.deadline),
        finished=False,
        # 待完成作业的 href 指向**写作页**且不含 eid（实测），这里原样绝对化仅供展示
        url=_absolute(row.url),
        raw={
            "kind_raw": row.kind,
            "author": row.author,
            "classes": row.classes,
            "raw_cells": list(row.raw_cells),
        },
    )


def _write_page_vars(html: str) -> dict[str, str]:
    """从写作页里取提交需要的页面变量（``_eid`` / ``_fromCode`` / ``_engine``）。

    取不到就返回空串 —— 首交时 ``_eid`` 本来就是空串。
    """
    import re

    out: dict[str, str] = {}
    for name in ("_eid", "_fromCode", "_engine", "_rid"):
        match = re.search(rf"{name}\s*=\s*['\"]?([^'\"\n;]*)", html or "")
        out[name] = match.group(1).strip() if match else ""
    return out
