"""学习通登录判定（login）的纯函数单测：不联网。"""

from __future__ import annotations

from kedaibiao.platforms.chaoxing import login


def test_looks_logged_in() -> None:
    assert login.looks_logged_in("课程 收件箱 小组 云盘")
    assert not login.looks_logged_in("用户登录 手机号/超星号 学习通密码")
    assert not login.looks_logged_in("")


def test_login_error_message() -> None:
    assert login.login_error_message("密码错误，请重试") == "密码错误"
    assert login.login_error_message("账号不存在") == "账号不存在"
    assert login.login_error_message("验证码错误") == "验证码错误"
    assert login.login_error_message("一切正常") == ""
