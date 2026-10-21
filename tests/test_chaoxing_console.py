"""学习通接入主控制台的单元测试：结构转换与动作注册（全离线）。"""

from __future__ import annotations

from kedaibiao.api import actions as action_mod
from kedaibiao.api.actions import register_platform
from kedaibiao.platforms.chaoxing import adapter as adapter_mod


def test_storage_state_from_cookie_header() -> None:
    state = adapter_mod._storage_state_from_header("_uid=42; uf=abc; fid=1")
    cookies = {item["name"]: item for item in state["cookies"]}
    assert set(cookies) == {"_uid", "uf", "fid"}
    assert cookies["_uid"]["value"] == "42"
    assert cookies["_uid"]["domain"] == ".chaoxing.com"
    assert state["origins"] == []


def test_html_headers_carry_browser_user_agent() -> None:
    # 学习通前置 tengine 会 403 掉非浏览器 UA（实测），这个头不能丢
    assert "Mozilla" in adapter_mod._HTML_HEADERS["User-Agent"]
    assert adapter_mod._HTML_HEADERS["Referer"].startswith("https://i.chaoxing.com")


def test_actions_registered_for_console() -> None:
    action_mod.load_builtin_actions()
    specs = {spec.name: spec for spec in register_platform("chaoxing")}
    for name in ("tasks", "study", "exams", "exam", "quiz"):
        assert name in specs, f"缺少动作 {name}"
    # 写操作必须标 mutates，前端才会提示「写操作」
    assert specs["study"].mutates is True
    assert specs["exam"].mutates is True
    assert specs["quiz"].mutates is True
    assert specs["tasks"].mutates is False
    assert specs["exams"].mutates is False


def test_confirm_gate_words() -> None:
    assert action_mod._chaoxing_confirm({"confirm": "true"})
    assert action_mod._chaoxing_confirm({"confirm": "确认"})
    assert not action_mod._chaoxing_confirm({})
    assert not action_mod._chaoxing_confirm({"confirm": "no"})
