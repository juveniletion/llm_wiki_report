# -*- coding: utf-8 -*-
"""
retrieval.py — 混合检索核心（BM25 + 向量 + RRF）

架构
----
    查询
     ├─→ BM25（词法）      ─┐
     │                      ├─→ RRF 融合 ─→ 排序结果
     └─→ bge-m3（语义）    ─┘

三条设计原则（都对应本项目既有的规范）
--------------------------------------
1. **按 markdown 章节切块**，而非固定 token。
   这样检索结果天然是「某篇词条的某一节」，agent 可直接写出行内证据坐标
   `[三产品成本基线.md:1 银黄口服液]`。固定 token 切块会从章节中间截断，
   检索结果不对应任何语义单元。

2. **严格两阶段分离**：BM25 与向量**各自独立排名**，再融合。
   不做分数归一化后加权——RRF 只用**排名**，避免两种分数量纲不可比的问题。

3. **检索结果附带已知冲突**：命中的章节若含 `Status` 块，
   一并返回。这保证 agent **每次检索都能看到已知矛盾**，而不是依赖它主动去查。

RRF 公式
--------
    RRF(d) = Σ_r  1 / (k + rank_r(d))      k = 60（业界默认，见下）

其中 rank_r(d) 是文档 d 在第 r 个检索器中的排名（从 1 开始）。
只用排名、不用分数，因此无需归一化，天然抗量纲差异。

依赖
----
- BM25：本文件纯 Python 实现（中文用字符 bigram，无需分词库）
- 向量：SiliconFlow `BAAI/bge-m3`（1024 维），索引需预建（见 build_index.py）
"""
from __future__ import annotations

import io
import json
import math
import os
import re
import sys
import urllib.request
from collections import Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()

# 默认根（本库自身）。**主 agent 可注入别的根**——见 WikiIndex。
WIKI_ROOT = Path(__file__).resolve().parent.parent
WIKI_DIR = WIKI_ROOT / "wiki"
INDEX_DIR = WIKI_ROOT / ".index"          # 派生缓存（gitignore）

RRF_K = 60          # RRF 常数；业界默认 60（见 baiyuan 白皮书 / enterprise-rag）
BM25_K1 = 1.5       # 词频饱和参数
BM25_B = 0.75       # 长度归一化强度


# ===========================================================================
# 一、切块：按 markdown 章节
# ===========================================================================

@dataclass
class Chunk:
    """一个可检索单元 = 某篇词条的某一节。"""
    chunk_id: str            # 稳定标识：<article>#<anchor>
    article: str             # 相对 wiki/ 的路径，如 'costs/三产品成本基线.md'
    title: str               # 词条标题
    section: str             # 章节标题（可能为空 = 词条头部）
    section_path: str        # 人类可读定位，如 '三产品成本基线 § 1. 银黄口服液'
    text: str                # 含标题的完整文本（供 BM25 与 embedding）
    start_line: int          # 在原文中的起始行（便于回溯）
    has_status: bool = False # 是否含 Status 块（已知冲突标记）
    status_blocks: List[str] = field(default_factory=list)
    # 来源层：'shared'（公司基线）| 'personal'（该用户自己的资料）
    # ⚠️ 必须在检索结果里带着走——否则模型分不清某个结论
    #    是公司口径还是某个用户自己传的东西。
    source: str = "shared"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def citation(self) -> str:
        """可直接写进词条的行内证据坐标形式。"""
        art = self.article.split("/")[-1]
        return f"{art}:{self.section}" if self.section else art


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_STATUS_RE = re.compile(r"^>\s*\*\*Status:\s*(Disputed|Outdated|Update)\*\*", re.M)


def chunk_article(path: Path, min_chars: int = 80,
                  wiki_dir: Optional[Path] = None,
                  rel: Optional[str] = None,
                  source: str = "shared") -> List[Chunk]:
    """
    把一篇词条按 `#`/`##`/`###` 切成块。

    - `#` 一级标题作为文章标题，合并进其后的第一个块
    - 空章节（正文 < min_chars）与下一个块合并，避免产生碎片

    `rel` / `source` 供 **overlay 模式**用：那时文件来自两层，
    用 `path.relative_to()` 推不出统一的相对路径，必须由调用方给定。
    """
    raw = path.read_text(encoding="utf-8")
    lines = raw.split("\n")
    if rel is None:
        rel = path.relative_to(wiki_dir or WIKI_DIR).as_posix()

    title = ""
    sections: List[Tuple[int, str, List[str]]] = []   # (起始行, 标题, 行内容)
    cur_title, cur_lines, cur_start = "", [], 0

    for i, ln in enumerate(lines):
        m = _HEADING_RE.match(ln)
        if m:
            level, text = len(m.group(1)), m.group(2).strip()
            if level == 1 and not title:
                title = text
                cur_start = i
                continue
            if level >= 2:
                if cur_title or cur_lines:
                    sections.append((cur_start, cur_title, cur_lines))
                cur_title, cur_lines, cur_start = text, [ln], i
                continue
        cur_lines.append(ln)

    if cur_title or cur_lines:
        sections.append((cur_start, cur_title, cur_lines))

    # 合并过短的块到前一块
    merged: List[Tuple[int, str, List[str]]] = []
    for start, sec, body in sections:
        if merged and len("\n".join(body)) < min_chars:
            ps, pt, pb = merged[-1]
            merged[-1] = (ps, pt, pb + body)
        else:
            merged.append((start, sec, body))

    out: List[Chunk] = []
    for idx, (start, sec, body) in enumerate(merged):
        text = "\n".join(body).strip()
        if not text:
            continue
        # 检索文本：冠以 词条标题 + 章节标题，提升召回（这两者往往就是查询词）
        searchable = f"{title} {sec}\n{text}".strip()
        status = _STATUS_RE.findall(text)
        blocks = []
        if status:
            # 把每个 Status 块整段抽出（供"检索时附带已知冲突"）
            for m in re.finditer(r"(?:^> .*\n)+", text, re.M):
                blk = m.group(0)
                if "**Status:" in blk:
                    blocks.append(blk.strip())
        anchor = sec or "head"
        out.append(Chunk(
            chunk_id=f"{rel}#{idx:02d}",
            article=rel,
            title=title,
            section=sec,
            section_path=f"{title} § {sec}" if sec else title,
            text=searchable,
            start_line=start + 1,
            has_status=bool(status),
            status_blocks=blocks,
            source=source,
        ))
    return out


def load_chunks(wiki_dir: Optional[Path] = None,
                shared_root: Optional[Path] = None,
                personal_root: Optional[Path] = None) -> List[Chunk]:
    """
    加载可检索块（排除 index.md / log.md）。

    **两种模式**：

    单层（默认）：只读 `wiki_dir`（或默认库）。
        → 共享基线自检、看板等场景用它。

    overlay（给了 `shared_root` / `personal_root`）：
        → 用 `overlay.overlay_articles()` 合并「共享基线 + 个人增量」，
          个人同名覆盖共享，然后**在合并后的语料上切块**。

    ⚠️ overlay 模式**必须在合并语料上切块，不能分别切再拼**：
       BM25 的 IDF 是按语料算的。个人语料只有几篇时，其稀有词 IDF 被
       极度放大、稳居个人列表第 1；而 RRF 只看排名，于是
       「3 篇里的第 1」与「12 篇里的第 1」权重相同 ——
       **个人内容被系统性过度加权**。合并后 IDF 在并集上算，问题消失。
    """
    if shared_root is not None or personal_root is not None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import overlay as OV
        chunks: List[Chunk] = []
        for p, tag in OV.overlay_articles(shared_root, personal_root):
            rel = _rel_to_wiki(p, shared_root, personal_root)
            if rel is None:
                continue
            chunks.extend(chunk_article(p, rel=rel, source=tag))
        return chunks

    base = wiki_dir or WIKI_DIR
    out: List[Chunk] = []
    for p in sorted(base.rglob("*.md")):
        if p.name in ("index.md", "log.md"):
            continue
        out.extend(chunk_article(p, wiki_dir=base))
    return out


def _rel_to_wiki(p: Path, shared_root: Optional[Path],
                 personal_root: Optional[Path]) -> Optional[str]:
    """还原文件相对它**所在层**的 `wiki/` 路径（两层必须用同一套键才能对齐）。"""
    for base in (personal_root, shared_root):
        if base is None:
            continue
        try:
            return p.relative_to(Path(base) / "wiki").as_posix()
        except ValueError:
            continue
    return None


# ===========================================================================
# 二、BM25（纯 Python，中文用字符 bigram）
# ===========================================================================

def tokenize(text: str) -> List[str]:
    """
    中英混合分词：
    - 汉字：单字 + 相邻二元组（bigram）——无分词库时对中文检索最有效
    - 英文/数字：按词切分并小写
    """
    toks: List[str] = []
    # 英文数字词
    for w in re.findall(r"[A-Za-z][A-Za-z0-9_\-\.]*|\d+(?:\.\d+)?", text):
        toks.append(w.lower())
    # 中文：连续汉字段
    for seg in re.findall(r"[一-鿿]+", text):
        toks.extend(seg)                                    # unigram
        toks.extend(seg[i:i + 2] for i in range(len(seg) - 1))  # bigram
    return toks


class BM25:
    """
    Okapi BM25。

        score(D,Q) = Σ_{q∈Q} IDF(q) · f(q,D)·(k1+1) / (f(q,D) + k1·(1-b+b·|D|/avgdl))
        IDF(q)     = ln( (N - n(q) + 0.5) / (n(q) + 0.5) + 1 )

    实现要点：IDF 用 +1 的下界形式，避免高频词产生负分（BM25+ 的常用修正）。
    """

    def __init__(self, docs: Sequence[str], k1: float = BM25_K1, b: float = BM25_B):
        self.k1, self.b = k1, b
        self.docs_tokens = [tokenize(d) for d in docs]
        self.N = len(self.docs_tokens)
        self.avgdl = (sum(len(t) for t in self.docs_tokens) / self.N) if self.N else 0.0
        self.tf: List[Counter] = [Counter(t) for t in self.docs_tokens]
        df: Counter = Counter()
        for t in self.tf:
            df.update(t.keys())
        self.idf: Dict[str, float] = {
            w: math.log((self.N - n + 0.5) / (n + 0.5) + 1) for w, n in df.items()
        }

    def scores(self, query: str) -> List[float]:
        q = tokenize(query)
        out = []
        for i, tf in enumerate(self.tf):
            dl = len(self.docs_tokens[i]) or 1
            s = 0.0
            for w in q:
                f = tf.get(w, 0)
                if not f:
                    continue
                idf = self.idf.get(w, 0.0)
                s += idf * (f * (self.k1 + 1)) / (f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1)))
            out.append(s)
        return out


# ===========================================================================
# 三、向量检索（SiliconFlow bge-m3）
# ===========================================================================

EMBED_MODEL = "BAAI/bge-m3"


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        for p in [WIKI_ROOT, *WIKI_ROOT.parents]:
            e = p / ".env"
            if e.exists():
                load_dotenv(dotenv_path=e)
                return
    except Exception:  # noqa: BLE001
        pass


def embed_texts(texts: List[str], batch: int = 16, timeout: int = 40) -> List[List[float]]:
    """
    调 SiliconFlow 计算 embedding。
    批量提交以减少往返；失败时**抛异常而非静默返回零向量**（避免污染索引）。
    """
    _load_env()
    key = os.getenv("SILICONFLOW_API_KEY", "")
    url = os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1").rstrip("/")
    if not key:
        raise RuntimeError("未找到 SILICONFLOW_API_KEY")

    vectors: List[List[float]] = []
    for i in range(0, len(texts), batch):
        part = texts[i:i + batch]
        req = urllib.request.Request(
            url + "/embeddings",
            data=json.dumps({"model": EMBED_MODEL, "input": part}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = sorted(data["data"], key=lambda d: d["index"])
        vectors.extend(it["embedding"] for it in items)
    return vectors


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return num / (na * nb) if na and nb else 0.0


# ===========================================================================
# 四、RRF 融合
# ===========================================================================

def rrf_fuse(rankings: List[List[int]], k: int = RRF_K) -> Dict[int, float]:
    """
    Reciprocal Rank Fusion。

        RRF(d) = Σ_r 1 / (k + rank_r(d))

    只用**名次**，不用分数 → 无需归一化，天然解决 BM25 与余弦相似度量纲不可比的问题。
    `k=60` 的作用：压低头部名次的权重差，让"多个检索器都排前列"比"单个检索器排第一"更重要。
    """
    fused: Dict[int, float] = {}
    for ranks in rankings:
        for pos, doc_idx in enumerate(ranks, start=1):
            fused[doc_idx] = fused.get(doc_idx, 0.0) + 1.0 / (k + pos)
    return fused


# ===========================================================================
# 五、索引（派生缓存，可重建）
# ===========================================================================

class Index:
    """
    向量索引的落盘/加载。BM25 每次现算（12 篇词条 <10ms，不值得落盘）。

    **绑定一个根目录** —— 主 agent 可指向别的 wiki，不必复制本文件。
    """

    def __init__(self, vectors: Dict[str, List[float]], meta: Dict[str, Any],
                 root: Optional[Path] = None):
        self.vectors = vectors
        self.meta = meta
        self.root = Path(root) if root else WIKI_ROOT

    def path(self) -> Path:
        return self.root / ".index" / "vectors.json"

    def save(self) -> None:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"meta": self.meta, "vectors": self.vectors}, ensure_ascii=False),
            encoding="utf-8", newline="\n",
        )

    @classmethod
    def load(cls, root: Optional[Path] = None) -> Optional["Index"]:
        r = Path(root) if root else WIKI_ROOT
        p = r / ".index" / "vectors.json"
        if not p.exists():
            return None
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return cls(d["vectors"], d.get("meta", {}), root=r)
        except Exception:  # noqa: BLE001
            return None


def build_index(verbose: bool = True, root: Optional[Path] = None,
                shared_root: Optional[Path] = None,
                personal_root: Optional[Path] = None) -> Index:
    """
    建向量索引。

    ⚠️ 建索引要联网调 embedding，**旧向量能复用就复用**——见下面的分层复用。
       共享 158 块一次要算不少 token；个人层通常只加几篇，
       如果每次全量重算，成本是 O(用户数 × 全库)。

    复用规则：**同 `chunk_id` + 同文本 → 直接拿旧向量**。
       overlay 模式下个人层独有的块是新 id，必须算；
       共享块的 id 与文本都没变，直接复用。
    """
    import hashlib
    r = Path(root) if root else WIKI_ROOT
    overlay_mode = shared_root is not None or personal_root is not None

    chunks = load_chunks(**(
        {"shared_root": shared_root, "personal_root": personal_root}
        if overlay_mode else {"wiki_dir": r / "wiki"}))
    sig = hashlib.sha256(
        "\n".join(f"{c.chunk_id}:{hashlib.md5(c.text.encode()).hexdigest()[:8]}"
                  for c in chunks).encode()
    ).hexdigest()[:16]

    old = Index.load(r)
    if old and old.meta.get("signature") == sig:
        if verbose:
            print(f"索引已是最新（{len(chunks)} 块，签名 {sig}），跳过重建")
        return old

    # ---- 向量复用：能查到旧向量的块不重算 ----
    prev = dict(old.vectors) if old else {}
    # 个人层还额外参考共享索引（那是最全的旧向量来源）
    if overlay_mode and shared_root is not None:
        sh = Index.load(Path(shared_root))
        if sh:
            for k, v in sh.vectors.items():
                prev.setdefault(k, v)

    have = {c.chunk_id: prev[c.chunk_id] for c in chunks if c.chunk_id in prev}
    todo = [c for c in chunks if c.chunk_id not in have]

    if verbose:
        print(f"embedding：复用 {len(have)} 块，新算 {len(todo)} 块"
              f"（共 {len(chunks)}）")
    new_vecs = embed_texts([c.text for c in todo]) if todo else []
    vectors = dict(have)
    vectors.update({c.chunk_id: v for c, v in zip(todo, new_vecs)})

    idx = Index(
        vectors=vectors,
        meta={"model": EMBED_MODEL, "signature": sig, "count": len(chunks),
              "reused": len(have), "computed": len(todo),
              "overlay": bool(overlay_mode),
              "built_at": __import__("datetime").datetime.now().isoformat(timespec="seconds")},
        root=r,
    )
    idx.save()
    if verbose:
        print(f"索引已保存：{idx.path()}（{len(vectors)} 向量）")
    return idx


# ===========================================================================
# 六、混合检索入口
# ===========================================================================

@dataclass
class Hit:
    chunk: Chunk
    rrf: float
    bm25_rank: Optional[int]
    vec_rank: Optional[int]
    cosine: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "citation": self.chunk.citation,
            "article": self.chunk.article,
            "section": self.chunk.section,
            "section_path": self.chunk.section_path,
            "start_line": self.chunk.start_line,
            "rrf": round(self.rrf, 6),
            "bm25_rank": self.bm25_rank,
            "vec_rank": self.vec_rank,
            "cosine": round(self.cosine, 4),
            "has_status": self.chunk.has_status,
            "status_blocks": self.chunk.status_blocks,
            "text": self.chunk.text,
            "source": self.chunk.source,
        }


def hybrid_search(
    query: str,
    top_k: int = 6,
    *,
    chunks: Optional[List[Chunk]] = None,
    index: Optional[Index] = None,
    with_conflicts: bool = True,
    allow_online: bool = True,
    root: Optional[Path] = None,
    shared_root: Optional[Path] = None,
    personal_root: Optional[Path] = None,
) -> List[Hit]:
    """
    BM25 + 向量 → RRF 融合检索。

    :param with_conflicts: 命中章节若含 `Status` 块，随结果一并返回。
                           **这是"自我发现矛盾"的第一层**——保证 agent 每次都能看到已知冲突。
    :param allow_online: False 时只用已落盘索引（离线场景）。
    :param root: 索引落在哪个根下（个人工作区时传用户 ws，索引落他自己的 `.index/`）。
    :param shared_root / personal_root: 给了就走 **overlay 合并模式**——
                   语料 = 共享基线 + 该用户的个人增量（个人同名覆盖共享）。
                   ⚠️ `personal_root` 必须**只传当前调用者自己的**工作区。
    """
    r = Path(root) if root else WIKI_ROOT
    overlay_mode = shared_root is not None or personal_root is not None

    if chunks is None:
        chunks = load_chunks(**(
            {"shared_root": shared_root, "personal_root": personal_root}
            if overlay_mode else {"wiki_dir": r / "wiki"}))
    if not chunks:
        return []

    # --- 检索器 1：BM25 ---
    bm = BM25([c.text for c in chunks])
    bs = bm.scores(query)
    bm_rank = sorted(range(len(chunks)), key=lambda i: -bs[i])
    bm_rank = [i for i in bm_rank if bs[i] > 0]          # 0 分不参与融合

    # --- 检索器 2：向量 ---
    vec_rank: List[int] = []
    cos_by_idx: Dict[int, float] = {}
    idx = index if index is not None else Index.load(r)
    # ⚠️ 光看"索引存在"不够——**必须确认它覆盖了当前语料**。
    #    实测踩到：索引建在改动之前，个人块没有向量。
    #    此时该块只在 BM25 排名里、不在向量排名里，
    #    RRF 只给它一半的分 → **用户自己上传的内容检索不到**，
    #    而且**不报任何错**（这是最坏的一种失败：静默失效）。
    #    所以这里抽查覆盖率，缺块就重建。
    if idx is not None and chunks:
        missing = sum(1 for c in chunks if c.chunk_id not in idx.vectors)
        if missing:
            if allow_online:
                try:
                    idx = build_index(verbose=False, root=r,
                                      **({"shared_root": shared_root,
                                          "personal_root": personal_root}
                                         if overlay_mode else {}))
                except Exception:  # noqa: BLE001
                    idx = None
            else:
                # 离线且索引不全 → 宁可不用向量，也别用半份索引：
                # BM25 单独跑仍能命中，混着半份向量反而会压制新内容。
                idx = None
    if idx is None and allow_online:
        try:
            idx = build_index(verbose=False, root=r,
                              **({"shared_root": shared_root,
                                  "personal_root": personal_root}
                                 if overlay_mode else {}))
        except Exception:  # noqa: BLE001
            idx = None
    if idx:
        try:
            qv = embed_texts([query])[0]
            sims = []
            for i, c in enumerate(chunks):
                v = idx.vectors.get(c.chunk_id)
                if not v:
                    continue
                s = cosine(qv, v)
                cos_by_idx[i] = s
                sims.append((i, s))
            sims.sort(key=lambda x: -x[1])
            vec_rank = [i for i, _ in sims]
        except Exception:  # noqa: BLE001
            vec_rank = []

    # --- RRF 融合 ---
    fused = rrf_fuse([r for r in (bm_rank, vec_rank) if r])

    hits: List[Hit] = []
    for i, score in sorted(fused.items(), key=lambda x: -x[1])[:top_k]:
        hits.append(Hit(
            chunk=chunks[i],
            rrf=score,
            bm25_rank=(bm_rank.index(i) + 1) if i in bm_rank else None,
            vec_rank=(vec_rank.index(i) + 1) if i in vec_rank else None,
            cosine=cos_by_idx.get(i, 0.0),
        ))

    if not with_conflicts:
        for h in hits:
            h.chunk.status_blocks = []
    return hits


# 来源层 → 给模型看的措辞
_SRC_LABEL = {"shared": "公司基线", "personal": "你的资料"}


def format_hits(hits: List[Hit], max_chars: int = 700,
                with_source: bool = False) -> str:
    """
    把检索结果格式化成给 LLM 读的文本（含坐标与已知冲突）。

    `with_source=True` 时每节前标出来源层——
    ⚠️ **overlay 模式必须开**：不标的话模型分不清某个结论是
       公司口径还是**某个用户自己传的**，会当成通用结论引用。
    """
    if not hits:
        return "未检索到相关内容。请尝试更换关键词，或用 list_wiki_articles 浏览全库。"
    out: List[str] = [f"混合检索命中 {len(hits)} 节（按 RRF 排序）：\n"]
    for n, h in enumerate(hits, 1):
        c = h.chunk
        src = f"bm25#{h.bm25_rank}" if h.bm25_rank else "bm25—"
        vs = f"vec#{h.vec_rank}({h.cosine:.3f})" if h.vec_rank else "vec—"
        tag = f"[{_SRC_LABEL.get(c.source, c.source)}] " if with_source else ""
        out.append(f"[{n}] {tag}{c.section_path}")
        line = f"    坐标: `[{c.citation}]`  |  {src} · {vs}  |  RRF={h.rrf:.5f}"
        if with_source and c.source == "personal":
            line += "  |  ⚠️ 仅当前用户可见，非公司口径"
        out.append(line)
        body = c.text
        if len(body) > max_chars:
            body = body[:max_chars] + " …（已截断，需要全文请用 read_wiki_article）"
        out.append("    " + body.replace("\n", "\n    "))
        if h.chunk.has_status and h.chunk.status_blocks:
            out.append(f"    ⚠️ 本节含已知冲突（{len(h.chunk.status_blocks)} 个 Status 块）：")
            for blk in h.chunk.status_blocks:
                out.append("      " + blk.replace("\n", "\n      ")[:500])
        out.append("")
    return "\n".join(out)


# ===========================================================================

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="混合检索（BM25 + 向量 + RRF）")
    ap.add_argument("query", nargs="*", help="查询")
    ap.add_argument("--top", type=int, default=6)
    ap.add_argument("--no-online", action="store_true", help="只用已落盘索引")
    ap.add_argument("--rebuild", action="store_true", help="强制重建索引")
    a = ap.parse_args()

    if a.rebuild:
        build_index()
        sys.exit(0)
    q = " ".join(a.query)
    if not q:
        ap.print_help()
        sys.exit(1)
    print(f"查询: {q}\n")
    print(format_hits(hybrid_search(q, top_k=a.top, allow_online=not a.no_online)))
