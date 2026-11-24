#!/usr/bin/env python3
"""U校园全接口自检矩阵 —— **只读 + 干跑，永不发写请求**。

用途：会话刚续上、平台改版之后，用一条命令把这套适配器用到的**每一个接口**
都打一遍，逐条给出 PASS/FAIL 与耗时，失败的那条直接打印异常类型与消息。

用法::

    PYTHONPATH=src .venv/bin/python scripts/unipus_api_matrix.py
    PYTHONPATH=src .venv/bin/python scripts/unipus_api_matrix.py --course 20001188271
    PYTHONPATH=src .venv/bin/python scripts/unipus_api_matrix.py --json out.json

覆盖范围（按调用链分组）：

- **会话**：``refresh_session`` / ``is_logged_in``
- **门户**：用户信息、书架、班级教材列表、教材详情、成绩账本
  （``aca/plan/detail`` + ``aca/achievement/queryUserScore``）
- **ucontent**：目录、必修集（``leafs``）、单任务状态（``progress/v2``）、
  题目内容（``v3/content``）、标准答案（``v3/answer`` —— 这条校验鉴权，
  是判断 annotator token 还有效的推荐探针）
- **适配器扩展**：``native_course_tree`` / ``native_assignment_state`` /
  ``native_plan``（干跑）
- **未接线端点**：``product/course/courseActiveFlag``、``tla/.../unitTaskSituation``
  —— 这两个常量已登记但代码里没有调用点，本脚本顺手探一次，看它们今天还活不活
- **时长**：只算计划（``plan_duration_pool``），**不建任何连接**

为什么单独一个脚本而不是塞进 `scripts/api_matrix_check.py`：那个跑的是**控制台
HTTP 路由**（离线假适配器），这里跑的是**真实平台协议**（要真会话）。两者失败
原因完全不同，混在一起会把「路由错了」和「平台改版了」搅成同一个红。

前置条件：账号库里已绑定 U校园 且**会话仍然有效**（``data/sessions/accounts/``
下的 cookie 文件）。会话过期时本脚本只会在第一条报 ``refresh_session=False``，
并给出平台的原始原因（例如 SSO ``2012 用户名或者密码错误``）—— 那时请先到控制台
「账号」页重新登录一次，密码不落库，只用于换 cookie。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Row:
    group: str
    name: str
    ok: bool
    detail: str = ""
    ms: int = 0


@dataclass
class Matrix:
    rows: list[Row] = field(default_factory=list)

    def add(self, group: str, name: str, ok: bool, detail: str = "", ms: int = 0) -> None:
        self.rows.append(Row(group, name, ok, detail, ms))
        mark = "PASS" if ok else "FAIL"
        print(f"  {mark}  {name:44} {detail[:64]}", flush=True)

    async def check(self, group: str, name: str, factory: Any) -> bool:
        started = time.monotonic()
        try:
            detail = await factory()
            self.add(group, name, True, str(detail), int((time.monotonic() - started) * 1000))
            return True
        except Exception as exc:  # noqa: BLE001 - 这里就是要打印任何异常
            self.add(
                group,
                name,
                False,
                f"{type(exc).__name__}: {exc}",
                int((time.monotonic() - started) * 1000),
            )
            return False


def _pick_course(courses: list[Any], wanted: str) -> Any:
    if wanted:
        for course in courses:
            if str(course.remote_id) == wanted:
                return course
        raise SystemExit(f"课程 {wanted!r} 不在课程列表里（共 {len(courses)} 门）")
    # 默认取第一门有实例 ID 的课，避免拿到合成课程时整轮都失败
    for course in courses:
        if str((course.raw or {}).get("course_instance_id") or ""):
            return course
    return courses[0]


def _split(aid: str, instance: str) -> tuple[str, str]:
    if ":" in aid:
        head, tail = aid.split(":", 1)
        if tail:
            return head, tail
    return instance, aid


async def run(course_id: str) -> tuple[Matrix, dict[str, Any]]:
    from kedaibiao.config.settings import load_settings
    from kedaibiao.platforms import load_builtin_adapters
    from kedaibiao.platforms.unipus import duration as duration_mod
    from kedaibiao.runtime import build_runtime, restore_adapter_session
    from kedaibiao.security.accounts import AccountStore

    load_builtin_adapters()
    settings = load_settings(root=".")
    store = AccountStore(settings.db_path)
    matrix = Matrix()
    meta: dict[str, Any] = {}
    runtime = None
    adapter = None
    try:
        creds, cookie_file = store.resolve_credentials(
            "unipus", session_dir=settings.session_dir
        )
        meta["username"] = creds.username
        meta["cookie_file"] = cookie_file
        runtime = build_runtime(root=".", cookie_file=cookie_file)
        adapter = runtime.make_adapter("unipus")
        restore_adapter_session(adapter, cookie_file)

        print("· 会话", flush=True)
        ok = await adapter.refresh_session(creds)
        matrix.add("会话", "refresh_session", bool(ok),
                   "会话有效" if ok else (getattr(adapter, "last_session_error", "") or "无效"))
        if not ok:
            meta["hint"] = "会话已过期：先到控制台「账号」页给 U校园 重新登录一次"
            return matrix, meta
        matrix.add("会话", "is_logged_in", bool(await adapter.is_logged_in()), "")

        client = adapter.client()
        print("· 门户", flush=True)
        await matrix.check("门户", "user/info", lambda: _user_info(client))
        await matrix.check("门户", "cmgt/course/my/bookshelf", lambda: _shelf(client))
        await matrix.check("门户", "course/getCourseListByStudent", lambda: _bindings(client))

        courses = await adapter.list_courses()
        course = _pick_course(courses, course_id)
        meta["course"] = course.name
        meta["courses"] = len(courses)
        matrix.add("门户", "list_courses（含书架合并）", True, f"{len(courses)} 门")

        instance = str((course.raw or {}).get("course_instance_id") or "")
        await matrix.check("门户", "aca/plan/detail + queryUserScore（成绩账本）",
                           lambda: _ledger(adapter, course))
        await matrix.check("门户", "product/course/courseActiveFlag（未接线）",
                           lambda: _active_flag(adapter, course))
        await matrix.check("门户", "tla/.../unitTaskSituation（未接线）",
                           lambda: _unit_situation(adapter, course))

        print("· ucontent", flush=True)
        await matrix.check("ucontent", "course/api/course/{id}/default（目录）",
                           lambda: _catalog(client, instance))
        await matrix.check("ucontent", "fetch_required_leafs（必修集）",
                           lambda: _leafs(adapter, course))

        tree = await adapter.native_course_tree(course, with_state=True, state_scope="required")
        groups = [g for unit in tree["units"] for g in unit["groups"]]
        required = [g for g in groups if g.get("required")]
        meta["units"] = len(tree["units"])
        meta["groups"] = len(groups)
        meta["required"] = len(required)
        matrix.add("适配器", "native_course_tree", True,
                   f"{len(tree['units'])} 单元 / {len(groups)} 组 / 必修 {len(required)}")

        target = required[0] if required else (groups[0] if groups else None)
        if target is None:
            matrix.add("ucontent", "选定测试任务组", False, "这门课没有任务组")
            return matrix, meta
        aid = str(target.get("assignment_id") or "")
        group_id = str(target.get("id") or "")
        meta["sample_group"] = f"{target.get('name')} ({group_id})"
        inst, gid = _split(aid, instance)

        await matrix.check("ucontent", "progress/v2（单任务状态）",
                           lambda: _group_state(client, inst, gid))
        await matrix.check("ucontent", "v3/content（题目内容）",
                           lambda: _content(client, inst, gid))
        await matrix.check("ucontent", "v3/answer（标准答案 · 鉴权探针）",
                           lambda: _answers(client, inst, gid))
        await matrix.check("适配器", "native_assignment_state",
                           lambda: _state(adapter, aid))
        await matrix.check("适配器", "native_plan（干跑，不写）",
                           lambda: _plan(adapter, aid))
        await matrix.check("适配器", "fetch_questions / fetch_standard_answers",
                           lambda: _questions(adapter, inst, gid))

        print("· 时长（只算计划，不连接）", flush=True)
        await matrix.check("时长", "plan_duration_pool（干跑）",
                           lambda: _duration_plan(duration_mod))

        # 控制台动作层（走 ConsoleService，和页面/CLI 同一条路径）
        print("· 控制台动作", flush=True)
        await run_actions(matrix, settings, aid)
        return matrix, meta
    finally:
        if adapter is not None:
            with contextlib.suppress(Exception):
                await adapter.close()
        if runtime is not None:
            runtime.close()
        store.close()


async def run_actions(matrix: Matrix, settings: Any, assignment_id: str) -> None:
    """控制台动作层：与页面 / CLI 完全同一条路径（``ConsoleService.run_action``）。

    ``submit`` 与 ``duration`` 都只跑**干跑**（``confirm`` 缺省），所以这一段
    同样不会发出任何写请求。
    """
    from kedaibiao.api.service import ConsoleService

    service = ConsoleService(root=settings.root)
    loop = asyncio.get_running_loop()
    cases = [
        ("courses", {}),
        ("assignments", {"courses_only": 1}),
        ("assignments", {"course": "", "with_state": 1}),
        ("detail", {"assignment_id": assignment_id}),
        ("answers", {"assignment_id": assignment_id}),
        ("submit", {"assignment_id": assignment_id}),      # 干跑
        ("duration", {"seconds": 60}),                     # 干跑（不建连接）
    ]
    for action, params in cases:
        label = action + ("（干跑）" if action in ("submit", "duration") else "")

        def call(action: str = action, params: dict[str, Any] = params) -> str:
            return service.run_action("unipus", action, params)  # type: ignore[arg-type]

        await matrix.check(
            "控制台", label, lambda call=call: loop.run_in_executor(None, call)
        )


async def _user_info(client: Any) -> str:
    info = await client.fetch_user_info()
    return f"{type(info).__name__}"


async def _shelf(client: Any) -> str:
    return f"{len(await client.fetch_bookshelf())} 本"


async def _bindings(client: Any) -> str:
    return f"{len(await client.fetch_course_bindings())} 条绑定"


async def _ledger(adapter: Any, course: Any) -> str:
    ledger = await adapter.native_grade_ledger(course)
    keys = ",".join(sorted(ledger)[:4])
    return f"键={keys}"


async def _active_flag(adapter: Any, course: Any) -> str:
    from kedaibiao.platforms.unipus import endpoints as ep

    rid = str((course.raw or {}).get("course_resource_id") or "")
    body = await adapter.client().request(
        ep.UnipusApp.PORTAL, "GET", ep.PORTAL_ACTIVE_FLAG,
        family=ep.UnipusApp.PORTAL.value, query={"courseResourceId": rid},
    )
    return str(body)[:60]


async def _unit_situation(adapter: Any, course: Any) -> str:
    from kedaibiao.platforms.unipus import endpoints as ep

    rid = str((course.raw or {}).get("course_resource_id") or "")
    body = await adapter.client().request(
        ep.UnipusApp.PORTAL, "GET", ep.PORTAL_UNIT_TASK_SITUATION,
        family=ep.UnipusApp.PORTAL.value, query={"id": rid},
    )
    return str(body)[:60]


async def _catalog(client: Any, instance: str) -> str:
    catalog = await client.fetch_catalog(instance)
    return f"{len(catalog.units)} 单元"


async def _leafs(adapter: Any, course: Any) -> str:
    required = await adapter.fetch_required_groups(course)
    return f"missing={required.missing} 条数={len(required.raw or {})}"


async def _group_state(client: Any, instance: str, group_id: str) -> str:
    state = await client.fetch_group_state(instance, group_id)
    return f"finished={bool(state.first_pass_datetime)} score={state.score[:6]}"


async def _content(client: Any, instance: str, group_id: str) -> str:
    return f"{len(await client.fetch_content(instance, group_id))} 题"


async def _answers(client: Any, instance: str, group_id: str) -> str:
    return f"{len(await client.fetch_answers(instance, group_id))} 条标准答案"


async def _state(adapter: Any, aid: str) -> str:
    out = await adapter.native_assignment_state(aid)
    return f"finished={out.get('finished')} items={out.get('total_items')}"


async def _plan(adapter: Any, aid: str) -> str:
    plan = await adapter.native_plan(aid)
    return f"can_submit={plan.get('can_submit')} 题={plan.get('question_count')}"


async def _questions(adapter: Any, instance: str, group_id: str) -> str:
    from kedaibiao.core.models import Assignment

    assignment = Assignment(remote_id=f"{instance}:{group_id}", title="matrix")
    questions = await adapter.fetch_questions(assignment)
    answers = await adapter.fetch_standard_answers(assignment)
    return f"{len(questions)} 题 / {len(answers)} 答案"


async def _duration_plan(module: Any) -> str:
    plan = module.plan_duration_pool("matrix-openid", "matrix-token", seconds=60, rooms=6)
    return f"{len(plan.rooms)} 房间（未连接）"


def main() -> int:
    parser = argparse.ArgumentParser(description="U校园全接口自检（只读 + 干跑）")
    parser.add_argument("--course", default="", help="教材/班级课程号（默认取第一门有实例的）")
    parser.add_argument("--json", default="", help="把结果另存为 JSON")
    parser.add_argument("--verbose", action="store_true", help="打印平台请求日志")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    print("U校园接口矩阵（只读 + 干跑；不会发出任何写请求）", flush=True)
    matrix, meta = asyncio.run(run(args.course))

    passed = sum(1 for row in matrix.rows if row.ok)
    failed = [row for row in matrix.rows if not row.ok]
    print("\n=== 汇总 ===")
    print(f"PASS {passed} / FAIL {len(failed)}  （课：{meta.get('course', '—')}）")
    for row in failed:
        print(f"  ✗ [{row.group}] {row.name}: {row.detail[:150]}")
    if meta.get("hint"):
        print(f"\n提示：{meta['hint']}")
    if args.json:
        payload = {
            "meta": meta,
            "rows": [{"group": r.group, "name": r.name, "ok": r.ok,
                      "detail": r.detail, "ms": r.ms} for r in matrix.rows],
        }
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        print(f"结果已写入 {args.json}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
