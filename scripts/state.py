# -*- coding: utf-8 -*-
"""
state.py — **关键数值快照**（派生的可检索层）

它在这一层的什么位置
--------------------
    raw/（不可变权威）      ← 第一真相。改了就是改历史，禁止。
      ↓ 编译
    wiki/（累积知识 + 坐标） ← 第二真相。冲突用 Status 块显式标注。
      ↓ 蒸馏（本模块）
    state/metrics.json       ← **纯派生视图**。可重生成、可校验，**绝不手改**。

判据：**state 与 wiki 不一致时，永远是 state 错。** 因为它是从 wiki 抽出来的。

为什么要它
----------
1. **快**：查一个关键数值不必扫全库——`lookup` 直接命中。
2. **可回溯**：每条都带坐标（wiki 章节 → 再回指 raw 文件），两跳可查证。
3. **可 diff**：稳定 ID（`entity|metric|period`）让"值变了吗"一目了然。
   这是现有机制做不到的——`check_evidence.py` 只验"字面量在不在"，**不说"值变了没"**。
4. **给报告用的取值清单**：报告可以直接查 state，不必反复检索。

它**不是**什么
--------------
- ❌ 不是新的事实来源。没有 wiki 里查不到的独有内容。
- ❌ 不是人工维护的。任何手工编辑都会被 `verify` 标为漂移。
- ❌ 不替代 `get_metric`。那个是**实时全库核对**，这个是**快照取数**；
  取值存疑时应回退到 `get_metric` + `trace_citation`。

抽取规则
--------
只抽**可结构化的数值三元组** `(entity, metric, period) → value`。

- **entity**：实体列（产品名称/药材名称/工厂/原材料/指标/设备…）的值；
  该列不存在时，从章节标题推断。
- **period**：`月份`/`日期` 列的值；或列名本身是 `N月`（宽表形态）。
- **metric**：列名（去掉单位括号）。列名是纯期间（如 `1月`）时回退到章节语义。
- **跳过**：序号、编号、规格型号、制造商、来源、评价、说明等非指标列。
"""
from __future__ import annotations

import hashlib
import io
import os
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()

# ⚠️ 与 ingest_tools 同一套约定：可由 `LLM_WIKI_WS` 指向个人工作区。
#    个人区的 state 落在自己的工作区里，**不写共享基线的 state/**。
_CODE_ROOT = Path(__file__).resolve().parent.parent
WIKI_ROOT = Path(os.getenv("LLM_WIKI_WS") or _CODE_ROOT)
STATE_DIR = WIKI_ROOT / "state"
STATE_FILE = STATE_DIR / "metrics.json"

SCHEMA_VERSION = 1

# 实体列（行内标识"这是什么"）
ENTITY_COLS = ("产品名称", "药材名称", "原材料", "工厂", "指标",
               "设备编号", "设备名称", "费用类别", "产品", "药材",
               # 分组列（表格按此分组，值只在首行给出，需 forward-fill）
               # ⚠️ 不要把 `月份` 放进来 —— 它同时属于 PERIOD_COLS，
               #    两用会让"期间"被当成"实体"，本库差点犯这个错。
               "车间", "产品类别", "条目", "类别")
# 期间列
PERIOD_COLS = ("月份", "日期", "周期", "时间")
# 非指标列（不抽为数值）
SKIP_COLS = ("序号", "编号", "数量", "质量标准", "药材来源", "规格型号", "制造商",
             "价格来源", "趋势", "对标评价", "影响", "说明", "备注", "风险等级",
             "材料来源", "变更内容", "版本", "占位符组", "信源", "证据定位",
             "来源文件", "置信度", "类型", "证据坐标", "计算", "异常月份", "波动判定",
             # 以下不是业务指标，是结构性计数（文档清单一类的元信息）
             "行数", "物理台数", "台账条目", "页数", "#", "序号", "数量合计")

# 行标签列：**这一列的值是"指标名"**（如对标表的「成本要素」= 单位成本/直接材料）。
# 数值列的 metric 名要把它拼进去，否则同一张表的所有行会撞同一个 ID
#   —— 本库实测：对标表因未拼行标签，产生 9 条 ID 相同的记录，diff 失效。
LABEL_COLS = ("成本要素", "指标", "项目", "维度", "类别", "费用类别", "费用名称",
              "工序", "原料名称", "原材料名称", "占位符", "物料")

# 期间列名形态：`1月` / `2026-05` / `P25`
_PERIOD_HDR_RE = re.compile(r"^(\d{1,2})月$")

# 数值（含千分位、百分号、负数、正号）；**用 \d+ 而非 \d{1,3}**，否则会切碎 11250.0
_NUM_RE = re.compile(r"^([-+]?\d+(?:,\d{3})*(?:\.\d+)?)\s*(%|万元|元)?$")


@dataclass
class Metric:
    """一条关键数值。ID 稳定 → 可 diff。"""
    id: str              # `entity|metric|period`（稳定，便于比对）
    entity: str
    metric: str
    period: str
    value: float
    unit: str
    article: str         # 相对 wiki/ 的路径
    section: str         # 所属章节标题
    line: int            # 在词条中的行号（便于回看）
    coord: str = ""      # 行内证据坐标（表格里若带「证据坐标」列）
    disputed: bool = False   # 所在章节是否含 Status 块（已知争议）

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# 表格解析
# ---------------------------------------------------------------------------

_SEP_RE = re.compile(r"^\|[\s:\-|]+\|$")


def _cells(line: str) -> List[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def iter_tables(text: str) -> Iterable[Tuple[List[str], List[List[str]], int]]:
    """遍历 markdown 表格，yield (表头, 数据行, 表头所在行号)。"""
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("|") and i + 1 < len(lines) and _SEP_RE.match(lines[i + 1].strip()):
            hdr = _cells(lines[i])
            rows, j = [], i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                rows.append(_cells(lines[j]))
                j += 1
            yield hdr, rows, i + 1
            i = j
        else:
            i += 1


def _num(s: str) -> Tuple[Optional[float], str]:
    """解析 `**3.50**` / `45,000` / `64.6%` → (值, 单位后缀)。失败返回 (None, '')。"""
    t = s.strip().strip("*").strip("`").strip()
    if not t or t in ("—", "-", "–", "【缺失】", "N/A", "无"):
        return None, ""
    m = _NUM_RE.match(t)
    if not m:
        return None, ""
    try:
        return float(m.group(1).replace(",", "")), (m.group(2) or "")
    except ValueError:
        return None, ""


def _unit_of(col: str, suffix: str) -> str:
    """单位：优先列名括号内（`产量(盒)` → 盒），否则取数值后缀（`%`）。"""
    m = re.search(r"[（(]([^）)]+)[）)]", col)
    if m:
        return m.group(1).strip()
    return suffix


# ---------------------------------------------------------------------------
# 章节上下文
# ---------------------------------------------------------------------------

_H2H3_RE = re.compile(r"^(#{2,3})\s+(.*)$")


def _sections(text: str) -> List[Tuple[int, str, int]]:
    """返回 [(起始行, 标题, 结束行)]，用于判断某行属于哪节、该节有无 Status 块。"""
    lines = text.split("\n")
    marks = [(i, m.group(2).strip()) for i, l in enumerate(lines)
             if (m := _H2H3_RE.match(l))]
    out = []
    for k, (i, title) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else len(lines)
        out.append((i, title, end))
    return out


def _section_for(line_no: int, secs: List[Tuple[int, str, int]]) -> str:
    cur = ""
    for start, title, end in secs:
        if start <= line_no < end:
            cur = title
    return cur


def _is_disputed(line_no: int, secs: List[Tuple[int, str, int]], lines: List[str]) -> bool:
    """该行所属章节内有无 Status 块。"""
    for start, _t, end in secs:
        if start <= line_no < end:
            return any("**Status:" in lines[i] for i in range(start, min(end, len(lines))))
    return False


# 已知实体（用于从章节标题推断 entity）
_ENTITY_HINT = re.compile(
    r"(银黄口服液|板蓝根颗粒|六味地黄胶囊|金银花|黄芩提取物|板蓝根|熟地黄|山茱萸|"
    r"山药|泽泻|茯苓|牡丹皮|蔗糖|糊精|空心胶囊|苯甲酸钠|纯化水|"
    r"中药一厂|中药二厂)"
)

# 类别词 → 规范实体名。用于**章节标题即分组标签**的情形
# （如 `### 2.1 口服液类（对应银黄口服液）`，表内并无「产品类别」列）。
# 不映射的话，同一指标在多张分表里会撞同一个 ID。
_CATEGORY_ALIAS = [
    ("口服液类", "银黄口服液"),
    ("颗粒剂类", "板蓝根颗粒"),
    ("胶囊剂类", "六味地黄胶囊"),
]


def _from_section(sec: str, title: str) -> str:
    """
    从章节标题/词条标题推断 entity。

    顺序：① 已知实体名（`_ENTITY_HINT`）
          ② 类别词映射（口服液类 → 银黄口服液）
          ③ 退化为章节标题本身（保证 ID 唯一，哪怕名字长）
    """
    m = _ENTITY_HINT.search(sec) or _ENTITY_HINT.search(title)
    if m:
        return m.group(1)
    for kw, ent in _CATEGORY_ALIAS:
        if kw in sec:
            return ent
    s = re.sub(r"^\d+[\.、]\s*", "", sec).strip()
    return s or title


# ---------------------------------------------------------------------------
# 抽取
# ---------------------------------------------------------------------------

def _article_title(text: str) -> str:
    for l in text.split("\n")[:6]:
        if l.startswith("# "):
            return l[2:].strip()
    return ""


def extract_from_article(path: Path, wiki_dir: Path) -> List[Metric]:
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    secs = _sections(text)
    rel = path.relative_to(wiki_dir).as_posix()
    title = _article_title(text)

    out: List[Metric] = []
    for hdr, rows, hdr_line in iter_tables(text):
        ncol = len(hdr)
        sec = _section_for(hdr_line - 1, secs)

        # ---- 给每列定性 ----
        wide_period: Dict[int, str] = {}   # 列索引 → 期间（`1月` 这类表头）
        entity_col: Optional[int] = None
        label_col: Optional[int] = None    # 行标签列（值为指标名）
        period_col: Optional[int] = None
        coord_col: Optional[int] = None
        metric_cols: List[int] = []

        # 数据列取样，判断是否数值列
        col_numeric = [0] * ncol
        for r in rows:
            for i in range(min(ncol, len(r))):
                if _num(r[i])[0] is not None:
                    col_numeric[i] += 1
        nrows = max(1, len(rows))

        for i, h in enumerate(hdr):
            hh = h.strip().strip("*")
            # ⚠️ **`指标` 优先当行标签，不当实体**。
            #    它在表里的作用是"这一行是什么指标"（如「材料成本占比」），
            #    而"这是什么实体/类别"通常写在章节标题里。
            #    若误当实体，同一指标在多张分表里会撞同一个 ID（本库踩过）。
            if hh in LABEL_COLS and label_col is None:
                label_col = i
                # 但仍可作为实体的**兜底**来源（当章节推断不出时）
                if hh in ENTITY_COLS and entity_col is None:
                    entity_col = i
                continue
            if hh in ENTITY_COLS and entity_col is None:
                entity_col = i
                continue
            if hh in PERIOD_COLS and period_col is None:
                period_col = i
                continue
            if "坐标" in hh or hh in ("信源坐标",):
                coord_col = i
                continue
            m = _PERIOD_HDR_RE.match(hh)
            if m:
                wide_period[i] = f"2026-{int(m.group(1)):02d}"   # 年份由章节/数据推，暂用 2026
                continue
            if hh in SKIP_COLS:
                continue
            if re.search(r"（[^）]*）|\([^)]*\)", hh) and "元" not in hh and "kg" not in hh \
                    and "盒" not in hh and "袋" not in hh and "粒" not in hh and "支" not in hh:
                # 括号里不是单位（如 `工序`、`关键工艺参数(CPP)`）→ 非指标
                continue
            if col_numeric[i] >= max(1, nrows // 2):     # 过半数为数值 → 指标列
                metric_cols.append(i)

        if not metric_cols:
            continue

        # ---- 逐行产出 ----
        #
        # **entity 向下填充（forward-fill）**：markdown 里常把同一实体的多行
        # 合并成"首行写实体、后续行留空"（分组表）。若不填充，同组各行会撞同一个 ID。
        #   例（对标表）：| **银黄口服液** | 单位成本 | … | / | | 直接材料 | … |
        prev_ent = ""
        for ri, r in enumerate(rows):
            if len(r) < ncol:
                r = r + [""] * (ncol - len(r))
            ln = hdr_line + 2 + ri

            # entity 的确定顺序：
            #   ① 表内实体列（含向下填充）—— **但若该列同时是行标签列，跳过**
            #      因为那种列的值是"指标名"（如「材料成本占比」），不是实体名。
            #      典型：行业基准表 `| 指标 | P25 | P50 | …`，实体其实在章节标题里。
            #   ② 章节/词条标题推断（含类别别名映射）
            #   ③ 退化为章节标题
            ent = ""
            entity_col_usable = (entity_col is not None
                                 and entity_col != label_col)
            if entity_col_usable and entity_col < len(r):
                col_ent = re.sub(r"[`*]", "", r[entity_col]).strip()
                col_ent = re.sub(r"^\s*—+\s*$", "", col_ent)
                if col_ent:
                    prev_ent = col_ent
                elif prev_ent:
                    col_ent = prev_ent
                ent = col_ent
            if not ent:
                ent = _from_section(sec, title)
            ent = re.sub(r"^\d+[\.、]\s*", "", ent).strip() or sec or title

            # period
            per = ""
            if period_col is not None and period_col < len(r):
                per = r[period_col].strip().strip("`")
            if not per and re.match(r"^\d{4}-\d{2}$", per or ""):
                pass

            coord = ""
            if coord_col is not None and coord_col < len(r):
                coord = r[coord_col].strip().strip("`").strip()

            # 行标签：本行的"指标名"。拼进 metric 名，避免同表各行撞 ID。
            #   例（对标表）：行标签「单位成本」+ 列名「一厂」 → metric = "单位成本·一厂"
            row_label = ""
            if label_col is not None and label_col < len(r):
                row_label = re.sub(r"[`*]", "", r[label_col]).strip()

            disputed = _is_disputed(ln - 1, secs, lines)

            # 宽表：每个期间列各自产出一条
            #   行标签在场时，用「行标签·列名」作 metric（期间已有独立字段，
            #   无需再拼「1月」这类列名，那只会让名字变长而信息重复）
            for i in wide_period:
                if i >= len(r):
                    continue
                v, suf = _num(r[i])
                if v is None:
                    continue
                if row_label:
                    mname = row_label
                else:
                    mname = _clean_metric(hdr[i], sec, title)
                out.append(_mk(ent, mname, wide_period[i], v,
                               _unit_of(hdr[i], suf), rel, sec, ln, coord, disputed))

            # 长表：每个指标列产出一条
            for i in metric_cols:
                v, suf = _num(r[i])
                if v is None:
                    continue
                base = _clean_metric(hdr[i], sec, title)
                mname = f"{row_label}·{base}" if row_label and row_label != base else base
                out.append(_mk(ent, mname, per, v,
                               _unit_of(hdr[i], suf), rel, sec, ln, coord, disputed))
    return out


def _clean_metric(col: str, section: str, title: str) -> str:
    """
    指标名。列名是**纯期间**（如 `1月`）或泛化（`值`）时，回退到章节语义——
    这种情况通常表的"是什么指标"写在章节标题或前文里。
    """
    c = re.sub(r"[`*]", "", col).strip()
    c = re.sub(r"[（(][^）)]*[）)]", "", c).strip()   # 去单位括号
    if not c or _PERIOD_HDR_RE.match(c) or c in ("值", "数值"):
        s = re.sub(r"^\d+[\.、]\s*", "", section or title).strip()
        return s or "（见章节）"
    return c


def _mk(ent, metric, period, value, unit, rel, sec, ln, coord, disputed) -> Metric:
    """
    ID = `article#|entity|metric|period`。

    ⚠️ **为什么 ID 里要带 article**：同一个 `(实体, 指标, 期间)` 在**不同词条**
    里合法地可以有不同的值——它们描述的是"该词条语境下的这个数"。
      - 「蔗糖 · 单盒成本」在银黄口服液里是 `0.10`、在板蓝根颗粒里是 `0.30`
        （同一种原料，不同产品配方的用量不同）
      - 「材料成本占比 · P25」在口服液类/颗粒剂类/胶囊剂类三张表里分别是 55/52/60
        （同一指标，不同产品类别）
    不带 article 就会把它们压成同一条 ID，**diff 时表现为"值在乱跳"**，
    而实际是抽样自不同语境。带上后，ID 稳定且语义自足。
    """
    return Metric(
        id=f"{rel}|{ent}|{metric}|{period}",
        entity=ent, metric=metric, period=period,
        value=value, unit=unit, article=rel, section=sec,
        line=ln, coord=coord, disputed=disputed,
    )


# ---------------------------------------------------------------------------
# 构建 / 校验 / 查询
# ---------------------------------------------------------------------------

def _wiki_signature(wiki_dir: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(wiki_dir.rglob("*.md")):
        if p.name in ("index.md", "log.md"):
            continue
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def extract_all(wiki_dir: Optional[Path] = None) -> List[Metric]:
    base = wiki_dir or (WIKI_ROOT / "wiki")
    out: List[Metric] = []
    for p in sorted(base.rglob("*.md")):
        if p.name in ("index.md", "log.md"):
            continue
        out.extend(extract_from_article(p, base))
    return out


def build(verbose: bool = True, wiki_dir: Optional[Path] = None) -> Dict[str, Any]:
    """抽取并写入 state/metrics.json。"""
    base = wiki_dir or (WIKI_ROOT / "wiki")
    ms = extract_all(base)
    sig = _wiki_signature(base)
    doc = {
        "schema_version": SCHEMA_VERSION,
        "wiki_signature": sig,
        "count": len(ms),
        "metrics": [m.to_dict() for m in ms],
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # newline="\n" 必须显式写：默认 None 会在 Windows 上把每个 \n 翻成 \r\n，
    # 使派生视图带上平台相关的换行符，diff 噪音大且跨平台不可复现。
    STATE_FILE.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                          encoding="utf-8", newline="\n")
    if verbose:
        n_dis = sum(1 for m in ms if m.disputed)
        print(f"state 已建立：{STATE_FILE.relative_to(WIKI_ROOT)}")
        print(f"  {len(ms)} 条关键数值（其中 {n_dis} 条位于含争议的章节）")
        print(f"  wiki 签名 {sig}")
    return doc


def load() -> Optional[Dict[str, Any]]:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def verify(wiki_dir: Optional[Path] = None) -> Dict[str, Any]:
    """
    校验 state 是否与 wiki 同步。

    返回 `{ok, wiki_signature, stale, added, removed, changed}`：
      - `stale=True` 表示 state 是旧的（wiki 变过）
      - `changed` 是**同 ID 但值不同**的条目 —— 这就是"关键数值变更日志"
    """
    base = wiki_dir or (WIKI_ROOT / "wiki")
    doc = load()
    if doc is None:
        return {"ok": False, "reason": "state 不存在，请先 build"}

    cur_sig = _wiki_signature(base)
    old = {m["id"]: m for m in doc.get("metrics", [])}
    new = {m.id: m for m in extract_all(base)}

    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = []
    for k in sorted(set(old) & set(new)):
        a, b = old[k], new[k]
        if abs(float(a["value"]) - new[k].value) > 1e-9 or a.get("unit", "") != new[k].unit:
            changed.append({"id": k, "was": a["value"], "now": new[k].value,
                            "was_unit": a.get("unit", ""), "now_unit": new[k].unit})

    stale = cur_sig != doc.get("wiki_signature")
    return {
        "ok": not (stale or added or removed or changed),
        "stale": stale,
        "wiki_signature": cur_sig,
        "state_signature": doc.get("wiki_signature"),
        "added": added, "removed": removed, "changed": changed,
    }


def lookup(entity: str = "", metric: str = "", period: str = "",
           doc: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """从快照里按条件取值（子串匹配，空条件表示不限）。"""
    doc = doc if doc is not None else load()
    if not doc:
        return []
    out = []
    for m in doc.get("metrics", []):
        if entity and entity not in m["entity"]:
            continue
        if metric and metric not in m["metric"]:
            continue
        if period and period not in m["period"]:
            continue
        out.append(m)
    return out


def format_lookup(rows: List[Dict[str, Any]], limit: int = 30) -> str:
    if not rows:
        return ("state 中无匹配条目。\n"
                "   → 若确信库里该有，先跑 `python scripts/state.py --build` 重建快照。")
    out = [f"state 命中 {len(rows)} 条（快照，非实时；取值存疑请回退 get_metric）:", ""]
    for m in rows[:limit]:
        u = f" {m['unit']}" if m.get("unit") else ""
        flag = "  ⚠️含争议" if m.get("disputed") else ""
        out.append(f"  {m['entity']} · {m['metric']}" + (f" · {m['period']}" if m['period'] else "")
                   + f"  =  {m['value']}{u}{flag}")
        loc = f"     来源: {m['article']}:L{m['line']}"
        if m.get("section"):
            loc += f"  §{m['section'][:40]}"
        out.append(loc)
        if m.get("coord"):
            out.append(f"     坐标: {m['coord'][:100]}")
    if len(rows) > limit:
        out.append(f"  … 另有 {len(rows) - limit} 条")
    return "\n".join(out)


# ---------------------------------------------------------------------------

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="关键数值快照（派生自 wiki，可重生成）")
    ap.add_argument("--build", action="store_true", help="重建快照")
    ap.add_argument("--verify", action="store_true", help="校验快照是否与 wiki 同步")
    ap.add_argument("--lookup", action="store_true", help="查询快照")
    ap.add_argument("--entity", default="")
    ap.add_argument("--metric", default="")
    ap.add_argument("--period", default="")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.build:
        build()
        return 0
    if a.verify:
        r = verify()
        if a.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
            return 0
        if r.get("reason"):
            print(f"❌ {r['reason']}")
            return 1
        if r["ok"]:
            print(f"✅ state 与 wiki 同步（签名 {r['wiki_signature']}）")
            return 0
        print(f"⚠️  state 已过期或与 wiki 不一致：")
        print(f"   wiki 签名: state={r['state_signature']} 当前={r['wiki_signature']}"
              f"{'  ← 已变' if r['stale'] else ''}")
        if r["changed"]:
            print(f"\n   ⭐ 值发生变化 {len(r['changed'])} 条（关键数值变更日志）：")
            for c in r["changed"][:20]:
                print(f"     {c['id']}  {c['was']} → {c['now']}")
        if r["added"]:
            print(f"\n   新增 {len(r['added'])} 条：{r['added'][:8]}")
        if r["removed"]:
            print(f"\n   消失 {len(r['removed'])} 条：{r['removed'][:8]}")
        print("\n   → 跑 `python scripts/state.py --build` 重建")
        return 2

    if a.lookup or a.entity or a.metric or a.period:
        rows = lookup(a.entity, a.metric, a.period)
        if a.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            print(format_lookup(rows))
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
