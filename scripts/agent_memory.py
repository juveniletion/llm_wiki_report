# -*- coding: utf-8 -*-
"""
agent_memory.py — **agent 记忆层**（读写 SQLite 权威区）

定位
----
这是本库**唯一**有权写「权威区」的模块。请与 `db_build.py` 对照记：

    db_build.py    只碰镜像区（mirror_*）——可重建，丢了不心疼
    agent_memory.py 只碰权威区（无前缀）——**不可重建**，DB 独有

两者**绝不互相调用**。dbbuild 的白名单不生成 DELETE 给权威表；
本模块也从不触碰 mirror_*。这是双区隔离的对称实现。

三层记忆（对应 Pi agent 的 session 模型 + 本库原有的 user_state 设计）
--------------------------------------------------------------------
    ① 会话历史    messages      一轮一轮的原始记录，**永不删除**
    ② 压缩摘要    conversations.context（JSON）  超窗后把远期历史压成结构化摘要
    ③ 用户偏好    user_state    跨会话有效（常用产品/月份/叙述风格）

为什么"永不删除"（Pi 的核心设计）
--------------------------------
压缩**不是**删历史，而是**追加一条摘要**并记录"摘要覆盖到哪条消息"。
原始消息留在库里，需要时可回溯。若压缩时删消息，就再也查不回来了——
本库对"不可逆"极其保守（见 SKILL.md 的 Lint 三级权限、Status 块设计）。

用法
----
    from agent_memory import Memory
    m = Memory()                       # 默认取 paths.default_db()
    uid = m.ensure_user("cli", name="本地用户")
    cid = m.new_conversation(uid, title="5月成本问答")
    m.append(cid, "user", "银黄5月为什么涨？")
    m.append(cid, "tool", "…", tool_name="get_metric", tool_args={"metric":"单位成本"})
    msgs = m.load(cid)
    m.save_summary(cid, "…摘要…", upto_message_id=msgs[-1]["id"])
    m.set_pref(uid, "default_product", "银黄口服液")

路径
----
默认库路径由 `paths.default_db()` 决定（环境变量 `LLM_WIKI_DB` > 容器 /data >
仓库内 state/ > 本机 F: 盘）。**不要在本文件里硬编码**——那会让容器部署失败。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()

HERE = Path(__file__).resolve().parent
WIKI_ROOT = HERE.parent
from paths import user_db as _user_db_path  # noqa: E402

# ⚠️ 这里**不能**再指向共享知识库。
#    未指定用户时的默认落点是"local 用户的私有库"，
#    而不是"所有人的公共库"——否则又写回一起去了。
DEFAULT_DB = _user_db_path("local")

# 权威区表名（用于自检与文档；db_build 也有对应白名单）
AUTHORITATIVE_TABLES = ("users", "conversations", "messages", "user_state")


class Memory:
    """
    单个用户的会话/消息/偏好。**只写该用户自己的库。**

    ⚠️ 一用户一个库文件（`data/users/<id>.db`）。
    这样"读到别人的数据"在**文件层面**就不可能发生——
    服务 alice 时打开的压根不是 bob 的库。

    各库里仍保留 `users` 表，但那是**影子行**（`external_id` 相同、id 也相同），
    目的是让所有 `WHERE user_id = ?` 的外键关系继续成立。
    **用户权威目录在 auth.db**（由 agent_auth.py 管），增删用户要去那里。
    """

    def __init__(self, user: Optional[str] = None,
                 db_path: Path | str | None = None,
                 external_id: str = "", name: str = ""):
        """
        三种构造方式：
            Memory("alice")                    按用户标识 → 自动定位到他的库
            Memory(db_path=Path("x.db"))       直接指定文件（测试/迁移用）
            Memory()                           落到 local 用户的私有库

        ⚠️ **第一个位置参数是「用户标识」，不是路径。**
           踩过的坑：`Memory(db or DEFAULT_DB)`（旧调用写法）会把整个路径
           当成用户名，生成 `users/C__Users_..._local.db.db` 这种畸形文件——
           而它还能正常读写，所以**不报错**，只有看磁盘才发现。
           现在对"看起来像路径"的入参做显式拦截，避免同类错误再静默发生。
        """
        from paths import user_db as _user_db

        if db_path is not None:
            self.db = Path(db_path)
            self.external_id = Path(db_path).stem
        else:
            ident = (user or external_id or "").strip()
            # 防呆：把路径当用户名传进来 → 直接报错，别生成畸形文件
            if ident and (("/" in ident) or ("\\" in ident) or ident.endswith(".db")):
                raise ValueError(
                    f"Memory() 的第一个参数是**用户标识**，不是路径，"
                    f"但收到 {ident!r}。要指定文件请用 `Memory(db_path=...)`。")
            if not ident:
                ident = "local"
            self.db = _user_db(ident)
            self.external_id = ident

        self.db.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()
        if name:
            self._sync_shadow_user(name)

    # ---- 连接 ---------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        return c

    def _ensure_schema(self) -> None:
        """
        建**本用户库**的表（幂等）。

        注意这里**没有** api_tokens —— 令牌在 auth.db，不随用户库走。
        也**没有** mirror_* —— 知识库是共享的，不在用户库里。
        """
        ddl = """
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            external_id   TEXT UNIQUE,
            name          TEXT,
            role          TEXT DEFAULT 'user',
            created_at    TEXT DEFAULT (datetime('now')),
            updated_at    TEXT DEFAULT (datetime('now')),
            note          TEXT
        );
        CREATE TABLE IF NOT EXISTS conversations (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL,
            title         TEXT,
            kind          TEXT DEFAULT 'chat',
            context       TEXT,
            created_at    TEXT DEFAULT (datetime('now')),
            updated_at    TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id);
        CREATE TABLE IF NOT EXISTS messages (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            role          TEXT NOT NULL,
            content       TEXT,
            tool_name     TEXT,
            tool_args     TEXT,
            tokens        INTEGER,
            created_at    TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, id);
        CREATE TABLE IF NOT EXISTS user_state (
            user_id       INTEGER NOT NULL,
            key           TEXT NOT NULL,
            value         TEXT,
            updated_at    TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, key),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """
        with self._conn() as c:
            c.executescript(ddl)

    def _sync_shadow_user(self, name: str = "") -> None:
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO users (external_id, name) VALUES (?,?)",
                      (self.external_id, name or self.external_id))

    # ---- 用户 ---------------------------------------------------------
    def ensure_user(self, external_id: str = "", name: str = "",
                    role: str = "user") -> int:
        """
        仅在本库里建**影子行**。真正的用户目录在 auth.db——
        要新增用户请走 `agent_auth.Auth.ensure_user()`。
        """
        eid = external_id or self.external_id
        with self._conn() as c:
            r = c.execute("SELECT id FROM users WHERE external_id = ?",
                          (eid,)).fetchone()
            if r:
                return int(r["id"])
            cur = c.execute(
                "INSERT INTO users (external_id, name, role) VALUES (?,?,?)",
                (eid, name or eid, role))
            return int(cur.lastrowid)

    # ---- 会话 ---------------------------------------------------------
    def new_conversation(self, user_id: int, title: str = "",
                         kind: str = "chat") -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO conversations (user_id, title, kind, context) "
                "VALUES (?,?,?,?)",
                (user_id, title, kind, json.dumps({}, ensure_ascii=False)))
            return int(cur.lastrowid)

    def active_conversation(self, user_id: int, kind: str = "chat") -> Optional[int]:
        """最近一个会话 id（用于"续上次"）。"""
        with self._conn() as c:
            r = c.execute(
                "SELECT id FROM conversations WHERE user_id = ? AND kind = ? "
                "ORDER BY updated_at DESC, id DESC LIMIT 1",
                (user_id, kind)).fetchone()
            return int(r["id"]) if r else None

    def list_conversations(self, user_id: int, limit: int = 20) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT c.id, c.title, c.kind, c.created_at, c.updated_at, "
                "       (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS n_msg "
                "FROM conversations c WHERE c.user_id = ? "
                "ORDER BY c.updated_at DESC, c.id DESC LIMIT ?",
                (user_id, limit)).fetchall()
            return [dict(r) for r in rows]

    def set_title(self, conv_id: int, title: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE conversations SET title = ?, updated_at = datetime('now') "
                      "WHERE id = ?", (title[:80], conv_id))

    # ---- 消息 ---------------------------------------------------------
    def append(self, conv_id: int, role: str, content: str = "",
               tool_name: str = "", tool_args: Any = None,
               tokens: Optional[int] = None) -> int:
        """
        追加一条消息。**永不删除历史**——压缩只改 conversations.context。
        `tool_args` 可以是 dict（会被序列化）。
        """
        args = ""
        if tool_args is not None:
            args = tool_args if isinstance(tool_args, str) else \
                json.dumps(tool_args, ensure_ascii=False)
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO messages (conversation_id, role, content, tool_name, "
                "tool_args, tokens) VALUES (?,?,?,?,?,?)",
                (conv_id, role, content or "", tool_name or "", args, tokens))
            c.execute("UPDATE conversations SET updated_at = datetime('now') WHERE id = ?",
                      (conv_id,))
            return int(cur.lastrowid)

    def load(self, conv_id: int, limit: int = 0) -> List[Dict[str, Any]]:
        """按时间正序取消息。limit>0 时取**最后** limit 条（近端优先）。"""
        with self._conn() as c:
            if limit > 0:
                rows = c.execute(
                    "SELECT * FROM (SELECT * FROM messages WHERE conversation_id = ? "
                    "ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                    (conv_id, limit)).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id ASC",
                    (conv_id,)).fetchall()
            return [dict(r) for r in rows]

    def count(self, conv_id: int) -> int:
        with self._conn() as c:
            return int(c.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                                 (conv_id,)).fetchone()[0])

    # ---- 压缩摘要（存在 conversations.context）-------------------------
    def get_context(self, conv_id: int) -> Dict[str, Any]:
        with self._conn() as c:
            r = c.execute("SELECT context FROM conversations WHERE id = ?",
                          (conv_id,)).fetchone()
            if not r or not r["context"]:
                return {}
            try:
                return json.loads(r["context"])
            except json.JSONDecodeError:
                return {}

    def save_summary(self, conv_id: int, summary: str, upto_message_id: int,
                     tokens_before: int = 0) -> None:
        """
        写入/更新压缩摘要。**不删任何消息**——只是把"摘要覆盖到哪条"记下来。
        `upto_message_id` 之前（含）的消息视为"已被摘要代表"。
        """
        ctx = self.get_context(conv_id)
        ctx["summary"] = summary
        ctx["summary_upto"] = int(upto_message_id)
        ctx["compactions"] = int(ctx.get("compactions", 0)) + 1
        ctx["tokens_before_last_compaction"] = int(tokens_before)
        ctx["compacted_at"] = datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            c.execute("UPDATE conversations SET context = ?, updated_at = datetime('now') "
                      "WHERE id = ?", (json.dumps(ctx, ensure_ascii=False), conv_id))

    def summarizable(self, conv_id: int) -> List[Dict[str, Any]]:
        """尚未被摘要覆盖的消息（即 `id > summary_upto`）。"""
        upto = int(self.get_context(conv_id).get("summary_upto", 0))
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM messages WHERE conversation_id = ? AND id > ? "
                "ORDER BY id ASC", (conv_id, upto)).fetchall()
            return [dict(r) for r in rows]

    # ---- 用户偏好（跨会话）--------------------------------------------
    def set_pref(self, user_id: int, key: str, value: Any) -> None:
        v = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        with self._conn() as c:
            c.execute(
                "INSERT INTO user_state (user_id, key, value, updated_at) "
                "VALUES (?,?,?,datetime('now')) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value, "
                "updated_at = datetime('now')",
                (user_id, key, v))

    def get_pref(self, user_id: int, key: str, default: Any = None) -> Any:
        with self._conn() as c:
            r = c.execute("SELECT value FROM user_state WHERE user_id = ? AND key = ?",
                          (user_id, key)).fetchone()
        if not r:
            return default
        try:
            return json.loads(r["value"])
        except (json.JSONDecodeError, TypeError):
            return r["value"]

    def all_prefs(self, user_id: int) -> Dict[str, Any]:
        with self._conn() as c:
            rows = c.execute("SELECT key, value FROM user_state WHERE user_id = ?",
                             (user_id,)).fetchall()
        out: Dict[str, Any] = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value"])
            except (json.JSONDecodeError, TypeError):
                out[r["key"]] = r["value"]
        return out

    # ---- 删除（显式级联，不依赖 PRAGMA 外键）---------------------------
    def delete_user(self, user_id: int) -> Dict[str, int]:
        """
        显式级联删除该用户的会话与消息。

        ⚠️ **为什么不靠 `ON DELETE CASCADE`**：
        `PRAGMA foreign_keys` 是**连接级**开关——不显式打开，SQLite 默认关。
        实测踩过：用裸 `sqlite3.connect()` 删用户，级联没生效，
        留下了 **1 条孤儿会话**（`user_id` 指向已不存在的用户）。
        依赖"每个调用方都记得开 PRAGMA"是不可靠的，所以这里手工级联。
        """
        with self._conn() as c:
            cid = [r["id"] for r in c.execute(
                "SELECT id FROM conversations WHERE user_id = ?", (user_id,))]
            n_msg = 0
            if cid:
                ph = ",".join("?" * len(cid))
                n_msg = c.execute(
                    f"SELECT COUNT(*) FROM messages WHERE conversation_id IN ({ph})",
                    cid).fetchone()[0]
                c.execute(f"DELETE FROM messages WHERE conversation_id IN ({ph})", cid)
                c.execute(f"DELETE FROM conversations WHERE id IN ({ph})", cid)
            c.execute("DELETE FROM user_state WHERE user_id = ?", (user_id,))
            c.execute("DELETE FROM users WHERE id = ?", (user_id,))
            return {"conversations": len(cid), "messages": int(n_msg)}

    def purge_orphans(self) -> Dict[str, int]:
        """清掉历史上因未开外键而产生的孤儿行。返回清理数量。"""
        with self._conn() as c:
            n_conv = c.execute(
                "SELECT COUNT(*) FROM conversations WHERE user_id NOT IN "
                "(SELECT id FROM users)").fetchone()[0]
            n_msg = c.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id NOT IN "
                "(SELECT id FROM conversations)").fetchone()[0]
            c.execute("DELETE FROM messages WHERE conversation_id NOT IN "
                      "(SELECT id FROM conversations)")
            c.execute("DELETE FROM conversations WHERE user_id NOT IN "
                      "(SELECT id FROM users)")
            return {"orphan_conversations": int(n_conv), "orphan_messages": int(n_msg)}

    # ---- 自检 ---------------------------------------------------------
    def stats(self) -> Dict[str, int]:
        with self._conn() as c:
            return {t: int(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
                    for t in AUTHORITATIVE_TABLES}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="agent 记忆层（权威区）")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--stats", action="store_true", help="各表行数")
    ap.add_argument("--conversations", action="store_true", help="列出会话")
    ap.add_argument("--show", type=int, help="打印某个会话的消息")
    ap.add_argument("--prefs", action="store_true", help="列出用户偏好")
    ap.add_argument("--purge-orphans", action="store_true",
                    help="清理孤儿行（未开外键时遗留）")
    a = ap.parse_args()

    m = Memory(a.db)
    if a.purge_orphans:
        print(json.dumps(m.purge_orphans(), ensure_ascii=False))
        return 0
    if a.conversations:
        uid = m.ensure_user("local")
        for c in m.list_conversations(uid):
            print(f"  #{c['id']:<4} [{c['kind']}] {c['title'] or '(无标题)'}  "
                  f"{c['n_msg']} 条  {c['updated_at']}")
        return 0
    if a.show:
        for msg in m.load(a.show):
            head = (msg["content"] or "").replace("\n", " ")[:90]
            extra = f" {msg['tool_name']}" if msg["tool_name"] else ""
            print(f"  #{msg['id']:<4} {msg['role']:<9}{extra:<16} {head}")
        ctx = m.get_context(a.show)
        if ctx.get("summary"):
            print(f"\n  压缩摘要（覆盖至消息 #{ctx.get('summary_upto')}，"
                  f"已压缩 {ctx.get('compactions')} 次）:")
            print("   " + ctx["summary"][:400].replace("\n", "\n   "))
        return 0
    if a.prefs:
        uid = m.ensure_user("local")
        print(json.dumps(m.all_prefs(uid), ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(m.stats(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
