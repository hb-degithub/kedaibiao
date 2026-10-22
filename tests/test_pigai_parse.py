"""批改网解析层单测：**完全离线**，只读 ``tests/fixtures/pigai/`` 下的脱敏样本。

设计原则（对应 task-3 / task-6 验收 + ``docs/integration-guide.md`` §6）：

* 不访问 ``www.pigai.org``，不读 ``data/recon/``（那里的样本含真实作文正文）；
* 表头不匹配必须抛错，绝不能按错位的列序取值；
* 四维分数两条取值路径（``title`` / 宽度比）都要覆盖；
* 未知 ``Annotation.level`` / ``.cate`` 原样保留；
* fixtures 里不得出现手机号 / 邮箱 / 长英文正文（本文件末尾有断言把这条钉在 CI 上）。

task-6 增量覆盖：

* ``TestEssayBody`` —— ``#contents`` 正文（大小写不敏感、不误取 3 个模板 / ``#beizhu``、
  只 strip 不归一化、``#contentsW`` 前缀不误配）；
* ``TestWritePage`` —— ``parse_write_page`` 全字段、fresh/with-draft 两种样本、
  页内变量优先于参数、非写作页/详情页必须报错；
* ``TestColumnIsolation`` —— 标题与 URL 不得跨列回落（验证者最小复现）；
* ``TestDimensionGuards`` —— 宽度 ``inf`` / ``0`` 的守卫、日志措辞、``ratio`` 恒有限。
"""

from __future__ import annotations

import math
import re
import unittest
from pathlib import Path

from kedaibiao.platforms.pigai import parse, protocol

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pigai"

LOGGER_NAME = "kedaibiao.platforms.pigai.parse"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _load_denylist() -> list[str]:
    """读本机 ``.secret-denylist``（已 gitignore）；没有就返回空表。

    真实姓名 / 学校 / 班级这类「正则认不出来」的值只登记在这里，
    仓库里不出现明文 —— 所以断言也读它，而不是把真实值抄进测试。
    """
    path = FIXTURES.parent.parent.parent / ".secret-denylist"
    if not path.is_file():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]



# --------------------------------------------------------------------------
# 构造样本（**非实测**，仅用于测边界：缺列、缺 title、表头错序、非详情页）
# --------------------------------------------------------------------------

_PENDING_HEADER = """
<div id="essayList" class="ydTable">
  <ul class="reItemM">
    <li style="width:80px;" class="ft">作业号</li>
    <li style="width:448px;">作业标题</li>
    <li style="width:50px;">类型</li>
    <li style="width:80px;">出题人</li>
    <li style="width:100px;">指定班级</li>
    <li style="width:80px;">截止时间</li>
    <li style="width:110px;">操作</li>
    <div style="clear:both"></div>
  </ul>
  <div class="line"></div>
"""

_DONE_HEADER = """
<div id="essayList" class="ydTable">
  <ul class="reItemM">
    <li style="width:80px;" class="ft">作文ID</li>
    <li style="width:448px;">作文标题</li>
    <li style="width:50px;">体裁</li>
    <li>提交时间</li>
    <li>截止日期</li>
    <li style="width:50px;">成绩</li>
    <li style="width:60px;">提交次数</li>
    <li style="width:110px;">操作</li>
    <div style="clear:both"></div>
  </ul>
  <div class="line"></div>
"""


def _dim_block(name: str, container_px: float, bar_px: float, title: str | None) -> str:
    """构造一个维度块；``title=None`` 表示进度条 ``td`` 上没有权威分数属性。"""
    title_attr = f' title="{title}"' if title is not None else ""
    return f"""
<div class="tbL"> {name}: </div>
<div style="float:left;width:{container_px}px;background:#EFEFEF;height:10px;">
  <table style="width:{bar_px}px;float:left;">
    <tr>
      <td style="width: 2px;"></td>
      <td style="width: {bar_px - 4}px;"{title_attr}></td>
      <td style="width: 2px;"></td>
    </tr>
  </table>
</div>
<div style="clear:both"></div>
"""


def _detail_doc(body: str) -> str:
    """包一层最小但形状正确的详情页外壳（有 #rescoreTxt，避免触发 PageShapeError）。"""
    return (
        '<html><body><b id="rescoreTxt" title="满分:100" content="100">80</b>'
        f"{body}</body></html>"
    )


class TestEssayRows(unittest.TestCase):
    """已完成列表（§6.2，8 列逐字核验）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = parse.parse_essay_rows(fixture("essays_done.html"))

    def test_row_count(self) -> None:
        self.assertEqual(len(self.rows), 3)

    def test_rid_and_eid_extracted(self) -> None:
        # eid 只在 href 里（§6.2 陷阱），rid 在第 0 列同时也在 href 里
        self.assertEqual(
            [(r.rid, r.eid) for r in self.rows],
            [
                ("1000002", "200000001"),
                ("1000003", "200000002"),
                ("1000004", "200000003"),
            ],
        )

    def test_other_columns(self) -> None:
        first = self.rows[0]
        self.assertEqual(first.title, "第1000002号 作文")
        self.assertEqual(first.genre, "作文")
        self.assertEqual(first.submitted_at, "2026-10-16")
        self.assertEqual(first.deadline, "2026-10-19")
        self.assertEqual(first.submit_count, 2)
        self.assertEqual(
            first.url, "/index.php?c=v2&a=view&eid=200000001&rid=1000002"
        )

    def test_unpublished_score_is_none(self) -> None:
        # ⚠ 截止日期前列表不公布成绩 → 此列是文字"未截止"（§6.2 业务要点）
        pending = self.rows[0]
        self.assertEqual(pending.score_raw, "未截止")
        self.assertIsNone(pending.score)

    def test_published_scores_are_floats(self) -> None:
        self.assertEqual(self.rows[1].score, 89.0)
        self.assertEqual(self.rows[2].score, 85.5)
        self.assertEqual(self.rows[2].submit_count, 1)

    def test_commented_out_actions_are_ignored(self) -> None:
        # 操作列里被注释掉的"查看"锚点不能被当成内容（注释必须丢弃）
        for row in self.rows:
            self.assertNotIn("<!--", row.title)
            self.assertNotIn("view2", row.url)

    def test_wrong_page_headers_raise(self) -> None:
        with self.assertRaises(parse.HeaderMismatchError):
            parse.parse_essay_rows(fixture("assignments_pending_row.html"))

    def test_missing_container_raises(self) -> None:
        with self.assertRaises(parse.HeaderMismatchError):
            parse.parse_essay_rows("<html><body>请先登录</body></html>")


class TestAssignmentRows(unittest.TestCase):
    """未完成列表（§6.1；行级证据见 parse.parse_assignment_rows 的 docstring）。"""

    def test_real_row_fixture(self) -> None:
        rows = parse.parse_assignment_rows(fixture("assignments_pending_row.html"))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.rid, "1000001")
        self.assertEqual(row.title, "示例课程（三）- Unit 6")
        self.assertEqual(row.kind, "作文")
        self.assertEqual(row.author, "张老师")
        self.assertEqual(row.classes, "示例1班,示例2班,示例3班,")
        self.assertEqual(row.deadline, "2026-11-16 23:59")

    def test_url_is_raw_write_page_relative(self) -> None:
        # 实测：href="?c=v2&a=write&rid=1000001" —— 无前导 /、不含 eid、指向写作页
        row = parse.parse_assignment_rows(fixture("assignments_pending_row.html"))[0]
        self.assertEqual(row.url, "?c=v2&a=write&rid=1000001")
        self.assertNotIn("eid", row.url)

    def test_raw_cells_always_filled(self) -> None:
        # 推断实现期的保底：7 列原始文本必须完整保留，供样本到位后校正
        row = parse.parse_assignment_rows(fixture("assignments_pending_row.html"))[0]
        self.assertEqual(
            row.raw_cells,
            (
                "1000001",
                "示例课程（三）- Unit 6",
                "作文",
                "张老师",
                "示例1班,示例2班,示例3班,",
                "2026-11-16 23:59",
                "查看",
            ),
        )

    def test_empty_state_returns_empty_list(self) -> None:
        self.assertEqual(parse.parse_assignment_rows(fixture("assignments_pending_empty.html")), [])

    def test_header_mismatch_raises(self) -> None:
        with self.assertRaises(parse.HeaderMismatchError):
            parse.parse_assignment_rows(fixture("essays_done.html"))

    def test_header_mismatch_message_shows_actual_headers(self) -> None:
        swapped = _PENDING_HEADER.replace("作业号", "作业编号", 1)
        with self.assertRaises(parse.HeaderMismatchError) as ctx:
            parse.parse_assignment_rows(swapped)
        self.assertIn("作业编号", str(ctx.exception))

    def test_row_with_wrong_cell_count_is_skipped(self) -> None:
        # 列数对不上 → 跳过该行（绝不猜列位），其余行照常返回
        good = (
            '<ul class="reItemS"><li>1</li><li>t</li><li>作文</li>'
            "<li>师</li><li>班</li><li>2026-01-01</li><li>查看</li></ul>"
        )
        bad = '<ul class="reItemS"><li>2</li><li>t</li><li>作文</li></ul>'
        html = f"{_PENDING_HEADER}{bad}{good}</div>"
        with self.assertLogs(LOGGER_NAME, level="WARNING"):
            rows = parse.parse_assignment_rows(html)
        self.assertEqual([r.rid for r in rows], ["1"])


class TestEssayDetail(unittest.TestCase):
    """评分详情页（§6.3~§6.6）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = fixture("essay_detail.html")
        cls.detail = parse.parse_essay_detail(cls.html)

    def test_score_and_full_marks_come_from_different_places(self) -> None:
        # 文本 = 得分，content 属性 = 满分（§6.3）
        self.assertEqual(self.detail.score, 85.5)
        self.assertEqual(self.detail.full_marks, 100.0)

    def test_dimension_names_and_count(self) -> None:
        self.assertEqual(
            tuple(d.name for d in self.detail.dimensions), protocol.DETAIL_DIM_NAMES
        )

    def test_dimensions_prefer_title(self) -> None:
        # 契约（protocol.Dimension）：优先进度条 td 的 title，source 如实填 "title"
        expected = (0.8951008, 0.86631035, 0.78319855, 0.78202034)
        for dim, value in zip(self.detail.dimensions, expected, strict=True):
            self.assertEqual(dim.source, "title")
            self.assertEqual(dim.ratio, value)

    def test_dimension_widths_read_from_dom(self) -> None:
        # 容器宽 / 条宽都从 DOM 读，不硬编码 154
        for dim in self.detail.dimensions:
            self.assertEqual(dim.container_width, 154.0)
        self.assertAlmostEqual(self.detail.dimensions[0].bar_width, 138.26512)

    def test_width_ratio_is_only_an_approximation(self) -> None:
        # 记录实测差异：宽度比系统性偏大 0.003 左右（最终简报的「无法解释」栏）
        first = self.detail.dimensions[0]
        width_ratio = first.bar_width / first.container_width
        self.assertAlmostEqual(width_ratio, 0.897825, places=6)
        self.assertNotAlmostEqual(width_ratio, first.ratio, places=3)
        self.assertAlmostEqual(width_ratio - first.ratio, 0.002725, places=6)

    def test_overall_comment(self) -> None:
        self.assertTrue(self.detail.overall_comment)
        self.assertEqual(
            self.detail.overall_comment, "占位总评：句式较丰富，建议增加衔接成分（此为脱敏占位文本）。"
        )

    def test_meta(self) -> None:
        self.assertEqual(self.detail.meta["满分"], "100")
        self.assertEqual(self.detail.meta["字数"], "100~200")
        self.assertEqual(self.detail.meta["开始时间"], "2026-09-21 08:00:00")
        self.assertEqual(self.detail.meta["截止时间"], "2026-10-19 23:59:00")
        self.assertEqual(self.detail.meta["批改公式"], "")

    def test_topic_and_requirements(self) -> None:
        self.assertEqual(self.detail.topic, "示例课程（三）- Unit 5（占位）")
        self.assertIn("满分: 100", self.detail.requirements)
        self.assertIn("字数: 100~200", self.detail.requirements)
        self.assertIn("要求：示例题目要求文本（占位）。", self.detail.requirements)
        self.assertNotIn("更多", self.detail.requirements)  # #yq_more 要排除

    def test_rid_eid_fall_back_to_embedded_js(self) -> None:
        self.assertEqual(self.detail.rid, "1000002")
        self.assertEqual(self.detail.eid, "200000001")

    def test_rid_eid_explicit_arguments_win(self) -> None:
        detail = parse.parse_essay_detail(self.html, rid="r1", eid="e1")
        self.assertEqual((detail.rid, detail.eid), ("r1", "e1"))

    def test_page_shape_error_on_non_detail_page(self) -> None:
        with self.assertRaises(parse.PageShapeError):
            parse.parse_essay_detail("<html><body>请登录后继续</body></html>")

    def test_parse_errors_are_value_errors(self) -> None:
        # 便于调用方一次性 ValueError 捕获
        self.assertTrue(issubclass(parse.HeaderMismatchError, ValueError))
        self.assertTrue(issubclass(parse.PageShapeError, ValueError))


class TestAnnotations(unittest.TestCase):
    """按句点评（§6.5）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.annotations = parse.parse_essay_detail(fixture("essay_detail.html")).annotations

    def test_count_and_sids(self) -> None:
        # 注释里的 sid=99 与无 sid 的 li.tdianping 都不算
        self.assertEqual(len(self.annotations), 5)
        self.assertEqual([a.sid for a in self.annotations], [0, 0, 1, 2, 3])

    def test_level_and_cate(self) -> None:
        levels = [a.level for a in self.annotations]
        self.assertEqual(levels[:3], ["error_trp", "error_trp", "error"])
        self.assertEqual(self.annotations[1].cate, "i_suggest.word")
        self.assertEqual(self.annotations[2].cate, "i_suggest.phrase")

    def test_unknown_level_and_cate_are_kept_raw(self) -> None:
        unknown = self.annotations[3]
        self.assertEqual(unknown.level, "mystery_level")
        self.assertEqual(unknown.cate, "brand.new.cate")

    def test_known_rare_level_is_kept(self) -> None:
        # ANNOTATION_LEVELS 里样本未出现的值（great）也要原样保留
        self.assertEqual(self.annotations[4].level, "great")
        unknown = [a.level for a in self.annotations if a.level not in protocol.ANNOTATION_LEVELS]
        self.assertEqual(unknown, ["mystery_level"])  # 只有刻意构造的那一条是未知值

    def test_sentence_keyword_and_message(self) -> None:
        first = self.annotations[0]
        self.assertEqual(first.sentence, "Placeholder sentence zero.")
        self.assertEqual(first.keyword, "_placeholder")  # = fb_js 的 kp，不是字典 key
        # short_msg 是 HTML 转义的，解析后必须已反转义
        self.assertIn("<b>加粗</b>", first.message)
        self.assertNotIn("&lt;", first.message)
        self.assertTrue(all(a.sentence for a in self.annotations))


class TestDimensionsFallback(unittest.TestCase):
    """四维取值两条路径（**构造样本**，真实样本四维都带 title）。"""

    def test_title_missing_falls_back_to_width_ratio(self) -> None:
        # 容器刻意用 200px（不是样本里的 154）→ 同时证明容器宽是从 DOM 读的
        body = f"<div>{_dim_block('词汇', 200, 150, None)}</div>"
        with self.assertLogs(LOGGER_NAME, level="WARNING"):
            detail = parse.parse_essay_detail(_detail_doc(body))
        self.assertEqual(len(detail.dimensions), 1)
        dim = detail.dimensions[0]
        self.assertEqual(dim.source, "width")
        self.assertEqual(dim.container_width, 200.0)
        self.assertEqual(dim.bar_width, 150.0)
        self.assertAlmostEqual(dim.ratio, 0.75, places=9)

    def test_title_wins_over_width_ratio(self) -> None:
        # 同一 DOM 上 title 与宽度比故意不同 → 必须取 title，并如实填 source
        body = f"<div>{_dim_block('词汇', 200, 150, '0.6000000')}</div>"
        with self.assertLogs(LOGGER_NAME, level="WARNING"):
            dim = parse.parse_essay_detail(_detail_doc(body)).dimensions[0]
        self.assertEqual(dim.source, "title")
        self.assertEqual(dim.ratio, 0.6)
        self.assertEqual((dim.bar_width, dim.container_width), (150.0, 200.0))

    def test_unreadable_dimension_is_skipped_not_fatal(self) -> None:
        body = (
            f"<div>{_dim_block('词汇', 200, 150, None)}</div>"
            '<div class="tbL">句子: </div>'  # 没有紧随的宽度容器
            f"<div>{_dim_block('篇章结构', 100, 50, None)}</div>"
        )
        with self.assertLogs(LOGGER_NAME, level="WARNING"):
            detail = parse.parse_essay_detail(_detail_doc(body))
        self.assertEqual([d.name for d in detail.dimensions], ["词汇", "篇章结构"])


class TestFbJs(unittest.TestCase):
    """内嵌批注字典 fb_js（§6.6；parse-local API，非 protocol 契约）。"""

    def test_fixture_entries(self) -> None:
        fb = parse.parse_fb_js(fixture("essay_detail.html"))
        self.assertEqual(len(fb), 3)
        entry = fb[" placeholder_keyword"]  # 字典 key 原样保留（真实样本带前导空格）
        self.assertEqual(entry.keyword, " placeholder_keyword")
        self.assertEqual(entry.kp, "_placeholder")
        self.assertEqual(entry.sids, (0, 2))
        self.assertEqual(entry.category, "学习提示")
        self.assertIn("示例批注占位说明。", entry.message)
        self.assertEqual(fb["k2"].sids, (2, 3))
        self.assertEqual(fb["or@4"].category, "推荐表达")

    def test_last_assignment_wins(self) -> None:
        # 页面里 var fb_js 先被赋 ""，再被赋真正的对象 → 后者胜
        html = (
            "<script>var fb_js= \"\";</script>"
            '<script>var fb_js = {"k1":{"kp":"k1","sid":[1],"txt":"a","category":"c"}};</script>'
        )
        fb = parse.parse_fb_js(html)
        self.assertEqual(list(fb), ["k1"])
        self.assertEqual(fb["k1"].sids, (1,))

    def test_invalid_json_is_not_fatal(self) -> None:
        html = '<script>var fb_js = {"k1": {broken,}}</script>'
        with self.assertLogs(LOGGER_NAME, level="WARNING"):
            self.assertEqual(parse.parse_fb_js(html), {})

    def test_absent_fb_js_returns_empty(self) -> None:
        self.assertEqual(parse.parse_fb_js("<html></html>"), {})


class TestClassRows(unittest.TestCase):
    """班级页（§6.8）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = parse.parse_class_rows(fixture("myclass.html"))

    def test_row_count(self) -> None:
        self.assertEqual(len(self.rows), 3)

    def test_fields(self) -> None:
        self.assertEqual(
            [(r.rid, r.submit_count, r.deadline, r.status) for r in self.rows],
            [
                ("1000001", 0, "2026-11-16 23:59", "交作业"),
                ("1000002", 1367, "2026-10-19 23:59", "交作业"),
                ("1000003", 43, "2026-09-19 23:59", "已截止"),
            ],
        )

    def test_commented_out_detail_block_does_not_duplicate_rows(self) -> None:
        # 每个条目里都有一条被注释掉的 <ul class="detail">，必须被忽略
        self.assertNotIn(None, [r.submit_count for r in self.rows])

    def test_no_entries_returns_empty_list(self) -> None:
        with self.assertLogs(LOGGER_NAME, level="WARNING"):
            self.assertEqual(parse.parse_class_rows("<html><body>空</body></html>"), [])


# --------------------------------------------------------------------------
# task-6：正文提取（A）、按作业号定位（B/C）、两个解析缺陷（3/4/5）
# --------------------------------------------------------------------------

#: 详情页与「已写过」写作页共用的占位正文（真实页面上两者是同一篇稿子）。
_BODY_PLACEHOLDER = "Placeholder paragraph one.\n\nPlaceholder paragraph two."

_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def _strip_comments(text: str) -> str:
    """剥掉 HTML 注释：注释里会出现说明性标记文字，断言标记本身时要先去掉。"""
    return _COMMENT_RE.sub("", text)


def _detail_with_body(body_html: str) -> str:
    """包一层带 ``#contentsW`` 的详情页外壳，正文容器由调用方给。

    ``#contentsW``（注意是 W 结尾）刻意保留：它能验证「按 id 精确选取」不会把
    前缀相同的 ``#contentsW`` 误当成 ``#contents``。
    """
    return (
        "<html><body>"
        f'<div id="sponeLeftT"><div id="contentsW">{body_html}</div></div>'
        '<b id="rescoreTxt" title="满分:100" content="100">80</b>'
        "</body></html>"
    )


class TestEssayBody(unittest.TestCase):
    """A｜详情页正文提取：``#contents``、大小写不敏感、不误取模板。"""

    def test_body_from_detail_fixture(self) -> None:
        detail = parse.parse_essay_detail(fixture("essay_detail.html"))
        self.assertEqual(detail.essay_text, _BODY_PLACEHOLDER)

    def test_detail_container_is_a_div_while_write_page_uses_textarea(self) -> None:
        # 实测事实：同一个 id 在详情页是 <div>、在写作页是大写 <TEXTAREA>。
        # 若按 protocol.DETAIL_BODY_TAG 硬过滤标签，详情页会读到 0 字。
        # 断言针对**标记本身**（先剥掉 HTML 注释，避免注释里的说明文字干扰）。
        detail_html = _strip_comments(fixture("essay_detail.html"))
        write_html = _strip_comments(fixture("write_with_draft.html"))
        self.assertIn('<div  id="contents">', detail_html)
        self.assertIsNone(re.search(r"<textarea\b", detail_html, re.I))
        self.assertIn('<TEXTAREA NAME="contents" id="contents"', write_html)
        self.assertTrue(parse.parse_essay_detail(detail_html).essay_text)

    def test_uppercase_textarea_body_is_read(self) -> None:
        html = _detail_with_body(
            '<TEXTAREA NAME="contents" id="contents" class="from_contents">'
            "Uppercase body.</TEXTAREA>"
        )
        self.assertEqual(parse.parse_essay_detail(html).essay_text, "Uppercase body.")

    def test_lowercase_and_mixed_case_body_are_read(self) -> None:
        for body_html, expected in (
            ('<textarea name="contents" id="contents">lower body</textarea>', "lower body"),
            ('<textarea ID="Contents">mixed case body</textarea>', "mixed case body"),
        ):
            with self.subTest(body=body_html):
                self.assertEqual(
                    parse.parse_essay_detail(_detail_with_body(body_html)).essay_text, expected
                )

    def test_draft_templates_and_beizhu_are_not_taken_as_body(self) -> None:
        html = _detail_with_body(
            '<textarea section-title="作文开头常见表达" pos="top" '
            'class="section-content draft-top">DRAFT-TOP-TEMPLATE</textarea>'
            '<textarea class="section-content draft-main">DRAFT-MAIN-TEMPLATE</textarea>'
            '<textarea class="section-content draft-tail">DRAFT-TAIL-TEMPLATE</textarea>'
            '<TEXTAREA NAME="contents" id="contents">REAL BODY</TEXTAREA>'
            '<textarea NAME="beizhu" id="beizhu">BEIZHU-PLACEHOLDER</textarea>'
        )
        self.assertEqual(parse.parse_essay_detail(html).essay_text, "REAL BODY")

    def test_body_is_only_stripped_not_normalized(self) -> None:
        # 用户要求「能原样看到作文内容」→ 段间空行、行内多空格都必须保留
        html = _detail_with_body('<div id="contents">\n  line  a\n\nline b\n</div>')
        self.assertEqual(parse.parse_essay_detail(html).essay_text, "line  a\n\nline b")

    def test_prefix_id_is_not_matched(self) -> None:
        # #contentsW 是外层容器，不能被当成 #contents
        self.assertEqual(parse.parse_essay_detail(_detail_with_body("")).essay_text, "")

    def test_missing_body_is_empty_not_an_error(self) -> None:
        detail = parse.parse_essay_detail(_detail_with_body(""))
        self.assertEqual(detail.essay_text, "")
        self.assertEqual(detail.score, 80.0)  # 其余字段照常

    def test_write_page_is_rejected_by_detail_parser(self) -> None:
        # #contents 不算详情页锚点（写作页也有它）→ 写作页必须被拒，而不是静默返回空对象
        with self.assertRaises(parse.PageShapeError):
            parse.parse_essay_detail(fixture("write_with_draft.html"))


class TestWritePage(unittest.TestCase):
    """B/C｜按作业号定位页（``Endpoints.lookup_by_rid``）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fresh = parse.parse_write_page(fixture("write_fresh.html"))
        cls.draft = parse.parse_write_page(fixture("write_with_draft.html"))

    def test_fresh_page_all_fields(self) -> None:
        w = self.fresh
        self.assertEqual(w.rid, "1000001")
        self.assertEqual(w.eid, "")  # _eid 存在但为空 = 未写过
        self.assertFalse(w.has_draft)
        self.assertEqual(w.title, "示例课程（三）- Unit 6")
        self.assertEqual(w.requirement, "示例题目要求文本（占位）。")
        self.assertEqual(w.word_range, "120~150")
        self.assertEqual(w.full_marks, 100.0)
        self.assertEqual(w.deadline, "2026-11-16 23:59")
        self.assertEqual(w.server_time, "2026-10-19 11:16:06")
        self.assertEqual(w.essay_text, "")
        self.assertTrue(w.no_paste)

    def test_draft_page_all_fields(self) -> None:
        w = self.draft
        self.assertEqual(w.rid, "1000002")
        self.assertEqual(w.eid, "200000001")
        self.assertTrue(w.has_draft)
        self.assertEqual(w.title, "示例课程（三）- Unit 5")
        self.assertEqual(w.requirement, "示例题目要求文本（占位）。")
        self.assertEqual(w.word_range, "100~200")
        self.assertEqual(w.full_marks, 100.0)
        self.assertEqual(w.deadline, "2026-10-19 23:59")
        self.assertEqual(w.essay_text, _BODY_PLACEHOLDER)

    def test_body_is_not_confused_with_templates(self) -> None:
        body = self.draft.essay_text
        self.assertEqual(body, _BODY_PLACEHOLDER)
        for token in ("DRAFT-", "BEIZHU", "常见表达"):
            self.assertNotIn(token, body)

    def test_word_range_ignores_live_word_count(self) -> None:
        # 页内两处「字数」：区间（#v2_request_titlex）与实时字数（#init_wc 的「字数:123 词」）
        self.assertIn('id="init_wc">123<', fixture("write_with_draft.html"))
        self.assertEqual(self.draft.word_range, "100~200")

    def test_page_vars_win_over_arguments(self) -> None:
        # ⚠ 语义要点：_eid 存在但为空是「这个作业没写过」的事实，不能被参数覆盖
        w = parse.parse_write_page(fixture("write_fresh.html"), rid="R", eid="E")
        self.assertEqual((w.rid, w.eid), ("1000001", ""))
        self.assertFalse(w.has_draft)

    def test_arguments_used_when_page_has_no_vars(self) -> None:
        html = (
            '<html><body><div id="timu">题目：[9] T</div>'
            '<div id="request_y"><p>R</p></div></body></html>'
        )
        w = parse.parse_write_page(html, rid="9", eid="E")
        self.assertEqual((w.rid, w.eid), ("9", "E"))

    def test_topic_without_bracket_is_tolerated(self) -> None:
        html = '<div id="timu">题目：没有编号的标题</div>'
        self.assertEqual(parse.parse_write_page(html).title, "没有编号的标题")

    def test_no_paste_flag_variants(self) -> None:
        base = '<div id="timu">题目：[9] T</div>'
        for value, expected in (('"1"', True), ('"0"', False), ('"true"', True), (None, False)):
            var = "" if value is None else f"<script>var _no_paste={value};</script>"
            with self.subTest(value=value):
                self.assertIs(parse.parse_write_page(base + var).no_paste, expected)

    def test_textarea_body_reads_regardless_of_tag_case(self) -> None:
        for tag in ("textarea", "TEXTAREA", "TextArea"):
            html = (
                '<html><body><div id="timu">题目：[7] T</div>'
                f'<{tag} name="contents" id="contents">Body via {tag}.</{tag}></body></html>'
            )
            with self.subTest(tag=tag):
                self.assertEqual(
                    parse.parse_write_page(html).essay_text, f"Body via {tag}."
                )

    def test_non_write_page_raises(self) -> None:
        for html in ("<html><body>请登录后继续</body></html>", ""):
            with self.subTest(html=html):
                with self.assertRaises(parse.PageShapeError):
                    parse.parse_write_page(html)

    def test_detail_page_is_rejected_not_silently_partial(self) -> None:
        # 详情页也有 #contents 与 _eid，但没有 #timu/#request_y/_rid → 必须报错
        with self.assertRaises(parse.PageShapeError):
            parse.parse_write_page(fixture("essay_detail.html"))


class TestColumnIsolation(unittest.TestCase):
    """修「需知会-1」：标题与 URL **不得跨列回落**（用验证者给的最小复现）。"""

    @staticmethod
    def _list_html(title_cell: str, action_cell: str, first_cell: str = "9999") -> str:
        headers = "".join(f"<li>{name}</li>" for name in protocol.PENDING_HEADERS)
        return (
            f'<div id="essayList"><ul class="reItemM">{headers}</ul>'
            '<div class="line"></div><ul class="reItemS">'
            f"<li>{first_cell}</li><li>{title_cell}</li><li>作文</li><li>师</li><li>班</li>"
            f"<li>2026-01-01</li><li>{action_cell}</li></ul></div>"
        )

    def test_title_cell_without_anchor_does_not_borrow_action_cell(self) -> None:
        html = self._list_html("纯文本标题", '<a href="?c=v2&a=write&rid=9999">查看</a>')
        rows = parse.parse_assignment_rows(html)
        self.assertEqual(len(rows), 1)
        # 旧实现（_first_anchor(cells[1]) or _first_anchor(cells[6])）这里会得到 "查看"
        self.assertEqual(rows[0].title, "纯文本标题")
        self.assertEqual(rows[0].url, "?c=v2&a=write&rid=9999")

    def test_action_cell_without_anchor_does_not_borrow_title_cell(self) -> None:
        html = self._list_html('<a href="?c=v2&a=view&eid=1&rid=9999">标题锚</a>', "查看")
        rows = parse.parse_assignment_rows(html)
        self.assertEqual(rows[0].title, "标题锚")
        # 旧实现这里会把标题格的 href 串成 url
        self.assertEqual(rows[0].url, "")

    def test_rid_falls_back_only_to_title_cell_href(self) -> None:
        html = self._list_html(
            '<a href="?c=v2&a=write&rid=7777">标题锚</a>',
            '<a href="?c=v2&a=write&rid=8888">查看</a>',
            first_cell="",
        )
        rows = parse.parse_assignment_rows(html)
        self.assertEqual(rows[0].rid, "7777")
        self.assertEqual(rows[0].url, "?c=v2&a=write&rid=8888")

    def test_fixture_row_unaffected(self) -> None:
        # 真实样本两格 href 相同 → 行为不变（回归保护）
        row = parse.parse_assignment_rows(fixture("assignments_pending_row.html"))[0]
        self.assertEqual(row.title, "示例课程（三）- Unit 6")
        self.assertEqual(row.url, "?c=v2&a=write&rid=1000001")


class TestDimensionGuards(unittest.TestCase):
    """修「建议-6/-7」：宽度 ``inf``/``nan``/``0`` 的守卫与日志措辞。"""

    @staticmethod
    def _doc(container_px: str, bar_px: str, title: str | None) -> str:
        title_attr = f' title="{title}"' if title is not None else ""
        return (
            '<html><body><b id="rescoreTxt" title="满分:100" content="100">80</b>'
            '<div class="tbL"> 词汇: </div>'
            f'<div style="float:left;width:{container_px}px">'
            f'<table style="width:{bar_px}px"><tr><td style="width:2px;"></td>'
            f'<td style="width:{bar_px}px;"{title_attr}></td></tr></table></div>'
            "</body></html>"
        )

    def test_malformed_width_without_title_is_skipped(self) -> None:
        # 宽度恶形 **且没有可用 title** → 这一维度确实没法给分，跳过（措辞要准确）
        huge = "9" * 400  # float("9"*400) == inf
        with self.assertLogs(LOGGER_NAME, level="WARNING") as ctx:
            detail = parse.parse_essay_detail(self._doc(huge, huge, None))
        self.assertEqual(detail.dimensions, ())
        self.assertIn("不是有限值", "\n".join(ctx.output))

    def test_finite_title_is_used_even_with_non_finite_width(self) -> None:
        # task-10 lead 裁定：title 权威且独立 —— 宽度 inf 也照样采用 title
        huge = "9" * 400
        with self.assertLogs(LOGGER_NAME, level="WARNING") as ctx:
            detail = parse.parse_essay_detail(self._doc(huge, huge, "0.6000000"))
        self.assertEqual(len(detail.dimensions), 1)
        dim = detail.dimensions[0]
        self.assertEqual(dim.source, "title")
        self.assertEqual(dim.ratio, 0.6)
        self.assertEqual(dim.bar_width, math.inf)  # 读到的值原样保留
        self.assertEqual(dim.container_width, math.inf)
        self.assertIn("宽度不可用", "\n".join(ctx.output))  # 但必须留痕，不静默

    def test_finite_title_is_used_when_only_bar_width_is_missing(self) -> None:
        # 容器宽读到了、进度条 table 没有宽度属性：宽度比不可用，但 title 能用
        html = (
            '<html><body><b id="rescoreTxt" title="满分:100" content="100">80</b>'
            '<div class="tbL"> 词汇: </div>'
            '<div style="float:left;width:200px">'
            '<table><tr><td style="width:2px;"></td><td title="0.6"></td></tr></table>'
            "</div></body></html>"
        )
        with self.assertLogs(LOGGER_NAME, level="WARNING"):
            detail = parse.parse_essay_detail(html)
        self.assertEqual(len(detail.dimensions), 1)
        dim = detail.dimensions[0]
        self.assertEqual((dim.source, dim.ratio), ("title", 0.6))
        self.assertEqual(dim.bar_width, 0.0)  # 读不到 → 0.0（消费方看 source）
        self.assertEqual(dim.container_width, 200.0)

    def test_zero_container_width_says_zero_not_missing(self) -> None:
        with self.assertLogs(LOGGER_NAME, level="WARNING") as ctx:
            detail = parse.parse_essay_detail(self._doc("0", "150", None))
        self.assertEqual(detail.dimensions, ())
        joined = "\n".join(ctx.output)
        self.assertIn("容器宽为 0", joined)
        self.assertNotIn("读不到", joined)  # 0 是「读到了 0」，不是「读不到」

    def test_zero_container_width_with_title_keeps_authoritative_score(self) -> None:
        # 有 title 就不做除法 → 容器宽 0 也能给出权威分数
        with self.assertLogs(LOGGER_NAME, level="WARNING"):
            detail = parse.parse_essay_detail(self._doc("0", "150", "0.6"))
        self.assertEqual(len(detail.dimensions), 1)
        self.assertEqual(detail.dimensions[0].source, "title")
        self.assertEqual(detail.dimensions[0].ratio, 0.6)

    def test_ratio_is_never_non_finite(self) -> None:
        huge = "9" * 400
        docs = (
            self._doc("200", "150", None),
            self._doc("200", "150", "0.75"),
            self._doc(huge, huge, None),
            self._doc(huge, "150", "0.6"),
            self._doc("0", "150", None),
            self._doc("200", huge, None),
        )
        for doc in docs:
            with self.assertLogs(LOGGER_NAME, level="WARNING"):
                detail = parse.parse_essay_detail(doc)
            for dim in detail.dimensions:
                self.assertTrue(math.isfinite(dim.ratio), dim)

    def test_non_finite_title_is_not_used_as_ratio(self) -> None:
        # title 本身也可能被构造成 inf；此时必须回退到宽度比而不是把 inf 当分数
        huge = "9" * 400
        with self.assertLogs(LOGGER_NAME, level="WARNING"):
            detail = parse.parse_essay_detail(self._doc("200", "150", huge))
        self.assertEqual(len(detail.dimensions), 1)
        self.assertEqual(detail.dimensions[0].source, "width")
        self.assertEqual(detail.dimensions[0].ratio, 0.75)


class TestSanitizedFixtures(unittest.TestCase):
    """把「fixtures 不含个人信息」这条验收标准钉在 CI 上。"""

    EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
    PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
    LONG_ENGLISH_RE = re.compile(r"[A-Za-z][A-Za-z ,.'-]{40,}")

    def _fixture_texts(self) -> dict[str, str]:
        files = sorted(FIXTURES.glob("*.html"))
        self.assertTrue(files, "fixtures 目录为空")
        return {path.name: path.read_text(encoding="utf-8") for path in files}

    def test_no_personal_data(self) -> None:
        for name, text in self._fixture_texts().items():
            with self.subTest(fixture=name):
                self.assertEqual(self.EMAIL_RE.findall(text), [])
                self.assertEqual(self.PHONE_RE.findall(text), [])

    def test_no_long_english_passages(self) -> None:
        for name, text in self._fixture_texts().items():
            with self.subTest(fixture=name):
                self.assertEqual(self.LONG_ENGLISH_RE.findall(text), [])

    def test_real_essay_sentence_absent(self) -> None:
        # 真实样本里的作文正文/例句绝不能出现在 fixtures 里
        forbidden = ("Stay at Home", "Going Out for Pleasure", "jukuu.com")
        for name, text in self._fixture_texts().items():
            with self.subTest(fixture=name):
                for token in forbidden:
                    self.assertNotIn(token, text)

    def test_real_personal_names_absent(self) -> None:
        """真实姓名 / 学校 / 班级只登记在本机 ``.secret-denylist``，仓库里不留明文。

        这里**不内联真实字面量**（那等于把要防的东西又抄一遍），改为两条：
        1) 本机有 ``.secret-denylist`` 时，逐条断言 fixtures 里不含这些值；
        2) 正向断言占位值确实存在 —— 证明脱敏真的做过，而不是样本里本来就没有。
        """
        texts = self._fixture_texts()
        denylist = _load_denylist()
        if denylist:
            for name, text in texts.items():
                with self.subTest(fixture=name):
                    for token in denylist:
                        self.assertNotIn(token, text)
        self.assertTrue(
            any("张老师" in text for text in texts.values()),
            "出题人 / 创建人应脱敏为「张老师」",
        )
        self.assertTrue(
            any("示例大学" in text or "示例学校" in text for text in texts.values()),
            "学校名应脱敏为「示例大学」",
        )
        # 年级 / 班级串一律脱敏：fixture 里不应再出现「NN级」这种原始标记
        grade_re = re.compile(r"(?<![0-9])[0-9]{2}级")
        for name, text in texts.items():
            with self.subTest(fixture=name):
                self.assertEqual(grade_re.findall(text), [], "年级标记应脱敏")

    def test_write_page_fixtures_exist(self) -> None:
        # task-6 的两个新样本必须在库里（verifier 要定向复核）
        names = set(self._fixture_texts())
        self.assertLessEqual({"write_fresh.html", "write_with_draft.html"}, names)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
