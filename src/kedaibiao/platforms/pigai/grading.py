"""批改网的「提交 → 分析 → 出分」链路。

## 为什么要单独一层

批改网提交作文后**不会立刻有分数**：平台要跑一遍机器分析，客户端必须主动
触发并轮询（`processStartIcomet` → `process`），否则作文永远停在「待分析」。

这一步是学生端 JS 自己做的（`spss.js` 的 `spssAnly2015`），我们从静态读出
再实测跑通：拿 key → 按服务端给的 ``sleep`` 轮询 → ``s`` 变成 ``ok`` 即完成。

## 另一个必须处理的状态：`未截止`

作业**未到截止时间时，平台不公布分数**（列表里显示「未截止」而不是数字）。
这不是失败，也不能当成 0 分 —— 循环要能识别它并停下来告诉用户。

## 快照

`EssaySnapshot` 把一次评分的全部可用信息收拢：分数、四个维度、
总评、逐条批注。这些正是「让 AI 按批注改写」的输入。
"""

from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: 这些状态表示「还没有分数」，且不是错误
PENDING_STATES = ("待分析", "分析中", "未截止", "")


class AnalysisTimeout(RuntimeError):
    """等分析结果超时（作文可能还在排队）。"""


class AnalysisFailed(RuntimeError):
    """平台明确拒绝了分析请求。"""


@dataclass(slots=True)
class Dimension:
    name: str
    ratio: float


@dataclass(slots=True)
class Annotation:
    level: str
    cate: str
    message: str
    sentence: str = ""


@dataclass(slots=True)
class EssaySnapshot:
    """一次评分的快照 —— 也是「让 AI 改写」的输入。"""

    rid: str
    eid: str
    score: float | None = None
    full_marks: float | None = None
    topic: str = ""
    essay_text: str = ""
    dimensions: list[Dimension] = field(default_factory=list)
    overall_comment: str = ""
    annotations: list[Annotation] = field(default_factory=list)
    #: 平台原始状态（例如「未截止」表示分数未公布）
    raw_state: str = ""
    #: 触发分析时平台拒绝的原因（「没找到作文」这类）——空串表示没出错
    analysis_error: str = ""
    #: 详情页读不到（老作文的页面换了形态）时的说明；分数可能来自列表
    detail_error: str = ""

    @property
    def graded(self) -> bool:
        return self.score is not None

    @property
    def score_pct(self) -> float | None:
        if self.score is None or not self.full_marks:
            return None
        return round(self.score / self.full_marks, 4)

    def weak_dimensions(self, *, limit: int = 2) -> list[Dimension]:
        """得分率最低的几项 —— 改写时优先针对它们。"""
        return sorted(self.dimensions, key=lambda d: d.ratio)[:limit]

    def feedback_lines(self, *, max_items: int = 12) -> list[str]:
        """把批注压成「可以喂给模型」的短句列表。

        只保留**可操作**的：语法/搭配/词汇误用类。像「易混词汇：A,B,C 都可表…」
        这种纯提示不占篇幅，否则 prompt 会被冲爆。
        """
        out: list[str] = []
        seen: set[str] = set()
        for item in self.annotations:
            text = re.sub(r"<[^>]+>", "", item.message or "").strip()
            if not text:
                continue
            # 去掉批改网标记的 <b> 包裹后剩下的内容可能高度重复，去重
            key = text[:40]
            if key in seen:
                continue
            seen.add(key)
            if item.level.startswith("warn") or item.level.startswith("error"):
                out.append(f"[{item.cate}] {text}")
            if len(out) >= max_items:
                break
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "rid": self.rid,
            "eid": self.eid,
            "score": self.score,
            "full_marks": self.full_marks,
            "score_pct": self.score_pct,
            "topic": self.topic,
            "essay_text": self.essay_text,
            "word_count": len(re.findall(r"[A-Za-z][A-Za-z'\-]*", self.essay_text or "")),
            "dimensions": [{"name": d.name, "ratio": round(d.ratio, 4)}
                           for d in self.dimensions],
            "weak_dimensions": [d.name for d in self.weak_dimensions()],
            "overall_comment": self.overall_comment,
            "annotations": [
                {"level": a.level, "cate": a.cate, "message": a.message,
                 "sentence": a.sentence}
                for a in self.annotations
            ],
            "feedback_lines": self.feedback_lines(),
            "raw_state": self.raw_state,
            "analysis_error": self.analysis_error,
            "detail_error": self.detail_error,
            "graded": self.graded,
        }


# --------------------------------------------------------------------------
# 平台交互
# --------------------------------------------------------------------------


def _post_form(http: Any, path: str, fields: dict[str, str], referer: str) -> Any:
    """发一次表单 POST（批改网的 ajax 端点都用这个形状）。"""
    return http.request(
        "POST",
        path,
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": referer,
            "Origin": "https://www.pigai.org",
        },
        body=urllib.parse.urlencode(fields).encode("utf-8"),
    )


#: 前几次轮询把服务端给的 ``sleep`` 压到 :data:`FAST_POLL_CAP` 秒。
#:
#: 为什么：平台建议的间隔约 12 秒，而实际常常 20~40 秒就出分；一开始就按 12 秒
#: 等，等于最坏情况白等一轮。前几次探得密一点能更早拿到结果，之后立刻回到
#: 服务端给的节奏 —— 只在**开头**提速，避免整段高频请求。
#: 如果平台对节奏变敏感（出现 429 / 拒绝），把 ``FAST_POLLS`` 调成 0 即可关闭。
FAST_POLLS = 3
FAST_POLL_CAP = 2.0


async def start_analysis(http: Any, *, base: str, rid: str, eid: str) -> str:
    """触发分析，返回轮询用的 key。"""
    referer = f"{base}/index.php?c=v2&a=write&rid={rid}"
    resp = await _post_form(
        http, f"{base}/index.php?c=ajax&a=processStartIcomet", {"eid": eid}, referer
    )
    text = resp.text()
    if text.lstrip().lower().startswith("msg"):
        raise AnalysisFailed(f"平台拒绝分析：{text[:200]}")
    match = re.search(r'"key"\s*:\s*"([^"]+)"', text)
    if not match:
        raise AnalysisFailed(f"分析响应里没有 key：{text[:200]}")
    return match.group(1)


async def poll_analysis(
    http: Any,
    *,
    base: str,
    rid: str,
    key: str,
    timeout: float = 180.0,
    on_tick: Any = None,
) -> bool:
    """轮询到分析完成。返回 True=完成；超时抛 :class:`AnalysisTimeout`。

    服务端每次会回一个 ``sleep``（毫秒），按它等就好 —— 自己定间隔只会更慢或更密。
    """
    referer = f"{base}/index.php?c=v2&a=write&rid={rid}"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    for i in range(200):
        if loop.time() > deadline:
            raise AnalysisTimeout(f"等待分析超时（{timeout:.0f}s）")
        resp = await _post_form(
            http, f"{base}/index.php?c=ajax&a=process", {"key": key, "i": str(i)}, referer
        )
        text = resp.text()
        state = re.search(r'"s"\s*:\s*"([^"]*)"', text)
        current = state.group(1) if state else "?"
        if on_tick is not None:
            try:
                on_tick(i, current, text)
            except Exception:  # noqa: BLE001 - 回调失败不影响轮询
                pass
        if current in ("ok", "done", "finish", "finished"):
            return True
        if current in ("error", "fail", "failed"):
            raise AnalysisFailed(f"分析失败：{text[:200]}")
        sleep_ms = re.search(r'"sleep"\s*:\s*(\d+)', text)
        delay = (int(sleep_ms.group(1)) if sleep_ms else 1500) / 1000.0
        if i < FAST_POLLS:
            # 开头几次探密一点：出分往往比平台建议的 12 秒早
            delay = min(delay, FAST_POLL_CAP)
        await asyncio.sleep(delay)
    raise AnalysisTimeout("轮询次数用尽仍未完成")


async def fetch_snapshot(
    adapter: Any, *, rid: str, eid: str
) -> EssaySnapshot:
    """读一次评分详情，转成 :class:`EssaySnapshot`。

    详情页在「未截止 / 待分析」时是另一种页面形态，``fetch_essay_detail``
    会抛 ``PageShapeError`` —— 那种情况返回一个 ``graded=False`` 的空快照，
    让调用方按「还没出分」处理，而不是当成错误。
    """
    from kedaibiao.platforms.pigai.parse import PigaiParseError

    rows = await adapter.list_finished_essays()
    state = ""
    list_score: float | None = None
    for row in rows:
        if row.rid == rid and row.eid == eid:
            state = row.score_raw
            list_score = row.score
            break

    # 注意：**列表里写「未截止」不代表没有分**。实测有些作业老师开了提前批改，
    # 列表还显示「未截止」，但详情页已经有分数、四维和逐句批注，而且可以反复重交。
    # 所以这里一律先读详情页，读不到才按列表状态当作「还没出分」。
    try:
        detail = await adapter.fetch_essay_detail(rid, eid)
    except (PigaiParseError, Exception) as exc:  # noqa: BLE001 - 页面形态未知就当没出分
        log.warning("评分详情解析失败（按未出分处理）: %s", exc)
        # 老作文的详情页换了形态（只有 JS 中转页 / 只加载正文），但**列表里有分数**。
        # 这时候不能装作没分 —— 用户明明看得到分，只是拿不到批注。
        if list_score is not None:
            return EssaySnapshot(
                rid=rid, eid=eid, score=list_score, full_marks=None,
                raw_state=state or "仅列表分数",
                detail_error=(
                    "详情页读不到（老作文的页面形态不同），分数取自「已提交列表」，"
                    "但拿不到四维、评语和逐句批注。"
                ),
            )
        return EssaySnapshot(rid=rid, eid=eid, raw_state=state or "解析失败")

    # 详情页解析成功但没读到分数（老作文跳转后的页面只给正文），
    # 而列表里明明有分 → 用列表分数，别让用户以为「没分」。
    score = detail.score
    detail_error = ""
    if score is None and list_score is not None:
        score = list_score
        detail_error = (
            "详情页只读到了正文，没读到分数；分数取自「已提交列表」，"
            "四维、评语与逐句批注拿不到。"
        )

    return EssaySnapshot(
        rid=rid,
        eid=eid,
        score=score,
        full_marks=detail.full_marks,
        topic=detail.topic or "",
        essay_text=detail.essay_text or "",
        dimensions=[Dimension(name=d.name, ratio=d.ratio) for d in detail.dimensions],
        overall_comment=detail.overall_comment or "",
        annotations=[
            Annotation(level=a.level, cate=a.cate, message=a.message,
                       sentence=getattr(a, "sentence", ""))
            for a in detail.annotations
        ],
        raw_state=state,
        detail_error=detail_error,
    )


async def wait_for_grade(
    adapter: Any,
    http: Any,
    *,
    base: str,
    rid: str,
    eid: str,
    timeout: float = 180.0,
    on_tick: Any = None,
    on_key: Any = None,
    resume_key: str = "",
) -> EssaySnapshot:
    """完整走一遍：触发分析 → 轮询 → 读分。

    若已经出分（列表里是数字）就直接读，不重复触发分析。

    ``resume_key`` 非空时**跳过触发**，直接用这个 key 续等 —— 给「等超时后
    再延长一轮」用：重复调 ``processStartIcomet`` 既没必要，也可能被平台拒。
    ``on_key`` 把本次的轮询 key 交回调用方，便于之后续等。
    """
    if resume_key:
        key = resume_key
        await poll_analysis(http, base=base, rid=rid, key=key, timeout=timeout,
                            on_tick=on_tick)
        for attempt in range(6):
            await asyncio.sleep(2.0 if attempt else 0.5)
            snapshot = await fetch_snapshot(adapter, rid=rid, eid=eid)
            if snapshot.graded:
                return snapshot
        return snapshot

    snapshot = await fetch_snapshot(adapter, rid=rid, eid=eid)
    if snapshot.graded:
        return snapshot
    # 「未截止」也可能已经有分（提前批改），fetch_snapshot 已经读过详情页了；
    # 真的没分才去触发/等分析。

    try:
        key = await start_analysis(http, base=base, rid=rid, eid=eid)
        if on_key is not None:
            with suppress(Exception):  # 回调失败不影响主流程
                on_key(key)
    except AnalysisFailed as exc:
        # 平台拒绝触发分析（常见：这篇已经批过了 / 分析队列不认这个 eid）。
        # 不是致命错误 —— 把原因带回给调用方，界面提示「手动获取 / 直接改写重交」。
        log.warning("触发分析被拒（rid=%s eid=%s）: %s", rid, eid, exc)
        snapshot.analysis_error = str(exc)
        return snapshot
    await poll_analysis(http, base=base, rid=rid, key=key, timeout=timeout, on_tick=on_tick)
    # 分析刚结束时列表可能还没刷新，稍等一下再读
    for attempt in range(6):
        await asyncio.sleep(2.0 if attempt else 0.5)
        snapshot = await fetch_snapshot(adapter, rid=rid, eid=eid)
        if snapshot.graded:
            return snapshot
    return snapshot
