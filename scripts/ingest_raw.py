# -*- coding: utf-8 -*-
"""
ingest_raw.py — 新文件采集器（把任意格式接进 llm-wiki）

职责边界（**只做确定性的事**）
------------------------------
本脚本负责：格式解析 → 落位 → 去重 → 派生可 grep 文本 → 找级联候选 → 生成编译简报 → 记日志。

本脚本**不做**：分诊、编译词条、写 Status 块。那些是**语义工作**，交给
`INGEST_AGENT.md` 定义的摄入 agent——因为编译必须"先 grep 到再写"，
而脚本里的 LLM 没有工具，会退化成凭记忆编造。

三种入口（**共用同一个 ingest_file()**）
--------------------------------------
    CLI       python scripts/ingest_raw.py <文件>
    Inbox     python scripts/ingest_raw.py --inbox
    程序化    from ingest_raw import ingest_file     # 网站拖拽 / 管理后台

设计原则：入口变了，规范不变。详见 scripts/hooks.py 与 references/schema.yaml。

支持格式
--------
文本直通（本身可 grep）：.md .txt .csv .py .json Dockerfile
需派生文本（二进制）：  .pdf .docx .xlsx .xls
可选加头（全部格式）：  元数据头写入**派生文本**，原文件保持逐字不变

已知限制（诚实声明，避免误判为 bug）
----------------------------------
1. **Excel 不保留整数的尾零**：单元格里存的数值是 `141`，`141.0` 只是**显示格式**。
   派生文本因此输出 `141` 而非 `141.0`。这**不是**精度截断（SKILL.md 2.1 禁止的是
   `11.21 → 11.2` 这类真实数值损失），但阅读时需知这一点。
   若确需还原显示形态，可读 `cell.number_format`——本脚本不做，因为那会引入
   权威件中并不存在的"精度"。
2. **PDF 表格可能串列**：`fitz` 的 `sort=True` 已大幅改善，但复杂跨页表格仍可能错行。
   **冲突时以 PDF 权威件为准**（见 SKILL.md）。
3. **.docx 的图片/批注不提取**：只转段落与表格。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hooks import (  # noqa: E402
    IngestHook, IngestResult, default_hooks, emit, register_hook, clear_hooks,
    hook_failures, ensure_utf8_stdout,
)

ensure_utf8_stdout()   # 编码包装统一在 hooks 里做，避免重复包装关闭 buffer

# =============================================================================
# 配置
# =============================================================================

WIKI_ROOT = Path(__file__).resolve().parent.parent

# 权威件（逐字保存，永不修改）
AUTHORITATIVE_EXT = {
    ".csv", ".pdf", ".md", ".py", ".docx", ".xlsx", ".xls", ".txt", ".json",
}

# 需生成派生文本的格式（二进制 → 不可 grep）
NEEDS_DERIVED = {".pdf", ".docx", ".xlsx", ".xls"}

# 已知主题目录（既有，冻结）。新增文件优先落这些
KNOWN_TOPICS = [
    "csv/cost_data", "csv/market_data", "pdf/pharma_docs",
    "templates", "rpa", "meta", "docs", "tabular",
]

# 主题推断规则：(正则, 主题, 置信度) —— 按顺序匹配，命中即停
#
# ⚠️ 顺序即优先级。**更专指的模式必须排在更泛的模式前面**。
#    例：一份"药材行情"文档正文里必然出现"成本"二字（如"对成本的影响"），
#    若让泛模式 `成本` 先命中，就会被错分到 cost_data。
TOPIC_RULES: List[Tuple[str, str, str]] = [
    # ---- 强特征（高置信，专指性强，先匹配）----
    (r"成本汇总|原材料消耗明细|制造费用明细|人工工时明细|预算数据", "csv/cost_data", "high"),
    (r"市场行情|药材价格|药材行情|价格行情|行业成本基准|行业基准", "csv/market_data", "high"),
    (r"配方文档|生产工艺文档|设备清单|质量管理规范|GMP", "pdf/pharma_docs", "high"),
    (r"报告模板|成本分析报告模板", "templates", "high"),
    (r"整改任务|mock_rpa|RPA接口|接口文档", "rpa", "high"),
    (r"问题检查报告|考题模拟数据包", "meta", "high"),
    # ---- 弱特征（低置信 → 提示 agent 复核）----
    # ⚠️ 行情/基准类弱特征提前：它们比泛"成本"更专指
    (r"行情|市场价格|价格|基准", "csv/market_data", "low"),
    (r"成本|费用|预算", "csv/cost_data", "low"),
    (r"生产|工艺|配方|质量", "pdf/pharma_docs", "low"),
    (r"模板|报告", "templates", "low"),
    (r"接口|API|契约|字段", "rpa", "low"),
]

# 实体词表：用于找「级联候选」。这里只放稳定的领域实体正字法，
# 其余靠「已有词条标题 + index 条目」动态补充（见 _build_vocabulary）
DOMAIN_TERMS = [
    "银黄口服液", "板蓝根颗粒", "六味地黄胶囊",
    "金银花", "黄芩提取物", "蔗糖", "苯甲酸钠", "纯化水",
    "板蓝根", "糊精", "熟地黄", "山茱萸", "山药", "泽泻", "茯苓", "牡丹皮",
    "空心胶囊", "包装材料",
    "EQ-TQ-001", "EQ-TQ-002", "EQ-JN-006", "EQ-GZ-001", "EQ-KL-002", "EQ-KL-007",
    "中药材一厂", "中药一厂", "中药二厂",
    "提取收率", "单耗", "贡献度", "环比", "同比", "预算偏差",
    "GMP", "RPA", "收率", "物料平衡",
]


# =============================================================================
# 一、格式解析
# =============================================================================

def _read_text_auto(path: Path) -> str:
    """按多种编码尝试读取文本（兼容 UTF-8 BOM / GBK）。"""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _parse_pdf(path: Path) -> str:
    import fitz  # pymupdf
    doc = fitz.open(str(path))
    out = []
    for i, page in enumerate(doc):
        out.append(f"===== PAGE {i + 1}/{len(doc)} =====\n")
        out.append(page.get_text("text", sort=True))
    doc.close()
    return "".join(out)


def _parse_docx(path: Path) -> str:
    import docx
    d = docx.Document(str(path))
    out: List[str] = []
    for para in d.paragraphs:
        if para.text.strip():
            style = para.style.name if para.style else ""
            prefix = "## " if style.lower().startswith("heading") else ""
            out.append(f"{prefix}{para.text}")
    for ti, table in enumerate(d.tables, 1):
        out.append(f"\n### 表格 {ti}\n")
        rows = [[c.text.strip() for c in r.cells] for r in table.rows]
        if not rows:
            continue
        out.append("| " + " | ".join(rows[0]) + " |")
        out.append("|" + "---|" * len(rows[0]))
        for r in rows[1:]:
            out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def _sheet_to_md(rows: List[List[Any]]) -> str:
    rows = [[("" if c is None else str(c)) for c in r] for r in rows]
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return "_(空表)_"
    w = max(len(r) for r in rows)
    rows = [r + [""] * (w - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * w]
    for r in rows[1:]:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def _parse_xlsx(path: Path) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
    out: List[str] = []
    for ws in wb.worksheets:
        out.append(f"\n## Sheet: {ws.title}\n")
        out.append(_sheet_to_md([list(r) for r in ws.iter_rows(values_only=True)]))
    wb.close()
    return "\n".join(out)


def _parse_xls(path: Path) -> str:
    import xlrd
    wb = xlrd.open_workbook(str(path))
    out: List[str] = []
    for ws in wb.sheets():
        out.append(f"\n## Sheet: {ws.name}\n")
        out.append(_sheet_to_md([ws.row_values(i) for i in range(ws.nrows)]))
    return "\n".join(out)


def parse_to_text(path: Path) -> Optional[str]:
    """把任意支持格式转成可 grep 的纯文本。文本直通格式返回原文。"""
    ext = path.suffix.lower()
    try:
        if ext in (".md", ".txt", ".csv", ".py", ".json") or path.name == "Dockerfile":
            return _read_text_auto(path)
        if ext == ".pdf":
            return _parse_pdf(path)
        if ext == ".docx":
            return _parse_docx(path)
        if ext == ".xlsx":
            return _parse_xlsx(path)
        if ext == ".xls":
            return _parse_xls(path)
    except ImportError as e:
        return f"【解析失败: 缺少依赖 {e.name}】"
    except Exception as e:  # noqa: BLE001
        return f"【解析失败: {type(e).__name__}: {e}】"
    return None


# =============================================================================
# 二、落位与去重
# =============================================================================

# 目录 → 允许的扩展名。**防止"内容提到 GMP 的 .txt 被塞进 pdf/ 目录"这类错位**。
# 曾实测出现：一个 .txt 因正文含"综合管理规范/收率"被推断为 pdf/pharma_docs。
#
# ⚠️ 重要设计说明：**`csv/` 前缀是历史命名，表示「表格数据」而非文件格式**。
#    因此 `csv/*` 接受 csv 与 xlsx/xls；`pdf/pharma_docs/` 则严格要求真 PDF，
#    因为那个目录的语义是"PDF 知识文档"。
#    实测教训：若把 `csv/cost_data` 限定为只收 `.csv`，
#    一份 `.xlsx` 格式的月度成本汇总就会无处可去（曾被错落到 `tabular/`）。
TOPIC_EXT_COMPAT: Dict[str, set] = {
    "csv/cost_data":   {".csv", ".xlsx", ".xls", ".md"},   # 表格数据，不限具体格式
    "csv/market_data": {".csv", ".xlsx", ".xls", ".md"},
    "pdf/pharma_docs": {".pdf"},                            # 严格：该目录语义是 PDF 知识文档
    "templates":       {".md", ".docx"},
    "rpa":             {".md", ".py", ".txt", ".json"},     # requirements.txt / Dockerfile 亦归此
    "meta":            {".md", ".txt", ".csv"},
    "docs":            None,                                # None = 不限
    "tabular":         {".xlsx", ".xls", ".csv"},
}


def _topic_accepts_ext(topic: str, ext: str, name: str) -> bool:
    allowed = TOPIC_EXT_COMPAT.get(topic)
    if allowed is None:
        return True
    if name == "Dockerfile":
        return topic == "rpa"
    return ext in allowed


def infer_topic(filename: str, content: str) -> Tuple[str, str]:
    """
    推断主题目录，返回 `(topic, confidence)`。

    **主题必须与扩展名相容**——否则顺延到下一条规则。
    相容性检查见 `TOPIC_EXT_COMPAT`。
    """
    ext = Path(filename).suffix.lower()
    hay = filename + "\n" + content[:4000]

    for pat, topic, conf in TOPIC_RULES:
        if re.search(pat, hay) and _topic_accepts_ext(topic, ext, filename):
            return topic, conf

    # 兜底：按扩展名分流，**不猜主题**
    return ("tabular" if ext in (".xlsx", ".xls") else "docs"), "unknown"


def _hash_index_path(wiki_root: Path) -> Path:
    return wiki_root / "raw" / ".hashes.json"


def _load_hashes(wiki_root: Path) -> Dict[str, str]:
    p = _hash_index_path(wiki_root)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _save_hashes(wiki_root: Path, idx: Dict[str, str]) -> None:
    p = _hash_index_path(wiki_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(idx, ensure_ascii=False, indent=2),
                 encoding="utf-8", newline="\n")


def find_duplicate(content_hash: str, wiki_root: Path) -> Optional[str]:
    return _load_hashes(wiki_root).get(content_hash)


def unique_path(dest_dir: Path, name: str) -> Path:
    """同名追加 -2/-3…，**绝不覆盖**（SKILL.md 5.1）。"""
    dest = dest_dir / name
    if not dest.exists():
        return dest
    stem, suf = Path(name).stem, Path(name).suffix
    n = 2
    while (dest_dir / f"{stem}-{n}{suf}").exists():
        n += 1
    return dest_dir / f"{stem}-{n}{suf}"


# =============================================================================
# 三、级联候选扫描
# =============================================================================

def _build_vocabulary(wiki_root: Path) -> List[str]:
    """
    动态词表 = 领域词表 + 已有词条标题。
    **从 wiki 自身读取**，不硬编码——wiki 长大了，扫描范围自动跟上。
    """
    vocab = set(DOMAIN_TERMS)
    wiki = wiki_root / "wiki"
    if wiki.exists():
        for md in wiki.rglob("*.md"):
            if md.name in ("index.md", "log.md"):
                continue
            # 用词条名（去扩展名）
            vocab.add(md.stem)
            # 用一级标题
            try:
                for line in md.read_text(encoding="utf-8").split("\n")[:5]:
                    if line.startswith("# "):
                        vocab.add(line[2:].strip().split("（")[0].strip())
                        break
            except Exception:  # noqa: BLE001
                pass
    return sorted(t for t in vocab if len(t) >= 2)


def find_cascade_candidates(content: str, wiki_root: Path,
                            top_n: int = 8) -> List[Dict[str, Any]]:
    """
    找出引用了新内容中实体的**已有词条**——它们是级联更新的起点。

    返回 `[{path, score, terms}]`，**带上命中词**，因为"哪篇文件被命中"远不如
    "因为哪个实体被命中"对 agent 有用：agent 据此判断该改哪一节。

    score = 该词条与新内容共享的**不同实体数**。分越高，越可能受实质影响。
    """
    wiki = wiki_root / "wiki"
    if not wiki.exists():
        return []
    terms = [t for t in _build_vocabulary(wiki_root) if t in content]
    if not terms:
        return []

    hits: List[Dict[str, Any]] = []
    for md in wiki.rglob("*.md"):
        if md.name in ("index.md", "log.md"):
            continue
        try:
            body = md.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        matched = sorted({t for t in terms if t in body})
        if matched:
            hits.append({
                "path": md.relative_to(wiki_root).as_posix(),
                "score": len(matched),
                "terms": matched,
            })
    hits.sort(key=lambda h: (-h["score"], h["path"]))
    return hits[:top_n]


# =============================================================================
# 四、采集主流程
# =============================================================================

def _derive_text_header(src_name: str, topic: str, digest: str) -> str:
    """
    派生文本的溯源头。
    **只加在派生文本上，不加在权威件上**——权威件必须逐字不变。
    """
    return (
        f"<!-- 派生文本 · 源文件: {src_name} · 落位: raw/{topic}/ · "
        f"sha256: {digest[:16]} · 生成: {datetime.now():%Y-%m-%d %H:%M} -->\n"
        f"<!-- 权威件为同目录下的原文件；两者冲突时以权威件为准（见 SKILL.md） -->\n\n"
    )


def ingest_file(
    source: Union[str, Path, bytes],
    *,
    filename: Optional[str] = None,
    topic: Optional[str] = None,
    hooks: Optional[List[IngestHook]] = None,
    wiki_root: Optional[Path] = None,
    dry_run: bool = False,
) -> IngestResult:
    """
    把**一个**文件采集进 raw/。三种入口共用本函数。

    :param source: 文件路径，或 `bytes`（网站上传场景）
    :param filename: 当 source 是 bytes 时必填
    :param topic: 强制指定主题；不给则自动推断
    :param hooks: 钩子列表；不给则用默认（控制台 + 审计）
    :param wiki_root: wiki 根目录；默认取本文件的上上级
    :param dry_run: 只分析不落盘
    """
    root = Path(wiki_root) if wiki_root else WIKI_ROOT
    ext = Path(filename).suffix.lower() if filename else ""

    # ---- 读取源 ----
    try:
        if isinstance(source, (bytes, bytearray)):
            if not filename:
                raise ValueError("source 为 bytes 时必须提供 filename")
            data = bytes(source)
            src_name = filename
        else:
            p = Path(source)
            if not p.exists():
                raise FileNotFoundError(str(p))
            data = p.read_bytes()
            src_name = filename or p.name
    except Exception as e:  # noqa: BLE001
        r = IngestResult(status="error", source=str(source),
                         error=f"{type(e).__name__}: {e}")
        emit("on_error", r, hooks)
        return r

    digest = hashlib.sha256(data).hexdigest()
    ext = Path(src_name).suffix.lower()

    # ---- 格式校验 ----
    if ext not in AUTHORITATIVE_EXT and src_name != "Dockerfile":
        r = IngestResult(status="error", source=src_name, content_hash=digest,
                         error=f"不支持的格式: {ext or '(无扩展名)'}")
        emit("on_error", r, hooks)
        return r

    # ---- 去重 ----
    dup = find_duplicate(digest, root)
    if dup:
        r = IngestResult(status="skipped", source=src_name, raw_path=dup,
                         is_duplicate=True, content_hash=digest, size_bytes=len(data),
                         message="内容哈希重复，已跳过（raw/ 中已有同一文件）")
        emit("on_skipped", r, hooks)
        return r

    # ---- 解析 ----
    tmp: Optional[Path] = None
    try:
        if isinstance(source, (bytes, bytearray)):
            tmp = root / "raw" / ".tmp_ingest"
            tmp.mkdir(parents=True, exist_ok=True)
            tmp = tmp / src_name
            tmp.write_bytes(data)
            parse_src = tmp
        else:
            parse_src = Path(source)
        text = parse_to_text(parse_src)
    finally:
        pass

    if text is None:
        r = IngestResult(status="error", source=src_name, content_hash=digest,
                         error=f"无法解析格式: {ext}")
        emit("on_error", r, hooks)
        return r

    # ---- 落位 ----
    detected, conf = (topic, "forced") if topic else infer_topic(src_name, text)
    dest_dir = root / "raw" / detected
    dest = unique_path(dest_dir, src_name)

    derived_rel: Optional[str] = None
    if not dry_run:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

        # 派生文本（二进制格式才需要）
        if ext in NEEDS_DERIVED:
            dv = dest.with_suffix(".txt")
            # 派生缓存钉死 LF：它是可再生的中间产物，不该带平台相关的换行符
            dv.write_text(_derive_text_header(src_name, detected, digest) + text,
                          encoding="utf-8", newline="\n")
            derived_rel = dv.relative_to(root).as_posix()

        # 记哈希
        idx = _load_hashes(root)
        idx[digest] = dest.relative_to(root).as_posix()
        _save_hashes(root, idx)

    if tmp and tmp.exists():
        shutil.rmtree(tmp.parent, ignore_errors=True)

    # ---- 级联候选 ----
    casc = find_cascade_candidates(text, root)

    # ---- 日志条目（草稿，由摄入 agent 定稿） ----
    today = datetime.now().strftime("%Y-%m-%d")
    rel = dest.relative_to(root).as_posix()

    r = IngestResult(
        status="collected",
        source=src_name,
        # dry_run 时**不报 raw_path**——调用方看到 None 才知道"还没落盘"，
        # 否则会误以为文件已存（本库踩过这个坑）。
        raw_path=None if dry_run else rel,
        topic=detected,
        detected_topic=detected,
        topic_confidence=conf,
        content_hash=digest,
        size_bytes=len(data),
        derived_text=derived_rel,
        cascade_candidates=casc,
        content_preview=text[:600],
        log_entry=f"## [{today}] ingest | <待定：主词条标题>\n"
                  f"- Disposition: <New; Update; Disputed>\n"
                  f"- Raw: {rel}",
        message="采集完成；下一步交摄入 agent 做分诊与编译（见 INGEST_AGENT.md）",
    )
    emit("on_collected", r, hooks)
    return r


# =============================================================================
# 五、入口：CLI / inbox / 程序化
# =============================================================================

def _brief(result: IngestResult) -> str:
    """编译简报——交给摄入 agent 的输入。"""
    d = result.to_dict()
    d["next_step"] = ("把本简报交给 INGEST_AGENT.md 定义的摄入 agent："
                      "它需先读 references/schema.yaml、命名与链接约定.md、"
                      "词条骨架.md、SKILL.md，再做 分诊→编译→级联→收尾。")
    return json.dumps(d, ensure_ascii=False, indent=2)


def ingest_dir(directory: Path, wiki_root: Path,
               hooks: Optional[List[IngestHook]] = None,
               topic: Optional[str] = None) -> List[IngestResult]:
    """递归采集一个目录下的全部受支持文件（`--inbox` 之外的一般目录入口）。"""
    directory = Path(directory)
    files = [f for f in sorted(directory.rglob("*"))
             if f.is_file()
             and not any(p.startswith(".") for p in f.relative_to(directory).parts)
             and (f.suffix.lower() in AUTHORITATIVE_EXT or f.name == "Dockerfile")]
    if not files:
        print(f"  （{directory} 下无受支持的文件）")
        return []
    out = []
    for f in files:
        print(f"\n📄 {f.relative_to(directory)}")
        out.append(ingest_file(f, topic=topic, hooks=hooks, wiki_root=wiki_root))
    return out


def ingest_inbox(wiki_root: Path, hooks: Optional[List[IngestHook]] = None) -> List[IngestResult]:
    """批量处理 inbox/：采集后把文件移到 inbox/.done/（不删除，留可逆）。"""
    inbox = wiki_root / "inbox"
    inbox.mkdir(exist_ok=True)
    files = [f for f in sorted(inbox.iterdir())
             if f.is_file() and not f.name.startswith(".")]
    if not files:
        print(f"  （inbox/ 为空，无可处理文件: {inbox}）")
        return []
    done = inbox / ".done"
    results = []
    for f in files:
        results.append(ingest_file(f, hooks=hooks, wiki_root=wiki_root))
        done.mkdir(exist_ok=True)
        try:
            shutil.move(str(f), str(unique_path(done, f.name)))
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️  移动 {f.name} 到 .done/ 失败: {e}")
    return results


def main() -> int:
    ap = argparse.ArgumentParser(
        description="把新文件采集进 llm-wiki（确定性部分；编译见 INGEST_AGENT.md）")
    ap.add_argument("files", nargs="*", help="要采集的文件路径")
    ap.add_argument("--topic", help="强制指定主题目录，如 csv/cost_data")
    ap.add_argument("--inbox", action="store_true", help="批量处理 inbox/")
    ap.add_argument("--dry-run", action="store_true", help="只分析不落盘")
    ap.add_argument("--json", action="store_true", help="输出编译简报(JSON)")
    ap.add_argument("--wiki-root", help="wiki 根目录（默认自动定位）")
    a = ap.parse_args()

    root = Path(a.wiki_root).resolve() if a.wiki_root else WIKI_ROOT
    hooks = default_hooks(root, verbose=not a.json)

    print("=" * 72)
    print("📥 ingest_raw — 采集新文件进 llm-wiki")
    print(f"   wiki 根: {root}")
    print("=" * 72)

    if not a.inbox and not a.files:
        ap.print_help()
        return 1

    results: List[IngestResult] = []
    if a.inbox:
        print(f"\n📂 处理 inbox/ ...")
        results = ingest_inbox(root, hooks=hooks)
    else:
        for f in a.files:
            fp = Path(f)
            if fp.is_dir():          # 传目录 → 递归采集（比报错更有用）
                print(f"\n📂 目录: {f}")
                results.extend(ingest_dir(fp, root, hooks=hooks, topic=a.topic))
                continue
            print(f"\n📄 {f}")
            results.append(ingest_file(f, topic=a.topic, hooks=hooks,
                                       wiki_root=root, dry_run=a.dry_run))

    # 汇总
    ok = [r for r in results if r.status == "collected"]
    sk = [r for r in results if r.status == "skipped"]
    er = [r for r in results if r.status == "error"]
    print(f"\n{'=' * 72}")
    print(f"📊 采集汇总: 成功 {len(ok)} · 跳过 {len(sk)} · 失败 {len(er)}")
    print(f"{'=' * 72}")

    if a.json and ok:
        print("\n" + "=" * 72)
        print("📋 编译简报（交给摄入 agent）")
        print("=" * 72)
        for r in ok:
            print(_brief(r))

    if er:
        return 2
    if ok:
        print("\n⚠️  下一步：按 INGEST_AGENT.md 让摄入 agent 完成 分诊→编译→级联→收尾。")
        print("   （采集只是确定性的一半；不编译，raw/ 不会变成知识。）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
