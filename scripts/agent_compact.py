# -*- coding: utf-8 -*-
"""
agent_compact.py — **上下文压缩**（移植 Pi agent 的设计，并修正其两处不适用处）

Pi 的本质设计（照搬）
--------------------
1. **压缩不是删历史**，而是"摘要远期 + 保留近端原文"两段式。
2. **切点只在消息边界**，绝不切开 `tool_call` 与它的 `tool_result`——
   切开会让模型看到"调了工具却没有结果"，行为会失稳。
3. **结构化摘要**（目标/约束/已做/结论/待办/关键上下文），而非自由发挥。
4. **溢出恢复只允许一次**：防"压缩 → 仍超窗 → 再压缩"死循环。
5. token 估算 = **provider 真实 usage 锚点 + 尾部启发式估算**。

修正 Pi 的两处（移植时必须改）
------------------------------
① **`chars/4` 是英文启发式，中文会严重低估。**
   本库是中文库，照搬会导致"以为没满、其实早超了"。
   改为 CJK 感知：中文字符按 ~1/1.5 token 计，ASCII 按 1/4。
② Pi 无硬性迭代上限（靠"模型不再发工具调用"收敛）。
   本库实测吃过亏——**agent 连查 58 次 grep_raw 全部落空、零产出后崩溃**。
   所以这里配一个 `max_tool_iterations`（见 agent_runtime.py）。

截断与压缩的分工
----------------
    截断（单条工具输出太大）  → agent_tools.py   局部、可续读
    压缩（整体历史太长）      → 本模块          全局、有损但结构化
两者互补：截断治"一条太胖"，压缩治"整体太长"。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()

# ---------------------------------------------------------------------------
# 一、token 估算（CJK 感知 —— 修正 Pi 的英文启发式）
# ---------------------------------------------------------------------------

_CJK = re.compile(r"[　-鿿＀-￯一-鿿]")

# 为什么是这两个数：
#   英文经验值 ~4 字符/token；中文在多数 BPE 词表里 ~1~1.5 字符/token。
#   取 1.5 是**偏保守**（宁可高估、早压缩，也不要低估到爆窗）。
#   两个常数都做成可调，换模型/换 tokenizer 时改这里即可。
ASCII_PER_TOKEN = 4.0
CJK_PER_TOKEN = 1.5


def estimate_tokens(text: str) -> int:
    """混合文本的 token 估算。中文按 1.5 字符/token，ASCII 按 4 字符/token。"""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    ascii_n = len(text) - cjk
    return int(cjk / CJK_PER_TOKEN + ascii_n / ASCII_PER_TOKEN) + 1


def estimate_messages(msgs: List[Dict[str, Any]]) -> int:
    """估算一组 DB 消息的 token 总量（含工具名与入参）。"""
    total = 0
    for m in msgs:
        total += estimate_tokens(str(m.get("content") or ""))
        if m.get("tool_name"):
            total += estimate_tokens(m["tool_name"])
        if m.get("tool_args"):
            total += estimate_tokens(str(m["tool_args"]))
        total += 4                      # 每条消息的角色/结构开销
    return total


# ---------------------------------------------------------------------------
# 二、切点查找 —— 绝不切开 tool_call / tool_result 对
# ---------------------------------------------------------------------------

def find_cut_index(msgs: List[Dict[str, Any]], keep_recent_tokens: int) -> int:
    """
    从尾部向前累积，直到超过 `keep_recent_tokens`，返回**可安全切分的位置**。

    安全切点只有一种：**一条 `user` 消息之前**。
    理由：一条 user 消息往后的内容构成一个完整的"回合"
    （user → [assistant 调工具 → tool 结果]* → assistant 回答）。
    若切在回合中间，模型会看到无主的工具结果或悬挂的工具调用，行为会失稳。

    返回 0 表示"无需压缩"或"找不到安全切点"（宁可不压，也不切坏）。
    """
    if not msgs:
        return 0
    acc = 0
    for i in range(len(msgs) - 1, -1, -1):
        acc += estimate_messages([msgs[i]])
        if acc > keep_recent_tokens:
            # 从 i 往前找最近的一条 user 消息，切在它之前
            for j in range(i, -1, -1):
                if msgs[j].get("role") == "user":
                    return j
            return 0                    # 前面没有 user 消息了 → 不切
    return 0


def is_safe_boundary(msgs: List[Dict[str, Any]], idx: int) -> bool:
    """校验 idx 是安全切点：前面不悬空 tool 结果、后面不以 tool 结果开头。"""
    if idx <= 0 or idx >= len(msgs):
        return False
    if msgs[idx].get("role") != "user":
        return False
    return True


# ---------------------------------------------------------------------------
# 三、结构化摘要（LLM）
# ---------------------------------------------------------------------------

SUMMARY_SYSTEM = """你是会话历史的摘要员。你的输出**取代**一段不再可见的对话历史。

铁律：
1. **你不是在回答问题**，不要继续对话、不要提问、不要客套。
2. **必须逐字保留**所有具体数值、证据坐标 `[文件:列:筛选]`、词条路径、结论。
   数字被概括成"有所上升"是不可接受的——那是信息丢失。
3. 已知的口径争议（Disputed）必须原样保留，不得抹平。
4. 严格按给定小节输出，不要增删小节。
5. 全程中文。
"""

SUMMARY_TEMPLATE = """请把下面这段会话历史压缩成结构化摘要。

## 格式（严格照此，小节名不要改）

### 目标
用户想要什么（一句话）

### 关键约束
本次对话中确立的、后续仍需遵守的规则（没有则写"无"）

### 已确认的事实
逐条列出**带坐标的**数值结论。格式：`<结论>` `<坐标>`
（这是最重要的一节——数值与坐标必须逐字保留）

### 已排除/存疑
已被否证的说法、标注为 Disputed 的口径

### 已生成的产物
报告文件路径、图表、表结构等

### 当前进度
已问到哪、接下来可能要问什么

## 已有的摘要（若有，请合并而非重写）

{previous}

## 需要压缩的历史

{history}
"""


def _render_history(msgs: List[Dict[str, Any]], max_chars: int = 20000) -> str:
    """把 DB 消息渲染成给摘要员看的文本。"""
    out: List[str] = []
    for m in msgs:
        role = m.get("role", "?")
        content = str(m.get("content") or "")
        if m.get("tool_name"):
            args = str(m.get("tool_args") or "")[:200]
            out.append(f"[工具 {m['tool_name']}({args})]\n{content[:1500]}")
        else:
            out.append(f"[{role}]\n{content[:2500]}")
    text = "\n\n".join(out)
    if len(text) > max_chars:
        # 摘要员的输入也有限度——超长时保留**头尾**（目标在头、最新在尾）
        head, tail = text[:max_chars // 2], text[-max_chars // 2:]
        text = head + "\n\n…（中间省略）…\n\n" + tail
    return text


def make_summary(llm: Any, msgs: List[Dict[str, Any]],
                 previous: str = "") -> str:
    """调用 LLM 生成结构化摘要。失败时返回**降级摘要**（不抛异常）。"""
    prompt = SUMMARY_TEMPLATE.format(
        previous=previous or "（无）",
        history=_render_history(msgs),
    )
    try:
        r = llm.invoke([("system", SUMMARY_SYSTEM), ("user", prompt)])
        txt = (r.content or "").strip()
        if txt:
            return txt
    except Exception as e:  # noqa: BLE001
        # 摘要失败**不能**让整轮对话崩掉——降级为机械摘要
        return (f"### 目标\n（摘要生成失败：{type(e).__name__}）\n\n"
                f"### 已确认的事实\n"
                f"（自动降级：以下为被压缩消息的原文摘录，未经结构化整理）\n"
                + "\n".join(f"- [{m.get('role')}] {str(m.get('content') or '')[:160]}"
                            for m in msgs[-12:]))
    return ""


# ---------------------------------------------------------------------------
# 四、压缩器
# ---------------------------------------------------------------------------

class Compactor:
    """
    触发条件：**总占用**（系统提示 + 历史）> `窗口 − 预留`。

    ⚠️ **系统提示必须计入预算**（实测踩过）：
       第一版只算历史，触发线设在 56,000。而系统提示（规则 + 偏好 + 摘要说明）
       常驻约 3,000 tok，于是触发那一刻的真实占用是 56,000 + 3,000 = 59,000，
       已吃掉 64k 窗口的 **92%**——只剩 5,000 tok 给当轮输出。
       而 DeepSeek 的**输入与输出共享同一窗口**，这意味着：
       一轮长报告很可能**先撞输出上限**，而不是先触发压缩。
       现在把系统提示算进分母，触发线自然前移。

    其余参数：保留近端 12k 原文（远端的压缩成摘要）。
    """

    def __init__(self, llm: Any = None,
                 context_window: int = 64000,
                 reserve_tokens: int = 8000,
                 keep_recent_tokens: int = 12000,
                 system_prompt_tokens: int = 0):
        self.llm = llm
        self.context_window = context_window
        self.reserve_tokens = reserve_tokens
        self.keep_recent_tokens = keep_recent_tokens
        # 常驻占用（系统提示 + 工具 schema）。由运行时在构建 agent 后回填。
        self.system_prompt_tokens = system_prompt_tokens
        self.overflow_used = False      # ⚠️ 溢出恢复只允许一次（防死循环）

    def threshold(self) -> int:
        """历史部分可用的上限 —— 已扣掉系统提示与预留。"""
        return self.context_window - self.reserve_tokens - self.system_prompt_tokens

    def total_usage(self, msgs: List[Dict[str, Any]]) -> int:
        """系统提示 + 历史的真实占用，用于日志与诊断。"""
        return self.system_prompt_tokens + estimate_messages(msgs)

    def needed(self, msgs: List[Dict[str, Any]]) -> bool:
        return estimate_messages(msgs) > self.threshold()

    def should_retry_after_overflow(self) -> bool:
        """provider 报超窗时，允许把保留量再压小重试——**仅一次**。"""
        if self.overflow_used:
            return False
        self.overflow_used = True
        self.keep_recent_tokens = max(2000, self.keep_recent_tokens // 2)
        self.reserve_tokens = max(1000, self.reserve_tokens // 2)
        return True

    def split(self, msgs: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]],
                                                        List[Dict[str, Any]]]:
        """切成 (待摘要, 保留原文)。找不到安全切点就返回 ([], 全部)。"""
        cut = find_cut_index(msgs, self.keep_recent_tokens)
        if cut <= 0 or not is_safe_boundary(msgs, cut):
            return [], list(msgs)
        return list(msgs[:cut]), list(msgs[cut:])

    def compact(self, msgs: List[Dict[str, Any]],
                previous_summary: str = "") -> Optional[Dict[str, Any]]:
        """
        压缩一次。返回 {"summary","upto_message_id","tokens_before",
                      "summarized": n, "kept": n}；无需压缩时返回 None。
        """
        if not self.needed(msgs):
            return None
        to_sum, keep = self.split(msgs)
        if not to_sum:
            return None                  # 找不到安全切点 → 不压（宁可超窗也不切坏）
        tokens_before = estimate_messages(msgs)
        summary = make_summary(self.llm, to_sum, previous_summary)
        if not summary:
            return None
        return {
            "summary": summary,
            "upto_message_id": int(to_sum[-1]["id"]),
            "tokens_before": tokens_before,
            "summarized": len(to_sum),
            "kept": len(keep),
        }


# ---------------------------------------------------------------------------
# 五、把摘要 + 近端原文拼成给 LLM 的消息列表
# ---------------------------------------------------------------------------

SUMMARY_PREFIX = ("【以下是你之前这段对话的摘要，原文已不在上下文中。"
                  "摘要里的数值与坐标是逐字保留的，可直接引用。】")

NO_MATERIAL_HINT = "（无——本次对话尚未产生需要保留的结论）"


def build_context(msgs: List[Dict[str, Any]], summary: str = "",
                  summary_upto: int = 0) -> List[Dict[str, Any]]:
    """
    返回送给 LLM 的**精简消息列表**：`[摘要] + 摘要之后的原文`。
    摘要以一条 `system` 消息注入（而不是伪装成 user），语义更清楚。
    """
    kept = [m for m in msgs if int(m.get("id", 0)) > int(summary_upto or 0)]
    out: List[Dict[str, Any]] = []
    if summary:
        out.append({"role": "system",
                    "content": f"{SUMMARY_PREFIX}\n\n{summary}"})
    out.extend(kept)
    return out


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="上下文压缩（token 估算 / 切点 / 摘要）")
    ap.add_argument("--text", help="估算一段文本的 token")
    ap.add_argument("--selftest", action="store_true", help="跑内置自检")
    a = ap.parse_args()

    if a.text:
        print(f"{estimate_tokens(a.text)} tokens  （{len(a.text)} 字符）")
        return 0

    if a.selftest:
        # 1) 中英文估算不该差 5 倍以上（Pi 的 /4 会让中文严重低估）
        zh = "银黄口服液单位成本环比上涨百分之二点八四" * 10
        en = "unit cost rose by two point eight four percent " * 10
        print(f"中文 {len(zh)} 字符 → {estimate_tokens(zh)} tokens")
        print(f"英文 {len(en)} 字符 → {estimate_tokens(en)} tokens")
        print("  说明：Pi 的 chars/4 会把中文估成 "
              f"{len(zh)//4}（低估 {(1-len(zh)//4/estimate_tokens(zh))*100:.0f}%）")

        # 2) 切点必须落在 user 边界
        msgs = []
        for i in range(10):
            msgs.append({"id": i * 3 + 1, "role": "user", "content": f"问题{i}" * 40})
            msgs.append({"id": i * 3 + 2, "role": "assistant",
                         "content": "", "tool_name": "search_wiki"})
            msgs.append({"id": i * 3 + 3, "role": "tool", "content": "结果" * 200})
        cut = find_cut_index(msgs, 300)
        print(f"\n切点 idx={cut}  role={msgs[cut]['role']}（必须是 user）")
        assert msgs[cut]["role"] == "user", "切点必须落在 user 消息边界"

        # 3) 真跑一次压缩（用桩 LLM，不联网）
        class StubLLM:
            def invoke(self, messages):
                class R:  # noqa: N801
                    content = ("### 目标\n问 5 月成本原因\n\n### 关键约束\n无\n\n"
                               "### 已确认的事实\n- 环比 `+2.84%` `[三产品成本基线.md:§1]`\n\n"
                               "### 已排除/存疑\n无\n\n### 已生成的产物\n无\n\n"
                               "### 当前进度\n已回答首问")
                return R()

        c = Compactor(llm=StubLLM(), context_window=2000, reserve_tokens=200,
                      keep_recent_tokens=400)
        print(f"阈值 {c.threshold()} tokens；实际 "
              f"{estimate_messages(msgs)} tokens → 需要压缩={c.needed(msgs)}")
        assert c.needed(msgs), "超过阈值就该判定需要压缩"

        rep = c.compact(msgs)
        assert rep, "应当产出压缩结果"
        print(f"待摘要 {rep['summarized']} 条 / 保留原文 {rep['kept']} 条"
              f"（压缩前 {rep['tokens_before']} tokens）")

        # 保留段必须以 user 开头（否则第一条是悬空的工具结果）
        to_sum, keep = c.split(msgs)
        assert not keep or keep[0]["role"] == "user", "保留段必须以 user 开头"
        print(f"保留段首条 role={keep[0]['role']} —— 无悬空工具结果 ✅")

        # 4) 拼上下文：摘要 + 近端原文，且不重复
        ctx = build_context(msgs, rep["summary"], rep["upto_message_id"])
        n_sys = sum(1 for x in ctx if x["role"] == "system")
        ids_in_ctx = {x.get("id") for x in ctx if x.get("id")}
        summarized_ids = {m["id"] for m in msgs if m["id"] <= rep["upto_message_id"]}
        print(f"\n上下文共 {len(ctx)} 条（含 {n_sys} 条摘要）；"
              f"被摘要的 {len(summarized_ids)} 条不再出现="
              f"{not (ids_in_ctx & summarized_ids)}")
        assert not (ids_in_ctx & summarized_ids), "已摘要的消息不该再出现在上下文里"
        after = estimate_messages([x for x in ctx if x["role"] != "system"])
        print(f"原 {rep['tokens_before']} → 存留原文 {after} tokens"
              f"（降 {100*(1-after/rep['tokens_before']):.0f}%）")

        # 5) 溢出恢复只允许一次
        print(f"\n溢出重试 #1: {c.should_retry_after_overflow()}")
        print(f"溢出重试 #2: {c.should_retry_after_overflow()}  ← 必须 False（防死循环）")
        assert not c.should_retry_after_overflow(), "溢出恢复必须只允许一次"
        print("\n✅ 全部自检通过")
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
