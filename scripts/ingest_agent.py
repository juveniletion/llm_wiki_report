# -*- coding: utf-8 -*-
"""
ingest_agent.py — **摄入 agent 运行时**（由独立的 LLM 实例执行摄入，不依赖人类代劳）

核心设计
--------
1. **无长期记忆**：每次运行都从磁盘**重新读取** `INGEST_AGENT.md` 作系统提示。
   规范改了，agent 下次自动跟上——**改规则不用改 agent**。
2. **独立实例**：它是一个独立的 ReAct agent（LangGraph + DeepSeek），
   我自己不介入它的推理与编辑；结束后我只做**独立核验**。
3. **工具受约束**：写工具会拒绝批次页命名、拒绝覆盖已有词条
   （见 `ingest_tools.py`），把规范**内建进工具**而不只写在提示里。
4. **采集与编译分离**：`ingest_raw.py` 做确定性采集；本 agent 做语义编译。

用法
----
    # 1) 先采集（确定性）：把新文件接进 raw/ 并生成编译简报
    python scripts/ingest_raw.py inbox/

    # 2) 再让独立 LLM 编译（语义）：读简报 → 分诊 → 编译 → 级联 → 收尾
    python scripts/ingest_agent.py --brief brief.json

    # 或一步到位（内部先采集再编译）
    python scripts/ingest_agent.py --inbox
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
CODE_ROOT = HERE.parent                 # 代码仓库（规范与工具都在这）
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(CODE_ROOT.parent / "agents"))

from paths import default_db  # noqa: E402

# ⚠️ `ingest_tools` **必须延迟导入**。
#    它的 `WIKI_ROOT` 在模块导入时由环境变量 `LLM_WIKI_WS` 决定；
#    若在模块级 import，那时还没解析命令行参数、环境变量也没设，
#    就会永远绑到默认根上——表现为"个人工作区不生效，但也不报错"。
_INGEST_TOOLS = None


def _ingest_tools():
    global _INGEST_TOOLS
    if _INGEST_TOOLS is None:
        import ingest_tools
        _INGEST_TOOLS = ingest_tools
    return _INGEST_TOOLS


def build_ingest_tools() -> list:
    return _ingest_tools().build_ingest_tools()


def set_workspace(root: Path) -> None:
    """
    指定本次摄入的工作区。**必须在首次使用工具之前调用。**

    共享基线（代码仓库）与工作区是两个根：
        规范（SKILL.md / INGEST_AGENT.md / references/）永远来自代码仓库
        读写（raw/ 与 wiki/）落在工作区
    """
    os.environ["LLM_WIKI_WS"] = str(Path(root).resolve())


def spec_root() -> Path:
    """规范来源 —— 永远是代码仓库，**不接受用户输入**。

    否则用户上传一个 SKILL.md 就能改掉"不许编造数字"这类根本约束。
    """
    return CODE_ROOT


def _utf8() -> None:
    if sys.platform == "win32" and not getattr(sys.stdout, "_cn_wrapped", False):
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
            sys.stdout._cn_wrapped = True
        except Exception:  # noqa: BLE001
            pass


def _load_env() -> None:
    for p in [CODE_ROOT, *CODE_ROOT.parents]:
        env = p / ".env"
        if env.exists():
            load_dotenv(dotenv_path=env)
            return


# ---------------------------------------------------------------------------
# 规范加载 —— "无长期记忆"的落点
# ---------------------------------------------------------------------------

BOOTSTRAP_FILES = [
    "INGEST_AGENT.md",
    "references/schema.yaml",
    "references/命名与链接约定.md",
    "references/词条骨架.md",
]

# SKILL.md 的**必读章节**（逐节抽取，而非全文——全文 2 万字会挤掉任务）
# ⚠️ 新增这类强制规则时，**必须同时加进这个列表**，否则 agent 运行时看不到。
#    本库踩过：规则写进 SKILL.md 5.3.1，却因未列入此处而从未被加载。
SKILL_CRITICAL_SECTIONS = [
    "## 二、铁律",             # 数值精度 / 溯源坐标 / 缺失与冲突
    "## 三、证据不变量",        # 先定位后书写
    "### 5.3.1",               # ⭐ 冲突扫描（编译前强制步骤）
    "## 六、Status 块",         # 冲突的落笔方式
    "## 九、领域硬约束",        # 单位口径 / 设备编号 / 4-6月根因
    "## 十、硬约束清单",        # 违反即失败的清单
]

MAX_BOOTSTRAP_CHARS = 60000   # 防止规范膨胀撑爆上下文


def load_spec() -> str:
    """
    从磁盘读取规范并拼成系统提示。
    **每次调用都重新读文件**——agent 不缓存、不记忆。
    """
    parts: List[str] = []
    for rel in BOOTSTRAP_FILES:
        p = CODE_ROOT / rel
        if not p.exists():
            parts.append(f"### [{rel}] —— ⚠️ 文件缺失，无法加载")
            continue
        txt = p.read_text(encoding="utf-8")
        if len(txt) > MAX_BOOTSTRAP_CHARS:
            txt = txt[:MAX_BOOTSTRAP_CHARS] + "\n\n...(本文过长已截断，请用工具读取完整内容)"
        parts.append(f"### [{rel}]\n\n{txt}")
    joined = "\n\n" + ("\n\n" + "=" * 70 + "\n\n").join(parts)

    return (
        "你是 pharma-cost-wiki 的**摄入 agent**。你**没有长期记忆**——"
        "以下是你本次运行的全部规则，从磁盘实时读取。\n"
        "严格按它们工作。**未读到的规则就等于不存在**，所以别跳过。\n"
        "全程用中文回答与思考。\n"
        + joined
    )


def _extract_section(txt: str, anchor: str, max_chars: int = 6000) -> str:
    """
    从 `anchor` 起，抽到**同级或更高级**的下一个标题为止。

    ⚠️ 关键：不是"遇到任何标题就停"。`## 二、铁律` 下辖 `### 2.1`/`### 2.2`/`### 2.3`，
    这些是**子节，要包含**。只有遇到 `## `（与 anchor 同级）或更高级才停。

    本库踩过：第一版写成"遇任何 `#{2,3}` 就停"，导致 `## 二、铁律` 只抽到标题行本身，
    6 个章节加起来仅 1716 字符——规则名在、内容全丢。
    """
    i = txt.find(anchor)
    if i < 0:
        return ""
    # anchor 自身的级别（数开头的 #）
    anchor_level = len(anchor) - len(anchor.lstrip("#"))
    rest = txt[i + len(anchor):]
    # 找下一个**级别 <= anchor_level** 的标题
    stop = len(rest)
    for m in re.finditer(r"\n(#{2,6}) ", rest):
        if len(m.group(1)) <= anchor_level:
            stop = m.start()
            break
    seg = rest[:min(stop, max_chars)]
    # 去掉尾部的分隔线
    seg = re.split(r"\n---\s*\n", seg)[0]
    return seg.strip()


def load_invariants() -> str:
    """
    SKILL.md 的**必读章节**（放在提示末尾强化，因为最容易被遗忘）。

    ⚠️ 只抽 `SKILL_CRITICAL_SECTIONS` 列出的章节。
    新增强制规则时**必须同步更新该列表**，否则规则形同虚设——本库踩过这个坑：
    「冲突扫描」写进 SKILL.md 5.3.1，却因未列入加载列表而从未进入 system prompt。
    """
    p = CODE_ROOT / "SKILL.md"
    if not p.exists():
        return ""
    txt = p.read_text(encoding="utf-8")
    out: List[str] = []
    for anchor in SKILL_CRITICAL_SECTIONS:
        seg = _extract_section(txt, anchor)
        if seg:
            out.append(f"{anchor}\n{seg}")
    if not out:
        return ""
    return ("## SKILL.md 必读章节（这些是硬性规则，不是建议）\n\n"
            + "\n\n".join(out))


# ---------------------------------------------------------------------------
# Agent 构建
# ---------------------------------------------------------------------------

DEFAULT_SYSTEM = "你是 pharma-cost-wiki 的摄入 agent。"


def build_agent(model: Optional[str] = None, temperature: float = 0.0) -> Any:
    """构建独立的摄入 agent（LangGraph ReAct）。"""
    _load_env()
    from langchain_openai import ChatOpenAI
    from langgraph.prebuilt import create_react_agent
    import httpx

    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    base_url = (os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")
    model_name = model or os.getenv("AGENT_MODEL", "deepseek-chat")
    if not api_key:
        raise SystemExit("未找到 DEEPSEEK_API_KEY（应在工作区根目录的 .env）")

    # Python 3.13 下 httpx 解压缩兼容
    llm = ChatOpenAI(
        model=model_name,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        http_client=httpx.Client(headers={"Accept-Encoding": "identity"}),
        http_async_client=httpx.AsyncClient(headers={"Accept-Encoding": "identity"}),
    )
    prompt = load_spec() + "\n\n" + load_invariants()
    return create_react_agent(model=llm, tools=build_ingest_tools(), prompt=prompt)


# ---------------------------------------------------------------------------
# 任务构造
# ---------------------------------------------------------------------------

def build_task(briefs: List[Dict[str, Any]]) -> str:
    """把采集简报转成给 agent 的任务描述。"""
    lines = [
        "## 本次摄入任务",
        "",
        f"采集层已完成，下面是 {len(briefs)} 份文件的**编译简报**。",
        "请你按 INGEST_AGENT.md 的流程完成 **分诊 → 编译 → 级联 → 收尾**。",
        "",
        "⚠️ 关键提醒（本库犯过的错，别重犯）：",
        "1. **先问「长进哪一页」，再问「要不要新建页」**——"
        "已有实体的新数据一律 `patch_wiki_article` 并入，不要建批次页。",
        "2. **写任何数值前先 `grep_raw` 定位**——找不到就不许写它的精确形式。",
        "3. **一次摄入通常改多个页面**：扩充实体页 + 更新 index 摘要 + 标冲突 + 重新组织既有表述。",
        "4. 收尾只做**语义部分**（改 index.md、写 log.md）；"
        "重建快照 / 同步 DB / 跑校验由运行时脚本自动完成，**不用你管**。",
        "5. **检索有停止条件**：同一目标连续查 3 次未命中就停，"
        "判定为「数据缺失」写进 log 继续——**不要反复换词重试**"
        "（实测有人连查 58 次全落空，最后把任务拖崩，零产出）。"
        "⚠️ `log.md` 是历史记录，可能引用已删除的文件；"
        "判断库里有什么要看 `raw/` 和 `wiki/`。",
        "",
        "## ⭐ 编译前必须完成的一步：冲突扫描",
        "",
        "**这不是建议，是义务。跳过它 = 本次编译未完成。**",
        "",
        "对本次将写入数值的**每个核心指标**，调 `cross_check_metric` 查 **raw/ 全库**",
        "（不只看本次的源文件——矛盾多半在「新数据 vs 既有数据」之间）。",
        "",
        "查到的每个命中，按四类判读后落笔：",
        "",
        "| 判定 | 落笔 |",
        "|:---|:---|",
        "| ① 真矛盾（8,500 vs 5,800） | 写 `Status: Disputed` |",
        "| ② 口径差异（定额 1.72 vs 实际 1.48） | 写 `Status: Disputed`，说明是口径差异 |",
        "| ③ 时点差异（1月 6.83 vs 2月 6.92） | **不是矛盾**，分别列出即可 |",
        "| ④ 主体差异（一厂 11.21 vs 二厂 11.60） | **不是矛盾**，标明主体即可 |",
        "| 无法判定 | 写 `Disputed`，说明为何无法判定 + 需补什么数据 |",
        "",
        "⚠️ 别把 ③④ 写成 Disputed——那会污染库。",
        "",
        "**收尾时在 `append_wiki_log` 里必须有一行：**",
        "`- 冲突扫描: 检查了 <N> 个核心指标（<列举>），发现 <M> 处候选，判定 <K> 处为真冲突`",
        "",
        "---",
        "",
    ]
    for i, b in enumerate(briefs, 1):
        lines.append(f"### 简报 {i}")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(b, ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def collect(inbox: bool, files: List[str]) -> List[Dict[str, Any]]:
    """调用采集层，返回简报列表。"""
    from ingest_raw import ingest_file, ingest_inbox
    # ⚠️ 收文件落的是**工作区**（个人区），不是代码仓库。
    ws = Path(os.getenv("LLM_WIKI_WS") or CODE_ROOT)
    briefs: List[Dict[str, Any]] = []
    if inbox:
        for r in ingest_inbox(ws, hooks=[]):
            briefs.append(r.to_dict())
    for f in files:
        r = ingest_file(f, hooks=[], wiki_root=ws)
        briefs.append(r.to_dict())
    return briefs


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

def run(agent: Any, task: str, verbose: bool = True) -> str:
    """流式执行，打印工具调用轨迹；返回最终回答。"""
    final = ""
    step_n = 0
    for step in agent.stream({"messages": [("user", task)]}, stream_mode="updates"):
        for _node, out in step.items():
            for msg in out.get("messages", []):
                tc = getattr(msg, "tool_calls", None)
                if tc:
                    for c in tc:
                        step_n += 1
                        if verbose:
                            args = str(c["args"])
                            print(f"  [{step_n:>2}] 调用 {c['name']}")
                            print(f"       {args[:160]}")
                elif msg.type == "tool":
                    if verbose:
                        s = str(msg.content).replace("\n", " ")
                        print(f"       ↳ {s[:140]}")
                elif msg.type == "ai" and msg.content:
                    final = msg.content
    return final


def main() -> int:
    _utf8()
    ap = argparse.ArgumentParser(description="pharma-cost-wiki 摄入 agent（独立 LLM 执行）")
    ap.add_argument("files", nargs="*", help="要摄入的文件")
    ap.add_argument("--inbox", action="store_true", help="处理 inbox/")
    ap.add_argument("--brief", help="直接读入采集简报 JSON（跳过采集）")
    ap.add_argument("--model", help="覆盖模型（默认取 .env 的 AGENT_MODEL）")
    ap.add_argument("--db", default=str(default_db()),
                    help="SQLite 镜像库路径（收尾时同步；默认取 LLM_WIKI_DB 环境变量）")
    ap.add_argument("--wiki-root",
                    help="工作区根。默认=代码仓库（即既有行为）。"
                         "指向个人工作区时，raw/ 与 wiki/ 的读写都落在那里，"
                         "共享基线不受影响。规范仍从代码仓库读。")
    ap.add_argument("--dump-prompt", action="store_true", help="只打印系统提示长度与工具列表，不执行")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    # ⚠️ 必须在**任何 ingest_tools 调用之前**设好，否则工具会绑到默认根
    if a.wiki_root:
        set_workspace(Path(a.wiki_root))

    if a.dump_prompt:
        spec = load_spec()
        print(f"系统提示: {len(spec):,} 字符")
        print(f"铁律摘要: {len(load_invariants()):,} 字符")
        print("工具:")
        for t in build_ingest_tools():
            print(f"  - {t.name}")
        return 0

    # ---- 取简报 ----
    if a.brief:
        briefs = json.loads(Path(a.brief).read_text(encoding="utf-8"))
        if isinstance(briefs, dict):
            briefs = [briefs]
    elif a.inbox or a.files:
        briefs = collect(a.inbox, a.files)
    else:
        ap.print_help()
        return 1

    ok = [b for b in briefs if b.get("status") == "collected"]
    skip = [b for b in briefs if b.get("status") == "skipped"]
    err = [b for b in briefs if b.get("status") == "error"]

    print("=" * 74)
    print("摄入 agent — 采集阶段完成")
    print(f"  成功 {len(ok)} · 跳过 {len(skip)} · 失败 {len(err)}")
    for b in skip:
        print(f"  跳过（重复）: {b['source']} → 已存在于 {b.get('raw_path')}")
    for b in err:
        print(f"  失败: {b['source']}: {b.get('error')}")
    print("=" * 74)

    if not ok:
        print("无新文件需要编译。")
        return 0

    # ---- 构建并运行 agent ----
    agent = build_agent(model=a.model)
    task = build_task(ok)
    print(f"\n交给独立 LLM 编译（提示 {len(load_spec()):,} 字符 + 任务 {len(task):,} 字符）\n")

    # ==================================================================
    # 确定性收尾 —— **脚本做，LLM 不参与**，且**必须 try/finally**
    #
    # 为什么不把这三步做成 agent 的工具（本库踩过的坑）：
    #   把它做成工具 = "LLM 得记得调"。它忘了、或在前一步崩了，产物就静默不完整。
    #   实测发生过：agent 已正确完成冲突扫描并写了 3 处 Disputed，
    #   但随后某次 patch 因编码错误抛异常，整个流程中断，
    #   append_wiki_log 与 state 重建**都没执行**。
    #
    # ⚠️ 为什么必须是 `finally` 而不是"跑完再调"（本库又踩一次的坑）：
    #   第一版写成 `final = run(...)` 之后再 `finalize()`。
    #   实测 agent 跑到一半遇上 `OpenAIConnectionError`，`run()` 直接抛异常穿透 main()，
    #   **`finalize()` 一行都没执行**（日志里连"确定性收尾"都没打印）。
    #   讽刺的是：当初把机械步骤从 LLM 手里收回来，正因为"LLM 崩了就不做"；
    #   结果包装它的代码自己也"崩了就不做"——**换汤不换药**。
    #
    #   教训（值得记住）：**"不依赖 LLM 的记性"不等于"不依赖任何东西"**。
    #   agent 会崩、网络会断、API 会限流——收尾逻辑必须挂在 `finally` 上，
    #   因为它服务的是"已经落盘的那部分工作"，而那部分已经存在了。
    # ==================================================================
    from finalize import finalize

    crashed: Optional[str] = None
    final = ""
    try:
        final = run(agent, task, verbose=not a.quiet)
    except Exception as e:  # noqa: BLE001
        # 不吞掉：记下来，收尾照跑，最后一起汇报并以非零码退出
        crashed = f"{type(e).__name__}: {e}"
        print("\n" + "!" * 74)
        print(f"⚠️  agent 异常中断：{crashed}")
        print("   按设计继续执行**确定性收尾**——已落盘的编辑不该因为中断而失去收尾。")
        print("!" * 74)
    finally:
        print("\n" + "=" * 74)
        print("进入确定性收尾（重建 state → 同步 DB → 机械校验）")
        print("=" * 74)
        # ⚠️ 个人工作区**不能跑 db 步**——那会往共享 SQLite 镜像里写个人数据。
        #    个人区只跑 state + check（都是本地派生，不外溢）。
        _skip = ["db", "graph"] if a.wiki_root else None
        rep = finalize(Path(getattr(a, "db", None) or default_db()), skip=_skip)

    # 收尾暴露的问题里，**语义部分**才交给 LLM（如结构问题该怎么修）。
    # 注意：这一步是可选的补救，不跑不影响已完成的机械收尾。
    #       且**只在 agent 没崩时**才回喂——崩了就把问题直接列出来交给人。
    if not rep.get("clean") and not a.quiet and not crashed:
        print("\n" + "=" * 74)
        print("收尾发现问题，交回 LLM 处理（仅语义部分）")
        print("=" * 74)
        ck = rep.get("check", {})
        fix_task = (
            "## 收尾发现的问题（由脚本检测，非你判断）\n\n"
            + "\n".join(f"- {p}" for p in rep["problems"])
            + "\n\n以下是机械校验的原始输出，请据此**修订 wiki**（只改语义问题，"
            "不要动 raw/）：\n\n```\n"
            + (ck.get("raw", "")[:4000] if ck.get("ok") else str(ck))
            + "\n```\n\n修完请简述改了什么。"
        )
        fix = run(agent, fix_task, verbose=not a.quiet)
        print("\n--- LLM 修订汇报 ---")
        print(fix)

        # 修订后再跑一次确定性收尾，确认干净
        print("\n" + "=" * 74)
        print("修订后复检")
        print("=" * 74)
        # ⚠️ 个人工作区**不能跑 db 步**——那会往共享 SQLite 镜像里写个人数据。
        #    个人区只跑 state + check（都是本地派生，不外溢）。
        _skip = ["db", "graph"] if a.wiki_root else None
        rep = finalize(Path(getattr(a, "db", None) or default_db()), skip=_skip)

    print("\n" + "=" * 74)
    print("摄入 agent 最终汇报")
    print("=" * 74)
    print(final or "（agent 未产出最终回答）")
    print()

    if crashed:
        print("⚠️  本次运行**中途异常中断**，但确定性收尾已按设计完成：")
        print(f"    中断原因: {crashed}")
        print("    → 请检查 wiki/ 里已落盘的编辑是否完整，必要时重跑。")

    if rep.get("clean"):
        print("✅ 收尾干净，全流程完成。" if not crashed else "✅ 收尾干净（尽管中途中断）。")
    else:
        print("⚠️  收尾仍有未解问题：")
        for p in rep["problems"]:
            print(f"   · {p}")

    if crashed:
        return 3                      # 中断：与"收尾不干净"(2) 区分开
    return 0 if rep.get("clean") else 2


if __name__ == "__main__":
    sys.exit(main())
