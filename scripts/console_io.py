# -*- coding: utf-8 -*-
"""
console_io.py — 共享的 I/O 工具

**为什么存在这个文件**

Windows 控制台默认 GBK，输出中文/emoji 会抛 `UnicodeEncodeError`。
常见做法是 `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, ...)`。

但这个「修复」有个陷阱：**每包装一次，就多一层 wrapper；
前一层被 GC 时会把底层 buffer 一起关掉**，之后所有 `print` 抛
`ValueError: I/O operation on closed file`。

本库曾在 `hooks.py` 与 `retrieval.py` **各写过一遍**同样的包装逻辑，
于是**同一个 bug 犯了两次**。抽到这里，保证只有一处实现。

**正确做法：无状态判断** —— 只看当前 `sys.stdout` 的类型与编码，
不看任何模块级标志。这样任意模块、任意次数调用都安全。
"""
from __future__ import annotations

import io
import sys


def ensure_utf8_stdout() -> bool:
    """
    确保 stdout 输出 UTF-8。**幂等，无状态，可安全重复调用。**

    返回 True 表示"当前已是 UTF-8"（无论是本函数设置的还是调用方已设置的）。

    判据（三条，任一成立即不包装）：
      1. 非 Windows —— 无需处理
      2. 当前 stdout 已是 UTF-8 的 TextIOWrapper
      3. 当前 stdout 没有 `buffer` 属性（如已被重定向到 StringIO）
    """
    if sys.platform != "win32":
        return True

    cur = sys.stdout
    if isinstance(cur, io.TextIOWrapper):
        enc = (cur.encoding or "").lower().replace("-", "")
        if enc == "utf8":
            return True

    buf = getattr(cur, "buffer", None)
    if buf is None:
        return False

    try:
        sys.stdout = io.TextIOWrapper(buf, encoding="utf-8", errors="replace")
        return True
    except (AttributeError, ValueError):
        return False


if __name__ == "__main__":
    # 自检：连续调用 3 次不应破坏 stdout
    for i in range(3):
        ensure_utf8_stdout()
    print("ensure_utf8_stdout 幂等性自检通过（连续调用 3 次仍可输出）")
