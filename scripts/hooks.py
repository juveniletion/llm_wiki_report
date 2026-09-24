# -*- coding: utf-8 -*-
"""
hooks.py — 采集层的**可扩展钩子契约**

设计意图
--------
把「文件从哪来」与「文件怎么入库」解耦：

    入口 (CLI / inbox / 网站拖拽 / 管理后台)
        ↓  都调用同一个 ingest_file()
    采集层 (ingest_raw.py)  ← 确定性：格式转换、加头、落位、去重
        ↓  产出 IngestResult
    钩子 (本文件)           ← 通知外部系统
        ↓
    编译层 (INGEST_AGENT.md) ← 语义：分诊、编译、级联

**入口变了，规范不变。** 未来加 HTTP 上传或后台表单，只需注册一个新 Hook，
`ingest_raw.py` 一行都不用改。

契约
----
- Hook **只接收** `IngestResult`，**不得修改** 已落盘的 raw/ 文件。
- Hook 抛异常**不影响**已落盘结果（采集层会捕获并降级为警告）。
- 所有 Hook 按注册顺序**串行**执行（index/log 是共享状态，不并行）。
"""
from __future__ import annotations

import io
import json
import sys
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional

def ensure_utf8_stdout() -> None:
    """
    确保 stdout 是 UTF-8。**真正幂等**——既看自己的标志，也看当前对象。

    为什么要看当前对象：若调用方（如测试脚本、notebook、上游进程）**已经**把
    stdout 包成了 UTF-8 TextIOWrapper，我们再用 `sys.stdout.buffer` 包一层，
    会让**前一个 wrapper 被 GC 时关闭底层 buffer**，后续所有 print 抛
    `ValueError: I/O operation on closed file`。本库在测试中反复踩到这个坑。

    判据：当前 stdout 已是 TextIOWrapper 且 encoding 为 utf-8 → 什么都不做。
    """
    global _STDOUT_WRAPPED
    if _STDOUT_WRAPPED or sys.platform != "win32":
        return
    cur = sys.stdout
    if isinstance(cur, io.TextIOWrapper) and (cur.encoding or "").lower().replace("-", "") == "utf8":
        _STDOUT_WRAPPED = True
        return
    buf = getattr(cur, "buffer", None)
    if buf is None:
        _STDOUT_WRAPPED = True
        return
    try:
        sys.stdout = io.TextIOWrapper(buf, encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    _STDOUT_WRAPPED = True


_STDOUT_WRAPPED = False
ensure_utf8_stdout()


# =============================================================================
# 一、结果契约（采集层 → 钩子 / 编译层）
# =============================================================================

@dataclass
class IngestResult:
    """一次采集的完整结果。既给钩子用，也是「编译简报」的数据源。"""

    status: str                      # collected | skipped | error
    source: str                      # 原始文件路径或上传文件名
    raw_path: Optional[str] = None   # 落位后的相对路径（相对 wiki 根）
    topic: Optional[str] = None      # 实际落位的主题目录
    is_duplicate: bool = False
    content_hash: str = ""
    size_bytes: int = 0
    derived_text: Optional[str] = None   # 派生文本文件路径（pdf/docx/xlsx 才有）
    cascade_candidates: List[Any] = field(default_factory=list)
    content_preview: str = ""
    log_entry: str = ""
    detected_topic: Optional[str] = None
    topic_confidence: str = "unknown"    # high | low | unknown
    message: str = ""
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


# =============================================================================
# 二、钩子基类
# =============================================================================

class IngestHook:
    """所有钩子的基类。按需覆写你关心的事件，其余留空即可。"""

    name = "IngestHook"

    def on_collected(self, result: IngestResult) -> None:
        """采集成功（raw 已落位）。适合：触发编译、通知前端、推消息。"""

    def on_skipped(self, result: IngestResult) -> None:
        """内容哈希重复，已跳过。适合：告知上传者"这份已经有了"。"""

    def on_error(self, result: IngestResult) -> None:
        """解析或写入失败。适合：告警、记录到后台。"""


# =============================================================================
# 三、预置实现
# =============================================================================

class ConsoleHook(IngestHook):
    """CLI 场景：把结果打印成人能读的样子。"""

    name = "Console"

    def on_collected(self, r: IngestResult) -> None:
        print(f"  ✅ 已采集 {r.source}")
        print(f"     落位: {r.raw_path}  (主题置信度: {r.topic_confidence})")
        if r.derived_text:
            print(f"     派生文本: {r.derived_text}")
        if r.cascade_candidates:
            print(f"     级联候选: {len(r.cascade_candidates)} 篇")
            for c in r.cascade_candidates[:4]:
                terms = "/".join(c.get("terms", [])[:4]) if isinstance(c, dict) else ""
                path = c.get("path", c) if isinstance(c, dict) else c
                print(f"       - {path}   ← {terms}")

    def on_skipped(self, r: IngestResult) -> None:
        print(f"  ⏭️  跳过（内容重复）: {r.source}")
        print(f"     已存在于: {r.raw_path}")

    def on_error(self, r: IngestResult) -> None:
        print(f"  ❌ 失败: {r.source}")
        print(f"     {r.error}")


class FileLogHook(IngestHook):
    """把每次采集追加到 raw/.ingest-audit.jsonl —— 给管理后台做审计用。"""

    name = "FileLog"

    def __init__(self, wiki_root: Path):
        self.path = Path(wiki_root) / "raw" / ".ingest-audit.jsonl"

    def _write(self, r: IngestResult, event: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"event": event, "at": datetime.now().isoformat(timespec="seconds")}
        rec.update(r.to_dict())
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def on_collected(self, r): self._write(r, "collected")
    def on_skipped(self, r):   self._write(r, "skipped")
    def on_error(self, r):     self._write(r, "error")


class CallbackHook(IngestHook):
    """
    程序化入口专用：把一个普通函数包成钩子。

    网站拖拽上传 / 管理后台的典型用法：

        def on_upload(r):
            if r.status == "collected":
                notify_frontend(r.raw_path)     # 刷新页面
                queue_compile(r.to_dict())      # 投递编译任务

        ingest_file(uploaded, hooks=[CallbackHook(on_upload)])
    """

    name = "Callback"

    def __init__(self, fn: Callable[[IngestResult], Any]):
        self.fn = fn

    def _emit(self, r: IngestResult) -> None:
        try:
            self.fn(r)
        except Exception:
            traceback.print_exc()

    on_collected = on_skipped = on_error = _emit


class HttpNotifyHook(IngestHook):
    """
    【预留】把结果 POST 给外部服务（网站后端 / 管理后台）。

    现在只打印，不真的发请求——等网站建好后再实现 `_post`。
    保留它是为了**固化接口形状**：将来接后端时不用改调用方。
    """

    name = "HttpNotify"

    def __init__(self, endpoint: str, token: Optional[str] = None):
        self.endpoint = endpoint
        self.token = token

    def _post(self, payload: dict) -> None:
        # TODO(网站上线后): 用 requests/httpx 实现真实 POST
        print(f"  🌐 [HttpNotify 预留] 将 POST 到 {self.endpoint}: "
              f"{json.dumps(payload, ensure_ascii=False)[:120]}...")

    def on_collected(self, r): self._post({"event": "collected", **r.to_dict()})
    def on_skipped(self, r):   self._post({"event": "skipped", **r.to_dict()})
    def on_error(self, r):     self._post({"event": "error", **r.to_dict()})


# =============================================================================
# 四、注册表
# =============================================================================

_registry: List[IngestHook] = []
_failures: List[str] = []


def register_hook(hook: IngestHook) -> IngestHook:
    _registry.append(hook)
    return hook


def clear_hooks() -> None:
    _registry.clear()
    _failures.clear()


def registered() -> List[str]:
    return [h.name for h in _registry]


def hook_failures() -> List[str]:
    """返回本次运行中被捕获的钩子异常（供调用方判读）。"""
    return list(_failures)


def emit(event: str, result: IngestResult,
         hooks: Optional[List[IngestHook]] = None) -> None:
    """
    向钩子广播 `event`。

    :param hooks: 本次调用专用钩子；不给则用全局注册表。
                  （调用方传了 hooks 就必须收到通知，否则"传入即生效"的直觉会被打破。）

    **任何钩子抛异常都不会中断采集**——已落盘的 raw 是既成事实，
    不能因为通知失败而回滚。
    """
    targets = hooks if hooks is not None else _registry
    for h in targets:
        fn = getattr(h, event, None)
        if fn is None:
            continue
        try:
            fn(result)
        except Exception as e:  # noqa: BLE001
            msg = f"{h.name}.{event}: {type(e).__name__}: {e}"
            _failures.append(msg)
            print(f"  ⚠️  钩子异常（已忽略，不影响已落盘结果）: {msg}")


# =============================================================================
# 五、默认装配
# =============================================================================

def default_hooks(wiki_root: Path, verbose: bool = True) -> List[IngestHook]:
    """CLI 默认：控制台输出 + 审计日志。"""
    hs: List[IngestHook] = [FileLogHook(wiki_root)]
    if verbose:
        hs.insert(0, ConsoleHook())
    return hs
