# -*- coding: utf-8 -*-
"""
report_build.py — **6 章月度成本分析报告生成器**

架构：确定性优先
----------------
```
    raw/templates/月度成本分析报告模板.md      ← 验收标准（6 章，不得删减）
              │
              ├── 确定性填充（脚本）────────────  90% 的占位符
              │     report_facts.py 提供全部数字 + 坐标 + 公式
              │     表格整块由 Python 渲染
              │
              └── 叙事生成（LLM）──────────────  约 6 段分析文本
                    只拿到「算术结论」，不参与任何计算
                    受 wiki 约束（Status 块 / 归因禁忌 / 单位口径）
```

**为什么这样切**：LLM 做算术又慢又爱错（本库两次事故：产量比 84% 应为 82.89%、
六味同比 +4.08% 应为 +4.09%）。而叙事需要判断力，脚本给不了。
各司其职——**数字归脚本，文字归 LLM**。

模板硬约束（`报告模板契约.md` §8）
----------------------------------
1. 占位符是 `{{中文名}}`，解析器要支持中文变量名
2. `{{xxx表格}}` 生成整块 markdown 表格文本
3. **不得删减章节**（6 章是验收清单）
4. **不得改动表头文字**
5. **三要素行的「去年同月/同比」两列固定为 `—`**（模板硬编码，照写）

用法
----
    python scripts/report_build.py --product 银黄口服液 --month 2026-05
    python scripts/report_build.py --product 银黄口服液 --month 2026-05 --dry-run
    python scripts/report_build.py --product 银黄口服液 --month 2026-05 --no-llm
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402
import report_facts as RF  # noqa: E402

ensure_utf8_stdout()

HERE = Path(__file__).resolve().parent
WIKI_ROOT = HERE.parent
TEMPLATE = WIKI_ROOT / "raw/templates/月度成本分析报告模板.md"
OUT_DIR = WIKI_ROOT / "reports"

PRODUCT_CODE = {"银黄口服液": "YH", "板蓝根颗粒": "BLG", "六味地黄胶囊": "LWD"}
PRODUCT_SPEC = {"银黄口服液": "10ml×10支/盒", "板蓝根颗粒": "10g×20袋/盒",
                "六味地黄胶囊": "0.3g×60粒/盒"}

# 波动告警阈值（赛题 5.2.3 原文："某项成本要素环比变动超过 ±10%"）
THRESHOLD_PCT = 10.0

# 赛题判的是「**成本要素**」，不是单位成本合计——三者口径不同，不可混用。
# ⚠️ 曾经把「单位成本环比」（+2.84%）当成告警对象，于是每月都报"超阈值"。
#    真值：材料 +3.11% / 人工 +2.00% / 制造 +2.59%，**无一超 10%**，本月不该告警。
ELEMENT_FACTS: List[Tuple[str, str]] = [
    ("直接材料", "材料成本环比"),
    ("直接人工", "人工成本环比"),
    ("制造费用", "制造费用环比"),
]


def evaluate_threshold(fs: RF.FactSet) -> Dict[str, Any]:
    """
    赛题 5.2.3 的告警判定：某项成本要素环比变动超 ±10% → 需生成重点分析段落。

    **判定必须由脚本做**——这是合规判定，把它交给 LLM 自由发挥，
    模型就能凭空说出"超阈值"（本库实测过：2.84% 被写成"超阈值"）。
    数字判定是脚本的活；LLM 只负责在**判定已定**的前提下写解释。
    """
    breaches: List[Tuple[str, str]] = []   # (要素名, 显示值)
    within: List[Tuple[str, str]] = []
    for label, fact in ELEMENT_FACTS:
        f = fs.facts.get(fact)
        if f is None or f.value is None:
            continue
        (breaches if abs(f.value) >= THRESHOLD_PCT else within).append(
            (label, f.display))
    return {"threshold": THRESHOLD_PCT, "breaches": breaches, "within": within}


def threshold_verdict(th: Dict[str, Any]) -> str:
    """告警结论——**纯脚本拼串**，不含任何 LLM 成分。"""
    t = th["threshold"]
    if th["breaches"]:
        detail = "、".join(f"{n} {v}" for n, v in th["breaches"])
        return (f"已有 {len(th['breaches'])} 项成本要素环比变动超过 ±{t:.0f}%：{detail}。"
                f"按赛题要求生成重点分析（见下方）。")
    detail = "、".join(f"{n} {v}" for n, v in th["within"]) or "—"
    return (f"本月各成本要素环比变动均在 ±{t:.0f}% 阈值以内（{detail}），"
            f"无成本要素触发告警。")


# 需要 LLM 写作的字段（其余全部由脚本填）
LLM_SCALAR_FIELDS = {
    "材料成本归因分析文本": "120-200 字。按贡献度排序说明材料成本变动的主因，点到具体药材。",
    "成本异常排查分析": "150-250 字。结合近 6 个月趋势指出异常月份与成因。",
    "差异结构拆解分析": "120-200 字。拆解一厂与二厂的成本结构差异出在哪一层。",
    "差异归因分析文本": "100-160 字。解释差异的可能成因，区分管理与采购因素。",
    "本月亮点": "60-100 字。成本管理上值得肯定的地方（数据支撑）。",
    "需关注问题": "80-140 字。最需要管理层关注的问题，按严重度排序。",
}
LLM_MFG_NOTES = ["折旧变动说明", "动力变动说明", "间接人工变动说明",
                 "检验变动说明", "其他变动说明"]

MAX_TOTAL_LLM_CHARS = 2400   # 防过度回答（本库实测：简单问题被答成 12,565 字）


# ---------------------------------------------------------------------------
# 一、确定性填充
# ---------------------------------------------------------------------------

def _fact_display(fs: RF.FactSet, name: str) -> Optional[str]:
    f = fs.facts.get(name)
    return f.display if f is not None else None


def _check_unique(pairs: List[Tuple[str, str]]) -> None:
    """
    占位符名**不得重复**——重复即"后写静默覆盖先写"。

    ⚠️ 这条守是**事后补的，因为已经出过事**：
       曾经 `("人工环比", "人工成本环比")` 与 `("人工环比", "单位成本环比")`
       同时存在，字典赋值后写覆盖先写，于是 2.2 表「直接人工 环比」
       印成了单位成本环比（+2.84%，真值 +2.00%）。
       **错数进了 3 份已交付报告，三层校验一个都没拦住**——
       因为 `check_math` 只复算它认识的路子，`check_report` 只管模板合规，
       而这个值是"合法但张冠李戴"，看起来完全正常。

       ⇒ 沉默的错误值是最贵的一种错误。宁可构建期直接炸。
    """
    seen: Dict[str, str] = {}
    for tpl_name, fact_name in pairs:
        if tpl_name in seen:
            raise ValueError(
                f"占位符 `{{{{{tpl_name}}}}}` 被映射了两次："
                f"`{seen[tpl_name]}` 与 `{fact_name}`。"
                f"后者会**静默覆盖**前者，导致报告里出现张冠李戴的正确数字。")
        seen[tpl_name] = fact_name


def render_materials_table(fs: RF.FactSet) -> str:
    rows = []
    for i, m in enumerate(fs.materials, 1):
        judge = ""
        try:
            d = float(m["环比"].rstrip("%"))
            judge = "↑ 上涨" if d > 0 else ("↓ 下降" if d < 0 else "→ 持平")
        except (ValueError, AttributeError):
            judge = "—"
        rows.append(f"| {i} | {m['原材料名称']} | {m['本月']} | {m['上月']} | "
                    f"{m['环比']} | {judge} |")
    return "\n".join(rows) if rows else "| — | （无原材料明细数据） | — | — | — | — |"


def render_trend_table(fs: RF.FactSet) -> str:
    rows = []
    for r in fs.monthly:
        rows.append(f"| {r['月份']} | {r['产量']} | {r['单位材料']} | {r['单位人工']} | "
                    f"{r['单位制造费用']} | {r['单位成本']} | {r['环比']} |")
    return "\n".join(rows) if rows else "| — | — | — | — | — | — | — |"


def render_market_table(fs: RF.FactSet) -> str:
    rows = []
    for m in getattr(fs, "market", []):
        impact = "直接影响" if m["用于本产品"] else "—"
        rows.append(f"| {m['原材料']} | {m['年初价']} | {m['6月价']} | {m['涨幅']} | "
                    f"{m['市场趋势']} | {impact} |")
    return "\n".join(rows) if rows else "| — | — | — | — | — | — |"


def render_peer_table(fs: RF.FactSet) -> str:
    rows = []
    for p in fs.peer:
        rows.append(f"| {p['维度']} | {p['一厂'] if '一厂' in p else p['中药一厂']} | "
                    f"{p['中药二厂']} | {p['差异']} | {p['差异率']} | {p['方向']} |")
    return "\n".join(rows) if rows else "| — | — | — | — | — | — |"


def render_advice_table(advice: List[Dict[str, str]]) -> str:
    rows = []
    for i, a in enumerate(advice, 1):
        rows.append(f"| {i} | {a.get('建议事项','—')} | {a.get('责任部门','—')} | "
                    f"{display_priority(a.get('优先级','中'))} | {a.get('预期效果','—')} | "
                    f"{a.get('建议完成时间','—')} |")
    return "\n".join(rows) if rows else "| — | （无建议） | — | — | — | — |"


# 报告里印**中文**（给人看），RPA 只收小写英文（接口约束）。
# 转换在提交边界做（`rpa_client.to_rpa_priority`），报告本身保持中文可读。
PRIORITY_ZH = {"高": "high", "中": "medium", "低": "low",
               "high": "high", "medium": "medium", "low": "low"}


def display_priority(v: Any) -> str:
    """任意写法 → 报告里显示的中文「高/中/低」。"""
    s = str(v).strip()
    rpa = PRIORITY_ZH.get(s, PRIORITY_ZH.get(s.lower(), "medium"))
    return {"high": "高", "medium": "中", "low": "低"}[rpa]


def render_task_table(advice: List[Dict[str, str]], month: str,
                      product: str) -> str:
    """
    6.4 整改任务清单。**字段与 RPA 接口一一对应**
    （`报告模板契约.md` §6.1）：任务编号→task_id、优先级→priority 等。

    机械部分归脚本：编号、截止时间。
    ⚠️ RPA 约束：`priority` 只能小写 high/medium/low；`task_id` 必须唯一。

    ⚠️ 编号**必须含产品码**：曾用 `TASK-<年月>-<序号>`，而编号里没有产品，
       于是同月不同产品撞同一个编号（实测银黄口服液与六味地黄胶囊都产出
       `TASK-202605-001`，任务内容却完全不同）。下游按 task_id 判唯一，
       第二个产品的任务会被判"已存在"而**根本没发出去**。
       现改为 `TASK-<产品码>-<年月>-<序号>`，与报告编号 RPT-{ym}-{code}-001 同构。
    """
    rows = []
    code = PRODUCT_CODE.get(product, "XX")
    ym = month.replace("-", "")
    for i, a in enumerate(advice, 1):
        pr = display_priority(a.get("优先级", "中"))
        rows.append(f"| TASK-{code}-{ym}-{i:03d} | {a.get('建议事项','—')} | "
                    f"{a.get('责任部门','—')} | {pr} | 月度成本分析报告 | "
                    f"{a.get('建议完成时间','—')} |")
    return "\n".join(rows) if rows else "| — | （无整改任务） | — | — | — | — |"


WIKI_LINKS = {
    "配方文档引用": "../../wiki/products/{p}.md",
    "工艺文档引用": "../../wiki/products/{p}.md",
    "GMP文档引用": "../../wiki/gmp/GMP质量约束与成本关联.md",
    "行业基准引用": "../../wiki/market/药材行情与行业基准.md",
}


def deterministic_map(fs: RF.FactSet, today: str) -> Dict[str, str]:
    """脚本能算的全在这里。返回 占位符名 → 字符串。"""
    code = PRODUCT_CODE.get(fs.product, "XX")
    ym = fs.month.replace("-", "")
    m: Dict[str, str] = {
        "报告标题": f"中药一厂 {fs.month} 月度产品成本多维深度分析报告 — {fs.product}",
        "报告编号": f"RPT-{ym}-{code}-001",
        "分析月份": fs.month,
        "编制日期": today,
        "报告类型": "月度产品成本多维深度分析报告",
        "产品名称": fs.product,
        "产品规格": PRODUCT_SPEC.get(fs.product, "—"),
    }
    # 2.1 三行（含产量/单位成本/总成本）——模板名 → fact 名
    pairs = [
        ("本月产量", "产量(盒)"), ("上月产量", "上月产量(盒)"), ("产量环比", "产量(盒)环比"),
        ("去年同月产量", "去年同月产量"), ("产量同比", "产量同比"), ("预算产量", "预算产量"),
        ("产量预算偏差", "产量预算偏差"),
        ("本月单位成本", "本月单位成本"), ("上月单位成本", "上月单位成本"),
        ("单位成本环比", "单位成本环比"), ("去年单位成本", "去年单位成本"),
        ("单位成本同比", "单位成本同比"), ("预算单位成本", "预算单位成本"),
        ("单位成本预算偏差", "单位成本预算偏差"),
        ("本月总成本", "本月总成本"), ("上月总成本", "上月总成本"), ("总成本环比", "总成本环比"),
        ("去年总成本", "去年总成本"), ("总成本同比", "总成本同比"),
        ("预算总成本", "预算总成本"), ("总成本预算偏差", "总成本预算偏差"),
        ("本月材料成本", "本月材料成本"), ("上月材料成本", "上月材料成本"),
        ("材料成本环比", "材料成本环比"), ("预算材料成本", "预算材料成本"),
        ("材料预算偏差", "材料预算偏差"),
        ("本月人工成本", "本月人工成本"), ("上月人工成本", "上月人工成本"),
        ("人工成本环比", "人工成本环比"), ("预算人工成本", "预算人工成本"),
        ("人工预算偏差", "人工预算偏差"),
        ("本月制造费用", "本月制造成本"), ("上月制造费用", "上月制造成本"),
        ("制造费用环比", "制造费用环比"), ("预算制造费用", "预算制造成本"),
        ("制造费用预算偏差", "制造费用预算偏差"),
        # 2.2
        ("材料金额", "材料金额"), ("材料占比", "材料占比"), ("材料环比", "材料成本环比"),
        ("材料贡献度", "材料贡献度"),
        ("人工金额", "人工金额"), ("人工占比", "人工占比"), ("人工环比", "人工成本环比"),
        ("人工贡献度", "人工贡献度"),
        ("制造费用金额", "制造费用金额"), ("制造费用占比", "制造费用占比"),
        ("制造费用贡献度", "制造费用贡献度"),
        ("单位成本", "本月单位成本"), ("总环比", "单位成本环比"),
        # 3.2
        # ⚠️ 3.2 的「单位人工成本 环比」**不能**再插一条 ("人工环比", …)——
        #    占位符 `{{人工环比}}` 在 2.2（不带 %）和 3.2（带 %）各出现一次，
        #    但两处要的是**同一个事实**（人工成本环比），本来就不该重名映射两次。
        #    曾经这里写过 ("人工环比", "单位成本环比")，字典后写覆盖先写，
        #    导致 2.2「直接人工 环比」印成了单位成本环比（+2.84%，真值 +2.00%）。
        #    这个错数进了 3 份已交付报告，且无人察觉——见下方 _check_unique 守卫。
        ("人工单位成本", "人工单位成本"), ("上月人工单位成本", "人工单位成本上月"),
        ("本月工时", "本月工时"), ("上月工时", "上月工时"), ("工时环比", "工时环比"),
        ("本月时薪", "本月时薪"), ("上月时薪", "上月时薪"), ("时薪环比", "时薪环比"),
        ("本月效率", "本月效率"), ("上月效率", "上月效率"), ("效率环比", "效率环比"),
        # 3.3
        ("本月折旧", "本月折旧费"), ("上月折旧", "上月折旧费"), ("折旧环比", "折旧费环比"),
        ("本月动力", "本月动力费(水电气)"), ("上月动力", "上月动力费(水电气)"),
        ("动力环比", "动力费(水电气)环比"),
        ("本月间接人工", "本月间接人工"), ("上月间接人工", "上月间接人工"),
        ("间接人工环比", "间接人工环比"),
        ("本月检验", "本月检验费"), ("上月检验", "上月检验费"), ("检验环比", "检验费环比"),
        ("本月其他", "本月其他制造费用"), ("上月其他", "上月其他制造费用"),
        ("其他环比", "其他制造费用环比"),
    ]
    # ⚠️ 值**保留 `%`**，是否去重交给 `fill()` 判断。
    #    原因：同一个占位符在不同节的模板写法不同——
    #      2.2 节 `{{人工环比}}`      （模板不带 %）
    #      3.2 节 `{{人工环比}}%`     （模板自带 %）
    #    一刀切剥掉会让 2.2 变成 `+2.84`（缺 %）。故由 fill 看后一个字符决定。
    _check_unique(pairs)
    for tpl_name, fact_name in pairs:
        v = _fact_display(fs, fact_name)
        if v is not None:
            m[tpl_name] = v

    # 波动阈值告警结论（赛题 5.2.3）——**脚本算，不由 LLM 写**
    m["波动告警结论"] = threshold_verdict(evaluate_threshold(fs))

    # 制造费用合计 = 五类之和（客户端算，不依赖 LLM）
    tot = sum(fs.facts[f"本月{n}"].value for n in RF.MFG_TEMPLATE_NAMES.values()
              if fs.facts.get(f"本月{n}") and fs.facts[f"本月{n}"].value is not None)
    tot_p = sum(fs.facts[f"上月{n}"].value for n in RF.MFG_TEMPLATE_NAMES.values()
                if fs.facts.get(f"上月{n}") and fs.facts[f"上月{n}"].value is not None)
    if tot:
        m["制造费用合计"] = f"{tot:.2f}"
        m["上月制造费用合计"] = f"{tot_p:.2f}"
        d = RF._pct(tot, tot_p)
        if d is not None:
            m["制造费用合计环比"] = f"{d:+.2f}"

    # 表格填充位
    m["原材料成本明细表格"] = render_materials_table(fs)
    m["近6个月成本趋势表格"] = render_trend_table(fs)
    m["原材料价格跟踪表格"] = render_market_table(fs)
    m["对标差异表格"] = render_peer_table(fs)
    # 知识库引用（本项目 "RAG" 要求的落点）
    for k, tpl in WIKI_LINKS.items():
        m[k] = tpl.format(p=fs.product)
    return m


# ---------------------------------------------------------------------------
# 二、模板填充
# ---------------------------------------------------------------------------

_PH = re.compile(r"\{\{([^{}]+)\}\}")


def fill(template: str, values: Dict[str, str]) -> tuple[str, List[str]]:
    """
    返回 (填充后文本, 未填占位符列表)。

    `%` 去重：模板有些地方自带 `%`（`{{产量环比}}%`），有些地方不带
    （2.2 节的 `{{人工环比}}`）。值统一带 `%`，这里若发现占位符后面紧跟 `%`
    就剥掉值里那个，避免出现 `+2.84%%`。
    """
    missing: List[str] = []

    def sub(m: re.Match) -> str:
        name = m.group(1).strip()
        if name not in values:
            missing.append(name)
            return "—"          # 缺失显式呈现，不留空
        v = values[name]
        if template[m.end():m.end() + 1] == "%" and v.endswith("%"):
            v = v[:-1]
        return v

    out = _PH.sub(sub, template)
    # 收敛连续空行：**可选的占位符**（如 `{{重点分析段落}}`）留空时，
    # 模板里为它预留的前后空行会叠成 2-3 行空白，PDF 里看着像排版事故。
    # markdown 里 1 个空行与 3 个空行语义相同，故统一压到 1 个。
    # （报告无围栏代码块，不存在"代码块内空行有意义"的情况。）
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out, missing


# ---------------------------------------------------------------------------
# 三、LLM 叙事生成
# ---------------------------------------------------------------------------

ANALYSIS_SYSTEM = """你是中药一厂财务部的成本分析撰写员。

**你的唯一职责是把给定的事实写成通顺的分析文字。你不做任何计算。**

铁律（违反即不合格）：
1. **只许使用我给你的数字。** 不许自己算、不许引入外部知识里的数字、不许估算。
2. **不要编造根因。** 归因只许用给定的「已知归因」；没有就写"需进一步核查"。
3. 单位一律 `元/盒`，**不得换算**成元/支或元/袋。
4. 涉及成本波动，**不得归因于设备故障**（raw 中提取罐全年无故障）。
5. 若事实里标了 `Disputed`，引用时必须注明"该口径存在争议"。
6. 严格按要求的字数写。**宁短勿长。**
7. 全程中文输出，不要英文思考过程。
"""


def _llm_client():
    from dotenv import load_dotenv
    for p in [WIKI_ROOT, *WIKI_ROOT.parents]:
        env = p / ".env"
        if env.exists():
            load_dotenv(dotenv_path=env)
            break
    from langchain_openai import ChatOpenAI
    import httpx
    key = os.getenv("DEEPSEEK_API_KEY", "")
    if not key:
        raise SystemExit("未找到 DEEPSEEK_API_KEY")
    return ChatOpenAI(
        model=os.getenv("AGENT_MODEL", "deepseek-chat"),
        api_key=key,
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        temperature=0.2,
        http_client=httpx.Client(headers={"Accept-Encoding": "identity"}),
        http_async_client=httpx.AsyncClient(headers={"Accept-Encoding": "identity"}),
    )


def _facts_brief(fs: RF.FactSet) -> str:
    """给 LLM 看的「算术结论」——不含原始 CSV，只有算好的值 + 坐标。"""
    lines = [f"产品 {fs.product}｜分析月 {fs.month}｜对照月 {fs.prev_month}｜去年同月 {fs.year_ago_month}", ""]
    lines.append("【核心指标】")
    for k in ("本月单位成本", "上月单位成本", "单位成本环比", "去年单位成本", "单位成本同比",
              "单位成本预算偏差", "本月材料成本", "上月材料成本", "材料成本环比",
              "本月人工成本", "人工成本环比", "本月制造成本", "制造费用环比"):
        f = fs.facts.get(k)
        if f:
            lines.append(f"  {k} = {f.display}{f.unit or ''}"
                         + (f"   （{f.formula}）" if f.formula else ""))
    lines.append("")
    lines.append("【成本结构】")
    for k in ("材料占比", "人工占比", "制造费用占比", "材料贡献度", "人工贡献度", "制造费用贡献度"):
        f = fs.facts.get(k)
        if f:
            lines.append(f"  {k} = {f.display}")
    lines.append("")
    lines.append("【近 6 个月单位成本】")
    lines.append("  " + "  ".join(f"{r['月份']}={r['单位成本']}({r['环比']})" for r in fs.monthly))
    lines.append("")
    if fs.materials:
        lines.append("【原材料环比】")
        lines.append("  " + "；".join(f"{m['原材料名称']} {m['上月']}→{m['本月']} ({m['环比']})"
                                       for m in fs.materials))
        lines.append("")
    if fs.peer:
        lines.append("【一厂 vs 二厂】")
        for p in fs.peer:
            lines.append(f"  {p['维度']}: 一厂 {p['中药一厂']} / 二厂 {p['中药二厂']} "
                         f"差异 {p['差异']} ({p['差异率']}) {p['方向']}")
        lines.append("")
    lines.append("【已知归因（只许用这些）】")
    lines.append("  · 金银花：产地减产、市场行情上涨（官方归因）")
    lines.append("  · 5 月为全品类药材行情高峰")
    lines.append("")
    if fs.conflicts:
        lines.append("【口径争议（引用须注明）】")
        for c in fs.conflicts:
            lines.append(f"  · {c}")
        lines.append("")
    if fs.missing:
        lines.append("【数据缺失（不得编造）】")
        for m in fs.missing:
            lines.append(f"  · {m}")
    return "\n".join(lines)


def generate_analysis(fs: RF.FactSet, llm: Any, verbose: bool = True,
                      threshold: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """让 LLM 写叙事段落 + 改进建议。一次调用拿一个结果，字段小、可控。

    ⚠️ 「重点分析段落」**只在脚本判定确有要素超 ±10% 时才生成**。
       无触发却让模型写，它会写出一段"看起来像告警"的文字——
       这正是本库最不能要的东西：**没有告警的地方不该有告警的语气**。
    """
    brief = _facts_brief(fs)
    out: Dict[str, str] = {}
    total = 0

    # 赛题 5.2.3：超阈值 → 重点分析段落。结论已由脚本钉死，这里只让 LLM 解释成因。
    th = threshold if threshold is not None else evaluate_threshold(fs)
    if th["breaches"]:
        detail = "、".join(f"{n} {v}" for n, v in th["breaches"])
        prompt = (
            f"## 事实（脚本已算好，只许引用）\n\n{brief}\n\n"
            f"## 已确定的判定（不要质疑、不要改数）\n"
            f"以下成本要素环比变动**已超过 ±{th['threshold']:.0f}% 告警阈值**：{detail}\n\n"
            f"## 任务\n写「重点分析段落」，200-300 字。要求：\n"
            f"① 逐项说明每个超阈值要素涨跌的成因（材料须点到具体药材）；\n"
            f"② 结合上面近 6 个月趋势与一厂/二厂对比给出判断；\n"
            f"③ 结尾给出可执行的应对方向。\n"
            f"**只许引用上面的数字，不许新造数字、不许重算。**\n"
            f"直接输出正文，不要标题、不要前后缀。")
        try:
            r = llm.invoke([("system", ANALYSIS_SYSTEM), ("user", prompt)])
            out["重点分析段落"] = (r.content or "").strip()
        except Exception as e:  # noqa: BLE001
            out["重点分析段落"] = f"（生成失败：{type(e).__name__}）"
            print(f"    ⚠️ 重点分析段落: {e}")
        total += len(out.get("重点分析段落", ""))
        if verbose:
            print(f"    ✓ 重点分析段落  {len(out.get('重点分析段落',''))} 字"
                  f"（触发的要素：{detail}）")
    else:
        out["重点分析段落"] = ""

    for name, spec in LLM_SCALAR_FIELDS.items():
        prompt = (f"## 事实（这些是脚本算好的，只许引用，不许改动或重算）\n\n{brief}\n\n"
                  f"## 任务\n请写「{name}」。要求：{spec}\n"
                  f"直接输出正文，不要标题、不要前后缀、不要 markdown 代码块。")
        try:
            r = llm.invoke([("system", ANALYSIS_SYSTEM), ("user", prompt)])
            txt = (r.content or "").strip()
        except Exception as e:  # noqa: BLE001
            txt = f"（生成失败：{type(e).__name__}）"
            print(f"    ⚠️ {name}: {e}")
        total += len(txt)
        out[name] = txt
        if verbose:
            print(f"    ✓ {name}  {len(txt)} 字")

    # 3.3 五条变动说明（每条一句话）
    mfg_lines = "；".join(f"{m['类别']} {m['上月']}→{m['本月']} ({m['环比']})" for m in fs.mfg)
    for cat in LLM_MFG_NOTES:
        key = cat.replace("变动说明", "")
        prompt = (f"## 事实\n\n{brief}\n\n制造费用五类：{mfg_lines}\n\n"
                  f"## 任务\n用**一句话（10-20 字）**说明「{key}」本月环比变动的原因。"
                  f"若变动不足 1%，写「基本持平」。直接输出那句话，不要前缀。")
        try:
            r = llm.invoke([("system", ANALYSIS_SYSTEM), ("user", prompt)])
            out[cat] = (r.content or "").strip()
        except Exception as e:  # noqa: BLE001
            out[cat] = "（生成失败）"
        total += len(out[cat])

    # 6.3 改进建议 → 结构化，供 6.3 表格 + 6.4 整改任务共用
    prompt = (f"## 事实\n\n{brief}\n\n"
              "## 任务\n给出 3 条改进建议。**只输出 JSON 数组**，每项字段：\n"
              '{"建议事项":"…","责任部门":"生产部|采购部|财务部|质量部",'
              '"优先级":"高|中|低","预期效果":"…","建议完成时间":"YYYY-MM-DD"}\n'
              "不得输出 JSON 以外的任何内容。建议须针对本产品本月实际数据。")
    out["_advice"] = "[]"
    try:
        r = llm.invoke([("system", ANALYSIS_SYSTEM), ("user", prompt)])
        raw = (r.content or "").strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
        out["_advice"] = raw
        if verbose:
            print(f"    ✓ 改进建议  3 条")
    except Exception as e:  # noqa: BLE001
        print(f"    ⚠️ 改进建议失败: {e}")

    out["_total_chars"] = total
    return out


# ---------------------------------------------------------------------------
# 四、编排
# ---------------------------------------------------------------------------

def build(product: str, month: str, use_llm: bool = True,
          verbose: bool = True,
          template_docx: Optional[str] = None,
          docx_out_path: Optional[str] = None) -> Dict[str, Any]:
    fs = RF.build(product, month)
    today = date.today().isoformat()

    # 先收集**全部**值，最后**只填一次**。
    # ⚠️ 不要"先填一次、拿到 LLM 文本再填第二次"——第一次填充会把未提供的
    #    占位符换成 `—`，占位符就没了，第二次无从替换（本库踩过这个坑）。
    vals = deterministic_map(fs, today)
    # 无超阈值要素（或未启用 LLM）时留空——`fill` 对空串不显示 `—`，
    # 而对"未提供的占位符"会显示 `—`。这里必须先占位，否则无告警的报告
    # 会出现一个孤零零的 `—`。
    vals["重点分析段落"] = ""

    # 波动告警结论先由脚本定死，再决定要不要让 LLM 写重点分析
    th = evaluate_threshold(fs)
    if verbose:
        n = len(th["breaches"])
        print(f"  波动阈值判定（脚本，±{th['threshold']:.0f}%）："
              + (f"{n} 项超阈值 → 将生成重点分析" if n else "无要素超阈值 → 不生成"))

    analysis: Dict[str, Any] = {}
    if use_llm:
        if verbose:
            print(f"  交给 LLM 写 {len(LLM_SCALAR_FIELDS)} 段分析 + {len(LLM_MFG_NOTES)} 条制造费用说明…")
        analysis = generate_analysis(fs, _llm_client(), verbose=verbose, threshold=th)
        try:
            advice = json.loads(analysis.get("_advice", "[]"))
            if not isinstance(advice, list):
                advice = []
        except (json.JSONDecodeError, TypeError):
            advice = []
        vals["改进建议表格"] = render_advice_table(advice)
        vals["整改任务表格"] = render_task_table(advice, month, fs.product)
        for k, v in analysis.items():
            if not k.startswith("_"):
                vals[k] = v

    # ---- 套模板 ----
    # `template_docx` 非空时走 **Word 模板**（赛题 5.1.1）：
    #   直接填那份 .docx 并保存，保留它的字体/表格样式/大纲。
    # 否则走默认的 .md 模板（产出 markdown，再按需导出 Word/PDF）。
    docx_out: Optional[Path] = None
    if template_docx is not None:
        import template_docx as TD
        docx_out, missing = TD.fill_docx(
            Path(template_docx), vals, Path(docx_out_path or (OUT_DIR / f"{product}_{month}.docx")),
            blank_keys={"重点分析段落"})
        report = f"（已按 Word 模板生成：{docx_out.name}）"
    else:
        template = TEMPLATE.read_text(encoding="utf-8")
        report, missing = fill(template, vals)

    # 统计
    stats = {
        "product": product, "month": month,
        "deterministic_fields": len([k for k in vals if not k.startswith("_")]),
        "missing": sorted(set(missing)),
        "llm_chars": analysis.get("_total_chars", 0),
        "facts": len(fs.facts),
        "conflicts": fs.conflicts,
        "data_missing": fs.missing,
    }
    return {"report": report, "facts": fs, "stats": stats, "analysis": analysis,
            "docx": docx_out}


def main() -> int:
    ap = argparse.ArgumentParser(description="6 章月度成本分析报告生成器")
    ap.add_argument("--product", required=True)
    ap.add_argument("--month", default="2026-05")
    ap.add_argument("--out", help="输出路径（默认 reports/<product>_<month>.md）")
    ap.add_argument("--dry-run", action="store_true", help="只填确定性部分，不调 LLM")
    ap.add_argument("--json", action="store_true", help="额外输出统计 JSON")
    ap.add_argument("--template-docx", metavar="PATH",
                    help="改用 Word 模板（赛题 5.1.1）；不传则用默认 .md 模板")
    a = ap.parse_args()

    if a.dry_run:
        print("=" * 74)
        print(f"确定性填充（--dry-run，不调用 LLM）  {a.product} {a.month}")
        print("=" * 74)
        r = build(a.product, a.month, use_llm=False, verbose=False,
                  template_docx=a.template_docx, docx_out_path=a.out)
        print(f"  已填占位符 {r['stats']['deterministic_fields']} 个")
        print(f"  仍待 LLM 填 {len(r['stats']['missing'])} 个：")
        for m in r["stats"]["missing"]:
            print(f"    · {m}")
        return 0

    print("=" * 74)
    print(f"生成报告  {a.product}  {a.month}")
    print("=" * 74)
    r = build(a.product, a.month, use_llm=True,
              template_docx=a.template_docx, docx_out_path=a.out)
    if r.get("docx"):
        # Word 模板模式：产物就是那份 docx，不再写 md
        out = Path(r["docx"])
    else:
        out = Path(a.out) if a.out else (OUT_DIR / f"{a.product}_{a.month}.md")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(r["report"], encoding="utf-8", newline="\n")

    print()
    print(f"✅ 已生成 {out.relative_to(WIKI_ROOT)}")
    print(f"   确定性占位符 {r['stats']['deterministic_fields']} 个")
    print(f"   LLM 分析文本 {r['stats']['llm_chars']} 字")
    if r["stats"]["missing"]:
        print(f"   ⚠️ 未填占位符 {len(r['stats']['missing'])} 个: "
              f"{', '.join(r['stats']['missing'][:6])}")
    if r["stats"]["data_missing"]:
        print(f"   ⚠️ 数据缺失 {len(r['stats']['data_missing'])} 项（未编造）")
    if a.json:
        print(json.dumps(r["stats"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
