"""把 U校园 的响应体翻成适配器内部结构（再往上由 ``adapter`` 翻成 ``core.models``）。

三条贯穿本模块的纪律：

1. **异常响应不能静默吞掉**：结构缺字段、正文不是 JSON、解密失败一律
   :class:`~kedaibiao.platforms.unipus.errors.UnipusProtocolError`；
2. **19 位 ID 精度**：题目 ``instanceId`` 全程 ``str``/``int``，绝不经过
   ``float()``；收到浮点会显式报错（规格第 6.2 节）；
3. **不发明语义**：规格说不清的地方（题面、答案字符串含义）只原样保留
   到 ``raw``，不推断。

层级（规格第 6.1 节）：::

    教学班 class → 班级教材绑定 → 单元 unit → 分组 group → 题目 item
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from kedaibiao.core.models import QuestionType
from kedaibiao.platforms.unipus.crypto import decrypt_unipus_cipher
from kedaibiao.platforms.unipus.endpoints import CIPHER_PREFIX
from kedaibiao.platforms.unipus.errors import UnipusProtocolError

# --------------------------------------------------------------------------
# 基础类型转换
# --------------------------------------------------------------------------


def loads_json(text: str, *, what: str) -> Any:
    """解析 JSON；失败时带正文摘要报错，**不吞**。"""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        preview = text[:200] if isinstance(text, str) else repr(text[:200])
        raise UnipusProtocolError(f"{what} 不是合法 JSON: {preview!r}") from exc


def loads_json_field(value: Any, *, what: str) -> Any:
    """某些字段是「字符串化 JSON」（``course`` / ``content`` / ``answer``）。

    字符串就二次解析；本来已经是 dict/list 就原样返回。
    """
    if isinstance(value, str):
        return loads_json(value, what=what)
    if isinstance(value, (dict, list)):
        return value
    raise UnipusProtocolError(f"{what} 既不是字符串化 JSON，也不是对象/数组：{type(value).__name__}")


def as_text(value: Any, *, field_name: str = "字段") -> str:
    """数字或字符串取文本（例如 ``school`` 可能是数字 ``1000``）。

    浮点直接拒绝：ID 类字段一旦经过浮点就可能损坏（规格第 6.2 节）。
    """
    if isinstance(value, bool):
        raise UnipusProtocolError(f"{field_name} 期望文本/整数，实际是布尔值")
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if value is None:
        return ""
    raise UnipusProtocolError(
        f"{field_name} 期望文本/整数，实际是 {type(value).__name__}（浮点会损坏 19 位 ID）"
    )


def precision_id(value: Any, *, field_name: str) -> str:
    """平台 ID：接受十进制数字串或整数，**绝不允许浮点**。

    规格第 6.2 节：19 位十进制 ID 超过 2^53，前端 JS 曾因双精度把它舍入过
    （``…042765`` → ``…042800``）。Python 的 ``json`` 保留任意精度整数，
    所以只要全程 ``str``/``int`` 就是安全的。
    """
    if isinstance(value, bool):
        raise UnipusProtocolError(f"{field_name} 期望十进制 ID，实际是布尔值")
    if isinstance(value, int):
        if value < 0:
            raise UnipusProtocolError(f"{field_name} 期望非负 ID，实际 {value}")
        return str(value)
    if isinstance(value, str):
        if not value.isdigit():
            raise UnipusProtocolError(f"{field_name} 期望十进制数字串，实际 {value!r}")
        return value
    raise UnipusProtocolError(
        f"{field_name} 期望十进制 ID（str/int），实际是 {type(value).__name__}；"
        "浮点会损坏 19 位 ID，必须拒绝"
    )


def as_optional_int(value: Any, *, field_name: str = "字段") -> int | None:
    """取整数；``None``/取不到返回 ``None``；数字串按整数解析。"""
    if value is None:
        return None
    if isinstance(value, bool):
        raise UnipusProtocolError(f"{field_name} 期望整数，实际是布尔值")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise UnipusProtocolError(f"{field_name} 期望整数，实际 {value!r}")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError as exc:
            raise UnipusProtocolError(f"{field_name} 期望整数，实际 {value!r}") from exc
    raise UnipusProtocolError(f"{field_name} 期望整数，实际 {type(value).__name__}")


def as_optional_float(value: Any, *, field_name: str = "字段") -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise UnipusProtocolError(f"{field_name} 期望数值，实际是布尔值")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError as exc:
            raise UnipusProtocolError(f"{field_name} 期望数值，实际 {value!r}") from exc
    raise UnipusProtocolError(f"{field_name} 期望数值，实际 {type(value).__name__}")


def as_optional_bool(value: Any, *, field_name: str = "字段") -> bool | None:
    """三态布尔：``None`` 表示响应里没有该字段（**不等于 False**）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
        raise UnipusProtocolError(f"{field_name} 期望 0/1，实际 {value!r}")
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1"):
            return True
        if text in ("false", "0"):
            return False
        if not text:
            return None
    raise UnipusProtocolError(f"{field_name} 期望布尔/0/1，实际 {value!r}")


def as_str_tuple(value: Any, *, field_name: str) -> tuple[str, ...]:
    """字符串列表（答案就是这种形状：``["D"]`` / ``["A","B","D"]``）。"""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise UnipusProtocolError(f"{field_name} 期望字符串数组，实际 {type(value).__name__}")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise UnipusProtocolError(
                f"{field_name} 期望字符串元素，实际 {type(item).__name__}"
            )
        out.append(item)
    return tuple(out)


def as_dict(value: Any, *, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UnipusProtocolError(f"{what} 期望对象，实际 {type(value).__name__}")
    return value


def dig(obj: Any, *keys: str) -> Any:
    """逐层取值，任何一层不是对象就返回 ``None``（不抛）。"""
    current = obj
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


# --------------------------------------------------------------------------
# 宽松查找（**只在登录链使用**，规格第 2.1 节）
# --------------------------------------------------------------------------


def deep_find_key(obj: Any, key: str) -> Any:
    """在整棵 JSON 里递归找第一个出现的 ``key``。"""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = deep_find_key(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = deep_find_key(item, key)
            if found is not None:
                return found
    return None


def deep_find_first_text(obj: Any, keys: tuple[str, ...]) -> str:
    """按 ``keys`` 顺序，取第一个非空文本值（展示名降级链用）。"""
    for key in keys:
        value = deep_find_key(obj, key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return ""


# --------------------------------------------------------------------------
# 会话（规格第 2.2 节）
# --------------------------------------------------------------------------

#: 展示名的降级顺序（规格第 2.1 节）
DISPLAY_NAME_KEYS: tuple[str, ...] = ("name", "nickName", "username", "userName", "phone")


@dataclass(slots=True, frozen=True)
class SessionInfo:
    """从 ``/api/account/user/info`` 取到的会话要素。"""

    open_id: str
    display_name: str = ""
    school: str = ""
    app_user_id: str = ""


def parse_session(body: Any) -> SessionInfo:
    """解析用户信息。

    ``open_id`` **只能取 ``ssoId``**，不许取 ``appUserId``（规格第 2.1 节）；
    ``school`` 可能是数字，必须能当文本取出来。
    """
    payload = as_dict(body, what="用户信息响应")
    open_id = as_text(deep_find_key(payload, "ssoId"), field_name="ssoId")
    if not open_id:
        raise UnipusProtocolError(
            "用户信息响应里取不到非空 ssoId（open_id 只能来自 ssoId，不能退回 appUserId）"
        )
    return SessionInfo(
        open_id=open_id,
        display_name=deep_find_first_text(payload, DISPLAY_NAME_KEYS),
        school=as_text(deep_find_key(payload, "school"), field_name="school"),
        app_user_id=as_text(deep_find_key(payload, "appUserId"), field_name="appUserId"),
    )


# --------------------------------------------------------------------------
# 班级×教材（规格第 6.1、6.2、6.4 节）
# --------------------------------------------------------------------------

#: 班级节点下教材列表的键（规格第 6.1 节层级图）
TEXTBOOK_LIST_KEYS: tuple[str, ...] = ("courseResourceList",)


@dataclass(slots=True, frozen=True)
class CourseBinding:
    """一个「教学班 × 教材」绑定：本适配器的 ``Course`` 就是它。"""

    class_id: str
    class_name: str
    class_numeric_id: str
    course_instance_id: str
    course_resource_id: str
    resource_id: str = ""
    strategy_id: str = ""
    book_name: str = ""
    finish_points: int | None = None
    total_points: int | None = None
    activated: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def remote_id(self) -> str:
        """门户「班级教材 ID」（``courseResourceId``）优先，缺失时退到实例 ID。"""
        return self.course_resource_id or self.course_instance_id


def _looks_like_textbook(node: Any) -> bool:
    """教材节点的判据：有 ``instanceId`` 或 ``resourceId``（规格第 6.4 节）。"""
    if not isinstance(node, dict):
        return False
    return bool(node.get("instanceId") or node.get("courseInstanceId") or node.get("resourceId"))


def _textbook_entries(class_node: dict[str, Any]) -> list[dict[str, Any]]:
    """取班级节点下的教材列表。

    主键是规格第 6.1 节点名的 ``courseResourceList``；该键不存在时，退化为
    「在本节点里找第一个装得下教材节点的列表」—— 这条兜底比"猜键名"更窄，
    判据仍是规格第 6.4 节给的 ``instanceId``/``resourceId``。
    """
    for key in TEXTBOOK_LIST_KEYS:
        value = class_node.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    for value in class_node.values():
        if isinstance(value, list) and any(_looks_like_textbook(item) for item in value):
            return [item for item in value if isinstance(item, dict)]
    return []


def _class_entries(payload: dict[str, Any], *, what: str) -> list[dict[str, Any]]:
    course_list = dig(payload, "value", "courseList")
    if not isinstance(course_list, list):
        raise UnipusProtocolError(f"{what} 里取不到 value.courseList[]（班级进度接口的真实结构）")
    return [item for item in course_list if isinstance(item, dict)]


def parse_course_list(body: Any) -> tuple[CourseBinding, ...]:
    """解析 ``/api/cmgt/course/getCourseListByStudent``。

    规格第 6.4 节：**缺失 ``instanceId`` 或 ``classId`` 的条目要丢弃**
    （避免跨班串号）。
    """
    payload = as_dict(body, what="班级进度响应")
    bindings: list[CourseBinding] = []
    for class_node in _class_entries(payload, what="班级进度响应"):
        class_id = as_text(
            class_node.get("classId") or class_node.get("classID"), field_name="classId"
        )
        if not class_id:
            continue
        class_name = as_text(
            class_node.get("name") or class_node.get("className") or "", field_name="班级名"
        )
        class_numeric_id = as_text(class_node.get("id"), field_name="class.id")
        for book in _textbook_entries(class_node):
            instance_id = as_text(
                book.get("instanceId") or book.get("courseInstanceId"), field_name="instanceId"
            )
            if not instance_id:
                continue
            bindings.append(
                CourseBinding(
                    class_id=class_id,
                    class_name=class_name,
                    class_numeric_id=class_numeric_id,
                    course_instance_id=instance_id,
                    course_resource_id=as_text(book.get("id"), field_name="教材 id"),
                    resource_id=as_text(book.get("resourceId"), field_name="resourceId"),
                    strategy_id=as_text(book.get("strategyId"), field_name="strategyId"),
                    book_name=as_text(
                        book.get("name") or book.get("resourceName") or "", field_name="教材名"
                    ),
                    finish_points=as_optional_int(
                        book.get("finishPointNum"), field_name="finishPointNum"
                    ),
                    total_points=as_optional_int(
                        book.get("totalPointNum"), field_name="totalPointNum"
                    ),
                    raw=dict(book),
                )
            )
    return tuple(bindings)


# --------------------------------------------------------------------------
# 门户书架（只用来补激活标记 / 资源 ID）
# --------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class BookshelfBook:
    course_instance_id: str
    resource_id: str = ""
    book_name: str = ""
    activated: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _bookshelf_class_entries(payload: dict[str, Any], *, what: str) -> list[dict[str, Any]]:
    """书架根可能是 ``data`` 或 ``value``，班级列表键有多个候选（规格第 6.4 节）。"""
    for root_key in ("data", "value"):
        root = payload.get(root_key)
        if not isinstance(root, dict):
            continue
        for list_key in ("classList", "classes", "courseList"):
            value = root.get(list_key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    raise UnipusProtocolError(f"{what} 里找不到班级列表（data/value 下的 classList/classes/courseList）")


def _parse_activation(value: Any, *, field_name: str) -> bool | None:
    """``activation`` 字段：**数字 0 表示已激活**（与命名直觉相反）。

    也接受布尔与字符串形式（规格第 6.4 节）。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value == 0:
            return True
        if value == 1:
            return False
        raise UnipusProtocolError(f"{field_name} 期望 0/1，实际 {value!r}")
    if isinstance(value, str):
        text = value.strip()
        if text in ("0", "true", "True"):
            return True
        if text in ("1", "false", "False"):
            return False
        if not text:
            return None
    raise UnipusProtocolError(f"{field_name} 期望激活标记，实际 {value!r}")


def parse_bookshelf(body: Any) -> tuple[BookshelfBook, ...]:
    """解析 ``/api/cmgt/course/my/bookshelf``（不含进度）。"""
    payload = as_dict(body, what="书架响应")
    books: list[BookshelfBook] = []
    for class_node in _bookshelf_class_entries(payload, what="书架响应"):
        for key in TEXTBOOK_LIST_KEYS:
            entries = class_node.get(key)
            if not isinstance(entries, list):
                continue
            for book in entries:
                if not _looks_like_textbook(book):
                    continue
                books.append(
                    BookshelfBook(
                        course_instance_id=as_text(
                            book.get("instanceId") or book.get("courseInstanceId") or "",
                            field_name="instanceId",
                        ),
                        resource_id=as_text(book.get("resourceId"), field_name="resourceId"),
                        book_name=as_text(
                            book.get("name") or book.get("resourceName") or "",
                            field_name="教材名",
                        ),
                        activated=_parse_activation(
                            book.get("activation") if "activation" in book else None,
                            field_name="activation",
                        ),
                        raw=dict(book),
                    )
                )
    return tuple(books)


# --------------------------------------------------------------------------
# 目录（单元 → 分组）
# --------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class CatalogGroup:
    group_id: str
    name: str = ""
    base: str = ""
    tab_type: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class CatalogUnit:
    unit_id: str
    name: str = ""
    groups: tuple[CatalogGroup, ...] = ()


@dataclass(slots=True, frozen=True)
class Catalog:
    units: tuple[CatalogUnit, ...] = ()
    #: 从目录响应里回收到的 ``course-v1:`` 资源 ID（书架缺失时的兜底）
    resource_id: str = ""


#: 资源 ID 的前缀（规格第 6.2 节：缺失时从目录响应里回收）
RESOURCE_ID_PREFIX = "course-v1:"
_RESOURCE_ID_RE = re.compile(r"course-v1:[A-Za-z0-9+._:%\-]+")


def recover_resource_id(raw_text: str) -> str:
    """在目录响应原文里回收第一个 ``course-v1:`` 资源 ID（可能藏在封面 URL 里）。"""
    match = _RESOURCE_ID_RE.search(raw_text)
    return match.group(0) if match else ""


def _iter_role_nodes(node: Any):
    """递归产出所有带 ``role`` 的节点（目录层级容器的键名规格未给）。"""
    if isinstance(node, dict):
        if "role" in node:
            yield node
        for value in node.values():
            yield from _iter_role_nodes(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_role_nodes(item)


def parse_catalog(body: Any) -> Catalog:
    """解析 ``/course/api/course/{courseId}/default``。

    ``course`` 字段是**字符串化 JSON**，需要二次解析；节点用 ``role``
    （``unit``/``group``）区分，ID 取 ``url`` 字段；同一单元 ID 可能重复
    出现，需要合并去重（规格第 6.4 节）。
    """
    payload = as_dict(body, what="目录响应")
    if "course" not in payload:
        raise UnipusProtocolError("目录响应里没有 course 字段（内容是字符串化 JSON）")
    course = loads_json_field(payload["course"], what="目录的 course 字段")

    order: list[str] = []
    units: dict[str, dict[str, Any]] = {}
    for node in _iter_role_nodes(course):
        role = node.get("role")
        node_id = as_text(node.get("url"), field_name="目录节点 url")
        if role == "unit":
            unit = units.setdefault(node_id, {"name": "", "groups": {}, "order": []})
            if not unit["name"]:
                unit["name"] = as_text(node.get("name"), field_name="单元名")
            if node_id not in order:
                order.append(node_id)
        elif role == "group":
            unit_id = order[-1] if order else ""
            unit = units.setdefault(unit_id, {"name": "", "groups": {}, "order": []})
            if unit_id not in order:
                order.append(unit_id)
            if node_id and node_id not in unit["groups"]:
                unit["groups"][node_id] = CatalogGroup(
                    group_id=node_id,
                    name=as_text(node.get("name"), field_name="分组名"),
                    base=as_text(node.get("base"), field_name="分组 base"),
                    tab_type=as_text(node.get("tab_type"), field_name="分组 tab_type"),
                    raw=dict(node),
                )
                unit["order"].append(node_id)

    parsed_units = tuple(
        CatalogUnit(
            unit_id=unit_id,
            name=units[unit_id]["name"],
            groups=tuple(units[unit_id]["groups"][gid] for gid in units[unit_id]["order"]),
        )
        for unit_id in order
    )
    return Catalog(units=parsed_units, resource_id=recover_resource_id(json.dumps(body)))


def catalog_group_ids(catalog: Catalog) -> tuple[str, ...]:
    """按目录顺序收集全部分组 ID（批量必修接口**必须显式带全**）。"""
    return tuple(group.group_id for unit in catalog.units for group in unit.groups)


# --------------------------------------------------------------------------
# 题型分派（规格第 7.2 节：顺序即语义）
# --------------------------------------------------------------------------

#: 顺序即优先级，**不要**重排（规格第 7.2 节警告）
ORAL_QUESTION_TYPES: tuple[str, ...] = (
    "basic-oral",
    "paragraph-follow",
    "oral-personal-state",
    "video-dub",
    "oral-aloud",
    "ai-spoken",
    "sentencerecord",
    "paragraphrecord",
    "paragraphrecord2",
    "video_dubbing",
)
STUDY_QUESTION_TYPES: tuple[str, ...] = ("discussion", "rich-text-read")
ROLE_PLAY_QUESTION_TYPES: tuple[str, ...] = ("role-play", "roleplay")
UPLOAD_REPLY_TYPES: tuple[str, ...] = ("multiFileUpload", "multimedia_upload")
DROPDOWN_REPLY_TYPES: tuple[str, ...] = ("dropdownSelection", "dropdownchoic", "dropdown")
FILL_BLANK_REPLY_TYPES: tuple[str, ...] = ("fillblank", "fillblankScoop")
BANKED_CLOZE_REPLY_TYPES: tuple[str, ...] = ("bankedcloze", "material-banked-cloze")

#: 平台题型名 → ``core.QuestionType``（**平台原始名只进 raw，不进 core**）
CORE_TYPE_BY_UNIPUS_TYPE: dict[str, QuestionType] = {
    "single-choice": QuestionType.SINGLE,
    "multi-choice": QuestionType.MULTIPLE,
    "dropdown": QuestionType.SINGLE,
    "fill-blank": QuestionType.FILL,
    "banked-cloze": QuestionType.FILL,
    "subjective": QuestionType.ESSAY,
    "sequence": QuestionType.UNKNOWN,
    "objective": QuestionType.UNKNOWN,
    "study": QuestionType.UNKNOWN,
    "oral": QuestionType.UNKNOWN,
    "oral-state": QuestionType.UNKNOWN,
    "role-play": QuestionType.UNKNOWN,
    "upload": QuestionType.UNKNOWN,
}


def classify_question(
    *,
    reply_type: str,
    question_type: str,
    category: int | None,
    child_count: int,
) -> tuple[str, int]:
    """按规格第 7.2 节判定平台题型名与「贡献项数」。

    返回 ``(平台题型名, 贡献项数)``。顺序是语义的一部分：

    - ``oral-state`` 的 ``reply_type`` 也是 ``record``，必须排在 ``oral`` 前，
      否则「整题只算 1 项」会静默失效；
    - ``category`` 的真实含义：``0`` 学习题、``1`` 客观题（**不是 0**）。
    """
    reply = reply_type or ""
    kind = question_type or ""

    if kind != "video-popup":
        if category == 0:
            return ("study", 0)
        if kind in STUDY_QUESTION_TYPES:
            return ("study", 0)
        if child_count == 0 and not reply:
            return ("study", 0)

    if kind in ROLE_PLAY_QUESTION_TYPES:
        return ("role-play", 1)
    if kind == "oral-state":
        return ("oral-state", 1)
    if reply == "record" or kind in ORAL_QUESTION_TYPES or "record" in kind or "oral" in kind:
        return ("oral", max(child_count, 1))
    if kind == "multiFileUpload" or reply in UPLOAD_REPLY_TYPES:
        return ("upload", max(child_count, 1))
    if reply == "text-area":
        return ("subjective", max(child_count, 1))
    if reply == "singlechoice":
        return ("single-choice", max(child_count, 1))
    if reply == "multichoice":
        return ("multi-choice", max(child_count, 1))
    if reply in DROPDOWN_REPLY_TYPES:
        return ("dropdown", max(child_count, 1))
    if reply in FILL_BLANK_REPLY_TYPES:
        return ("fill-blank", max(child_count, 1))
    if (
        reply in BANKED_CLOZE_REPLY_TYPES
        or "bankedcloze" in kind
        or "banked-cloze" in kind
    ):
        return ("banked-cloze", max(child_count, 1))
    if reply == "sequence" or kind == "sequence" or "sequence" in kind:
        return ("sequence", max(child_count, 1))
    return ("objective", max(child_count, 1))


def core_question_type(unipus_type: str) -> QuestionType:
    """平台题型名 → ``core.QuestionType``；未登记的按 ``UNKNOWN``。"""
    return CORE_TYPE_BY_UNIPUS_TYPE.get(unipus_type, QuestionType.UNKNOWN)


# --------------------------------------------------------------------------
# 内容（题目）与答案
# --------------------------------------------------------------------------

#: 只有 ``type == 1``（音频）与 ``2``（视频）算媒体项；还见过 4/5/6（正文等）
MEDIA_CONTENT_TYPES: frozenset[int] = frozenset({1, 2})


@dataclass(slots=True, frozen=True)
class ContentQuestion:
    """内容侧一道题（规格第 7.1 节的字段是解析实现实际读取的全部）。"""

    instance_id: str
    unipus_type: str
    item_count: int
    child_count: int
    reply_type: str = ""
    question_type: str = ""
    category: int | None = None
    media_types: tuple[int, ...] = ()
    inner: dict[str, Any] = field(default_factory=dict)
    item: dict[str, Any] = field(default_factory=dict)

    @property
    def core_type(self) -> QuestionType:
        return core_question_type(self.unipus_type)


@dataclass(slots=True, frozen=True)
class StandardAnswer:
    """一道题的标准答案：外层下标与内容题序一一对应，内部按子题分组。"""

    children: tuple[tuple[str, ...], ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)


def _cipher_text(payload: dict[str, Any], field_name: str) -> str:
    """取密文字段；按名字取不到时，在顶层扫一遍 ``unipus.`` 开头的字符串兜底。"""
    value = payload.get(field_name)
    if isinstance(value, str) and value:
        return value
    for candidate in payload.values():
        if isinstance(candidate, str) and candidate.startswith(CIPHER_PREFIX):
            return candidate
    raise UnipusProtocolError(f"响应里既没有 {field_name} 字段，也找不到 {CIPHER_PREFIX} 开头的密文")


def _cipher_k(payload: dict[str, Any]) -> Any:
    if "k" not in payload:
        raise UnipusProtocolError("密文外壳缺少 k 字段（密钥 = 1a2b3c4d + k）")
    return payload["k"]


def parse_content(body: Any) -> tuple[ContentQuestion, ...]:
    """解析 ``/course/api/v3/content/…`` 的密文内容。

    两套 schema 由 ``courseId`` 前缀决定（规格第 7.1 节）：``course-v2:``
    是数组（取题目用这条），``course-v1:`` 是预览对象。本阶段只实现数组那条；
    遇到对象外壳会**显式报错**而不是返回空列表。
    """
    payload = as_dict(body, what="内容响应")
    cipher_text = _cipher_text(payload, "content")
    decrypted = decrypt_unipus_cipher(cipher_text, _cipher_k(payload))
    data = loads_json(decrypted, what="内容密文解密后的 JSON")

    if isinstance(data, dict):
        raise UnipusProtocolError(
            "内容密文解密后是对象（course-v1: 预览 schema，"
            "键形如 questions:questions）；本阶段只支持 course-v2: 的数组 schema"
        )
    if not isinstance(data, list):
        raise UnipusProtocolError(f"内容密文解密后期望数组，实际 {type(data).__name__}")

    questions: list[ContentQuestion] = []
    for raw_item in data:
        item = as_dict(raw_item, what="内容项")
        instance_id = precision_id(item.get("id"), field_name="题目 instanceId")
        if "content" not in item:
            raise UnipusProtocolError(f"题目 {instance_id} 缺少 content 字段")
        inner = loads_json_field(item["content"], what=f"题目 {instance_id} 的 content")
        inner = as_dict(inner, what=f"题目 {instance_id} 的 content")
        children = inner.get("children")
        child_count = len(children) if isinstance(children, list) else 0
        contents = inner.get("contents")
        media_types: list[int] = []
        if isinstance(contents, list):
            for entry in contents:
                if isinstance(entry, dict):
                    entry_type = as_optional_int(entry.get("type"), field_name="contents[].type")
                    if entry_type is not None:
                        media_types.append(entry_type)
        reply_type = as_text(
            inner.get("replyType") or item.get("replyType") or "", field_name="replyType"
        )
        question_type = as_text(
            item.get("type") or inner.get("type") or "", field_name="question type"
        )
        category = as_optional_int(
            inner.get("category") if inner.get("category") is not None else item.get("category"),
            field_name="category",
        )
        unipus_type, item_count = classify_question(
            reply_type=reply_type,
            question_type=question_type,
            category=category,
            child_count=child_count,
        )
        questions.append(
            ContentQuestion(
                instance_id=instance_id,
                unipus_type=unipus_type,
                item_count=item_count,
                child_count=child_count,
                reply_type=reply_type,
                question_type=question_type,
                category=category,
                media_types=tuple(media_types),
                inner=inner,
                item=dict(item),
            )
        )
    return tuple(questions)


def parse_answers(body: Any) -> tuple[StandardAnswer, ...]:
    """解析 ``/course/api/v3/answer/…``。

    ⚠️ 密文字段名是 ``data``，**不是** ``content``（规格第 3.3 节）。
    答案字符串的语义未确认，因此只原样保留，不做任何解码（规格 U2）。
    """
    payload = as_dict(body, what="答案响应")
    cipher_text = _cipher_text(payload, "data")
    decrypted = decrypt_unipus_cipher(cipher_text, _cipher_k(payload))
    data = loads_json(decrypted, what="答案密文解密后的 JSON")
    if not isinstance(data, list):
        raise UnipusProtocolError(f"答案密文解密后期望数组，实际 {type(data).__name__}")

    answers: list[StandardAnswer] = []
    for index, raw_item in enumerate(data):
        item = as_dict(raw_item, what=f"答案项[{index}]")
        raw_answer = item.get("answer")
        if raw_answer is None:
            # 口语/主观题在答案接口处为空，这是**合法**的，不是错误
            answers.append(StandardAnswer(children=(), raw=dict(item)))
            continue
        inner = loads_json_field(raw_answer, what=f"答案项[{index}] 的 answer")
        inner = as_dict(inner, what=f"答案项[{index}] 的 answer")
        children = inner.get("children")
        groups: list[tuple[str, ...]] = []
        if isinstance(children, list):
            for child_index, child in enumerate(children):
                if not isinstance(child, dict):
                    raise UnipusProtocolError(
                        f"答案项[{index}].children[{child_index}] 期望对象"
                    )
                groups.append(
                    as_str_tuple(
                        child.get("answers"),
                        field_name=f"答案项[{index}].children[{child_index}].answers",
                    )
                )
        answers.append(StandardAnswer(children=tuple(groups), raw=dict(item)))
    return tuple(answers)


# --------------------------------------------------------------------------
# 分组状态与必修集（规格第 6.3、6.4 节）
# --------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class GroupState:
    """``progress/v2`` 的单任务状态。

    ⚠️ ``server_state``（``state.state``）只表示"服务端有痕迹"，
    **不是**班级完成度；``score``/``pass`` 也不可信（规格第 6.4、11.2 节）。
    "做过没有"只能看 ``first_pass.datetime`` 是否为空。
    """

    first_pass_datetime: str | None = None
    finished: bool = False
    last_submit: str | None = None
    server_state: int | None = None
    score: tuple[int, ...] = ()
    score_pct: float | None = None
    flow_required: bool | None = None
    task_mini_score_pct: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def parse_group_state(body: Any) -> GroupState:
    payload = as_dict(body, what="分组状态响应")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise UnipusProtocolError(
            "分组状态响应里没有 data 对象（缺 lastSubmit 时平台会回 data:null）"
        )
    first_pass = data.get("first_pass")
    first_pass = first_pass if isinstance(first_pass, dict) else {}
    first_pass_datetime = first_pass.get("datetime")
    if first_pass_datetime is not None and not isinstance(first_pass_datetime, str):
        raise UnipusProtocolError(
            f"first_pass.datetime 期望字符串或 null，实际 {type(first_pass_datetime).__name__}"
        )
    state = data.get("state")
    state = state if isinstance(state, dict) else {}
    scores: list[int] = []
    raw_score = state.get("score")
    if isinstance(raw_score, list):
        for entry in raw_score:
            value = as_optional_int(entry, field_name="state.score[]")
            if value is not None:
                scores.append(value)
    elif raw_score is not None:
        raise UnipusProtocolError("state.score 期望数组")
    flow = data.get("flowStrategy")
    flow = flow if isinstance(flow, dict) else {}
    strategy = data.get("strategy")
    strategy = strategy if isinstance(strategy, dict) else {}
    return GroupState(
        first_pass_datetime=first_pass_datetime,
        finished=bool(first_pass_datetime),
        last_submit=as_text(state.get("lastSubmit"), field_name="lastSubmit") or None,
        server_state=as_optional_int(state.get("state"), field_name="state.state"),
        score=tuple(scores),
        score_pct=as_optional_float(state.get("score_pct"), field_name="score_pct"),
        flow_required=as_optional_bool(flow.get("required"), field_name="flowStrategy.required"),
        task_mini_score_pct=as_optional_float(
            strategy.get("task_mini_score_pct"), field_name="task_mini_score_pct"
        ),
        raw=dict(data),
    )


@dataclass(slots=True, frozen=True)
class RequiredGroup:
    group_id: str
    required: bool | None = None
    min_score_pct: float | None = None


@dataclass(slots=True, frozen=True)
class RequiredSet:
    """必修集（``leafs``）。

    ``missing=True`` 表示 ``leafs`` 为空 —— 规格第 6.3 节明确：这是
    **信息缺失**，不是"没有必修"，不能据此做任何统计或提交决策。
    """

    scope: str
    groups: tuple[RequiredGroup, ...] = ()
    missing: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


#: 必修集口径标识：ucontent 的 leafs 按 (实例, openId) 记账，不随班级变化
SCOPE_UCONTENT_LEAFS = "ucontent_leafs"


def parse_required_leafs(body: Any) -> RequiredSet:
    payload = as_dict(body, what="批量进度响应")
    leafs = dig(payload, "rt", "leafs")
    if leafs is None or leafs == {}:
        # 规格第 6.3 节：空 leafs = 信息缺失，不是"没有必修"
        return RequiredSet(scope=SCOPE_UCONTENT_LEAFS, missing=True, raw=payload)
    if not isinstance(leafs, dict):
        # 结构变了要报错，不能当"信息缺失"糊过去
        raise UnipusProtocolError(
            f"rt.leafs 期望对象（分组 ID → 策略），实际 {type(leafs).__name__}"
        )
    groups: list[RequiredGroup] = []
    for group_id, node in leafs.items():
        node = node if isinstance(node, dict) else {}
        strategies = node.get("strategies")
        strategies = strategies if isinstance(strategies, dict) else {}
        groups.append(
            RequiredGroup(
                group_id=as_text(group_id, field_name="leafs 键"),
                required=as_optional_bool(
                    strategies.get("required"), field_name="strategies.required"
                ),
                min_score_pct=as_optional_float(
                    strategies.get("min_score_pct"), field_name="strategies.min_score_pct"
                ),
            )
        )
    return RequiredSet(scope=SCOPE_UCONTENT_LEAFS, groups=tuple(groups), raw=payload)


# --------------------------------------------------------------------------
# 作业标识约定
# --------------------------------------------------------------------------

#: ``Assignment.remote_id`` 的分隔符：``{实例 ID}/{分组 ID}``
#:
#: ⚠️ 这个值同时是 ``RunRecord.assignment_id`` 与**幂等键**
#: （``ProgressStore.already_done``），**一旦上线就不能改格式**。
ASSIGNMENT_ID_SEPARATOR = "/"


def pack_assignment_id(course_instance_id: str, group_id: str) -> str:
    """把 (实例 ID, 分组 ID) 打成一个不透明 ID 给 ``Assignment.remote_id``。"""
    if not course_instance_id or not group_id:
        raise UnipusProtocolError("打作业 ID 需要非空的实例 ID 与分组 ID")
    if ASSIGNMENT_ID_SEPARATOR in group_id:
        raise UnipusProtocolError(f"分组 ID 不应含分隔符 {ASSIGNMENT_ID_SEPARATOR!r}：{group_id!r}")
    return f"{course_instance_id}{ASSIGNMENT_ID_SEPARATOR}{group_id}"


def split_assignment_id(remote_id: str) -> tuple[str, str]:
    """从 ``Assignment.remote_id`` 拆回 (实例 ID, 分组 ID)。

    分组 ID 形如 ``u1g2`` 不含 ``/``，所以从**最后一个** ``/`` 切开。
    """
    if not isinstance(remote_id, str) or ASSIGNMENT_ID_SEPARATOR not in remote_id:
        raise UnipusProtocolError(
            f"作业 ID 格式应为 实例ID{ASSIGNMENT_ID_SEPARATOR}分组ID，实际 {remote_id!r}"
        )
    course_instance_id, _, group_id = remote_id.rpartition(ASSIGNMENT_ID_SEPARATOR)
    if not course_instance_id or not group_id:
        raise UnipusProtocolError(f"作业 ID 格式不完整：{remote_id!r}")
    return course_instance_id, group_id
