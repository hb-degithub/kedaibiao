"""批改网学生端协议契约 —— 端点、选择器、字段名的**唯一出处**。

纪律（沿用 ``docs/research/uai-core.md`` 的「端点唯一出处」原则）：
本文件之外**不允许**再出现任何硬编码的 URL 路径、DOM 选择器或 cookie 名。
新增端点/选择器一律先加到这里，再由 ``client.py`` / ``parse.py`` 引用。

证据来源：``docs/research/pigai.md``（Stage 1a 匿名侦察 + Stage 1b 登录后只读实测）。
凡标注「推断」的常量，均无实测样本，改动前请先补样本。

安全约定：
  * 凭据只从环境变量 / ``.env`` 读，本模块与调用方一律不得打印密码；
  * cookie 的**值**不得进日志或产物；
  * 本模块只描述**只读**端点；写入类端点（写作页/删除）**刻意不在此声明**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

# --------------------------------------------------------------------------
# 站点
# --------------------------------------------------------------------------

BASE_URL: Final = "https://www.pigai.org"
"""必须走 HTTPS：明文密码虽经 RSA 加密，但会话与表单仍应加密传输。"""

USER_AGENT: Final = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# --------------------------------------------------------------------------
# 登录（全部已核验，见 pigai.md §4）
# --------------------------------------------------------------------------

LOGIN_PATH: Final = "/index.php?a=login"
"""登录表单 action。**只接受 POST**；GET 会返回「请在正确的入口登录」。"""

LOGIN_FIELD_USER: Final = "username"
LOGIN_FIELD_PASSWORD: Final = "password"
"""明文密码字段：原站 JS 会先清空它，实际凭据走 LOGIN_FIELD_PASSWORD_ENCRYPT。"""

LOGIN_FIELD_PASSWORD_ENCRYPT: Final = "password_encrypt"
LOGIN_FIELD_REMEMBER: Final = "remember"
LOGIN_FIELD_CHECKHASH: Final = "checkhash"
"""隐藏字段，前端 JS **从不赋值** → 无客户端签名。固定提交空串。"""

LOGIN_FORM_MARKER: Final = "lg_from_w"
"""登录表单的 id（裸串，仅供文档与注释引用）。"""

LOGIN_FORM_MARKER_STRICT: Final = 'id="lg_from_w"'
"""**会话判据请用这个**：带属性名的形态。

裸子串会误判 —— 任何页面只要脚本文案里提到 `lg_from_w`（例如本站自己的 JS）
就会被判成「未登录」。实测合法页里它只出现在表单属性上，所以带上 `id=` 更稳。
`client.is_logged_in()` 与 `adapter` 的会话守卫都应使用本常量。
"""

SESSION_EXPIRED_MARKERS: Final = ('id="lg_from_w"', "a=loginForm")
"""**会话失效页的判据**：任一命中即视为会话失效。

实测（2026-10-19，样本在 ``data/recon/pigai/raw/``）：

| 标记 | 失效页命中 | 合法页命中 |
| --- | --- | --- |
| ``id="lg_from_w"`` | 2 / 2 | 0 / 7 |
| ``a=loginForm`` | 2 / 2 | 0 / 7 |

会话失效时站点返回 **200 的小页面**（约 1.7 KB），正文是
「使用此功能，请先登录　3 秒后系统将自动跳转」，并用 JS 跳到
``/index.php?a=loginForm&reurl=<原URL>``。**它不含 ``id="lg_from_w"``** ——
所以只查登录表单标识会**漏判**，把「会话失效」误报成「页面结构变了」
（独立验证者的「需知会-5」）。``a=loginForm`` 是站点路由参数，比那句中文文案更耐改。

匹配是**精确子串**（保守方向）：写法变体（``id = "lg_from_w"``、单引号、大写 ``ID``）
可能漏判。漏判的后果只是报出后续的解析错误，**不会**误判成「已登录」。
"""

LOGIN_SUCCESS_COOKIE: Final = "_JUKU_USER"
"""登录后出现的主登录态 cookie（385 字符，domain=.pigai.org）。"""

COOKIE_PHPSESSID: Final = "PHPSESSID"
COOKIE_LANG: Final = "old"
"""匿名访问首页即下发的语言/旧版偏好 cookie，登录前需先取。"""

RSA_PUBKEY_B64: Final = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCYlII16dSwQErDgjIl8BzU4NEL2IzvyWiL"
    "Nxie3mkpw6eseF/iUVb3bisAFH+lzgnrv/mBOKUMkbqtW2+8en/6r0hj6ctvGT+UOtg4P5LF"
    "/jxkbE+cA2fVJK2RaBzeEEbrKOvauVnGkEOvPVl1/NK4NgeN6aSPIK9ECfXcjlEOHwIDAQAB"
)
"""前端 ``2020.js`` 第 141 行硬编码的 SPKI 公钥（RSA-1024 / e=65537）。"""

# --------------------------------------------------------------------------
# 只读端点
# --------------------------------------------------------------------------


class Endpoints:
    """只读端点。写入类端点刻意不声明。"""

    HOME: Final = "/"
    ESSAYS_DONE: Final = "/index.php?c=write"
    """已完成作文列表（含成绩、提交次数）。已核验。"""

    ASSIGNMENTS_PENDING: Final = "/index.php?c=write&a=waitComplete"
    """未完成作业列表（作业号/标题/类型/出题人/指定班级/截止时间）。已核验（行级 DOM 亦已实测）。"""

    MY_CLASS: Final = "/index.php?c=write&a=myclass"
    """班级作业（ID/提交人数/截止日期/状态）。已核验。"""

    @staticmethod
    def essay_view(eid: str, rid: str) -> str:
        """作文评分详情（总分/四维/总评/按句点评）。已核验。"""
        return f"/index.php?c=v2&a=view&eid={eid}&rid={rid}"

    @staticmethod
    def essay_history(eid: str) -> str:
        """多稿历史版本。已核验。"""
        return f"/index.php?c=write&a=history&eid={eid}"

    @staticmethod
    def request_detail(rid: str) -> str:
        """题目要求详情。页面中引用，未单独取。"""
        return f"/index.php?c=v2&a=viewrequest&rid={rid}"

    @staticmethod
    def lookup_by_rid(rid: str) -> str:
        """**按作业号定位作业** —— 站点自己的「查找作文」表单 action（METHOD=GET）。

        实测要点（2026-10-19）：

        - 它返回的页面与写作页**功能等价**：同一 ``_eid``、``#contents`` 里能读到
          已有稿件正文（1070 字符，与写作页逐字一致）。两者**不是逐字节相同**
          （同一 URL 两次抓取的长度也会浮动），所以不要做字节比对。
        - ``rid`` 指向**未写过**的作业时，``_eid`` 为空、``#contents`` 为空。
        - **GET 无副作用**：进入前后各取一次列表快照，稿次总和 19→19，
          逐条 `rid`/稿次一致 —— 不建草稿、不加稿次。
        - 刻意使用站点自己的**读语义**动作名（``searchReq``）而非写作页动作名，
          这样本项目「源码里不出现写入端点字面量」的静态审计仍然成立。
        """
        return f"/index.php?c=write&a=searchReq&rid={rid}"


# --------------------------------------------------------------------------
# DOM 选择器 / 结构常量（已核验，除注明「推断」者）
# --------------------------------------------------------------------------

LIST_CONTAINER_ID: Final = "essayList"
LIST_HEADER_CLASS: Final = "reItemM"
LIST_ROW_CLASS: Final = "reItemS"
"""两条列表（已完成 / 未完成）共用 ``#essayList > ul.reItemM(表头) + ul.reItemS(行)``。

⚠ 表头 ``ul`` 内嵌了 ``<div style="clear:both">``（非法 HTML），
  严格解析器（bs4/lxml）可能把它移出 ``ul`` → 解析请用标准库 ``html.parser``。
"""

DONE_HEADERS: Final = (
    "作文ID",
    "作文标题",
    "体裁",
    "提交时间",
    "截止日期",
    "成绩",
    "提交次数",
    "操作",
)
PENDING_HEADERS: Final = (
    "作业号",
    "作业标题",
    "类型",
    "出题人",
    "指定班级",
    "截止时间",
    "操作",
)
"""表头文案，用于**校验**而非定位（列序若变应报错而不是错位取值）。"""

DETAIL_SCORE_ID: Final = "rescoreTxt"
"""总分：``<b id="rescoreTxt" title="满分:100" content="100">85.5</b>``。
``content`` 属性是满分，文本是得分。"""

DETAIL_COMMENT_ID: Final = "allpingyu_txt"
"""总评。"""

DETAIL_REQUIREMENTS_ID: Final = "yaoqiuW"
DETAIL_META_CLASS: Final = "stu_info_item"
"""作业元信息 ``<ul>``：开始时间 / 截止时间 / 满分 / 字数 / 批改方式 / 批改公式。"""

DETAIL_TOPIC_TAG: Final = "h3"
DETAIL_DIM_LABEL_CLASS: Final = "tbL"
"""四维标签。其后的进度条给出该维度得分，优先级见 ``DETAIL_DIM_SCORE_ATTR``。"""

DETAIL_DIM_SCORE_ATTR: Final = "title"
"""**权威分数所在**：进度条 ``td`` 的 ``title`` 属性（实测 8 位有效数字）。
宽度比值只是它的渲染近似，实测系统性偏大 0.003~0.006 —— 只在 title 缺失时兜底。

⚠ 不要为了「看起来更精确」而改用宽度比值；那是更差的近似。"""

#: ── 选择器归属规则 ────────────────────────────────────────────────
#: 本模块只放**跨模块契约**：端点、cookie/表单字段名、顶层 landmark、数据结构。
#: 仅被单个模块使用的细粒度选择器（例如 ``parse.py`` 内的 ``.yq_itme``、
#: ``li[sid]`` 的属性名、班级页的 ``.baseinfo`` / ``ul.detail`` / ``.m-score``）
#: 允许留在该模块内，但：
#:   1. **不得重复定义**本模块已有的常量；
#:   2. 必须在那个模块里以模块私有常量集中声明，不得散落在函数体内。
#: 目的只是防重复与防漂移，不是把 protocol.py 变成选择器垃圾场。

DETAIL_DIM_NAMES: Final = ("词汇", "句子", "篇章结构", "内容相关")
"""与官方 Open API ``score_cat`` 同名同序同量纲，构成交叉印证。"""

DETAIL_FB_JS_VAR: Final = "fb_js"
"""内嵌批注字典：``{关键词: {kp, sid[], txt, category}}``。"""

DETAIL_BODY_ID: Final = "contents"
"""**正文容器的 id —— 详情页与写作页共用**。

⚠ **选择器只按 id 匹配，不要按标签名。** 同一个 id 在两个页面是**不同标签**（实测）：

| 页面 | 实测形态 |
| --- | --- |
| 详情页 ``?c=v2&a=view`` | ``<div id="contents"><div id="parahtml" style="white-space:pre-wrap">…`` |
| 写作页 / lookup 页 | ``<TEXTAREA NAME="contents" id="contents" class="from_contents">…`` |

另外两个坑：

1. 页面上还有 **3 个** ``<textarea class="section-content draft-top|main|tail">``
   —— 那是「作文开头 / 主体 / 结尾**常见表达模板**」，**绝不能被当成正文**；
2. 正文除 ``strip()`` 外**不做任何归一化**（用户要求能原样看到，且将来要能回写）。

实测长度：详情页 strip 后 **1070** 字符、写作页 **1070** 字符，两者**逐字相同**。
"""

DETAIL_BODY_TAG: Final = "div"
"""详情页正文容器的标签（``div``）。**不要拿它当选择器** —— 见 ``DETAIL_BODY_ID``。"""

WRITE_BODY_TAG: Final = "TEXTAREA"
"""写作页 / lookup 页正文容器的标签（**大写** ``TEXTAREA``）。

勘误（2026-10-19，由 parser-dev 发现）：本常量原名为 ``DETAIL_BODY_TAG`` 且值即为此，
被误当成两个页面通用 —— **在详情页上是错的**（详情页是 ``div``）。
若确需按标签匹配，必须**大小写不敏感**：写成小写 ``textarea`` 会漏掉这个大写标签
（曾因此把正文长度时而读成 1070、时而读成 0）。
"""

ANNOTATION_LEVELS: Final = ("warn", "error", "error_trp", "great")
"""官方 API 定义的四类标注等级。

2026-10-19 线上端到端复核确认四类**全部出现**（见 pigai.md §6.10）——
早先只有 `error` / `error_trp` 的观察来自另一篇作文的离线样本。
"""

UNPUBLISHED_SCORE_MARKER: Final = "未截止"
"""⚠ 业务要点：**截止日期前列表不公布成绩**，此列可能是文字而非分数。
读分数必须走详情页 ``#rescoreTxt``。"""

FRONTEND_SWITCH_VARS: Final = ("_no_paste", "_snt_is_change", "_school_opt")
"""前端开关（如 ``_no_paste="1"`` 禁止粘贴）。

**红线：只读取、不利用。** 适配器不得据此绕过任何教师设置的约束。
"""

# --------------------------------------------------------------------------
# 数据结构契约（parse.py 产出，adapter.py 消费）
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AssignmentRow:
    """未完成作业列表的一行。**行级 DOM 已实测**（见 pigai.md §6.1）。

    勘误：本类初版注释曾写「行级 DOM 未实测（推断）」（对应已撤回的 U15）——
    那是 lead 的抽取错误，该页一直有真实数据行。"""

    rid: str
    title: str
    kind: str = ""
    author: str = ""
    classes: str = ""
    deadline: str = ""
    url: str = ""
    #: 原始单元格文本，保底用；解析器错位时可据此校正
    raw_cells: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EssayRow:
    """已完成作文列表的一行（8 列，已逐字核验）。"""

    rid: str
    eid: str
    title: str
    genre: str = ""
    submitted_at: str = ""
    deadline: str = ""
    #: 可能是 ``"未截止"`` 等文字，不保证可转 float
    score_raw: str = ""
    submit_count: int = 0
    url: str = ""

    @property
    def score(self) -> float | None:
        try:
            return float(self.score_raw)
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True, slots=True)
class Dimension:
    """一个评分维度。

    ``ratio`` 的取值优先级（2026-10-19 依实测修正）：

    1. **首选** 进度条所在 ``td`` 的 ``title`` 属性 —— 实测为 8 位有效数字的权威分数
       （如 ``0.8951008``）；
    2. **兜底** 进度条宽 / 容器宽 —— 实测比 ``title`` 系统性偏大 0.003~0.006
       （如 ``138.26512 / 154 = 0.897825`` 而 ``title = 0.8951008``），仅在前者缺失时使用。

    ``source`` 记录本次取值来自哪一路（``"title"`` / ``"width"``），便于事后核对。

    **``source`` 与宽度字段的不变式**（消费方请按此判断，别只看宽度是不是 0）：

    - ``source == "width"`` → 两个宽度都是**有限非负数**，且
      ``ratio == bar_width / container_width``。
      ⚠ **``bar_width`` 可以是 ``0.0``** —— 某一维度真得 0 分是**合法页面**
      （实测：容器 200px、进度条 0px、无 title → ``ratio = 0.0``）。
      所以**不要断言** ``bar_width > 0``；要判断「这一维有没有数据」请看 ``source``。
    - ``source == "title"`` → ``ratio`` 来自权威的 ``title``，**与宽度无关**；
      两个宽度**仅供诊断**，可能是 ``0.0``（页面没给宽度属性），也可能是
      ``inf`` 等异常读数（**原样保留，不一定被清洗**）。**别拿它们做判断。**

    另：宽度异常**不会**让一个有效的 ``title`` 失效 —— 两者来源独立（见 pigai.md §6.4）。

    > 勘误：本节初版写「``source == "width"`` ⇒ 宽度一定是**有限正数**」，
    > 那是**过度声称** —— 独立验证者用「某一维 0 分」的最小复现推翻。已改为「有限非负」。
    """

    name: str
    ratio: float
    bar_width: float
    container_width: float
    source: str = "title"


@dataclass(frozen=True, slots=True)
class Annotation:
    """一条按句点评。``level`` / ``cate`` 一律**原样保留**未知取值。"""

    sid: int
    sentence: str
    level: str = ""
    cate: str = ""
    keyword: str = ""
    message: str = ""


@dataclass(frozen=True, slots=True)
class EssayDetail:
    """作文评分详情页。"""

    rid: str = ""
    eid: str = ""
    score: float | None = None
    full_marks: float | None = None
    dimensions: tuple[Dimension, ...] = ()
    overall_comment: str = ""
    annotations: tuple[Annotation, ...] = ()
    #: 开始时间 / 截止时间 / 满分 / 字数 / 批改方式 / 批改公式
    meta: dict[str, str] = field(default_factory=dict)
    topic: str = ""
    requirements: str = ""
    essay_text: str = ""
    """**作文正文**（``#contents``）。

    详情页与写作页都有这个容器；本字段取自**详情页**。除 ``strip()`` 外不做归一化
    —— 用户要求能「看到之前作业的内容」，所以必须原样保留。
    取不到时为空串（**不代表没有正文**，先看 §6.3 的容器说明）。

    **边界（有意取舍）**：若页面把非正文文案（工具条、提示）混进了 ``#contents``，
    它会**原样**出现在这里 —— 这是「不归一化」的代价。消费方要纯正文需自行判断；
    实测 3 份样本未出现这种情况（独立验证者已如实标注为未验证项）。
    """


@dataclass(frozen=True, slots=True)
class WritePage:
    """**写作页 / 按作业号定位**返回的页面（``Endpoints.lookup_by_rid``）。

    用途：按作业号定位一个作业、并读回已有稿子的正文。

    ⚠ 该页面同时是「写作」入口 —— 但本适配器**只 GET，不 POST**。
    GET 已被实测证明无副作用（不建草稿、不加稿次），见 ``lookup_by_rid`` 的 docstring。
    """

    rid: str = ""
    eid: str = ""
    """非空表示这个作业已经写过（有稿）。未写过时是空串（实测 ``var _eid = ''``）。"""

    title: str = ""
    requirement: str = ""
    """题目要求正文（页面「要求」段）。"""

    word_range: str = ""
    """字数区间**原文**，如 ``"120~150"``。"""

    full_marks: float | None = None
    deadline: str = ""
    """截止时间**原文**（如 ``"2026-11-16 23:59"``），此处不做时区解释。"""

    server_time: str = ""
    """页面给出的「当前时间」= 服务端时钟；可用于判断是否已截止。"""

    essay_text: str = ""
    """``#contents`` 正文；未写过则为空串。除 ``strip()`` 外不做归一化。"""

    no_paste: bool = False
    """前端开关 ``_no_paste``。

    **红线：只读取、不利用。** 适配器不得据此绕过教师设置的任何约束。
    """

    @property
    def has_draft(self) -> bool:
        """是否已有内容（有 ``eid`` 或正文非空）。"""
        return bool(self.eid) or bool(self.essay_text.strip())


@dataclass(frozen=True, slots=True)
class ClassRow:
    """班级页的一个作业条目。"""

    rid: str
    submit_count: int | None = None
    deadline: str = ""
    status: str = ""


__all__ = [
    "ANNOTATION_LEVELS",
    "BASE_URL",
    "COOKIE_LANG",
    "COOKIE_PHPSESSID",
    "DETAIL_COMMENT_ID",
    "DETAIL_BODY_ID",
    "DETAIL_BODY_TAG",
    "WRITE_BODY_TAG",
    "DETAIL_DIM_LABEL_CLASS",
    "DETAIL_DIM_NAMES",
    "DETAIL_DIM_SCORE_ATTR",
    "DETAIL_FB_JS_VAR",
    "DETAIL_META_CLASS",
    "DETAIL_REQUIREMENTS_ID",
    "DETAIL_SCORE_ID",
    "DETAIL_TOPIC_TAG",
    "DONE_HEADERS",
    "Endpoints",
    "FRONTEND_SWITCH_VARS",
    "LIST_CONTAINER_ID",
    "LIST_HEADER_CLASS",
    "LIST_ROW_CLASS",
    "LOGIN_FIELD_CHECKHASH",
    "LOGIN_FIELD_PASSWORD",
    "LOGIN_FIELD_PASSWORD_ENCRYPT",
    "LOGIN_FIELD_REMEMBER",
    "LOGIN_FIELD_USER",
    "LOGIN_FORM_MARKER",
    "LOGIN_FORM_MARKER_STRICT",
    "LOGIN_PATH",
    "LOGIN_SUCCESS_COOKIE",
    "PENDING_HEADERS",
    "RSA_PUBKEY_B64",
    "SESSION_EXPIRED_MARKERS",
    "UNPUBLISHED_SCORE_MARKER",
    "USER_AGENT",
    "Annotation",
    "AssignmentRow",
    "ClassRow",
    "Dimension",
    "EssayDetail",
    "EssayRow",
    "WritePage",
]
