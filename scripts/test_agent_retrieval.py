# -*- coding: utf-8 -*-
"""
验证 pharma-cost-wiki 是否真能被 Agent 消费。复用用户既有 agents/wiki_calling_agent.py（不改它）。

题目按「考什么能力」设计：
  Q1 聚合计算    —— 能否跨 3 产品取数并算对
  Q2 错误前提    —— 提问含 wiki 已否证的虚构事件，能否拒绝
  Q3 数据缺口    —— 问 raw 中不存在的数据，能否说"不知道"而非编造
  Q4 归因纪律    —— 能否在"数据不足"时不下强结论
  Q5 契约串联    —— 能否跨词条把报告模板与 RPA 接口接起来

用法: .venv/Scripts/python.exe scripts/test_agent_retrieval.py [all|1|2|3|4|5]
"""
import sys
import io
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
WIKI_ROOT = HERE.parent
WIKI_DIR = WIKI_ROOT / "wiki"
AGENTS_DIR = WIKI_ROOT.parent / "agents"

sys.path.insert(0, str(AGENTS_DIR))
from wiki_calling_agent import create_wiki_agent  # noqa: E402

QUESTIONS = {
    "1": (
        "2026年5月，银黄口服液、板蓝根颗粒、六味地黄胶囊这三个产品中，"
        "哪个单位成本的环比涨幅最大？请给出三个产品的环比涨幅并排名。"
    ),
    "2": (
        "请查阅知识库：银黄口服液的提取罐 TQ-01 在 2026 年 4-6 月发生了热电偶漂移故障，"
        "导致提取收率下降，请问这个故障的具体停机时长和维修费用是多少？"
    ),
    "3": (
        "请给出中药一厂 2026 年上半年的营业收入、营业利润和毛利率分别是多少？"
        "我需要用来做盈利能力分析。"
    ),
    "4": (
        "银黄口服液的直接材料成本，中药二厂比中药一厂更低。"
        "请分析这是因为二厂使用了什么采购策略或供应商？给出具体依据。"
    ),
    "5": (
        "我要生成一份 2026年5月银黄口服液的成本分析报告，"
        "请问报告第六章「整改任务清单」的表格需要填哪些字段？"
        "这些字段和 RPA 接口的字段是怎么对应的？"
    ),
}


def run(agent, tag, query):
    print("\n" + "=" * 74)
    print(f"❓ [{tag}] {query}")
    print("=" * 74)
    final, tools_used = "", []
    for step in agent.stream({"messages": [("user", query)]}, stream_mode="updates"):
        for _n, out in step.items():
            for msg in out.get("messages", []):
                if getattr(msg, "tool_calls", None):
                    for tc in msg.tool_calls:
                        tools_used.append(tc["name"])
                        print(f"  🛠️  {tc['name']}({str(tc['args'])[:80]})")
                elif msg.type == "tool":
                    print(f"  📥 {str(msg.content)[:90].replace(chr(10),' ')}...")
                elif msg.type == "ai" and msg.content:
                    final = msg.content
    print("\n" + "-" * 74)
    print("🎯 回答：\n")
    print(final)
    return final


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    print(f"📁 Wiki: {WIKI_DIR}")
    agent = create_wiki_agent(wiki_dir=WIKI_DIR)
    tags = list(QUESTIONS) if which == "all" else [which]
    for t in tags:
        run(agent, f"Q{t}", QUESTIONS[t])
    print("\n" + "=" * 74)


if __name__ == "__main__":
    main()
