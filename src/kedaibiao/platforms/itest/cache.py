"""iTEST 控制台缓存的落盘位置。

取卷是**不可逆**的（一进作答页就开始计时），所以试卷只在「取卷」动作里拿一次，
之后 AI 答题 / 填答案 / 交卷都读这份缓存。缓存放在运行期数据目录（``data/`` 已被
git 忽略），按考试号分文件：

```
data/itest/paper-<考试号>.json     试卷结构（section → question → 槽位）
data/itest/answers-<考试号>.json   答案单（qid → 字母串 / 整句译文）
data/itest/*.png                   截图（排查用）
```

为什么不用数据库：这些是**一次性中间产物**，交完卷就该丢；放文件比建表简单，
也方便人直接打开看。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def cache_dir(settings: Any) -> Path:
    """``<session_dir 的父目录>/itest`` —— 与截图、cookie 同处运行期数据目录。"""
    session_dir = Path(getattr(settings, "session_dir", "./data/sessions") or "./data/sessions")
    return session_dir.parent / "itest"


def paper_path(settings: Any, exam_id: str) -> Path:
    return cache_dir(settings) / f"paper-{exam_id}.json"


def answers_path(settings: Any, exam_id: str) -> Path:
    return cache_dir(settings) / f"answers-{exam_id}.json"


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json(path: Path, payload: dict[str, Any]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return True
    except OSError:
        return False
