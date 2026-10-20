# 东软（NEUMOOC）测试夹具

这里的 JSON 全部是**离线夹具**：从协议档案 `docs/research/neusoft.md` 的字段形状
手写出来的最小样本，不是真实抓包。

## 为什么不用真实抓包

`docs/security.md` 把抓包列入「不进 git」清单 —— 真实响应里带学号、姓名、
课程名和有效 token。所以：

- 所有 ID 用假值（`1000000000000000001` 这种位数一致的号）；
- 课程名用「示例课程」；
- token 一律 `fake-token`；
- 需要新场景时**手写**一份，不要贴真实响应。

## 命名

| 前缀 | 归属 |
| --- | --- |
| `study_*` | 刷课模块（resources / study） |
| `homework_*` | 作业模块 |
| `attendance_*` | 考勤模块 |

夹具按模块分前缀，是为了并行开发时不同窗口不会改到同一个文件。

## 用法

```python
import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "neusoft"

def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))
```

或者直接内联在测试里 —— 夹具文件只在这种场景才划算：同一份响应被多个测试复用，
或者它太长（试卷、资源树）以至于内联会淹没断言。
