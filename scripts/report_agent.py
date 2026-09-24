# -*- coding: utf-8 -*-
"""
report_agent.py — **主 agent 运行时**（报告生成侧，只读 wiki）

与摄入 agent 的分工
------------------
    摄入 agent (ingest_agent.py)   读 raw + **写** wiki    ← 维护知识库
    主 agent   (report_agent.py)   读 wiki + 只读 raw      ← 消费知识库

本 agent **不改知识库**。它回答带证据的问题、取数、溯源。
按需求，本版**不套报告模板**（不生成 6 章研报）——那是后续可加的层。

设计要点
--------
1. **无长期记忆**：每次运行从磁盘重读 `REPORT_AGENT.md`（若存在）
   与 `SKILL.md` 的必读章节作系统提示。
2. **只读工具**：`report_tools.py` 全部工具无写能力。
3. **约束内建**：系统提示强制"每个数字带坐标、冲突必须显式说明、
   不得取平均"——这些不是建议，是交付要求。
4. **归档预留**：`--archive` 参数已定义但**本版不实现回写**，
   仅打印提示（见 SKILL.md 的 Query→Archive 区分）。

用法
----
    python scripts/report_agent.py "2026年5月银黄口服液单位成本涨了多少？主因是什么？"
    python scripts/report_agent.py --ask          # 交互式
    python scripts/report_agent.py -q "..." --json # 输出 JSON
    python scripts/report_agent.py -q "..." --archive   # 预留：归档（暂不实现）
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402
from errors import ConfigError  # noqa: E402

ensure_utf8_stdout()

HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = HERE.parent

# 主 agent 的必读章节（与摄入 agent 的 SKILL_CRITICAL_SECTIONS 不同，
# 因为两者的职责不同：摄入侧重"怎么写"，报告侧重"怎么引"）
SKILL_SECTIONS_FOR_REPORT = [
    "## 二、铁律",            # 数值精度 / 溯源坐标 / 缺失与冲突
    "## 三、证据不变量",       # 先定位后书写（对报告即"先溯源后引用"）
    "## 六、Status 块",        # 冲突的读法
    "## 九、领域硬约束",       # 单位口径 / 设备编号 / 4-6月根因
    "## 十、硬约束清单",
]

REPORT_SYSTEM_TEMPLATE = """你是制药企业成本分析的**报告撰写专家**，服务于 2026 年重庆 AI 大赛赛题。

你**没有长期记忆**——以下规则从磁盘实时读取，是你本次运行的全部依据。

## 你的资料边界（重要）

你**只能**依据知识库的内容作答。知识库是 `{root}`。
你没有别的数据源，**不得**使用你自己的训练知识补充本厂的具体数字。

## 三条硬性交付要求

1. **每个数字必须带证据坐标**，格式 `[词条名.md:章节名]`。
   取数用 `get_metric`（它会显示全部来源，避免只取一处）；
   写之前用 `search_wiki` 找到对应章节并抄下坐标。

2. **已知冲突必须显式说明，不得含糊带过。**
   写报告前**先调 `list_known_conflicts`**。涉及争议结论时，
   必须写出"存在两种来源，分别为…"，并说明工程判定与取数准则。
   **不得取平均，不得只引一方。**

3. **数据缺失就明说。** 若某指标在库中不存在，回答"知识库中无此数据"，
   并说明需要什么。**严禁编造**。

## 你的工具

- `search_wiki(query)` —— 混合检索，返回章节 + 坐标 + 已知冲突
- `read_article(path)` —— 读整篇词条（需要完整表格时用）
- `get_metric(metric, entity)` —— **取数首选**，跨来源交叉核对
- `trace_citation(citation)` —— 把坐标反查到 raw 原文（溯源）
- `list_known_conflicts()` —— 列出全部已知冲突
- `list_articles()` / `run_evidence_check()`

## 工作方式

1. 先 `search_wiki` 或 `get_metric` 定位相关章节
2. 涉及数值时，**先 `list_known_conflicts`** 确认有无争议
3. 必要时 `trace_citation` 回溯原始证据
4. 作答时**逐数字给坐标**，争议处显式标注

全程用中文。
"""


def _extract_section(txt: str, anchor: str, max_chars: int = 5000) -> str:
    """从 anchor 起抽到同级或更高级标题为止（与 ingest_agent 同一逻辑）。"""
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
    seg = rest[:min(stop, max_chars)]
    return re.split(r"\n---\s*\n", seg)[0].strip()


def load_constraints(root: Path) -> str:
    """读 SKILL.md 的报告侧必读章节（每次运行重读，无缓存）。"""
    p = root / "SKILL.md"
    if not p.exists():
        return ""
    txt = p.read_text(encoding="utf-8")
    out = []
    for a in SKILL_SECTIONS_FOR_REPORT:
        s = _extract_section(txt, a)
        if s:
            out.append(f"{a}\n{s}")
    return "\n\n".join(out)


def load_report_spec(root: Path) -> str:
    """读 REPORT_AGENT.md（若存在）—— 报告侧专属规范，便于人类调整交付要求。"""
    p = root / "REPORT_AGENT.md"
    if p.exists():
        return "\n\n### [REPORT_AGENT.md]\n\n" + p.read_text(encoding="utf-8")
    return ""


def _load_env(root: Path) -> None:
    try:
        from dotenv import load_dotenv
        for p_ in [root, *root.parents]:
            e = p_ / ".env"
            if e.exists():
                load_dotenv(dotenv_path=e)
                return
    except Exception:  # noqa: BLE001
        pass


def build_agent(root: Optional[Path] = None, model: Optional[str] = None,
                temperature: float = 0.0) -> Any:
    """构建主 agent（LangGraph ReAct + 只读工具）。"""
    ROOT = Path(root) if root else DEFAULT_ROOT
    _load_env(ROOT)

    from langchain_openai import ChatOpenAI
    from langgraph.prebuilt import create_react_agent
    import httpx
    from report_tools import build_report_tools

    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    base_url = (os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")
    model_name = model or os.getenv("AGENT_MODEL", "deepseek-chat")
    if not api_key:
        raise ConfigError(
            "未找到 DEEPSEEK_API_KEY",
            hint="在工作区根目录建 .env，写入 DEEPSEEK_API_KEY=...")

    llm = ChatOpenAI(
        model=model_name, api_key=api_key, base_url=base_url,
        temperature=temperature,
        http_client=httpx.Client(headers={"Accept-Encoding": "identity"}),
        http_async_client=httpx.AsyncClient(headers={"Accept-Encoding": "identity"}),
    )
    prompt = (REPORT_SYSTEM_TEMPLATE.format(root=ROOT)
              + "\n\n## 知识库规则（必读章节）\n\n" + load_constraints(ROOT)
              + load_report_spec(ROOT))
    return create_react_agent(model=llm, tools=build_report_tools(ROOT), prompt=prompt)


def run(agent: Any, question: str, verbose: bool = True) -> str:
    """流式执行，打印工具轨迹；返回最终回答。"""
    final, n = "", 0
    for step in agent.stream({"messages": [("user", question)]}, stream_mode="updates"):
        for _node, out in step.items():
            for msg in out.get("messages", []):
                tc = getattr(msg, "tool_calls", None)
                if tc:
                    for c in tc:
                        n += 1
                        if verbose:
                            print(f"  [{n:>2}] {c['name']}({str(c['args'])[:100]})")
                elif msg.type == "tool" and verbose:
                    print(f"       ↳ {str(msg.content)[:110].replace(chr(10),' ')}")
                elif msg.type == "ai" and msg.content:
                    final = msg.content
    return final


def main() -> int:
    ap = argparse.ArgumentParser(description="主 agent：读 wiki 回答带证据的问题（只读）")
    ap.add_argument("question", nargs="*", help="问题")
    ap.add_argument("-q", "--query", help="问题（等价于位置参数）")
    ap.add_argument("--ask", action="store_true", help="交互式提问")
    ap.add_argument("--wiki-root", help="wiki 根目录（默认本库）")
    ap.add_argument("--model", help="覆盖模型")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    ap.add_argument("--dump-prompt", action="store_true", help="只打印提示长度与工具，不执行")
    ap.add_argument("--archive", action="store_true",
                    help="【预留】把答案归档进 wiki（本版未实现，仅提示）")
    a = ap.parse_args()

    ROOT = Path(a.wiki_root).resolve() if a.wiki_root else DEFAULT_ROOT

    if a.dump_prompt:
        from report_tools import build_report_tools
        cons = load_constraints(ROOT)
        print(f"wiki 根: {ROOT}")
        print(f"系统提示: 模板 {len(REPORT_SYSTEM_TEMPLATE):,} + 约束 {len(cons):,} 字符")
        print("只读工具:")
        for t in build_report_tools(ROOT):
            print(f"  - {t.name}")
        return 0

    q = a.query or " ".join(a.question)

    if a.archive:
        print("⚠️  --archive 为**预留参数**，本版未实现归档回写。")
        print("   理由：SKILL.md 区分了「知识」与「归档」，逐月报告属 Query→Archive，")
        print("   而回写会改变知识库——需人类显式批准后另行实现。")
        print()

    agent = build_agent(ROOT, model=a.model)

    if a.ask or not q:
        print("主 agent（只读）。空行退出。\n")
        while True:
            try:
                q = input("❓ ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not q:
                break
            print()
            print(run(agent, q))
            print("\n" + "-" * 68 + "\n")
        return 0

    print(f"❓ {q}\n" + "=" * 70)
    answer = run(agent, q)
    if a.json:
        print(json.dumps({"question": q, "answer": answer}, ensure_ascii=False, indent=2))
    else:
        print("\n" + "-" * 70 + "\n")
        print(answer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
