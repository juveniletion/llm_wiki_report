# -*- coding: utf-8 -*-
"""
ingest_tools.py — 摄入 agent 的工具集（读 + 写）

设计约束（每条都对应 `SKILL.md` 里的一条规则）
------------------------------------------------
1. **先定位后书写**：`grep_raw` 是写任何数值前的必经步骤。
   工具返回**带行号**的命中，agent 可据此写行内证据坐标。
2. **实体页优先**：`write_wiki_article` **拒绝**批次页式命名
   （`7-8月增量数据` / `2026年8月成本` 之类），强制 agent 先考虑"并入已有页"。
3. **外科编辑**：`patch_wiki_article` 用精确替换，而非整文件覆写
   —— 增量维护的本质是"改一处"，不是"重写一篇"。
4. **不可变源**：所有工具**只读** raw/，没有任何写 raw 的能力。
5. **可验证**：`run_evidence_check` 让 agent 自己跑校验器收尾。

与 `agents/wiki_tools.py` 的关系：那里是**只读**的对话工具；
本模块是**摄入专用**（读 raw + 写 wiki），两者互不干扰。
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from langchain_core.tools import tool

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

# =============================================================================
# 两个根 —— 必须分清
#
#   CODE_ROOT   代码仓库。放校验器（scripts/check_evidence.py）等**工具**。
#               **永远不接受用户输入。**
#   WIKI_ROOT   本次要读写的工作区。默认是代码仓库本身（既有行为不变），
#               但可由环境变量 `LLM_WIKI_WS` 指向某个用户的个人工作区。
#
# ⚠️ 校验器必须挂在 CODE_ROOT 上，不能跟 WIKI_ROOT 走 ——
#    否则用户上传一个假的 check_evidence.py 就能让"机械校验"永远通过。
#
# ⚠️ 为什么用**环境变量**而不是给工厂传参：
#    11 个 @tool 函数体里有 17 处直接引用这些模块级名。
#    改成闭包工厂要动全部 17 处、且极易漏改（漏了就会静默读写错根）。
#    环境变量在**模块导入时求值**，工具体一行都不用改。
#    ⇒ 代价：必须在 import 本模块**之前**设好环境变量（见 ingest_agent.py）。
# =============================================================================

CODE_ROOT = Path(__file__).resolve().parent.parent      # 永远是代码仓库

WIKI_ROOT = Path(os.getenv("LLM_WIKI_WS") or CODE_ROOT)  # 工作区（可注入）
RAW_DIR = WIKI_ROOT / "raw"
WIKI_DIR = WIKI_ROOT / "wiki"
CHECKER = CODE_ROOT / "scripts" / "check_evidence.py"    # 校验器永远来自代码仓库

# 批次页式命名 —— write_wiki_article 会拒绝这些
BATCH_PAGE_PATTERNS = [
    r"\d+\s*[-~至]\s*\d+\s*月",          # 7-8月 / 7~8月 / 7至8月
    r"\d{4}\s*年\s*\d{1,2}\s*月",        # 2026年8月
    r"增量|补充数据|新增数据|批次|本次摄入",
    r"^\d{4}-\d{2}-\d{2}",               # 2026-09-22 开头
]


def _rel(p: Path) -> str:
    try:
        return p.relative_to(WIKI_ROOT).as_posix()
    except ValueError:
        return str(p)


def _safe(p: str) -> Path:
    """
    把相对路径解析到工作区内，拒绝越权。

    ⚠️ 旧实现用 `str(target).startswith(str(WIKI_ROOT))` 做前缀比较，**有洞**：
       `/data/ws-evil/x` 与 `/data/ws` 共享前缀，能通过检查。
    改用 `is_relative_to()`（路径组件级比较，不做字符串匹配）。
    另注意 Windows 下 `resolve()` **不规范化大小写**，
    故比较前统一转小写，避免 `C:\\Users` 与 `c:\\users` 被判为不同。
    """
    root = WIKI_ROOT.resolve()
    target = (root / p).resolve()

    def _norm(x: Path) -> str:
        return str(x).replace("\\", "/").lower()

    if not (_norm(target) == _norm(root)
            or _norm(target).startswith(_norm(root) + "/")):
        raise ValueError(f"路径越出工作区: {p}")
    return target


def _read_text(p: Path) -> str:
    """
    读取文本；**非 UTF-8 时抛出可读的诊断，而不是静默降级**。

    为什么需要：本库曾出现 `wiki/index.md` 含 `\\xa1\\xab`（GBK 残留的半截全角字符），
    导致 `patch_wiki_article` 抛裸 `UnicodeDecodeError` 而**中断整个摄入**，
    agent 完全不知道该文件坏了、更不知道该修哪里。

    现在改为**抛出带诊断的 ValueError**：指出文件、字节位置、坏字节内容，
    并给出处置建议（先查再手工修）。agent 读到这条消息就能自行修复。

    **刻意不用 `errors="replace"` 静默降级**——那会把 `\\xa1\\xab` 变成 `\\ufffd` 写回文件，
    造成**不可逆的内容丢失**。宁可直接失败。
    """
    b = p.read_bytes()
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError as e:
        bad = b[max(0, e.start - 40):e.start + 40]
        raise ValueError(
            f"{_rel(p)} 不是合法 UTF-8（位置 {e.start}，附近字节 {bad!r}）。\n"
            f"   → 这通常是编辑时混入了 GBK 编码的半截字符（如 `\\xa1\\xab` = 全角「～」）。\n"
            f"   → 请先查看该处、手工修正坏字节，再继续。"
            f"**不要**用 errors='replace' 覆盖——那会永久丢失内容。"
        ) from e


def _write_text(p: Path, text: str) -> None:
    """
    写文本；**必须显式 `newline="\\n"`**。

    为什么必须写这个参数：`Path.write_text()` / `open()` 默认 `newline=None`，
    会把文本里的 `\\n` **翻译成本平台的换行**（Windows 上是 `\\r\\n`）。
    而 `_read_text` 是**裸 decode**，不做逆翻译——于是每次"读→改→写"都多留一个 `\\r`：

        第 1 次 patch → `\\r\\n`    第 2 次 → `\\r\\r\\n`    第 9 次 → `\\r×8\\n`

    本库实测踩到：一次摄入做了 9 次 `patch_wiki_article`，结果
    `state.py` 按 `\\n` 切行时把 195 行的词条看成 **1300 行**，
    表格解析整段失效——`state/` 里的关键数值从 555 条**静默掉到 339 条**。

    ⚠️ 危险点是**静默**：不报错、文件还能读、肉眼看着也对，只有 diff 和抽数会异常。
    所以这里显式钉死 `\\n`，让**写入与读取对换行符的处理对称**。
    """
    p.write_text(text, encoding="utf-8", newline="\n")


# ===========================================================================
# 一、读 raw —— 证据来源
# ===========================================================================

@tool
def list_raw_files(topic: str = "") -> str:
    """
    列出 raw/ 下的证据文件。**开始任何编译前先调用它**，明确有哪些证据可用。
    :param topic: 可选，限定子目录（如 'csv/cost_data'、'pdf/pharma_docs'）
    """
    base = RAW_DIR / topic if topic else RAW_DIR
    if not base.exists():
        return f"目录不存在: {topic}"
    files = []
    for f in sorted(base.rglob("*")):
        if f.is_file() and not f.name.startswith("."):
            files.append(f"{_rel(f)}  ({f.stat().st_size:,} B)")
    if not files:
        return f"{_rel(base)} 下无文件"
    return f"raw/ 共 {len(files)} 个文件:\n" + "\n".join(files)


@tool
def grep_raw(pattern: str, path: str = "") -> str:
    """
    【写任何数值前的必经步骤】在 raw/ 中检索字面量，返回**带行号**的命中。

    用途：SKILL.md 要求"先定位后书写"——写入任何数字前必须先在 raw 中找到它。
    未命中即**不得**写该数值的精确形式。

    :param pattern: 要检索的字面量或正则（如 '141.0'、'提取收率'）
    :param path: 可选，限定某个 raw 文件（相对 wiki 根的路径）
    """
    target = _safe(path) if path else RAW_DIR
    if not target.exists():
        return f"文件不存在: {path}"
    files = [target] if target.is_file() else [
        f for f in sorted(target.rglob("*"))
        if f.is_file() and f.suffix.lower() in (".csv", ".txt", ".md", ".py", ".json")
    ]
    try:
        rx = re.compile(pattern)
    except re.error:
        rx = re.compile(re.escape(pattern))

    out: List[str] = []
    total = 0
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").split("\n")
        except Exception:  # noqa: BLE001
            continue
        hits = [(i, l.strip()) for i, l in enumerate(lines, 1) if rx.search(l)]
        if hits:
            total += len(hits)
            out.append(f"\n📄 {_rel(f)}  （{len(hits)} 处命中）")
            for i, l in hits[:12]:
                out.append(f"  L{i}: {l[:160]}")
            if len(hits) > 12:
                out.append(f"  … 另有 {len(hits) - 12} 处")
    if not out:
        return (f"❌ 在 raw/ 中**未找到** `{pattern}`。\n"
                f"   → 按 SKILL.md：**不得写入该数值的精确形式**，"
                f"请删除它或用不含精度的方式陈述。")
    return f"✅ raw/ 中找到 `{pattern}`，共 {total} 处：\n" + "\n".join(out)


@tool
def cross_check_metric(metric: str, entities: str = "") -> str:
    """
    【冲突扫描专用】跨来源核对一个**指标**在各 raw 文件中的全部取值。

    用途（对应 SKILL.md 5.3.1 的强制步骤）：编译任何词条前，对将要写入数值的
    核心指标调用本工具，检查**不同来源是否给出不同值**。

    ⚠️ **必须查全库，不能只看本次的源文件** —— 矛盾多半发生在"新数据 vs 既有数据"之间。

    **结构化数据按结构查**：CSV 里指标名常在**表头**（数据行只有数字），
    因此本工具对 CSV 走「列名匹配」而非「行文本匹配」——否则会漏掉宽表。
    文本文件（PDF/Word 派生文本、md）才用行级匹配。

    本工具只**呈现**各处取值，**不做判定**。同一指标不同值可能是四种情况，
    区分它们需要语义理解（正则做不到、你能）：
     ① 真矛盾（一方写错）        → 标 Disputed
     ② 口径差异（定额 vs 实际）   → 标 Disputed，并说明是口径差异
     ③ 时点差异（1月 vs 2月）     → **不是矛盾**，分别列出即可
     ④ 主体差异（一厂 vs 二厂）   → **不是矛盾**，标明主体即可

    :param metric: 指标关键词，如 '单位成本'、'提取收率'、'维修费'、'金银花'
    :param entities: 可选，逗号分隔的实体名，用于过滤（如 '银黄口服液,板蓝根颗粒'）
    """
    import csv as _csv

    ents = [e.strip() for e in entities.split(",") if e.strip()] if entities else []
    _NUMRE = r"^[-+]?\d+(?:,\d{3})*(?:\.\d+)?$"

    sections: List[str] = []      # 每个文件的呈现块
    total_vals = 0

    for f in sorted(RAW_DIR.rglob("*")):
        if not f.is_file() or f.name.startswith("."):
            continue
        ext = f.suffix.lower()
        if ext not in (".csv", ".txt", ".md", ".py", ".json"):
            continue
        rel = _rel(f)

        # ---------------- CSV：列名匹配（宽表 + 长表） ----------------
        if ext == ".csv":
            rows = None
            for enc in ("utf-8-sig", "utf-8", "gb18030"):
                try:
                    with open(f, encoding=enc, newline="") as fh:
                        rows = list(_csv.DictReader(fh))
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
                except Exception:  # noqa: BLE001
                    rows = None
                    break
            if not rows:
                continue
            cols = list(rows[0].keys())
            # ① 宽表：**列名**含 metric
            wide = [c for c in cols if c and metric in c]
            # ② 长表：行内类别列（费用类别/指标/原材料名称）含 metric
            rowlabel = next((c for c in ("费用类别", "指标", "原材料名称")
                             if c in cols), None)
            valcol = next((c for c in cols if c and ("元/盒" in c or "元/元" in c
                                                     or c.endswith("(元)") or "价格" in c)), None)

            lines: List[str] = []
            for ln, r in enumerate(rows, start=2):
                if ents and not any(
                        e == str(r.get(k, "")).strip()
                        for k in ("产品名称", "药材名称", "工厂") for e in ents):
                    continue
                per = str(r.get("月份", "")).strip()
                fac = str(r.get("工厂", "")).strip()
                tag = " ".join(x for x in (fac, per) if x)
                for c in wide:
                    v = str(r.get(c, "")).strip()
                    if re.match(_NUMRE, v):
                        lines.append(f"   L{ln}: `{c}` = {v}" + (f"   [{tag}]" if tag else ""))
                        total_vals += 1
                if rowlabel and valcol and metric in str(r.get(rowlabel, "")):
                    v = str(r.get(valcol, "")).strip()
                    if re.match(_NUMRE, v):
                        lines.append(f"   L{ln}: `{r[rowlabel]} / {valcol}` = {v}"
                                     + (f"   [{tag}]" if tag else ""))
                        total_vals += 1
            if lines:
                sections.append(f"📄 {rel}  （列名匹配，{len(lines)} 个取值）\n"
                                + "\n".join(lines[:24]))

        # ---------------- 文本：行级匹配 ----------------
        else:
            try:
                tlines = f.read_text(encoding="utf-8", errors="replace").split("\n")
            except Exception:  # noqa: BLE001
                continue
            lines = []
            for i, ln in enumerate(tlines, 1):
                if metric not in ln:
                    continue
                if ents and not any(e in ln for e in ents):
                    continue
                # 抽**完整**数字（不跨分隔符切碎）
                nums = re.findall(r"(?<![\d.])[-+]?\d+(?:,\d{3})*(?:\.\d+)?(?![\d.])", ln)
                nums = [n for n in nums if len(n.replace(",", "").replace("-", "").replace(".", "")) >= 2]
                if not nums:
                    continue
                # 取末尾 4 个（成本文本里值通常在指标词之后）
                lines.append(f"   L{i}: 数值 {', '.join(nums[-4:])}\n        {ln.strip()[:150]}")
                total_vals += len(nums[-4:])
            if lines:
                sections.append(f"📄 {rel}  （行级匹配，{len(lines)} 行）\n"
                                + "\n".join(lines[:16]))

    if not sections:
        return (f"❌ 在 raw/ 全库中未找到指标 `{metric}`"
                + (f"（实体限 {ents}）" if ents else "") + "。\n"
                f"   → 按 SKILL.md：**不得写入该指标的精确数值**。")

    head = [
        f"指标「{metric}」在 raw/ 全库的取值分布"
        + (f"（实体限 {ents}）" if ents else ""),
        f"共 {len(sections)} 个文件命中，{total_vals} 个取值：",
        "",
    ]
    tail = [
        "─" * 66,
        "⚠️ **请自行判读**（本工具不判定）：",
        "   ① 真矛盾       → 写 Status: Disputed",
        "   ② 口径差异     → 写 Status: Disputed，说明是哪种口径差异",
        "   ③ 时点差异     → **不是矛盾**，分别列出即可",
        "   ④ 主体差异     → **不是矛盾**，标明主体（一厂/二厂）",
        "",
        "   ⚠️ 注意：**宽表 CSV 的指标名在表头**，本工具已按列名匹配；",
        "      若你要找的指标在某个文件里出现 0 次，先确认它的列名写法。",
        "   判为真/口径差异的，必须在 log 中记「冲突扫描」一行。",
    ]
    return "\n".join(head + sections + [""] + tail)


@tool
def read_raw(path: str, start: int = 0, limit: int = 120) -> str:
    """
    读取 raw/ 中某个文件的文本（PDF/Word/Excel 读其同名 .txt 派生文本）。
    :param path: 相对 wiki 根的路径
    :param start: 起始行（从 0 计）
    :param limit: 最多读取行数
    """
    p = _safe(path)
    if not p.exists():
        return f"文件不存在: {path}"
    if p.suffix.lower() in (".pdf", ".docx", ".xlsx", ".xls"):
        cand = p.with_suffix(".txt")
        if cand.exists():
            p = cand
        else:
            return (f"{_rel(p)} 是二进制格式且无同名 .txt 派生文本。"
                    f"请先跑 `python scripts/ingest_raw.py {_rel(p)}` 生成派生文本。")
    lines = p.read_text(encoding="utf-8", errors="replace").split("\n")
    seg = lines[start:start + limit]
    head = f"📄 {_rel(p)}  行 {start + 1}-{start + len(seg)} / 共 {len(lines)} 行\n"
    body = "\n".join(f"{start + i + 1}: {l}" for i, l in enumerate(seg))
    return head + body


# ===========================================================================
# 二、读 wiki —— 分诊与定位级联目标
# ===========================================================================

def _overlay_articles() -> List[Tuple[Path, str]]:
    """
    合并视图：**共享基线 + 个人增量** → [(路径, 来源标签)]。

    实现已抽到 `overlay.py` ——**读写两侧共用同一份规则**，
    免得各写一份、慢慢漂移（曾经的教训：摄入侧有、读取侧没有，
    结果用户上传的东西问答时检索不到）。

    这里只是把本模块的两个根（CODE_ROOT / WIKI_DIR）喂进去。
    """
    import overlay as OV
    # 单根模式（未注入工作区时 CODE_ROOT == WIKI_ROOT）：
    # 只有一层，把它当作 personal 传进去即可 —— 别传成两个 None，
    # 那会返回空清单，agent 会以为"库里什么都没有"。
    same = CODE_ROOT == WIKI_ROOT
    return OV.overlay_articles(
        shared_root=None if same else CODE_ROOT,
        personal_root=WIKI_ROOT)


def _overlay_lookup(path: str) -> Optional[Tuple[Path, str]]:
    """
    按相对路径找词条，**个人层优先**。

    先查工作区，再回退到共享基线 —— 这就是 overlay 的读语义。
    返回 (实际文件路径, 来源标签)。
    """
    import overlay as OV
    same = CODE_ROOT == WIKI_ROOT
    return OV.overlay_lookup(
        path,
        shared_root=None if same else CODE_ROOT,
        personal_root=WIKI_ROOT)


@tool
def list_wiki_articles() -> str:
    """
    列出**共享基线 + 个人增量**的全部词条，用于分诊时判断"已有知识是什么"。

    ⚠️ 在个人工作区里，这里看到的是**合并视图**——公司基线的词条也在其中，
    否则 agent 会误判"库是空的"而拒绝编译。
    """
    arts = _overlay_articles()
    if not arts:
        return "词条库为空（共享基线与个人工作区都没有词条）"
    lines = []
    for p, s in arts:
        first = ""
        for l in _read_text(p).split("\n")[:3]:
            if l.startswith("# "):
                first = l[2:].strip()
                break
        try:
            rel = p.relative_to(WIKI_DIR).as_posix()
        except ValueError:
            rel = p.relative_to(CODE_ROOT / "wiki").as_posix()
        mark = "（个人）" if s == "personal" else ""
        lines.append(f"  {rel}{mark}   {first}")
    n_p = sum(1 for _, s in arts if s == "personal")
    return (f"共 {len(arts)} 篇词条"
            f"（共享基线 {len(arts) - n_p} + 个人 {n_p}）：\n" + "\n".join(lines))


@tool
def read_wiki_article(path: str) -> str:
    """
    读取一篇词条。分诊与级联更新前必读。

    ⚠️ **个人层优先**：同名时读个人工作区那份；没有才回退到共享基线。
    返回值会标明来源，让 agent 知道"我读的是基线还是我自己的修订"。
    """
    hit = _overlay_lookup(path)
    if not hit:
        return f"词条不存在: {path}。先调用 list_wiki_articles。"
    p, source = hit
    tag = "个人工作区" if source == "personal" else "共享基线"
    return f"📄 {path}（来自：{tag}）\n\n" + _read_text(p)


@tool
def search_wiki(keyword: str) -> str:
    """
    跨词条全文检索。**分诊与级联都要用**：判断新内容与哪些已有词条相关。

    搜索范围 = **共享基线 + 个人工作区**（合并去重，个人同名优先）。
    只搜 wiki/，不搜 raw/（raw 用 grep_raw）。
    """
    hits: List[str] = []
    for p, src in _overlay_articles():
        try:
            lines = _read_text(p).split("\n")
        except Exception:  # noqa: BLE001
            continue
        found = [(i, l.strip()) for i, l in enumerate(lines, 1)
                 if keyword.lower() in l.lower()]
        if found:
            try:
                rel = p.relative_to(WIKI_DIR).as_posix()
            except ValueError:
                rel = p.relative_to(CODE_ROOT / "wiki").as_posix()
            tag = "（个人）" if src == "personal" else ""
            hits.append(f"\n📄 {rel}{tag}  （{len(found)} 处）")
            for i, l in found[:6]:
                hits.append(f"  L{i}: {l[:150]}")
    if not hits:
        return f"词条库中未检索到 `{keyword}`（已搜共享基线与个人工作区）"
    return f"词条库中找到 `{keyword}`：" + "\n".join(hits)


# ===========================================================================
# 三、写 wiki —— 受约束的编辑
# ===========================================================================

def _reject_batch_name(path: str) -> Optional[str]:
    stem = Path(path).stem
    for pat in BATCH_PAGE_PATTERNS:
        if re.search(pat, stem):
            return (f"❌ 文件名 `{stem}` 像**批次页/日期页**，违反 SKILL.md 第四节 4.1。\n"
                    f"   `wiki` 是累积的实体页，不是摄入归档。请先问：\n"
                    f"     ① 这份内容讲的是**已有词条描述的那个实体**吗？→ 用 "
                    f"`patch_wiki_article` **并入该页**\n"
                    f"     ② 它引入了 wiki 里**没有任何词条能承载的全新实体**吗？→ 才可新建\n"
                    f"     ③ 都不是 → **停下来问人类**\n"
                    f"   批次记录请写进 `append_wiki_log`，不要建页。")
    return None


@tool
def write_wiki_article(path: str, content: str) -> str:
    """
    **新建**一篇词条。⚠️ 只用于 wiki 中从未有过的**全新实体**。
    已有实体的新数据/新发现 → 请用 `patch_wiki_article` 并入原页。

    :param path: 相对 wiki/ 的路径，如 'costs/成本基线.md'
    :param content: 完整 markdown 内容（含元数据头）
    """
    reject = _reject_batch_name(path)
    if reject:
        return reject
    p = _safe(Path("wiki") / path)
    if p.exists():
        return (f"⚠️ {_rel(p)} 已存在。**不得覆盖已有词条**。\n"
                f"   若确实要更新它，请用 `patch_wiki_article` 做精确替换。")
    p.parent.mkdir(parents=True, exist_ok=True)
    _write_text(p, content)
    return f"✅ 已新建 {_rel(p)}  （{len(content):,} 字符）"


@tool
def patch_wiki_article(path: str, old_text: str, new_text: str) -> str:
    """
    **外科式编辑**已有词条：把 `old_text` 精确替换为 `new_text`。
    这是**增量维护的主要手段** —— 新数据让已有页面"变厚"，而不是另起一页。

    用法要点：
    - `old_text` 必须在文件中**唯一**，否则报错（请给更多上下文）
    - 加数据：找到某行，把它替换为"原行 + 新行"
    - 标冲突：在某段后插入 `> **Status: Disputed**` 块
    - 改结论：把旧措辞替换为限定后的新措辞

    :param path: 相对 wiki/ 的路径
    :param old_text: 要被替换的原文（须唯一）
    :param new_text: 替换后的文本
    """
    p = _safe(Path("wiki") / path)
    if not p.exists():
        return f"词条不存在: {path}。新建请用 write_wiki_article。"
    text = _read_text(p)
    n = text.count(old_text)
    if n == 0:
        return (f"❌ 未找到 `old_text`。请先 `read_wiki_article('{path}')` "
                f"核对原文（注意空格与全角标点）。")
    if n > 1:
        return (f"❌ `old_text` 出现 {n} 次，不唯一。请扩充上下文使其唯一。")
    _write_text(p, text.replace(old_text, new_text, 1))
    delta = len(new_text) - len(old_text)
    return (f"✅ 已更新 {_rel(p)}  （{'+' if delta >= 0 else ''}{delta:,} 字符）")


@tool
def append_wiki_log(entry: str) -> str:
    """
    追加一条操作日志到 `wiki/log.md`。**只追加，不修改**（日志是审计轨迹）。
    格式见 SKILL.md 第五节 5.5 或 命名与链接约定.md 第五节。
    :param entry: 完整条目，含 `## [YYYY-MM-DD] ingest | <主词条标题>` 与 Disposition/Raw 子项
    """
    p = WIKI_DIR / "log.md"
    if not p.exists():
        return "wiki/log.md 不存在"
    with open(p, "a", encoding="utf-8", newline="\n") as f:
        f.write("\n" + entry.rstrip() + "\n")
    return f"✅ 已追加日志（{len(entry)} 字符）"


@tool
def run_evidence_check() -> str:
    """
    跑机械证据校验器，返回摘要。**收尾前必调**。
    结构层（索引缺失/死链/证据错误）必须为 0；保真可疑点需人工判读。
    """
    if not CHECKER.exists():
        return f"校验器不存在: {_rel(CHECKER)}"
    try:
        r = subprocess.run(
            [sys.executable, str(CHECKER), str(WIKI_ROOT)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120,
        )
        out = (r.stdout or "") + (r.stderr or "")
    except Exception as e:  # noqa: BLE001
        return f"校验器执行失败: {type(e).__name__}: {e}"
    # 只返回结构化摘要，避免刷屏
    keep = [l for l in out.split("\n")
            if re.match(r"^(文章|\[1\]|\[2\]|\[3\]|\[4\]|\[5\])", l)
            or "死链" in l or "缺失" in l]
    return "校验器输出摘要：\n" + "\n".join(keep[:30])


# ===========================================================================

def build_ingest_tools() -> List:
    """返回摄入 agent 的完整工具集。"""
    return [
        # 读 raw（证据）
        list_raw_files, grep_raw, read_raw,
        # ⭐ 冲突扫描（编译前强制步骤，见 SKILL.md 5.3.1）
        cross_check_metric,
        # 读 wiki（分诊 / 级联定位）
        list_wiki_articles, read_wiki_article, search_wiki,
        # 写 wiki（受约束）
        write_wiki_article, patch_wiki_article, append_wiki_log,
        # 自检
        run_evidence_check,
    ]


if __name__ == "__main__":
    print("摄入 agent 工具集：")
    for t in build_ingest_tools():
        print(f"  - {t.name}: {t.description.splitlines()[0][:60]}")
