# -*- coding: utf-8 -*-
"""
agent_runtime.py — **主 agent 运行时**（记忆 + 压缩 + 工具 + 事件流）

装配关系
--------
```
                    ┌──────────────────────────────────────────┐
   用户提问 ──────▶ │ agent_runtime.AgentSession                │
                    │                                          │
                    │  记忆   agent_memory.Memory   ← 权威区   │
                    │  压缩   agent_compact.Compactor          │
                    │  工具   agent_tools.GuardedTools         │
                    │  数据   report_tools.build_report_tools  │
                    └──────────────────────────────────────────┘
                                     │ 事件流
                     ┌───────────────┼───────────────┐
                     ▼               ▼               ▼
                   CLI            FastAPI/SSE      测试
```

设计要点（Pi agent 的思路 + 本库的修正）
----------------------------------------
1. **长期记忆**：Pi 只持久化 session（无跨会话记忆）。本库的 SQLite 权威区
   早就为 it 留了表，所以：会话/消息**落库**，偏好**跨会话**。
2. **压缩**：每轮开工前检查 token，超阈值则"摘要远期 + 保留近端原文"。
   ⚠️ **压缩不删消息**——只更新 `conversations.context.summary_upto`。
3. **工具**：包一层 GuardedTools，得到截断/错误回喂/循环防护/钩子。
4. **事件流**：`chat()` 是**生成器**，逐个 yield 事件。这样 CLI、SSE、测试
   三方共用同一份逻辑——**前端不需要懂 agent 的内部结构**。
5. **与摄入 agent 的分工不变**：本 agent **只读知识库**。它写的是
   自己的记忆（权威区），不是 wiki。

用法
----
    from agent_runtime import AgentSession
    s = AgentSession.open(external_id="cli")
    for ev in s.chat("银黄口服液 5 月为什么涨？"):
        print(ev["type"], ev.get("text") or ev.get("name"))
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()

HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = HERE.parent

# ---------------------------------------------------------------------------
# 系统提示：在 report_agent 原版基础上，**注入记忆与偏好**
# ---------------------------------------------------------------------------

BASE_SYSTEM = """你是制药企业成本分析的**报告撰写专家**，服务于 2026 年重庆 AI 大赛赛题。

## 你的资料边界（重要）

你**只能**依据知识库（`{root}`）的内容作答。你没有别的数据源，
**不得**用训练知识补充本厂的具体数字。

## 三条硬性交付要求

1. **每个数字必须带证据坐标**，格式 `[词条名.md:章节名]`。
   取数用 `get_metric`（它显示全部来源）；写之前先 `search_wiki` 定位章节。

2. **已知冲突必须显式说明，不得含糊带过。**
   涉及数值时**先调 `list_known_conflicts`**。有争议就写出"两种来源分别为…"
   并说明取数准则。**不得取平均，不得只引一方。**

3. **数据缺失就明说。** 库中没有就说"知识库中无此数据"并说明缺什么。
   **严禁编造。**

## 效率纪律（本库的教训）

- **查不到是有效结论。** 同一目标连续 3 次未命中就停，直接说明"库中无此数据"。
  **不要反复换关键词重试**——实测有过连查 58 次全部落空、最终零产出的先例。
- 一次工具调用能解决的就不要拆成十次。先 `search_wiki`，再决定要不要 `read_article`。

## 回答长度

**按问作答，不要过度展开。**
- 简单事实问题 → 一句话结论 + 坐标 + 必要的约束提醒（3-5 行）
- 归因/分析问题 → 结构化短答（结论 → 依据 → 争议提示）
- 只有明确要求"生成报告/详细分析"时才写长文
"""


def _extract_section(txt: str, anchor: str, max_chars: int = 5000) -> str:
    i = txt.find(anchor)
    if i < 0:
        return ""
    level = len(anchor) - len(anchor.lstrip("#"))
    rest = txt[i + len(anchor):]
    stop = len(rest)
    for m in re.finditer(r"\n(#{2,6}) ", rest):
        if len(m.group(1)) <= level:
            stop = m.start()
            break
    return re.split(r"\n---\s*\n", rest[:min(stop, max_chars)])[0].strip()


SKILL_SECTIONS = ["## 二、铁律", "## 三、证据不变量", "## 六、Status 块",
                  "## 九、领域硬约束", "## 十、硬约束清单"]


def load_constraints(root: Path) -> str:
    p = root / "SKILL.md"
    if not p.exists():
        return ""
    txt = p.read_text(encoding="utf-8")
    out = [f"{a}\n{s}" for a in SKILL_SECTIONS
           if (s := _extract_section(txt, a))]
    return "\n\n".join(out)


def build_system_prompt(root: Path, prefs: Optional[Dict[str, Any]] = None,
                        summary: str = "", summary_upto: int = 0) -> str:
    """
    拼系统提示。**偏好与摘要都进系统提示**——它们跨轮次有效，
    比伪装成 user 消息更不容易被模型当成"用户刚说的"。
    """
    p = BASE_SYSTEM.format(root=root)
    cons = load_constraints(root)
    if cons:
        p += "\n\n## 知识库规则（必读章节）\n\n" + cons
    if prefs:
        lines = [f"- {k}：{v}" for k, v in prefs.items()]
        p += ("\n\n## 该用户的长期偏好（跨会话记住的，无需再问）\n\n"
              + "\n".join(lines))
    return p


def _load_env(root: Path) -> None:
    try:
        from dotenv import load_dotenv
        for d in [root, *root.parents]:
            e = d / ".env"
            if e.exists():
                load_dotenv(dotenv_path=e)
                return
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------

class AgentSession:
    """一次可持久化的 agent 会话。`chat()` 产出事件流。"""

    def __init__(self, memory: Any, conv_id: int, user_id: int,
                 root: Path, model: Optional[str] = None,
                 compactor: Any = None,
                 personal: Optional[Path] = None):
        self.mem = memory
        self.conv_id = conv_id
        self.user_id = user_id
        self.root = root
        # 该用户的个人工作区（`data/users/<id>/ws`）。
        # ⚠️ 与 `root` 是两个不同的根，**不能合并**：
        #    root 放规范/scripts，永不接受用户输入；
        #    personal 只放他自己的 wiki/raw。
        self.personal = Path(personal) if personal else None
        self.model = model
        self.compactor = compactor
        self._llm = None
        self._agent = None
        self._tools = None

    # ---- 构造 ---------------------------------------------------------
    @classmethod
    def open(cls, external_id: str = "cli", root: Optional[Path] = None,
             model: Optional[str] = None, new: bool = False,
             title: str = "", db: Optional[str] = None,
             personal: Optional[Path] = None) -> "AgentSession":
        """
        打开某个用户的会话。

        ⚠️ 记忆落在**该用户自己的库**（`data/users/<id>.db`）——
           不同用户的对话物理隔离在不同文件里，读到别人的数据在文件层面不可能发生。

        `personal` 是该用户的**个人工作区**（`data/users/<id>/ws`）。
           给了它，检索范围 = 公司共享基线 + 他自己的资料（个人同名覆盖共享）。
           ⚠️ **必须只传调用者自己的工作区**——传成别人的就是跨用户泄漏。

        `db` 仅用于测试/迁移时直接指定库文件；常规调用不要传。
        """
        from agent_memory import Memory
        from agent_compact import Compactor

        # ⚠️ 必须走 `db_path=` 关键字。旧写法 `Memory(db or DEFAULT_DB)` 会把
        #    路径当成**第一个位置参数（用户标识）**，生成
        #    `users/C__Users_..._local.db.db` 这种畸形文件——而且它能正常读写、
        #    不报任何错，只有翻磁盘才发现。
        mem = Memory(db_path=db) if db else Memory(external_id)
        uid = mem.ensure_user(external_id, name=external_id)
        cid = None if new else mem.active_conversation(uid, kind="chat")
        if cid is None:
            cid = mem.new_conversation(uid, title=title or "成本分析问答")
        return cls(mem, cid, uid, Path(root or DEFAULT_ROOT),
                   model=model, compactor=Compactor(),
                   personal=personal)

    # ---- 惰性构建 LLM / agent -----------------------------------------
    def _ensure_llm(self) -> Any:
        if self._llm is not None:
            return self._llm
        _load_env(self.root)
        from langchain_openai import ChatOpenAI
        import httpx
        key = os.getenv("DEEPSEEK_API_KEY", "")
        if not key:
            raise SystemExit("未找到 DEEPSEEK_API_KEY（应在工作区根目录的 .env）")
        self._llm = ChatOpenAI(
            model=self.model or os.getenv("AGENT_MODEL", "deepseek-chat"),
            api_key=key,
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            temperature=0.0,
            http_client=httpx.Client(headers={"Accept-Encoding": "identity"}),
            http_async_client=httpx.AsyncClient(headers={"Accept-Encoding": "identity"}),
        )
        return self._llm

    def build_tools(self, max_iterations: int = 24, max_repeat: int = 3) -> Any:
        """构建受保护的工具集（幂等）。"""
        if self._tools is not None:
            return self._tools
        from report_tools import build_report_tools
        from agent_tools import GuardedTools, LoopGuard
        # ⚠️ 必须把 personal 传下去：不传的话，用户自己上传的资料
        #    **检索不到**（agent 只读共享基线）——上传功能等于白做。
        gt = GuardedTools(build_report_tools(self.root, overlay=self.personal),
                          guard=LoopGuard(max_iterations=max_iterations,
                                          max_repeat=max_repeat))
        self._tools = gt
        return gt

    def _ensure_agent(self) -> Any:
        if self._agent is not None:
            return self._agent
        from langgraph.prebuilt import create_react_agent
        from agent_compact import estimate_tokens
        gt = self.build_tools()
        prefs = self.mem.all_prefs(self.user_id)
        ctx = self.mem.get_context(self.conv_id)
        prompt = build_system_prompt(self.root, prefs,
                                     ctx.get("summary", ""),
                                     int(ctx.get("summary_upto", 0)))
        wrapped = gt.wrapped()
        self._agent = create_react_agent(model=self._ensure_llm(),
                                         tools=wrapped, prompt=prompt)

        # ⚠️ 把**系统提示 + 工具 schema** 的常驻占用回填给压缩器。
        #    它们每轮都发，必须计入预算；否则压缩触发时窗口已被吃掉九成
        #    （实测：只算历史时，触发点占用 92% 窗口，只剩 5k tok 给输出）。
        if self.compactor is not None:
            tool_schema = sum(estimate_tokens(
                f"{getattr(t, 'name', '')}{getattr(t, 'description', '')}")
                for t in wrapped)
            self.compactor.system_prompt_tokens = estimate_tokens(prompt) + tool_schema
        return self._agent

    # ---- 上下文准备（记忆 → 精简消息）--------------------------------
    def _prepare_context(self) -> List[Dict[str, Any]]:
        """
        取历史 + 必要时压缩，返回给 LLM 的消息。
        压缩是**有副作用的**（写 conversations.context），但**不删消息**。
        """
        from agent_compact import build_context, estimate_messages

        msgs = self.mem.load(self.conv_id)
        ctx = self.mem.get_context(self.conv_id)
        summary = ctx.get("summary", "")
        upto = int(ctx.get("summary_upto", 0))
        pending = [m for m in msgs if int(m.get("id", 0)) > upto]

        # 只看**未被摘要覆盖**的部分是否超阈值
        if self.compactor and self.compactor.needed(pending):
            self.compactor.llm = self._ensure_llm()
            rep = self.compactor.compact(pending, previous_summary=summary)
            if rep:
                self.mem.save_summary(self.conv_id, rep["summary"],
                                      rep["upto_message_id"], rep["tokens_before"])
                summary, upto = rep["summary"], rep["upto_message_id"]
                self._last_compaction = rep
            else:
                self._last_compaction = None
        else:
            self._last_compaction = None

        return build_context(self.mem.load(self.conv_id), summary, upto)

    # ---- 主入口：事件流 ----------------------------------------------
    def chat(self, question: str, verbose: bool = False) -> Generator[Dict[str, Any], None, None]:
        """
        产出一串事件。**每种事件都有 `type`**：
            start       开始（含会话 id、是否触发了压缩）
            compact     发生了压缩
            tool_call   要调工具（name/args）
            tool_result 工具返回（name/ok/truncated/preview）
            token       回答正文的一个片段
            done        结束（含完整回答、工具统计）
            error       出错
        调用方（CLI / SSE / 测试）只需按 type 分发。
        """
        yield {"type": "start", "conversation_id": self.conv_id,
               "question": question}

        self.mem.append(self.conv_id, "user", question)

        try:
            history = self._prepare_context()
        except Exception as e:  # noqa: BLE001
            yield {"type": "error", "message": f"上下文准备失败：{e}"}
            return

        if getattr(self, "_last_compaction", None):
            r = self._last_compaction
            yield {"type": "compact", "summarized": r["summarized"],
                   "kept": r["kept"], "tokens_before": r["tokens_before"],
                   "text": r["summary"]}

        try:
            agent = self._ensure_agent()
        except Exception as e:  # noqa: BLE001
            yield {"type": "error", "message": f"agent 构建失败：{e}"}
            return

        # 历史（含刚追加的 user 消息）：转成 LangChain 的 (role, content) 元组
        lc_msgs = []
        for m in history:
            role = m.get("role", "user")
            if role == "tool":
                continue                       # 工具调用由 agent 自己重放，不回灌
            lc_msgs.append((role if role in ("user", "assistant", "system") else "user",
                            str(m.get("content") or "")))
        if not lc_msgs or lc_msgs[-1][1] != question:
            lc_msgs.append(("user", question))

        gt = self.build_tools()
        before = len(gt.trace)
        final = ""
        crashed = None
        try:
            for step in agent.stream({"messages": lc_msgs}, stream_mode="updates"):
                for _node, out in step.items():
                    for msg in out.get("messages", []):
                        tc = getattr(msg, "tool_calls", None)
                        if tc:
                            for c in tc:
                                yield {"type": "tool_call", "name": c["name"],
                                       "args": c.get("args")}
                        elif msg.type == "tool":
                            # 从 trace 里取最近一条未播报的，拿 ok/truncated
                            ev = next((e for e in reversed(gt.trace)
                                       if e.name and e.seq > 0), None)
                            yield {"type": "tool_result",
                                   "name": getattr(msg, "name", ""),
                                   "ok": getattr(ev, "ok", True) if ev else True,
                                   "truncated": getattr(ev, "truncated", False) if ev else False,
                                   "preview": str(msg.content)[:300].replace("\n", " ")}
                        elif msg.type == "ai" and msg.content:
                            final = msg.content
        except Exception as e:  # noqa: BLE001
            crashed = f"{type(e).__name__}: {e}"
            yield {"type": "error", "message": f"agent 运行中断：{crashed}"}

        if final:
            # 按段 yield，前端可以流式渲染
            for piece in _chunks(final, 200):
                yield {"type": "token", "text": piece}

        self.mem.append(self.conv_id, "assistant", final or f"（未产出回答：{crashed}）")
        if self.mem.count(self.conv_id) <= 2:
            self.mem.set_title(self.conv_id, question[:40])

        yield {"type": "done", "answer": final, "error": crashed or "",
               "guard": gt.guard.summary(),
               "trace": gt.trace_dicts()[before:]}

    # ---- 收尾 ---------------------------------------------------------
    def remember_pref(self, key: str, value: Any) -> None:
        self.mem.set_pref(self.user_id, key, value)


def _chunks(text: str, n: int) -> Generator[str, None, None]:
    for i in range(0, len(text), n):
        yield text[i:i + n]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="主 agent 运行时（记忆 + 压缩 + 工具）")
    ap.add_argument("question", nargs="*")
    ap.add_argument("-q", "--query")
    ap.add_argument("--user", default="cli", help="外部用户标识")
    ap.add_argument("--new", action="store_true", help="新开一个会话")
    ap.add_argument("--db", help="记忆库路径（默认 F:\\llm-wiki.db）")
    ap.add_argument("--model")
    ap.add_argument("--show-memory", action="store_true", help="打印会话与摘要")
    ap.add_argument("--set-pref", nargs=2, metavar=("KEY", "VALUE"),
                    help="写入一条跨会话偏好，然后退出")
    ap.add_argument("--context-window", type=int, default=64000,
                    help="上下文窗口（调小便于实测压缩，默认 64000）")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    if a.set_pref:
        sess0 = AgentSession.open(a.user, model=a.model, db=a.db)
        v = a.set_pref[1]
        try:
            v = json.loads(v)          # 允许 JSON 值（列表/数字）
        except json.JSONDecodeError:
            pass
        sess0.remember_pref(a.set_pref[0], v)
        print(f"✅ 已记住 {a.user} 的偏好 {a.set_pref[0]} = {v!r}")
        return 0

    sess = AgentSession.open(a.user, model=a.model, new=a.new, db=a.db)
    from agent_compact import Compactor
    sess.compactor = Compactor(context_window=a.context_window,
                               reserve_tokens=max(200, a.context_window // 8),
                               keep_recent_tokens=max(500, a.context_window // 8))

    if a.show_memory:
        print(f"用户 {a.user} · 会话 #{sess.conv_id}")
        for c in sess.mem.list_conversations(sess.user_id):
            print(f"  #{c['id']:<4} {c['title'][:36]:<36} {c['n_msg']:>3} 条  {c['updated_at']}")
        ctx = sess.mem.get_context(sess.conv_id)
        if ctx.get("summary"):
            print(f"\n压缩摘要（覆盖至消息 #{ctx['summary_upto']}，共压缩 "
                  f"{ctx['compactions']} 次）:\n{ctx['summary'][:600]}")
        print(f"\n偏好: {sess.mem.all_prefs(sess.user_id)}")
        return 0

    q = a.query or " ".join(a.question)
    if not q:
        ap.print_help()
        return 1

    print(f"❓ {q}\n" + "=" * 70)
    for ev in sess.chat(q, verbose=not a.quiet):
        t = ev["type"]
        if t == "start":
            if not a.quiet:
                print(f"  会话 #{ev['conversation_id']}")
        elif t == "compact":
            print(f"  🗜  上下文压缩：{ev['summarized']} 条→摘要，保留 {ev['kept']} 条原文"
                  f"（压缩前 {ev['tokens_before']} tokens）")
        elif t == "tool_call" and not a.quiet:
            print(f"  🔧 {ev['name']}({str(ev['args'])[:90]})")
        elif t == "tool_result" and not a.quiet:
            mark = "✓" if ev["ok"] else "✗"
            extra = " [截断]" if ev["truncated"] else ""
            print(f"     {mark}{extra} {ev['preview'][:90]}")
        elif t == "token":
            print(ev["text"], end="", flush=True)
        elif t == "error":
            print(f"\n  ⚠️ {ev['message']}")
        elif t == "done":
            print()
            g = ev["guard"]
            print(f"\n{'-'*70}")
            print(f"  工具调用 {g['iterations']} 次 · 不同调用 {g['distinct_calls']} · "
                  f"拦截 {g['blocked']}")
            if ev.get("error"):
                print(f"  ⚠️ 中断：{ev['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
