# -*- coding: utf-8 -*-
"""
report_tools.py — **连接层**：把 llm-wiki 暴露给主 agent（报告生成侧）

与摄入侧的分工
--------------
    摄入 agent (ingest_tools.py)  →  读 raw + **写** wiki
    主 agent   (report_tools.py)  →  读 wiki + **只读** raw（回溯证据）→ 出报告

本模块提供的全部工具都是**只读**的：主 agent 不改知识库。

五条设计原则
------------
1. **检索走混合（BM25 + 向量 + RRF）**，而非子串匹配——带排序、带语义召回。
2. **结果自带证据坐标**（`[三产品成本基线.md:1. 银黄口服液]`），
   主 agent 可直接写进报告，不必自己编坐标。
3. **命中含 Status 块的章节时，冲突随结果返回** —— 「自我发现矛盾」的第一层。
   主 agent 因此**不可能看不到**已知矛盾。
4. **取数工具带全库交叉核对**（`get_metric`）—— 取一个值的同时看到它在各来源的分布，
   避免"只取一处就当唯一真相"。
5. **坐标可回溯**（`trace_citation`）—— 从词条坐标反查到 raw 原文，
   支撑报告"每个数字都能溯源"的要求。
"""
from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402
from langchain_core.tools import tool      # noqa: E402
import retrieval as R                      # noqa: E402

ensure_utf8_stdout()

# 本库默认根；主 agent 可用 --wiki-root 指向别处
DEFAULT_ROOT = Path(__file__).resolve().parent.parent


def _wiki_dir(root: Path) -> Path:
    return root / "wiki"


def _raw_dir(root: Path) -> Path:
    return root / "raw"


def _rel(p: Path, base: Path) -> str:
    try:
        return p.relative_to(base).as_posix()
    except ValueError:
        return str(p)


def _load_graph(root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """
    读因果图谱（派生视图）。

    图谱是 `graph_build.py` 从 wiki/raw 抽出来的，位置在 `<root>/graph/graph.json`。
    **不在这里构造**——只读。缺失时返回 None，由调用方提示重建。

    之所以做成"每次都重读文件"而不是缓存实例：图谱是会变的派生视图，
    缓存会让 agent 用上过期的知识。这点开销（~几十 KB JSON）可以接受。
    """
    p = (root or DEFAULT_ROOT) / "graph" / "graph.json"
    if not p.exists():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return {"nodes": {n["id"]: n for n in doc.get("nodes", [])},
            "edges": doc.get("edges", [])}


def _graph_types(g: Dict[str, Any]) -> Dict[str, str]:
    return {nid: n.get("type", "") for nid, n in g["nodes"].items()}


def build_report_tools(root: Optional[Path] = None,
                       overlay: Optional[Path] = None) -> List:
    """
    构造绑定到某个 wiki 根的**只读**工具集。

    用工厂而非模块级工具，是为了让主 agent 能指向任意 wiki
    （与 `retrieval.py` 的可注入设计一致）。

    :param root: **代码仓库根**。放规范、scripts —— 永不接受用户输入。
    :param overlay: 某个用户的**个人工作区**（`data/users/<id>/ws`）。
                    给了它就走 overlay 语义：读 = 共享基线 + 该用户的增量，
                    个人同名覆盖共享。

    ⚠️ **两个根不能合并**：`root` 是可执行规范所在处，用户上传一个
       `SKILL.md` 就能改掉整个摄入规则 —— 那等于让被审的人写审计标准。
       `overlay` 只放用户自己的 wiki/raw，权限上写不到 `root`。
    """
    ROOT = Path(root) if root else DEFAULT_ROOT
    WIKI = _wiki_dir(ROOT)
    RAW = _raw_dir(ROOT)

    # overlay 模式下的两个根。单根模式时 shared 为空、personal 就是 ROOT 自己。
    if overlay is not None:
        OV_SHARED: Optional[Path] = ROOT
        OV_PERSONAL: Optional[Path] = Path(overlay)
    else:
        OV_SHARED, OV_PERSONAL = None, None

    def _ov_args() -> Dict[str, Any]:
        return ({"shared_root": OV_SHARED, "personal_root": OV_PERSONAL}
                if OV_PERSONAL is not None else {})

    def _src_tag(tag: str) -> str:
        """来源标签 → 给模型看的措辞。"""
        return "公司基线" if tag == "shared" else "你的资料"

    # ---------------------------------------------------------------- 检索
    @tool
    def search_wiki(query: str, top_k: int = 6) -> str:
        """
        【首选检索】在知识库中做混合检索（BM25 词法 + 向量语义 + RRF 融合）。

        检索范围 = **公司共享基线 + 你自己的资料**（个人同名覆盖共享）。

        返回按相关度排序的**章节**，每节附带：
        - **来源层**：`[公司基线]` 或 `[你的资料]`
        - 行内证据坐标 `[词条.md:章节名]`，可直接写进报告
        - 命中的来源（bm25/vec 各自名次）
        - **若该节含已知冲突（Status 块），一并返回** —— 引用前必须先读冲突说明

        ⚠️ 标着 `[你的资料]` 的只代表**当前用户自己**上传的内容，
           **不等于公司口径**。引用时必须说明这一点，不要当通用结论用。

        :param query: 自然语言查询，如 '银黄口服液 5 月成本上涨原因'
        :param top_k: 返回条数（默认 6）
        """
        hits = R.hybrid_search(query, top_k=top_k, root=ROOT,
                               allow_online=False, **_ov_args())
        if not hits:
            # 离线索引缺失时，给一次在线重建的机会
            try:
                R.build_index(verbose=False, root=ROOT, **_ov_args())
                hits = R.hybrid_search(query, top_k=top_k, root=ROOT,
                                       allow_online=False, **_ov_args())
            except Exception as e:  # noqa: BLE001
                return (f"检索失败，且无法重建索引：{type(e).__name__}: {e}\n"
                        f"   → 请先运行 `python scripts/retrieval.py --rebuild`")
        # overlay 模式必须标来源——否则模型会把"某个用户自己传的资料"
        # 当成公司口径来引用。
        return R.format_hits(hits, with_source=OV_PERSONAL is not None)

    @tool
    def list_articles() -> str:
        """
        列出知识库的全部词条（路径 + 标题 + **来源层**）。
        用于了解库里有什么，再决定读哪篇。
        """
        if OV_PERSONAL is not None:
            import overlay as OV
            pairs = OV.overlay_articles(OV_SHARED, OV_PERSONAL)
        else:
            pairs = [(p, "shared") for p in sorted(WIKI.rglob("*.md"))
                     if p.name not in ("index.md", "log.md")]
        if not pairs:
            return "知识库暂无词条"
        n_p = sum(1 for _, s in pairs if s == "personal")
        out = [f"知识库共 {len(pairs)} 篇词条"
               + (f"（公司基线 {len(pairs) - n_p} 篇 + 你的资料 {n_p} 篇）"
                  if OV_PERSONAL is not None else "：")]
        for p, s in pairs:
            title = ""
            for line in p.read_text(encoding="utf-8", errors="replace").split("\n")[:4]:
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
            rel = (p.relative_to(OV_PERSONAL / "wiki").as_posix()
                   if s == "personal" and OV_PERSONAL is not None
                   else _rel(p, WIKI))
            tag = f"[{_src_tag(s)}]" if OV_PERSONAL is not None else ""
            out.append(f"  {tag} {rel:<45} {title}")
        return "\n".join(out)

    @tool
    def read_article(path: str) -> str:
        """
        读取一篇词条的**完整内容**。需要细读某一节的完整表格时用它
        （search_wiki 返回的是截断片段）。

        :param path: 相对 wiki/ 的路径，如 'costs/三产品成本基线.md'
        """
        # overlay 模式：个人层优先（`overlay_lookup` 就是这个语义）
        if OV_PERSONAL is not None:
            import overlay as OV
            hit = OV.overlay_lookup(path, OV_SHARED, OV_PERSONAL)
            if hit is None:
                return f"词条不存在: {path}。先调 list_articles 看有哪些。"
            p, tag = hit
            rel = (p.relative_to(OV_PERSONAL / "wiki").as_posix()
                   if tag == "personal" else _rel(p, WIKI))
            return (f"📄 [{_src_tag(tag)}] {rel}\n"
                    + ("⚠️ 这是你自己上传的资料，非公司口径。\n" if tag == "personal" else "")
                    + "\n" + p.read_text(encoding="utf-8", errors="replace"))

        p = (WIKI / path).resolve()
        if not str(p).startswith(str(WIKI.resolve())):
            return f"路径越出 wiki 目录: {path}"
        if not p.exists():
            cands = list(WIKI.rglob(Path(path).name))
            if not cands:
                return f"词条不存在: {path}。先调 list_articles 看有哪些。"
            p = cands[0]
        return f"📄 {_rel(p, WIKI)}\n\n" + p.read_text(encoding="utf-8", errors="replace")

    # ---------------------------------------------------------------- 取数
    @tool
    def get_metric(metric: str, entity: str = "") -> str:
        """
        【取数首选】取一个指标的**全部来源取值**并做交叉核对。

        与 search_wiki 的区别：那个返回"相关章节"，这个返回"**这个数在各处的取值**"。
        写报告的数字时用它——它会同时显示其他来源的值，**避免把单处取值当成唯一真相**。

        输出含：
        - wiki 词条中的记载（带坐标）
        - raw/ 中的原始取值（带文件与行号）
        - **若不同来源不一致，明确列出**（可能是真矛盾 / 口径差异 / 时点差异 / 主体差异）

        :param metric: 指标名，如 '单位成本'、'金银花'、'提取收率'
        :param entity: 可选，限定实体，如 '银黄口服液'
        """
        out: List[str] = [f"指标「{metric}」"
                          + (f"（实体：{entity}）" if entity else "") + " 的取值分布：", ""]

        # --- ① wiki 词条中的记载 ---
        out.append("【wiki 词条记载】")
        w_hits: List[Tuple[str, int, str]] = []
        for f in sorted(WIKI.rglob("*.md")):
            if f.name in ("index.md", "log.md"):
                continue
            for i, ln in enumerate(
                    f.read_text(encoding="utf-8", errors="replace").split("\n"), 1):
                if metric in ln and (not entity or entity in ln):
                    w_hits.append((_rel(f, WIKI), i, ln.strip()))
        if w_hits:
            for rel, i, ln in w_hits[:12]:
                out.append(f"  {rel}:L{i}  {ln[:140]}")
            if len(w_hits) > 12:
                out.append(f"  … 另有 {len(w_hits) - 12} 处")
        else:
            out.append("  （wiki 中未直接出现该指标）")

        # --- ② raw/ 中的原始取值 ---
        #
        # ⚠️ **CSV 必须按列名查，不能按行文本查** —— 宽表里指标名只在**表头**，
        #    数据行只有数字。用行匹配会得到"raw 中未找到"的假阴性。
        #    （本库在 conflict.py 与 ingest_tools.cross_check_metric 都踩过这个坑。）
        out.append("")
        out.append("【raw/ 原始取值】")
        r_lines: List[str] = []
        _NUMRE = r"^[-+]?\d+(?:,\d{3})*(?:\.\d+)?$"

        for f in sorted(RAW.rglob("*")):
            if not f.is_file() or f.name.startswith("."):
                continue
            ext = f.suffix.lower()

            # ---- CSV：列名匹配 ----
            if ext == ".csv":
                import csv as _csv
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
                hit_cols = [c for c in cols if c and metric in c]
                ent_cols = [c for c in ("产品名称", "药材名称", "工厂", "费用类别",
                                        "指标", "原材料名称") if c in cols]
                vals: List[str] = []
                for ln, r in enumerate(rows, start=2):
                    if entity and entity not in [str(r.get(c, "")).strip() for c in ent_cols]:
                        continue
                    per = str(r.get("月份", "")).strip()
                    fac = str(r.get("工厂", "")).strip()
                    tag = " ".join(x for x in (fac, per) if x)
                    for c in hit_cols:
                        v = str(r.get(c, "")).strip()
                        if re.match(_NUMRE, v):
                            vals.append(f"    L{ln}: `{c}` = {v}"
                                        + (f"   [{tag}]" if tag else ""))
                if vals:
                    r_lines.append(f"  📄 {_rel(f, RAW)}（列名匹配，{len(vals)} 个取值）")
                    r_lines.extend(vals[:16])
                    if len(vals) > 16:
                        r_lines.append(f"    … 另有 {len(vals) - 16} 个")

            # ---- 文本：行级匹配 ----
            else:
                if ext not in (".txt", ".md"):
                    continue
                hits = []
                for i, ln in enumerate(
                        f.read_text(encoding="utf-8", errors="replace").split("\n"), 1):
                    if metric in ln and (not entity or entity in ln):
                        hits.append(f"    L{i}: {ln.strip()[:150]}")
                if hits:
                    r_lines.append(f"  📄 {_rel(f, RAW)}（行级匹配，{len(hits)} 行）")
                    r_lines.extend(hits[:10])
                    if len(hits) > 10:
                        r_lines.append(f"    … 另有 {len(hits) - 10} 行")

        if r_lines:
            out.extend(r_lines)
        else:
            out.append("  （raw 中未找到；若确信存在，请确认该指标的**列名写法**）")

        out.append("")
        out.append("─" * 62)
        out.append("⚠️ **引用前请判读**：若同一指标在不同来源取值不同，可能是")
        out.append("   ① 真矛盾  ② 口径差异（定额/实际）  ③ 时点差异（1月/2月）  ④ 主体差异（一厂/二厂）")
        out.append("   前两者须在报告中显式说明；后两者应分别列出并标明差异来源。**不得取平均，不得混用。**")
        return "\n".join(out)

    # ---------------------------------------------------------------- 快照
    @tool
    def lookup_state(entity: str = "", metric: str = "", period: str = "") -> str:
        """
        【快速取数】从**关键数值快照**（`state/metrics.json`）直接取值。

        与 `get_metric` 的区别：
        - `get_metric` 是**实时**扫全库，慢但最新，且做跨来源交叉核对
        - `lookup_state` 是**快照**，快，适合已知要找什么的场景

        适用：写报告时需要快速确认某个既有数值。
        不适用：**取值存疑时**——快照可能是旧的，此时应回退 `get_metric` + `trace_citation`。

        每条结果都带来源词条:行号，可进一步溯源。
        带 `⚠️含争议` 标记的，说明该值所在章节有 Status 块。

        :param entity: 实体（子串匹配），如 '银黄口服液'
        :param metric: 指标（子串匹配），如 '单位成本'
        :param period: 期间（子串匹配），如 '2026-05'
        """
        import state as S
        doc = S.load()
        if not doc:
            return ("state 快照不存在。请先运行 `python scripts/state.py --build`，"
                    "或改用 `get_metric`（实时全库检索）。")
        # 快照可能过期 —— 先提示
        v = S.verify(ROOT / "wiki")
        warn = ""
        if not v.get("ok"):
            warn = ("⚠️ **快照可能已过期**（wiki 已变动）：\n"
                    + (f"   值变化 {len(v['changed'])} 条、新增 {len(v['added'])}、"
                       f"消失 {len(v['removed'])}\n" if 'changed' in v else "")
                    + "   → 建议改用 `get_metric`（实时），或重建快照\n\n")
        rows = S.lookup(entity, metric, period, doc=doc)
        return warn + S.format_lookup(rows)

    # ---------------------------------------------------------------- 溯源
    @tool
    def trace_citation(citation: str) -> str:
        """
        【溯源】把一条行内证据坐标反查到 **raw/ 原文**。

        报告的"每个数字都能溯源"要求，靠这个工具兑现。

        :param citation: 坐标，如 '三产品成本基线.md:1. 银黄口服液'
                         或带列名/筛选条件的形式 '中药一厂_成本汇总_2026年1-6月.csv:单位成本(元/盒):银黄口服液&2026-05'
        """
        # 坐标格式是**三段**：`文件:列名:筛选条件`
        #   例：中药一厂_成本汇总_2026年1-6月.csv:单位成本(元/盒):银黄口服液&2026-05
        # 注意列名本身可能含 `:`（罕见），但筛选条件一定在**最后一个** `:` 之后。
        # 旧写法 `parts[1]` 会丢掉筛选条件 —— 本库踩过，导致"行筛选失效、返回全表"。
        parts = citation.split(":")
        fname = parts[0].strip()
        needle = ":".join(parts[1:]).strip() if len(parts) > 1 else ""

        # 在 raw/ 中找同名文件（含派生 .txt）
        # ⚠️ overlay 模式下**个人 raw 优先**：用户上传的 CSV 与共享同名时，
        #    该用户引用的是他自己那份（与 wiki 层的"个人优先"一致）。
        _raw_dirs = [RAW]
        if OV_PERSONAL is not None:
            _raw_dirs = [OV_PERSONAL / "raw", RAW]
        cands = [f for d in _raw_dirs if d.exists()
                 for f in d.rglob("*")
                 if f.is_file() and (f.name == fname or f.stem == Path(fname).stem)]
        if not cands:
            # 也可能是 wiki 词条名 → 提示改用 read_article
            w = [f for f in WIKI.rglob("*") if f.is_file() and f.stem == Path(fname).stem]
            if w:
                return (f"`{fname}` 是**词条**而非 raw 文件。\n"
                        f"   词条内容请用 `read_article('{_rel(w[0], WIKI)}')`；\n"
                        f"   若要找它引用的证据，请用词条 `Raw:` 字段里的文件名再调本工具。")
            return f"在 raw/ 中找不到文件 `{fname}`。请用 list_articles / search_wiki 确认名称。"

        out = [f"坐标 `{citation}` 的原始出处：", ""]

        # 把坐标的筛选条件拆成"要匹配的片段"：
        #   '单位成本(元/盒):银黄口服液&2026-05'
        #   → 列名候选 ['单位成本']，行筛选 ['银黄口服液','2026-05']
        cond = needle
        col_part, _, row_part = cond.partition(":")
        col_keys = [k for k in re.split(r"[()（）\s]+", col_part) if len(k) >= 2]
        row_keys = [k for k in re.split(r"[&,、\s]+", row_part) if len(k) >= 2]

        for f in cands[:2]:
            txt = f.read_text(encoding="utf-8", errors="replace")
            lines = txt.split("\n")
            out.append(f"📄 {_rel(f, RAW)}  （{len(lines)} 行）")

            # ---- CSV：按行筛选条件定位到**具体数据行** ----
            if f.suffix.lower() == ".csv":
                import csv as _csv
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
                if rows:
                    cols = list(rows[0].keys())
                    # 列名匹配要**精确**：'单位成本(元/盒)' 不能因为含 '元/盒'
                    # 就匹配上 '直接材料(元/盒)'。
                    # 策略：① 完整子串 ② 第一个关键词的最长匹配，取唯一/最前者。
                    hit_cols: List[str] = []
                    if col_part:
                        hit_cols = [c for c in cols if c == col_part]
                        if not hit_cols:
                            hit_cols = [c for c in cols if c and col_part in c]
                        if not hit_cols and col_keys:
                            first = col_keys[0]           # 如 '单位成本'
                            cands_ = [c for c in cols if c and first in c]
                            # 收敛：优先"最短"（最贴近关键词，少带修饰）
                            hit_cols = sorted(cands_, key=len)[:1]
                    matched = []
                    for ln, r in enumerate(rows, start=2):
                        rowtext = ",".join(str(v) for v in r.values())
                        if row_keys and not all(k in rowtext for k in row_keys):
                            continue
                        matched.append((ln, r))
                    if matched:
                        for ln, r in matched[:6]:
                            # 先给表头列名，再给该行的值
                            if hit_cols:
                                for c in hit_cols[:3]:
                                    out.append(f"  L{ln}: **{c}** = {str(r.get(c,'')).strip()}")
                                    if not row_keys:
                                        break
                            else:
                                out.append(f"  L{ln}: " + ",".join(
                                    f"{k}={v}" for k, v in list(r.items())[:8]))
                        if len(matched) > 6:
                            out.append(f"  … 另有 {len(matched) - 6} 行匹配")
                    else:
                        out.append(f"  （无行匹配 {'&'.join(row_keys)}）")
                    out.append("")
                    continue

            # ---- 文本：行级关键词匹配 ----
            keys = col_keys + row_keys
            hits = [(i, l) for i, l in enumerate(lines, 1)
                    if (not keys or any(k in l for k in keys))]
            if hits:
                for i, l in hits[:10]:
                    out.append(f"  L{i}: {l.strip()[:160]}")
                if len(hits) > 10:
                    out.append(f"  … 另有 {len(hits) - 10} 行")
            else:
                out.append("  （未匹配；文件前 5 行供参考）")
                for i, l in enumerate(lines[:5], 1):
                    out.append(f"  L{i}: {l.strip()[:160]}")
            out.append("")
        return "\n".join(out)

    # ---------------------------------------------------------------- 冲突
    @tool
    def list_known_conflicts() -> str:
        """
        列出知识库中**已知的冲突**（所有 `Status` 块）。

        写报告前调用它，明确哪些结论有争议、不能单方面下断言。
        返回每条冲突所在的词条、章节与冲突块原文。
        """
        out: List[str] = []
        # overlay 模式：扫合并视图（个人层的冲突也要能看到）
        if OV_PERSONAL is not None:
            import overlay as OV
            _files = OV.overlay_articles(OV_SHARED, OV_PERSONAL)
        else:
            _files = [(f, "shared") for f in sorted(WIKI.rglob("*.md"))
                      if f.name not in ("index.md", "log.md")]
        for f, _tag in _files:
            if f.name in ("index.md", "log.md"):
                continue
            txt = f.read_text(encoding="utf-8", errors="replace")
            if "**Status:" not in txt:
                continue
            rel = _rel(f, WIKI)
            if _tag == "personal" and OV_PERSONAL is not None:
                rel = f"[你的资料] " + f.relative_to(
                    OV_PERSONAL / "wiki").as_posix()
            # 按行扫描，收集每个 Status 块的起始
            lines = txt.split("\n")
            cur: List[str] = []
            for ln in lines:
                if ln.startswith("> **Status:"):
                    if cur:
                        out.append((rel, "\n".join(cur)))
                    cur = [ln]
                elif cur and ln.startswith(">"):
                    cur.append(ln)
                elif cur:
                    out.append((rel, "\n".join(cur)))
                    cur = []
            if cur:
                out.append((rel, "\n".join(cur)))

        if not out:
            return "未发现已知冲突（无 Status 块）。"
        res = [f"知识库中共有 **{len(out)}** 处已知冲突（Status 块）：", ""]
        for n, (rel, blk) in enumerate(out, 1):
            res.append(f"[{n}] 出自 {rel}")
            res.append("    " + blk.replace("\n", "\n    ")[:600])
            res.append("")
        res.append("⚠️ 报告涉及这些结论时，**必须注明争议**，不得单方面断言。")
        return "\n".join(res)

    # ---------------------------------------------------------------- 自检
    @tool
    def run_evidence_check() -> str:
        """
        跑机械证据校验器，报告结构层问题（索引缺失/死链/证据错误）。
        报告定稿前调用一次，确认知识库自身健康。
        """
        import subprocess
        checker = ROOT / "scripts" / "check_evidence.py"
        if not checker.exists():
            return f"校验器不存在: {checker}"
        try:
            r = subprocess.run(
                [sys.executable, str(checker), str(ROOT)],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=120,
            )
            out = (r.stdout or "") + (r.stderr or "")
        except Exception as e:  # noqa: BLE001
            return f"校验器执行失败: {type(e).__name__}: {e}"
        keep = [l for l in out.split("\n")
                if re.match(r"^(文章|\[1\]|\[2\]|\[3\]|\[4\]|\[5\])", l)]
        return "校验器摘要：\n" + "\n".join(keep[:30])

    # ---- 图谱（多跳关系查询）----
    #
    # ⭐ 这两个工具补的是**检索做不到的事**：
    #    `search_wiki` 找的是"提到某件事的段落"；
    #    图谱穿透给的是"**这个产品经哪些设备、哪台出过故障**"——一条路径。
    #
    # 引擎是 `graph_build.py` 产出的派生图谱（从 wiki/raw 抽取、每条边带坐标），
    # 与 `state/`、`.index/` 同级，可再生。
    @tool
    def graph_query(entity: str) -> str:
        """
        【图谱·关系】查一个实体的**直接关联**（产品→原料/工序、工序→设备…）。

        什么时候用它（而不是 search_wiki）：
        - 你想知道"这个东西**跟什么相连**"，而不是"哪段文字提到了它"
        - 例：`graph_query('银黄口服液')` → 列出它的原料与各道工序

        :param entity: 实体名，如 '银黄口服液' / 'EQ-TQ-001' / '金银花' / '六味地黄胶囊'
        """
        # 个人工作区若已建自己的图谱就用它（含他上传内容抽出的节点），
        # 否则退回共享图谱。
        g = None
        if OV_PERSONAL is not None:
            g = _load_graph(OV_PERSONAL)
        if g is None:
            g = _load_graph(ROOT)
        if g is None:
            return "图谱尚未构建。请先跑 `python scripts/graph_build.py`。"
        if entity not in g["nodes"]:
            near = [n for n in g["nodes"] if entity and entity in n][:6]
            hint = f"\n   相近的实体：" + "、".join(near) if near else \
                   "\n   可用 graph_trace 从产品名开始，或先 search_wiki 确认叫法。"
            return f"图谱中没有节点 `{entity}`。{hint}"

        n = g["nodes"][entity]
        outs = [e for e in g["edges"] if e["src"] == entity]
        ins = [e for e in g["edges"] if e["dst"] == entity]
        L = [f"【{n['type']}】{entity}"]
        if n.get("coord"):
            L.append(f"  坐标 {n['coord']}")
        for k, v in (n.get("attrs") or {}).items():
            if v:
                L.append(f"  {k}: {v}")
        if outs:
            L.append(f"\n→ 指向 {len(outs)} 项：")
            for e in outs[:20]:
                L.append(f"  -[{e['relation']}]→ {e['dst']}")
        if ins:
            L.append(f"\n← 被 {len(ins)} 项指向：")
            for e in ins[:10]:
                L.append(f"  {e['src']} -[{e['relation']}]→")
        return "\n".join(L)

    @tool
    def graph_trace(product: str, max_hops: int = 4) -> str:
        """
        【图谱·穿透】从产品出发做**多跳因果穿透**，找出关联的设备故障或成本异动。

        ⭐ 这是图谱相对"段落检索"的核心价值：给的是**路径**，不只是段落。
        例：`graph_trace('银黄口服液')` 能查出
        「银黄 → 灌装 → EQ-GZ-001 → 2025-11 伺服电机故障」，
        而检索只能找到"提到故障的那段文字"。

        ⚠️ 它会明确告诉你**这次成本异动是不是设备导致的**——
        若不是，不要在归因里写设备故障。

        :param product: 产品名，如 '银黄口服液' / '板蓝根颗粒' / '六味地黄胶囊'
        :param max_hops: 最大跳数（默认 4）
        """
        # 个人工作区若已建自己的图谱就用它（含他上传内容抽出的节点），
        # 否则退回共享图谱。
        g = None
        if OV_PERSONAL is not None:
            g = _load_graph(OV_PERSONAL)
        if g is None:
            g = _load_graph(ROOT)
        if g is None:
            return "图谱尚未构建。请先跑 `python scripts/graph_build.py`。"
        if product not in g["nodes"]:
            return (f"图谱中没有产品 `{product}`。"
                    f"可用：{ '、'.join(sorted(n for n,t in _graph_types(g).items() if t=='product')) }")

        nodes = g["nodes"]
        edges = g["edges"]

        def paths_from(start, hops):
            out = []

            def dfs(node, path, seen):
                if len(path) >= hops or len(out) >= 40:
                    if path:
                        out.append(list(path))
                    return
                nxt = [e for e in edges if e["src"] == node]
                if not nxt:
                    out.append(list(path)); return
                for e in nxt:
                    if e["dst"] in seen:
                        continue
                    path.append(e); dfs(e["dst"], path, seen | {e["dst"]}); path.pop()

            dfs(start, [], {start})
            return [p for p in out if p]

        ps = paths_from(product, max_hops)

        # 1) 找出路径上的真实故障事件
        incidents = []
        for p in ps:
            for e in p:
                if e["relation"] == "发生故障":
                    n = nodes.get(e["dst"])
                    if not n or any(x["id"] == e["dst"] for x in incidents):
                        continue
                    incidents.append({"id": e["dst"], **n.get("attrs", {}),
                                      "equipment_id": e["src"],
                                      "chain": " ".join([product] + [f"-[{x['relation']}]→ {x['dst']}" for x in p]),
                                      "coord": n.get("coord", "")})

        # 2) 该产品的成本异动
        anomalies = [n for n in nodes.values()
                     if n["type"] == "anomaly"
                     and (n.get("attrs") or {}).get("涉及产品", "").strip() == product]

        equip_caused = any("故障" in (a.get("attrs") or {}).get("事件", a.get("label", ""))
                           or "故障" in ((a.get("attrs") or {}).get("涉及产品", "") or "")
                           for a in []) or any(
            "故障" in a["label"] for a in anomalies)

        L = [f"【因果穿透】{product}"]

        if anomalies:
            L.append(f"\n已登记的成本异动（{len(anomalies)} 项）：")
            for a in anomalies:
                at = a.get("attrs") or {}
                L.append(f"  · {a['label']} ｜ {at.get('发生月份','')} ｜ 官方根因：{at.get('官方根因','')}")
        else:
            L.append("\n该产品未登记成本异动。")

        if incidents:
            L.append(f"\n工艺路径上的设备故障（{len(incidents)} 起）：")
            for i in incidents:
                # 设备名里常已含编号（如「口服液灌封一体机 `EQ-GZ-001`」），
                # 再拼一次会重复。先去掉名称中的编号再拼。
                nm = re.sub(r"`?EQ-[A-Z]{2}-\d{3}`?", "", i.get("设备", "")).strip()
                L.append(f"  · {i.get('日期','')} {i['equipment_id']} {nm}："
                         f"{i.get('故障','')}；影响 {i.get('影响','')}")
                L.append(f"    路径 {i['chain']}")
                if i.get("coord"):
                    L.append(f"    坐标 {i['coord']}")
        else:
            L.append("\n工艺路径上**无任何设备故障记录**。")

        L.append("\n⚠️ 归因判定（**粗判，非结论**）：")
        if equip_caused:
            L.append("  该产品**有**设备故障被登记为成本异动事件。")
            L.append("  ⚠️ 但这只说明「事件被登记过」，**不说明量级是否足以解释成本变动**。")
            L.append("     下结论前必须核对：该故障的影响量（减产/停工）换算成成本，")
            L.append("     与同期成本变动是否同量级。**量级不匹配时不得归因于它。**")
        else:
            L.append("  该产品**没有被登记为设备故障致因的**成本异动——")
            L.append("  不要在回答里写设备故障归因。")
            if incidents:
                L.append("  （路径上虽有历史故障，但发生月份与异动窗口不重合，不构成因果。）")
        return "\n".join(L)

    return [
        search_wiki, list_articles, read_article,   # 检索
        get_metric, lookup_state,                    # 取数（实时 / 快照）
        trace_citation,                              # 溯源
        list_known_conflicts,                        # 冲突
        run_evidence_check,                          # 自检
        graph_query, graph_trace,                    # 图谱（多跳关系）
    ]


if __name__ == "__main__":
    ts = build_report_tools()
    print(f"主 agent 只读工具集（{len(ts)} 个）：")
    for t in ts:
        print(f"  - {t.name}")
