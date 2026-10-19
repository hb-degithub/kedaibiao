"""批改网「提交作文」载荷构造与响应解析。

## 依据

`docs/research/pigai-write-protocol.md`：
- 主提交端点 `POST /index.php?c=ajax&a=postSave`
- 请求体 18 个字段**逐个**从写作页内嵌 JS 读出（§4.2）
- 响应是**纯文本**：`eid=<数字>` 成功 / `msg:<文本>` 失败（§4.4）

## 编码形态：**已由真实探测确认是「单次」**

原静态分析的推断是「二次百分号编码」（客户端 encodeURIComponent 一次 + 表单
再编码一次）。**实测推翻了它。**

探测方法（2026-10-19，用真实会话对 `saveCaogao2` 写作草稿，再用只读的
`loadCaogao2` 读回比对）：

| 发送方式 | 服务端存下的内容 | 判定 |
| --- | --- | --- |
| 二次编码 | `kedaibiao-probe%20%E7%A9%BA%E6%A0%BC...` | ✗ `%20` 成了字面量 |
| 单次编码 | `kedaibiao-probe 空格 中文 测试` | ✓ **逐字还原** |

所以：**值只做一次 `quote`，`urlencode` 再转义一次即可**（即不做预编码 + 让
`urlencode` 处理，或预编码一次后**不再**二次转义）。本模块的 ``double_encode``
默认 ``False``，保留参数只为可复现那次探测。

> 如果当初照静态推断实现二次编码，交上去的每篇作文都会是 `%20` 乱码，
> 而且**只有交完才会发现** —— 这就是写操作不肯猜的理由。

## 与答题器答案的关系

作文是**整篇文本**，不是选择题。所以这里只接收最终正文文本（由答题器或人提供），
不涉及 ``Answer.choices``。
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from typing import Any

#: 提交端点（主路径）
SUBMIT_PATH = "/index.php?c=ajax&a=postSave"
#: 表单回退端点（分析阶段异常时的老路径）
SUBMIT_FALLBACK_PATH = "/index.php?c=write&a=postSave"


class SubmitPayloadError(ValueError):
    pass


@dataclass(slots=True)
class SubmitPlan:
    payload: dict[str, str]
    rid: str
    eid: str
    double_encoded: bool
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"rid={self.rid} eid={self.eid or '(首交)'} "
            f"编码={'二次' if self.double_encoded else '单次'} "
            f"正文字符数={len(self.payload.get('utContent', ''))}"
        )


def encode_field(value: str, *, double: bool = True) -> str:
    """按确认过的编码形态处理一个字段值。

    ``double=True``：先 ``quote`` 一次（等价于 JS 的 ``encodeURIComponent``），
    再 ``quote`` 一次（模拟 jQuery/表单序列化对 ``%`` 的再编码）。
    """
    once = urllib.parse.quote(value, safe="")
    return urllib.parse.quote(once, safe="") if double else once


def build_submit_payload(
    *,
    essay: str,
    title: str,
    rid: str,
    eid: str = "",
    remark: str = "",
    remark_original: str = "",
    file_name: str = "",
    file_path: str = "",
    tiku_id: str = "0",
    engine: str = "",
    from_code: str = "",
    auto_del: str = "",
    stu_class: str = "",
    double_encode: bool = False,
) -> SubmitPlan:
    """按 §4.2 的 18 个字段拼请求体。

    只带**有证据**的字段；字段集之外一律不加（§4.2 末尾的「严禁脑补」）。
    """
    if not rid.strip():
        raise SubmitPayloadError("缺少作业号 rid")
    if not essay.strip():
        raise SubmitPayloadError("正文为空，不提交")

    warnings: list[str] = []
    if double_encode:
        warnings.append(
            "使用了二次百分号编码 —— 实测这条路是**错的**（会把 %20 变字面量），"
            "只保留用于复现那次探测"
        )

    payload: dict[str, str] = {
        "utContent": encode_field(essay, double=double_encode),
        "utTitle": encode_field(title, double=double_encode),
        "bzold": encode_field(remark_original, double=double_encode),
        "bz": encode_field(remark, double=double_encode),
        "fileName": encode_field(file_name, double=double_encode),
        "filePath": encode_field(file_path, double=double_encode),
        "rid": rid.strip(),
        "eid": eid.strip(),
        "type": "0",
        "utype": "",
        "gao": "1",
        "uncheck": "",
        "tiku_id": tiku_id or "0",
        "engine": engine,
        "fromCode": from_code,
        "autoDel": auto_del,
    }
    if stu_class:
        payload["stu_class"] = encode_field(stu_class, double=double_encode)

    return SubmitPlan(
        payload=payload, rid=rid.strip(), eid=eid.strip(),
        double_encoded=double_encode, warnings=warnings,
    )


def encode_body(payload: dict[str, str]) -> bytes:
    """序列化成表单体。

    注意：这里**不再**对值做额外编码 —— 值已经按目标形态处理过了，
    ``urlencode`` 只会做一次标准转义。编码形态由 :func:`encode_field` 决定。
    """
    return urllib.parse.urlencode(payload).encode("utf-8")


# --------------------------------------------------------------------------
# 响应解析（纯文本）
# --------------------------------------------------------------------------


@dataclass(slots=True)
class SubmitResult:
    ok: bool
    eid: str = ""
    message: str = ""
    raw_text: str = ""

    def summary(self) -> str:
        if self.ok:
            return f"提交成功，稿号 {self.eid}"
        return f"提交失败：{self.message or '未知错误'}"


def parse_submit_response(text: str) -> SubmitResult:
    """解析纯文本响应（§4.4）。

    - ``eid=<数字>`` → 成功
    - ``msg:<文本>`` → 业务失败
    - 其它 → 未知错误（原样当错误信息）
    """
    body = (text or "").strip()
    if not body:
        return SubmitResult(ok=False, message="响应为空", raw_text=body)

    lowered = body.lower()
    if lowered.startswith("eid"):
        _, _, value = body.partition("=")
        eid = value.strip()
        if eid:
            return SubmitResult(ok=True, eid=eid, raw_text=body)
        return SubmitResult(ok=False, message="响应像成功但没给稿号", raw_text=body)

    if lowered.startswith("msg"):
        _, _, message = body.partition(":")
        return SubmitResult(ok=False, message=message.strip() or body, raw_text=body)

    return SubmitResult(ok=False, message=body[:300], raw_text=body)
