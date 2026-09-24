# -*- coding: utf-8 -*-
"""
db_build.py — 把 llm-wiki 的文件镜像进 SQLite（**只碰镜像区**）

★★ 最重要的约束：本脚本 MUST NOT 触碰权威区 ★★

  【镜像区】mirror_*      从 raw/ + wiki/ + state/ 抽取。可随时重建。
  【权威区】其余全部表      用户 / 会话 / 消息 / 用户状态。**DB 独有，不可重建。**

重建的实现方式不是"删库重建"，而是**逐表 DELETE/INSERT 镜像表**——
权威表连 DELETE 语句都不会出现在本文件里。这是刻意的：即使有人误跑本脚本，
对话历史也**物理上不可能**被抹掉。

用法
----
    python scripts/db_build.py                  # 建/更新镜像（默认 F:\\llm-wiki.db）
    python scripts/db_build.py --db D:\\x.db     # 指定库位置
    python scripts/db_build.py --stats           # 只看现状，不写
    python scripts/db_build.py --verify-mirror   # 校验镜像是否与文件系统一致
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()

WIKI_ROOT = Path(__file__).resolve().parent.parent
WIKI_DIR = WIKI_ROOT / "wiki"
RAW_DIR = WIKI_ROOT / "raw"
STATE_FILE = WIKI_ROOT / "state" / "metrics.json"
SCHEMA_FILE = Path(__file__).resolve().parent / "db_schema.sql"

from paths import knowledge_db  # noqa: E402

# ⚠️ 本脚本只写**共享知识库**（`shared/knowledge.db`）。
#    它里面**只有** mirror_* 表 —— 用户数据已迁到各自的 `users/<id>.db`，
#    认证数据在 `auth.db`。三者物理分开，见 db_schema.sql 头部说明。
DEFAULT_DB = knowledge_db()

# 权威区表名 —— 本脚本**不会有**任何针对这些表的写操作。
#
# 这张白名单现在有两层作用：
#   ① 自检：重建镜像前后比对行数（这些表若存在且被改，立刻抛错）
#   ② 防回归：知识库里**本就不该有**这些表。
#      万一将来有人误把用户表建进知识库，`assert_authoritative_intact`
#      会在重建时发现"凭空多出来的表被改动"而报错。
#
# 注意 `api_tokens` 也在列——认证令牌属于权威数据，绝不能被镜像重建碰到。
AUTHORITATIVE_TABLES = {"users", "conversations", "messages", "user_state",
                        "api_tokens"}

CJK_RE = re.compile(r"([\u4e00-\u9fff])")


def seg(text: str) -> str:
    """
    中文逐字分词（入库 FTS 前调用）。

    为什么需要：中文无空格，FTS5 的 unicode61 会把「银黄口服液」当成一个 token，
    搜「成本」永远搜不到。逐字加空格后每个字成为独立 token，任意长度子串都能命中。
      实测：'银黄口服液单位成本上涨' → 搜「成本」「单位成本」「银黄」均命中
    英文数字不受影响（它们本就以空格分词）。
    """
    return CJK_RE.sub(r" \1 ", text)


# ---------------------------------------------------------------------------
# 连接与建表
# ---------------------------------------------------------------------------

def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
    con.execute(
        "INSERT INTO meta(key,value) VALUES('schema_version','1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')"
    )
    con.commit()


def assert_authoritative_intact(con: sqlite3.Connection, before: Dict[str, int]) -> None:
    """
    自检：重建前后权威区行数必须一致。
    这是"物理上不可能误删"之外的第二道保险——万一将来有人加了代码，
    这里会立刻炸。
    """
    for t in sorted(AUTHORITATIVE_TABLES):
        try:
            after = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            continue
        b = before.get(t, 0)
        if after != b:
            raise RuntimeError(
                f"❌ 权威区表 `{t}` 行数被改变：{b} → {after}。"
                f"这不是本脚本该做的事，已中止。"
            )


def authoritative_counts(con: sqlite3.Connection) -> Dict[str, int]:
    out = {}
    for t in sorted(AUTHORITATIVE_TABLES):
        try:
            out[t] = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            pass
    return out


# ---------------------------------------------------------------------------
# 抽取：raw → documents / blobs
# ---------------------------------------------------------------------------

TEXT_EXT = {".csv", ".txt", ".md", ".py", ".json"}


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _topic_of(rel: str) -> str:
    parts = Path(rel).parts
    if len(parts) >= 3 and parts[0] == "csv":
        return f"{parts[0]}/{parts[1]}"
    return parts[0] if parts else ""


def build_documents(con: sqlite3.Connection) -> int:
    """raw/ 下每个文件一条；二进制另存 blobs。"""
    n = 0
    for f in sorted(RAW_DIR.rglob("*")):
        if not f.is_file() or f.name.startswith("."):
            continue
        rel = f.relative_to(WIKI_ROOT).as_posix()
        data = f.read_bytes()
        sha = _sha256(data)
        ext = f.suffix.lower()
        is_bin = ext in (".pdf", ".docx", ".xlsx", ".xls")

        # 派生文本：二进制格式取其同名 .txt（权威件仍是二进制）
        derived = None
        if is_bin:
            sib = f.with_suffix(".txt")
            if sib.exists():
                derived = sib.read_text(encoding="utf-8", errors="replace")
        elif ext in TEXT_EXT:
            derived = data.decode("utf-8", errors="replace")

        cur = con.execute(
            """INSERT INTO mirror_documents
               (rel_path,file_name,ext,topic,size_bytes,sha256,is_binary,derived_text)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(rel_path) DO UPDATE SET
                 size_bytes=excluded.size_bytes, sha256=excluded.sha256,
                 derived_text=excluded.derived_text, extracted_at=datetime('now')""",
            (rel, f.name, ext, _topic_of(f.relative_to(RAW_DIR).as_posix()),
             len(data), sha, 1 if is_bin else 0, derived),
        )
        if cur.lastrowid:
            doc_id = cur.lastrowid
        else:
            doc_id = con.execute(
                "SELECT id FROM mirror_documents WHERE rel_path=?", (rel,)
            ).fetchone()[0]

        if is_bin:
            con.execute(
                "INSERT INTO mirror_blobs(document_id,content) VALUES(?,?) "
                "ON CONFLICT(document_id) DO UPDATE SET content=excluded.content",
                (doc_id, data),
            )
        # FTS：文档级全文（仅对可提取出文本的文档建索引）
        if derived:
            con.execute(
                "INSERT INTO mirror_fts(ref_kind,ref_id,title,body) VALUES('document',?,?,?)",
                (rel, f.name, seg(derived)),
            )
        n += 1
    return n


# ---------------------------------------------------------------------------
# 抽取：wiki → articles / chunks / conflicts
# ---------------------------------------------------------------------------

_META_RE = re.compile(r"^>\s*(所属分类|实体类型|信息截点|信源摘要|最后编译|Raw)\s*[:：]\s*(.*)$")
_H_RE = re.compile(r"^(#{2,3})\s+(.*)$")
_STATUS_RE = re.compile(r"^>\s*\*\*(Status:\s*\w+)\*\*")


def _parse_meta(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.split("\n")[:14]:
        m = _META_RE.match(line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def _title(text: str) -> str:
    for l in text.split("\n")[:6]:
        if l.startswith("# "):
            return l[2:].strip()
    return ""


def build_articles(con: sqlite3.Connection) -> Tuple[int, int, int]:
    n_art = n_chunk = n_conf = 0
    for f in sorted(WIKI_DIR.rglob("*.md")):
        if f.name in ("index.md", "log.md"):
            continue
        text = f.read_text(encoding="utf-8")
        lines = text.split("\n")
        rel = f.relative_to(WIKI_DIR).as_posix()
        meta = _parse_meta(text)
        topic = Path(rel).parts[0] if len(Path(rel).parts) > 1 else ""

        raw_field = meta.get("Raw", "")
        raws = re.findall(r"\(([^)]+)\)", raw_field)

        cur = con.execute(
            """INSERT INTO mirror_articles
               (rel_path,topic,title,info_cutoff,last_compiled,raw_fields,body,char_count,sha256)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(rel_path) DO UPDATE SET
                 title=excluded.title, info_cutoff=excluded.info_cutoff,
                 last_compiled=excluded.last_compiled, raw_fields=excluded.raw_fields,
                 body=excluded.body, char_count=excluded.char_count, sha256=excluded.sha256""",
            (rel, topic, _title(text), meta.get("信息截点", ""), meta.get("最后编译", ""),
             json.dumps(raws, ensure_ascii=False), text, len(text), _sha256(text.encode())),
        )
        art_id = cur.lastrowid or con.execute(
            "SELECT id FROM mirror_articles WHERE rel_path=?", (rel,)
        ).fetchone()[0]
        n_art += 1

        # ---- 章节切块（与 retrieval.py 同一切法）----
        marks = [(i, m.group(2).strip()) for i, l in enumerate(lines) if (m := _H_RE.match(l))]
        secs = [(marks[k][0], marks[k][1], marks[k + 1][0] if k + 1 < len(marks) else len(lines))
                for k in range(len(marks))]
        if not secs:
            secs = [(0, "", len(lines))]
        for idx, (start, sec, end) in enumerate(secs):
            body = "\n".join(lines[start:end]).strip()
            if not body:
                continue
            key = f"{rel}#{idx:02d}"
            has_status = 1 if any(_STATUS_RE.match(lines[i])
                                  for i in range(start, min(end, len(lines)))) else 0
            con.execute(
                """INSERT INTO mirror_chunks
                   (article_id,chunk_key,section,section_path,start_line,text,has_status)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(chunk_key) DO UPDATE SET
                     section=excluded.section, section_path=excluded.section_path,
                     text=excluded.text, has_status=excluded.has_status""",
                (art_id, key, sec, f"{_title(text)} § {sec}" if sec else _title(text),
                 start + 1, body, has_status),
            )
            n_chunk += 1
            # FTS
            con.execute(
                "INSERT INTO mirror_fts(ref_kind,ref_id,title,body) VALUES('chunk',?,?,?)",
                (key, _title(text), seg(body)),
            )

        # ---- Status 块 ----
        for i, l in enumerate(lines):
            m = _STATUS_RE.match(l)
            if not m:
                continue
            # 收集该块连续的行
            blk = [l]
            j = i + 1
            while j < len(lines) and lines[j].startswith(">"):
                blk.append(lines[j])
                j += 1
            sec = ""
            for s, t_, e in secs:
                if s <= i < e:
                    sec = t_
            con.execute(
                "INSERT INTO mirror_conflicts(article,section,kind,block_text,line) VALUES(?,?,?,?,?)",
                (rel, sec, m.group(1).replace("Status:", "").strip(),
                 "\n".join(blk), i + 1),
            )
            n_conf += 1
    return n_art, n_chunk, n_conf


# ---------------------------------------------------------------------------
# 抽取：state/metrics.json → metrics + 变更日志
# ---------------------------------------------------------------------------

def build_metrics(con: sqlite3.Connection) -> Tuple[int, int]:
    if not STATE_FILE.exists():
        return 0, 0
    doc = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    ms = doc.get("metrics", [])

    # 先取旧值，用于生成变更日志
    old = {r["metric_key"]: r["value"]
           for r in con.execute("SELECT metric_key,value FROM mirror_metrics")}

    n_chg = 0
    for m in ms:
        key = m["id"]
        oldv = old.get(key)
        newv = float(m["value"])
        if oldv is not None and abs(oldv - newv) > 1e-9:
            con.execute(
                "INSERT INTO mirror_metric_changes(metric_key,old_value,new_value) VALUES(?,?,?)",
                (key, oldv, newv),
            )
            n_chg += 1
        con.execute(
            """INSERT INTO mirror_metrics
               (metric_key,entity,metric,period,value,unit,article,section,line,coord,disputed)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(metric_key) DO UPDATE SET
                 value=excluded.value, unit=excluded.unit, line=excluded.line,
                 coord=excluded.coord, disputed=excluded.disputed""",
            (key, m["entity"], m["metric"], m["period"], newv, m.get("unit", ""),
             m["article"], m.get("section", ""), m.get("line", 0),
             m.get("coord", ""), 1 if m.get("disputed") else 0),
        )
    return len(ms), n_chg


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _clear_mirror(con: sqlite3.Connection) -> None:
    """
    清空**仅镜像区**。

    ⚠️ 本函数内**绝不出现** users/conversations/messages/user_state。
    这是双区隔离的执行点——重建镜像时权威区连 DELETE 都不会被生成。
    """
    con.execute("DELETE FROM mirror_fts")
    for t in ("mirror_metric_changes", "mirror_metrics", "mirror_conflicts",
              "mirror_chunks", "mirror_articles", "mirror_blobs", "mirror_documents"):
        con.execute(f"DELETE FROM {t}")
    con.execute("DELETE FROM sqlite_sequence WHERE name LIKE 'mirror_%'")


def build(db_path: Path, verbose: bool = True) -> Dict[str, Any]:
    con = connect(db_path)
    ensure_schema(con)
    before = authoritative_counts(con)

    _clear_mirror(con)
    n_doc = build_documents(con)
    n_art, n_chunk, n_conf = build_articles(con)
    n_met, n_chg = build_metrics(con)

    # ⚠️ 除了建库时间，还要记下**当时 state 的 wiki_signature**。
    #    没有它，读库的一方无法判断"这个库是不是已经落后于 wiki 了"——
    #    而**读到一个过期的库，比读不到库更危险**：数据看起来完好，
    #    实际上和当前知识库对不上（本库最怕的"正确但过时"）。
    #    有了签名，读方可机械比对，过期就拒绝使用。
    sig = ""
    if STATE_FILE.exists():
        try:
            sig = json.loads(STATE_FILE.read_text(encoding="utf-8")) \
                    .get("wiki_signature", "")
        except (json.JSONDecodeError, OSError):
            sig = ""

    con.execute(
        "INSERT INTO meta(key,value) VALUES('built_at','" +
        datetime.now().isoformat(timespec="seconds") + "') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')"
    )
    con.execute(
        "INSERT INTO meta(key,value) VALUES('wiki_signature',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
        (sig,),
    )
    con.commit()

    assert_authoritative_intact(con, before)   # 双区隔离自检
    after = authoritative_counts(con)

    stats = {
        "db": str(db_path), "documents": n_doc, "articles": n_art,
        "chunks": n_chunk, "metrics": n_met, "conflicts": n_conf,
        "metric_changes": n_chg, "authoritative": after,
    }
    if verbose:
        print(f"✅ 镜像已建立：{db_path}")
        print(f"   documents {n_doc} · articles {n_art} · chunks {n_chunk}")
        print(f"   metrics {n_met} · conflicts {n_conf} · 本次值变更 {n_chg}")
        print(f"   权威区（未触碰）：{after}")
    con.close()
    return stats


def stats(db_path: Path) -> None:
    if not db_path.exists():
        print(f"库不存在：{db_path}")
        return
    con = connect(db_path)
    ensure_schema(con)
    print(f"库：{db_path}  （{db_path.stat().st_size/1024:.0f} KB）\n")
    print("【镜像区】（可重建）")
    blob_kb = con.execute(
        "SELECT COALESCE(sum(length(content)),0)/1024 FROM mirror_blobs"
    ).fetchone()[0]
    for t in ("mirror_documents", "mirror_blobs", "mirror_articles",
              "mirror_chunks", "mirror_metrics", "mirror_conflicts",
              "mirror_metric_changes"):
        n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        extra = f"   （{blob_kb:.0f} KB 二进制）" if t == "mirror_blobs" else ""
        print(f"   {t:<24} {n:>6}{extra}")
    n_fts = con.execute("SELECT count(*) FROM mirror_fts").fetchone()[0]
    print(f"   {'mirror_fts':<24} {n_fts:>6}   （全文索引条目）")
    print("\n【权威区】（不可重建 ★ 重建脚本不碰）")
    for t in sorted(AUTHORITATIVE_TABLES):
        try:
            n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            print(f"   {t:<24} {n:>6}")
        except sqlite3.OperationalError:
            print(f"   {t:<24} （不存在）")
    con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="把 llm-wiki 镜像进 SQLite（只碰镜像区）")
    ap.add_argument("--db", default=str(DEFAULT_DB), help=f"库路径（默认 {DEFAULT_DB}）")
    ap.add_argument("--stats", action="store_true", help="只看现状，不写")
    a = ap.parse_args()

    db = Path(a.db)
    if a.stats:
        stats(db)
        return 0
    build(db)
    print()
    stats(db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
