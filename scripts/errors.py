# -*- coding: utf-8 -*-
"""
errors.py — 本库的**可预期失败**异常族

为什么单独有这个模块
--------------------
本库原先在这些地方用 `raise SystemExit(...)` 报"环境不齐"：

    report_facts._read        数据文件缺失
    agent_runtime._ensure_llm 缺 DEEPSEEK_API_KEY
    benchmark_engine / report_build / ingest_agent  同上

⚠️ `SystemExit` 继承自 **`BaseException`**，不是 `Exception`。
   而 `server/app.py` 里所有兜底都是 `except Exception`（`app.py` 的
   chat / search / decide / export 等若干处），**一处都拦不住它**。

   后果不是"报错"，而是**请求直接断连**：
     · HTTP 层没有任何响应，浏览器看到的是 connection reset
     · 日志里只有一句 SystemExit，看不出是哪个请求、哪个文件
     · 前端只能显示"网络错误"，把"缺个 CSV"误报成"服务挂了"

   ⇒ 缺数据/缺配置是**可预期**的失败，必须能被捕获、
     能返回一句人话（"缺哪个文件、怎么补"），而不是掀桌子。

两类失败，两种处置
------------------
| 异常 | 含义 | 用户该做什么 |
|:---|:---|:---|
| `DataMissing` | 数据文件不在 | 放数据 / 指对目录 |
| `ConfigError` | 缺 API Key 之类的配置 | 配 `.env` |

两者都继承 `RuntimeError`（进而 `Exception`），所以：
  · 老的 `except Exception` 照样能兜住 → **不引入回归**
  · `app.py` 可以再对这两类单独处理，给出结构化 JSON + 明确指引

用法
----
    from errors import DataMissing, ConfigError
    raise DataMissing(f"raw 文件缺失: {name}", hint="先跑 python scripts/db_build.py")
"""

from __future__ import annotations

from typing import Optional


class AgentError(RuntimeError):
    """
    本库所有"可预期失败"的基类。

    `hint` 是**给人看的下一步**——不要写成"请联系管理员"这种废话，
    要写具体到能照做的命令或路径。
    """

    def __init__(self, message: str, hint: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint or ""

    def as_dict(self) -> dict:
        """给 HTTP 层用的结构化形式。"""
        d = {"error": type(self).__name__, "message": self.message}
        if self.hint:
            d["hint"] = self.hint
        return d


class DataMissing(AgentError):
    """
    数据文件/目录缺失——**最常见的部署事故**。

    典型场景：克隆了代码但没放数据，或数据放在别处。
    因为 `report_facts._read()` 是 6 个看板接口的共同入口，
    任何一个 CSV 缺失会同时打掉 /api/overview、/api/trend、
    /api/heatmap、/api/products、/api/benchmark、/api/decide。
    """


class ConfigError(AgentError):
    """配置缺失（缺 API Key 等）——功能不可用，但服务本身是活的。"""
