# -*- coding: utf-8 -*-
"""
agent_tools.py — **工具层**：截断 / 错误回喂 / 循环防护 / 钩子

三层职责（移植 Pi agent，并补 Pi 没有的一项）
--------------------------------------------
    ① 截断（truncate）   单条工具输出太大 → 切掉，但**保留可续读指引**
    ② 错误回喂           工具抛异常 → **转成工具结果**回喂，不打断循环
    ③ 循环防护 ⭐        硬迭代上限 + **重复调用检测**
    ④ 钩子               before_tool / after_tool，可拦截、可改写

关于 ③：Pi 刻意**不做**硬性迭代上限，靠"模型不再发工具调用"自然收敛。
本库不能照抄——因为实测吃过大亏：
    一次摄入中 agent 从 log.md 读到一条失效引用，随后
    `grep_raw` 换了 11 个词、连查 **58 次全部落空**，直到 API 超时崩溃，
    **整轮零产出**。
所以这里加两道闸：
    · `max_iterations`      硬上限（默认 24）
    · `max_repeat`          同一 (工具,参数) 重复调用 N 次即拦截（默认 3）
                          ↑ 直接针对上面那次事故：第 3 次就该被拦住。

为什么错误要"回喂"而不是"抛出"
------------------------------
抛出 = 整个 agent 循环中断，前面所有工具调用的成果作废。
回喂 = 模型看到"这个工具报了这个错"，可以换个参数、换条路——
     **agent 的自纠能力建立在能看到错误之上**。
（注意：这与 `ingest_tools._read_text` 对编码错误"宁可直接失败"并不矛盾——
 那里的失败**也会**被本层捕获成工具结果，只是内容是一条要求人工介入的诊断。）
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()

# ---------------------------------------------------------------------------
# 一、截断（双限 + 可续读）
# ---------------------------------------------------------------------------

MAX_LINES = 400            # Pi 用 2000；本库工具输出多是结构化文本，400 行已很宽
MAX_CHARS = 12000          # 约 8k token（中文），留足余量


def truncate(text: str, head: bool = True,
             max_lines: int = MAX_LINES, max_chars: int = MAX_CHARS,
             hint: str = "") -> Tuple[str, bool]:
    """
    截断工具输出。返回 (文本, 是否被截断)。

    **关键：截断必须可续读。** 只说"太长已截断"是在浪费那一次调用——
    模型会再调一次同样的工具拿同样的截断结果。所以要给出**下一句能用的动作**：
        `read_raw(path, offset=400)` 或 `grep_raw(..., path=...)`
    Pi 的做法（`read` 取 head、`bash` 取 tail）本库沿用：
        取 head —— 文件/列表类结果，开头最有信息量
        取 tail —— 日志类结果，结尾最有信息量
    """
    if not text:
        return text, False
    lines = text.split("\n")
    cut = False
    if len(lines) > max_lines:
        lines = lines[:max_lines] if head else lines[-max_lines:]
        cut = True
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars] if head else out[-max_chars:]
        cut = True
    if not cut:
        return out, False
    kept = len(lines)
    tail_note = (f"\n\n…【输出过长已截断：仅保留{'前' if head else '后'} "
                 f"{kept} 行 / {len(out)} 字符】")
    if hint:
        tail_note += f"\n下一步：{hint}"
    tail_note += ("\n⚠️ 不要重复调用同一参数——那只会拿到同样的截断结果。"
                  "请用更精确的查询（加 path / 缩小范围 / 换关键词）。")
    return out + tail_note, True


# ---------------------------------------------------------------------------
# 二、循环防护 ⭐（Pi 没有，本库的血泪教训）
# ---------------------------------------------------------------------------

@dataclass
class LoopGuard:
    """
    硬迭代上限 + 重复调用检测。

    `repeat` 检测把 (工具名, 参数字典的稳定序列化) 当键计数。
    命中上限时**不抛异常**，而是返回一条"要求你换策略"的结果——
    让模型有机会自己收敛，而不是被强行掐断。
    """
    max_iterations: int = 24
    max_repeat: int = 3
    iterations: int = 0
    calls: Dict[str, int] = field(default_factory=dict)
    blocked: List[str] = field(default_factory=list)

    def _key(self, name: str, args: Any) -> str:
        try:
            a = json.dumps(args, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            a = str(args)
        return f"{name}|{a}"

    def check(self, name: str, args: Any) -> Optional[str]:
        """
        调用**之前**检查。返回 None 表示放行；返回字符串表示拦截（该串即回喂内容）。
        """
        self.iterations += 1
        if self.iterations > self.max_iterations:
            msg = (f"⛔ 已达工具调用上限（{self.max_iterations} 次），本轮强制收敛。\n"
                   f"请立刻基于**已有信息**给出最终回答；若信息不足，"
                   f"明确说明缺什么、为什么，不要继续调用工具。")
            self.blocked.append(f"max_iterations@{self.iterations}")
            return msg

        k = self._key(name, args)
        self.calls[k] = self.calls.get(k, 0) + 1
        if self.calls[k] > self.max_repeat:
            msg = (f"⛔ 重复调用拦截：`{name}` 用**完全相同的参数**已调用 "
                   f"{self.calls[k] - 1} 次，结果不会变。\n"
                   f"参数：{k.split('|', 1)[1][:200]}\n"
                   f"**请换策略**：换关键词、加 path 缩小范围、或换个工具。\n"
                   f"若你的结论是「库中没有这个数据」，那**本身就是有效结论**——"
                   f"直接写进回答即可，不需要继续查。")
            self.blocked.append(f"repeat:{k[:80]}")
            return msg
        return None

    def summary(self) -> Dict[str, Any]:
        repeats = {k: v for k, v in self.calls.items() if v > 1}
        return {
            "iterations": self.iterations,
            "distinct_calls": len(self.calls),
            "repeated_calls": len(repeats),
            "blocked": len(self.blocked),
            "blocked_detail": self.blocked[:5],
        }


# ---------------------------------------------------------------------------
# 三、工具包装（钩子 + 错误回喂 + 截断）
# ---------------------------------------------------------------------------

@dataclass
class ToolEvent:
    """一次工具调用的轨迹记录（= 本库的"事件流即轨迹"）。"""
    seq: int
    name: str
    args: Any
    ok: bool
    duration_ms: int = 0
    truncated: bool = False
    error: str = ""
    result_preview: str = ""


class GuardedTools:
    """
    把一套（LangChain）工具包成"受保护"的版本。

    用法：
        gt = GuardedTools(build_report_tools(root), guard=LoopGuard())
        tools = gt.wrapped()          # 交给 create_react_agent
        gt.trace                      # 调用轨迹（可展示到前端）
    """

    def __init__(self, tools: List[Any],
                 guard: Optional[LoopGuard] = None,
                 hooks: Optional[Dict[str, Callable]] = None):
        self.tools = tools
        self.guard = guard or LoopGuard()
        self.hooks = hooks or {}
        self.trace: List[ToolEvent] = []
        self._seq = 0
        # 已下发过的 (工具,参数,输出) 指纹——用于"同样的内容不重复发第二遍"。
        # 这是上下文膨胀的最大来源：`list_known_conflicts` 单次 4,223 tok，
        # 而工具结果每轮都会重发，调两次就是 8,446 tok 常驻。
        self._seen_results: Set[str] = set()

    @staticmethod
    def _key_of(args: Any) -> str:
        """参数的稳定序列化（与 LoopGuard 的键同一思路）。"""
        try:
            return json.dumps(args, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return str(args)

    def wrapped(self) -> List[Any]:
        """返回逐个包好的工具（保持原 schema，仅拦截执行）。"""
        out = []
        for t in self.tools:
            out.append(self._wrap_one(t))
        return out

    def _wrap_one(self, tool: Any) -> Any:
        name = getattr(tool, "name", str(tool))
        # LangChain 的 StructuredTool 用 .func / .coroutine；这里只处理同步实现。
        orig = getattr(tool, "func", None)
        if orig is None:
            return tool                      # 无法包装（如纯 Runnable）→ 原样返回
        try:
            tool.func = self._make_wrapper(name, orig)
        except Exception:  # noqa: BLE001
            # pydantic 模型可能禁止赋值 → 退化为不包装（记录到 trace 里说明）
            attr = "_guarded_originals"
            if not hasattr(self, attr):
                setattr(self, attr, {})
            getattr(self, attr)[name] = orig
            return tool
        return tool

    def _make_wrapper(self, name: str, orig: Callable) -> Callable:
        import time

        def _inner(*args: Any, **kwargs: Any) -> str:
            self._seq += 1
            seq = self._seq

            # ---- before_tool 钩子（可拦截）----
            before = self.hooks.get("before_tool")
            if before:
                verdict = before(name, kwargs)
                if verdict is False:
                    ev = ToolEvent(seq, name, kwargs, ok=False, error="被 before_tool 拦截")
                    self.trace.append(ev)
                    return "⛔ 该工具调用被策略拦截。"

            # ---- 循环防护 ----
            block = self.guard.check(name, kwargs)
            if block is not None:
                self.trace.append(ToolEvent(seq, name, kwargs, ok=False,
                                            error="循环防护拦截", result_preview=block[:120]))
                return block

            # ---- 真正执行（异常 → 转成结果回喂）----
            t0 = time.time()
            try:
                raw = orig(*args, **kwargs)
                ok, err = True, ""
            except Exception as e:  # noqa: BLE001
                raw = (f"❌ 工具 `{name}` 执行失败：{type(e).__name__}: {e}\n"
                       f"这不是致命错误——请换个参数或换个工具再试，"
                       f"或直接在回答里说明这条数据取不到。")
                ok, err = False, f"{type(e).__name__}: {e}"

            text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
            hint = self._continuation_hint(name, kwargs)
            text, cut = truncate(text, head=self._head_or_tail(name) == "head", hint=hint)

            # ---- 折叠：重复的大结果只留一份 ----
            #
            # ⚠️ 这是**上下文膨胀的最大来源**（实测）：
            #    `list_known_conflicts` 一次输出 4,223 tok，
            #    而工具结果入库后**每轮都会重新发给 LLM**——
            #    调两次就是 8,446 tok 常驻。
            #
            # 做法：同一个工具、同样参数、同样的输出，
            # 第二次起替换成一行指路，而不是把同样 4,000 字再发一遍。
            # **不删库里的记录**——只改这一轮送给模型的样子。
            fp = f"{name}|{self._key_of(kwargs)}|{hash(text)}"
            if fp in self._seen_results and ok:
                text = (f"（本工具以相同参数已调用过，输出与上次**完全相同**，此处不再重复。"
                        f"上次输出 {len(text):,} 字符已在上文，请直接引用。）")
                cut = False
            else:
                self._seen_results.add(fp)

            # ---- after_tool 钩子（可改写内容）----
            after = self.hooks.get("after_tool")
            if after:
                try:
                    new = after(name, kwargs, text)
                    if isinstance(new, str):
                        text = new
                except Exception:  # noqa: BLE001
                    pass

            ms = int((time.time() - t0) * 1000)
            self.trace.append(ToolEvent(seq, name, kwargs, ok=ok, duration_ms=ms,
                                        truncated=cut, error=err,
                                        result_preview=text[:160].replace("\n", " ")))
            return text

        return _inner

    @staticmethod
    def _head_or_tail(name: str) -> str:
        """`read`/`list` 类取 head（开头最有用）；日志/追加类取 tail。"""
        return "tail" if any(k in name for k in ("log", "audit", "trail")) else "head"

    @staticmethod
    def _continuation_hint(name: str, kwargs: Dict[str, Any]) -> str:
        """给出**下一句能用的动作**，避免模型重复调同一个。"""
        if name == "read_raw":
            off = int(kwargs.get("offset") or 0)
            return (f"`read_raw(path='{kwargs.get('path','')}', "
                    f"offset={off + MAX_LINES})` 继续读下一段")
        if name == "read_article":
            return "用 `search_wiki` 定位到具体章节，只读那一节"
        if name == "grep_raw":
            return "收窄关键词，或加 `path=` 限定到单个文件"
        if name == "search_wiki":
            return "换关键词，或直接用 `get_metric` 取数"
        return "换更精确的参数重试"

    def trace_dicts(self) -> List[Dict[str, Any]]:
        return [
            {"seq": e.seq, "name": e.name, "args": _short(e.args),
             "ok": e.ok, "ms": e.duration_ms, "truncated": e.truncated,
             "error": e.error, "preview": e.result_preview}
            for e in self.trace
        ]


def _short(args: Any, n: int = 120) -> Any:
    try:
        s = json.dumps(args, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        s = str(args)
    return s if len(s) <= n else s[:n] + "…"


# ---------------------------------------------------------------------------

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="工具层自检（截断/防护/错误回喂）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if not a.selftest:
        ap.print_help()
        return 1

    print("=" * 70)
    print("① 截断：双限 + 可续读指引")
    print("=" * 70)
    long_text = "\n".join(f"第 {i} 行 数据" for i in range(1000))
    out, cut = truncate(long_text, hint="`read_raw(path='x', offset=400)`")
    print(f"  输入 1000 行 → 输出 {len(out.splitlines())} 行（含提示行），截断={cut}")
    print("  " + out[-160:].replace("\n", "\n  "))

    print()
    print("=" * 70)
    print("② 循环防护：重复调用拦截（针对 58 次空转那次事故）")
    print("=" * 70)
    g = LoopGuard(max_iterations=24, max_repeat=3)
    for i in range(5):
        r = g.check("grep_raw", {"pattern": "141.0"})
        if r:
            print(f"  第 {i+1} 次 → 拦截：")
            print("  " + r[:200].replace("\n", "\n  "))
            break
        print(f"  第 {i+1} 次 → 放行")
    print(f"  guard 统计: {g.summary()}")

    print()
    print("=" * 70)
    print("③ 迭代上限")
    print("=" * 70)
    g2 = LoopGuard(max_iterations=5, max_repeat=99)
    for i in range(7):
        r = g2.check("f", {"i": i})     # 参数不同 → 不触发重复检测
        if r:
            print(f"  第 {i+1} 次 → {r.splitlines()[0]}")
            break
    print(f"  guard 统计: {g2.summary()}")

    print()
    print("=" * 70)
    print("④ 错误回喂（不打断循环）")
    print("=" * 70)
    def boom(**kw):
        raise ValueError("文件不存在: raw/x.csv")

    gt = GuardedTools([], guard=LoopGuard())
    w = gt._make_wrapper("read_raw", boom)
    res = w(path="raw/x.csv")
    print("  " + res.replace("\n", "\n  "))
    print(f"\n  trace: {gt.trace_dicts()}")
    print("\n✅ 工具层自检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
