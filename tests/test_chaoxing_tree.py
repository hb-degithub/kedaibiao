"""chaoxing/tree.py 的测试：真实抓包 fixture + 合成 DOM 边界。

真实 fixture 来自 ``data/chaoxing-probe/``，复制时做了两件事：

1. 只保留目录树 / tab 条所在的 DOM 区域（去掉脚本与 CSS），体积从 100KB+
   降到可入库的几十 KB；
2. 学生姓名脱敏（真实抓包里学习页片段含「胡澜译」，替换为「张三」）。

缩过的 HTML 与原始抓包的解析结果完全一致（见 ``test_trimmed_fixture_matches_original``，
该用例只在原始抓包存在时运行，CI 里没有那份大文件也会跳过）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kedaibiao.platforms.chaoxing.tree import CourseTree, parse_chapter_tree, parse_tabs

FIXTURES = Path(__file__).parent / "fixtures" / "chaoxing"
PHYSICS = FIXTURES / "tree_205643380.html"
PROBABILITY = FIXTURES / "tree_219456814.html"
PROBABILITY_TABS = FIXTURES / "tabs_219456814_461423614.html"

#: 原始抓包目录（仓库里有，但测试不依赖它存在）
PROBE = Path(__file__).parent.parent / "data" / "chaoxing-probe"

PHYSICS_CHAPTERS = [
    "第一章 质点运动学",
    "第二章 质点动力学",
    "第三章 刚体力学",
    "热学基础",
    "静电场",
    "稳恒磁场",
    "电磁感应和电磁波",
    "振动和波动",
    "波动光学",
    "量子物理基础",
    "大学物理-公式推导",
]

PROBABILITY_CHAPTERS = [
    "随机事件与概率",
    "随机变量及其分布",
    "随机变量的数字特征",
    "大数定律与中心极限定理",
    "数理统计的基本概念",
    "参数估计",
    "假设检验",
    "章复习",
    "64学时重修专用",
]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _count_states(tree: CourseTree) -> dict[str, int]:
    counts = {"done": 0, "todo": 0, "unknown": 0}
    for section in tree.sections:
        counts[section.state] += 1
    return counts


# --------------------------------------------------------------------------
# 1) 真实 fixture：大学物理（含嵌套小节 10.3 → 10.3.1）
# --------------------------------------------------------------------------
def test_physics_tree_sections_and_chapters() -> None:
    tree = parse_chapter_tree(_read(PHYSICS))

    assert len(tree.sections) == 47
    assert tree.chapter_names() == PHYSICS_CHAPTERS
    assert _count_states(tree) == {"done": 21, "todo": 26, "unknown": 0}


def test_physics_section_sample() -> None:
    tree = parse_chapter_tree(_read(PHYSICS))

    section = tree.by_id("177855067")
    assert section is not None
    assert section.title == "1.1 一维，多维运动的描述"
    assert section.index_text == "1.1"
    assert section.state == "done"
    assert section.chapter_title == "第一章 质点运动学"
    assert section.tabs == []

    pending = tree.by_id("226749389")
    assert pending is not None
    assert pending.title == "2.4 保守力 势能 机械能守恒定律"
    assert pending.state == "todo"


def test_physics_nested_section_keeps_document_order() -> None:
    tree = parse_chapter_tree(_read(PHYSICS))

    outer = tree.by_id("723881997")
    inner = tree.by_id("723882529")
    assert outer is not None and inner is not None
    assert outer.index_text == "10.3"
    assert inner.index_text == "10.3.1"
    assert inner.title == "新建目录"
    assert inner.chapter_title == "量子物理基础"

    ids = [section.chapter_id for section in tree.sections]
    assert ids.index("723881997") < ids.index("723882529")


def test_physics_has_no_tabs_in_tree_page() -> None:
    """目录页本身不带 tab 条 —— tab 是学习页里按小节单独渲染的。"""
    tree = parse_chapter_tree(_read(PHYSICS))

    assert all(section.tabs == [] for section in tree.sections)
    assert list(tree.iter_task_points()) == []


# --------------------------------------------------------------------------
# 2) 真实 fixture：概率论（章节测验 tab 在学习页片段里，不在目录页）
# --------------------------------------------------------------------------
def test_probability_tree_sections_and_chapters() -> None:
    tree = parse_chapter_tree(_read(PROBABILITY))

    assert len(tree.sections) == 33
    assert tree.chapter_names() == PROBABILITY_CHAPTERS
    assert _count_states(tree) == {"done": 1, "todo": 32, "unknown": 0}
    assert all(section.tabs == [] for section in tree.sections)


def test_probability_section_sample() -> None:
    tree = parse_chapter_tree(_read(PROBABILITY))

    done = tree.by_id("461423614")
    assert done is not None
    assert done.title == "基本概念"
    assert done.index_text == "1.1"
    assert done.state == "done"
    assert done.chapter_title == "随机事件与概率"

    todo = tree.by_id("461423615")
    assert todo is not None
    assert todo.title == "等可能概型"
    assert todo.state == "todo"


def test_probability_tabs_fixture_is_real_section_tab_bar() -> None:
    tabs = parse_tabs(_read(PROBABILITY_TABS))

    assert [(tab.kind, tab.index, tab.card_id, tab.title) for tab in tabs] == [
        ("courseware", 1, "415843880", "课件"),
        ("quiz", 2, "415843881", "测验"),
        ("extra", 3, "415843882", "扩展资源"),
    ]


def test_tabs_attach_to_matching_section_by_chapter_id() -> None:
    """把真实目录页和真实 tab 条拼在一起：tab 应挂到 461423614 这个小节。"""
    combined = _read(PROBABILITY).replace("</body>", _read(PROBABILITY_TABS) + "\n</body>")
    tree = parse_chapter_tree(combined)

    assert len(tree.sections) == 33
    section = tree.by_id("461423614")
    assert section is not None
    assert [tab.kind for tab in section.tabs] == ["courseware", "quiz", "extra"]
    assert [tab.index for tab in section.tabs] == [1, 2, 3]

    assert [s.chapter_id for s in tree.iter_task_points()] == ["461423614"]
    assert all(
        section.tabs == [] for section in tree.sections if section.chapter_id != "461423614"
    )


# --------------------------------------------------------------------------
# 3) 合成 DOM：嵌套章 / 无 tab 小节 / 未知状态 / 引号变体
# --------------------------------------------------------------------------
def _page(body: str) -> str:
    return f"<!DOCTYPE html><html><body>{body}</body></html>"


def _chapter_html(chapter_title: str, sections_html: str) -> str:
    return f"""
    <div class="chapter_unit">
      <div class="chapter_item">
        <div class="catalog_title chapter_Thats_bnt">
          <div class="catalog_name newCatalog_name">
            <a class="clicktitle"><span title="{chapter_title}">{chapter_title}</span></a>
          </div>
        </div>
      </div>
      <div class="catalog_level"><ul>{sections_html}</ul></div>
    </div>"""


def _section_html(
    chapter_id: str,
    index_text: str,
    title: str,
    *,
    task_html: str = "",
    nested_html: str = "",
    onclick: str | None = None,
    section_id: str | None = None,
) -> str:
    element_id = f' id="cur{chapter_id}"' if section_id is None else f' id="{section_id}"'
    onclick_attr = f' onclick="{onclick}"' if onclick else ""
    return f"""
    <li>
      <div class="chapter_item"{element_id}{onclick_attr} title="{title}">
        <div class="catalog_title">
          <div class="catalog_name newCatalog_name">
            <a class="clicktitle"><span class="catalog_sbar">{index_text}</span> {title}</a>
          </div>
          {task_html}
        </div>
      </div>
      {nested_html}
    </li>"""


def test_synthetic_nested_chapters_and_sections() -> None:
    inner = _section_html("102", "1.1.1", "细分小节")
    outer = _section_html("101", "1.1", "位移", nested_html=f"<ul>{inner}</ul>")
    html = _page(
        _chapter_html("第一章  运动学", outer)
        + _chapter_html("第二章  动力学", _section_html("201", "2.1", "牛顿定律"))
    )

    tree = parse_chapter_tree(html)

    assert [section.chapter_id for section in tree.sections] == ["101", "102", "201"]
    assert tree.chapter_names() == ["第一章 运动学", "第二章 动力学"]
    assert tree.by_id("102") is not None
    assert tree.by_id("102").chapter_title == "第一章 运动学"
    assert tree.by_id("102").index_text == "1.1.1"
    assert tree.by_id("201").chapter_title == "第二章 动力学"


def test_synthetic_unknown_state_and_no_tabs() -> None:
    html = _page(_chapter_html("没有状态标记的章", _section_html("301", "3.1", "无任务小节")))

    tree = parse_chapter_tree(html)
    section = tree.by_id("301")

    assert section is not None
    assert section.state == "unknown"
    assert section.tabs == []
    assert list(tree.iter_task_points()) == []


def test_synthetic_pending_count_zero_is_still_todo() -> None:
    """真实数据里概率论整门课都是 0 个待完成任务点，仍显示为待完成样式。"""
    task = (
        '<div class="catalog_task"><div class="catalog_jindu">'
        '<span class="catalog_points_yi">0</span></div></div>'
    )
    html = _page(_chapter_html("章", _section_html("302", "3.2", "零待完成", task_html=task)))

    section = parse_chapter_tree(html).by_id("302")

    assert section is not None
    assert section.state == "todo"


def test_synthetic_single_and_double_quote_onclick() -> None:
    single_quoted_in_attr = _section_html(
        "401", "4.1", "单引号 onclick", onclick="toOld('1', '401', '3',0)"
    )
    # 属性用单引号包住、toOld 参数用双引号，且故意不给 id，逼解析器走 onclick 兜底
    double_quoted_in_attr = _section_html(
        "402",
        "4.2",
        "双引号 onclick",
        onclick='toOld(&quot;1&quot;, &quot;402&quot;, &quot;3&quot;, 0)',
        section_id="",
    )
    html = _page(_chapter_html("章", single_quoted_in_attr + double_quoted_in_attr))

    tree = parse_chapter_tree(html)

    assert [section.chapter_id for section in tree.sections] == ["401", "402"]
    assert tree.by_id("402") is not None
    assert tree.by_id("402").title == "双引号 onclick"


def test_synthetic_tab_title_fallback_and_quote_styles() -> None:
    html = _page(
        """
        <div class="prev_list" id="prev_tab"><ul>
          <li title="课件" cardid="11"
              onclick="changeDisplayContent(1,3,'501','1','2','');"></li>
          <li title="测验" cardid="12"
              onclick='changeDisplayContent(2,3,&quot;501&quot;,&quot;1&quot;,&quot;2&quot;,"");'></li>
          <li cardid="13" onclick="changeDisplayContent(3,3,'501','1','2','')">
            <div><span class="num">3</span><span class="spanText">扩展资源</span></div>
          </li>
        </ul></div>
        """
    )

    tabs = parse_tabs(html)

    assert [(tab.kind, tab.index, tab.card_id, tab.title) for tab in tabs] == [
        ("courseware", 1, "11", "课件"),
        ("quiz", 2, "12", "测验"),
        ("extra", 3, "13", "扩展资源"),
    ]


def test_synthetic_tab_without_onclick_numbers_by_order() -> None:
    html = _page(
        """
        <ul>
          <li cardid="21" title="讨论"></li>
          <li cardid="22" title="课件"></li>
        </ul>
        """
    )

    tabs = parse_tabs(html)

    assert [(tab.kind, tab.index, tab.card_id, tab.title) for tab in tabs] == [
        ("unknown", 1, "21", "讨论"),
        ("courseware", 2, "22", "课件"),
    ]


def test_synthetic_tabs_inside_section_are_attached() -> None:
    tab_html = (
        '<ul class="prev_ul">'
        '<li title="课件" cardid="31" '
        "onclick=\"changeDisplayContent(1,1,'601','1','2','');\"></li>"
        "</ul>"
    )
    html = _page(
        _chapter_html(
            "章",
            _section_html("601", "6.1", "内嵌 tab", nested_html=tab_html),
        )
    )

    tree = parse_chapter_tree(html)
    section = tree.by_id("601")

    assert section is not None
    assert [tab.kind for tab in section.tabs] == ["courseware"]
    assert [s.chapter_id for s in tree.iter_task_points()] == ["601"]


def test_chapter_names_deduplicates_same_title() -> None:
    html = _page(
        _chapter_html("同一章", _section_html("701", "7.1", "甲"))
        + _chapter_html("同一章", _section_html("702", "7.2", "乙"))
    )

    tree = parse_chapter_tree(html)

    assert tree.chapter_names() == ["同一章"]
    assert len(tree.sections) == 2


# --------------------------------------------------------------------------
# 4) 边界：空 HTML / 缺属性 / 畸形未闭合
# --------------------------------------------------------------------------
def test_empty_html_returns_empty_tree() -> None:
    tree = parse_chapter_tree("")

    assert isinstance(tree, CourseTree)
    assert tree.sections == []
    assert tree.chapter_names() == []
    assert list(tree.iter_task_points()) == []
    assert tree.by_id("anything") is None
    assert len(tree) == 0
    assert list(tree) == []


def test_html_without_chapter_dom_does_not_raise() -> None:
    tree = parse_chapter_tree("<html><body><p>没有目录</p></body></html>")

    assert tree.sections == []
    assert tree.chapter_names() == []


def test_section_with_missing_attributes_does_not_raise() -> None:
    html = _page(
        _chapter_html("章", '<li><div class="chapter_item" id="cur7"></div></li>')
    )

    tree = parse_chapter_tree(html)
    section = tree.sections[0]

    assert section.chapter_id == "7"
    assert section.title == ""
    assert section.index_text == ""
    assert section.state == "unknown"
    assert section.chapter_title == "章"


def test_malformed_unclosed_html_keeps_parsed_section() -> None:
    html = (
        '<div class="chapter_unit">'
        '<div class="chapter_item"><div class="catalog_name">'
        '<span title="第一章 未闭合">第一章 未闭合</span></div></div>'
        '<div class="chapter_item" id="cur9" title="9.1 没有收尾">'
        '<span class="catalog_sbar">9.1</span>'
    )

    tree = parse_chapter_tree(html)

    assert len(tree.sections) == 1
    section = tree.by_id("9")
    assert section is not None
    assert section.title == "9.1 没有收尾"
    assert section.index_text == "9.1"
    assert section.chapter_title == "第一章 未闭合"


def test_tab_only_html_is_not_a_course_tree() -> None:
    tree = parse_chapter_tree(_read(PROBABILITY_TABS))

    assert tree.sections == []
    assert tree.chapter_names() == []
    assert list(tree.iter_task_points()) == []


# --------------------------------------------------------------------------
# 5) fixture 自身约束：脱敏、去脚本、可解析
# --------------------------------------------------------------------------
@pytest.mark.parametrize("path", [PHYSICS, PROBABILITY, PROBABILITY_TABS])
def test_fixture_is_sanitized_and_script_free(path: Path) -> None:
    text = _read(path)

    assert "胡澜译" not in text
    assert "<script" not in text.lower()
    assert len(text) < 200_000


@pytest.mark.skipif(not PROBE.exists(), reason="仓库里没有原始抓包目录")
def test_trimmed_fixture_matches_original() -> None:
    """缩过的 fixture 与原始抓包解析结果一致 —— 证明裁剪没丢小节/状态/tab。"""
    physics_original = parse_chapter_tree(_read(PROBE / "tree_205643380.html"))
    physics_trimmed = parse_chapter_tree(_read(PHYSICS))
    assert physics_trimmed.sections == physics_original.sections
    assert physics_trimmed.chapters == physics_original.chapters

    probability_original = parse_chapter_tree(_read(PROBE / "tree_219456814.html"))
    probability_trimmed = parse_chapter_tree(_read(PROBABILITY))
    assert probability_trimmed.sections == probability_original.sections
    assert probability_trimmed.chapters == probability_original.chapters
