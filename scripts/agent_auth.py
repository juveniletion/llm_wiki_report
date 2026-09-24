# -*- coding: utf-8 -*-
"""
agent_auth.py — **认证目录**（`auth.db`）

职责边界
--------
```
    auth.db          谁是谁（users）+ 谁能进（api_tokens）   ← 本模块
    users/<id>.db    某人的对话/摘要/偏好                    ← agent_memory.py
    shared/*.db      知识库镜像（全体共享只读）              ← db_build.py
```

三者的关系：**令牌 → 认证目录 → 用户标识 → 对应的私有库**。

为什么认证目录必须单独一个库
----------------------------
因为这是"鸡生蛋"：要打开某个用户的库，得先知道他是谁；
而"他是谁"不能从用户库里读——那得先打开他的库。

所以 `users` 与 `api_tokens` 放在一个**中央目录**里，
它只回答两件事：这个令牌有效吗？它对应哪个用户？

为什么令牌只存哈希
------------------
`api_tokens` 表存的是 `sha256(token)`，**不存原文**。
令牌原文只在签发时返回一次，之后无法找回（丢了就重新签发）。
这样即使 `auth.db` 被读走，也拿不到可用的令牌。

修的是什么问题
--------------
原实现里"当前用户"由客户端自己填的 `user` 参数决定：

    GET /api/chat/history?user=demo     ← 填谁的名字就读谁的对话

实测确认：改个参数就能读到别人的全部历史与偏好，**没有任何校验**。
现在改为服务端认定：客户端只能出示令牌，**说不出自己是谁**。

用法
----
    from agent_auth import Auth
    a = Auth()
    uid = a.ensure_user("alice", name="爱丽丝")      # 建目录项
    tok = a.issue_token(uid, label="web 前端")        # 返回明文，仅此一次
    who = a.resolve(tok)                              # → {"user_id":.., "external_id":"alice"} 或 None
    a.revoke(tok)                                     # 吊销

CLI：
    python scripts/agent_auth.py --users
    python scripts/agent_auth.py --issue alice --label "web"
    python scripts/agent_auth.py --list-tokens
    python scripts/agent_auth.py --revoke <token>
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402
from paths import auth_db, ensure_parent, user_db_key  # noqa: E402

ensure_utf8_stdout()

TOKEN_PREFIX = "lw_"          # llm-wiki 前缀，便于人眼识别
TOKEN_BYTES = 32              # 32 字节 → 43 字符 base64url，足够抗暴力


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# =============================================================================
# 密码哈希 —— 标准库 PBKDF2，**不引第三方依赖**
#
# 与"依赖尽量少"的一贯取舍一致（前端 vendor、检索自研同理）。
# Django 长期用的就是 PBKDF2-SHA256，安全性足够；argon2 更强，但要多装
# 二进制依赖，部署时可能得编译。
#
# 存法：`pbkdf2_sha256$<迭代数>$<salt_b64>$<hash_b64>`
#   ⚠️ **迭代数写进字符串里**，将来调高迭代次数时旧密码仍能校验，
#      不用强制所有人改密码。
# =============================================================================

PBKDF2_ITER = 600_000          # OWASP 2023 对 PBKDF2-SHA256 的建议值
PASSWORD_MIN = 8
PASSWORD_MAX = 128


def hash_password(password: str, iterations: int = PBKDF2_ITER) -> str:
    if not isinstance(password, str):
        raise ValueError("密码必须是字符串")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join([
        "pbkdf2_sha256", str(iterations),
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    ])


def verify_password(password: str, stored: str) -> bool:
    """
    核对密码。**用 `hmac.compare_digest` 而不是 `==`**——
    字符串比较会在第一个不同字节处短路返回，攻击者可据**耗时差异**
    逐字节猜出哈希，这叫时序攻击。
    """
    if not stored or not isinstance(password, str):
        return False
    try:
        algo, iters, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        want = base64.b64decode(hash_b64)
        got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                  salt, int(iters))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


# =============================================================================
# 用户名校验 —— ⚠️ 这里挡的是一个**会导致数据串号**的问题
#
# `paths.user_db_key()` 把用户名转成文件名，是**多对一**的：
#     'a/b'  → 'a_b'
#     'a_b'  → 'a_b'      ← 两个不同的用户名映射到同一个文件
# 如果不拦，两个用户会**共用同一个数据区**——那比"读不到"严重得多：
# 甲上传的资料会出现在乙的知识库里，而且双方都察觉不到。
#
# 所以注册时两件事都要做：
#   ① 白名单字符（挡住 '/' 之类需要转义的）
#   ② **按 user_db_key 归一化后查重**（挡住 'a/b' 与 'a_b' 这类）
# =============================================================================

USERNAME_RE = re.compile(r"^[0-9A-Za-z一-鿿_-]{3,32}$")


def validate_username(name: str) -> Tuple[bool, str]:
    n = (name or "").strip()
    if not n:
        return False, "用户名不能为空"
    if len(n) < 3:
        return False, "用户名至少 3 个字符"
    if len(n) > 32:
        return False, "用户名最多 32 个字符"
    if not USERNAME_RE.match(n):
        return False, "用户名只能包含中文、字母、数字、下划线、连字符"
    return True, ""


def validate_password(pw: str) -> Tuple[bool, str]:
    if not isinstance(pw, str) or len(pw) < PASSWORD_MIN:
        return False, f"密码至少 {PASSWORD_MIN} 位"
    if len(pw) > PASSWORD_MAX:
        return False, f"密码最多 {PASSWORD_MAX} 位"
    # 刻意**不强制**大小写/符号组合：固定用户群（厂内几人），
    # 过长口令规则只会让人写在便签上，反而更不安全。
    return True, ""


SESSION_DAYS = 30
LOGIN_MAX_FAILS = 5            # 连续失败几次锁
LOGIN_LOCK_MINUTES = 5         # 锁多久


class Auth:
    """认证目录。**只碰 auth.db。**"""

    def __init__(self, db_path: Optional[Path] = None):
        self.db = Path(db_path) if db_path else auth_db()
        ensure_parent(self.db)
        self._ensure_schema()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        return c

    def _ensure_schema(self) -> None:
        """
        建目录表（幂等）。**不含对话/消息**——那些在每用户库里。

        `users` 用 `ALTER TABLE ADD COLUMN` 增列而不是改建表：
        既有库（已有 screenshot_bot 账号）要能**原地升级**，不能要求重建。
        SQLite 没有 `ADD COLUMN IF NOT EXISTS`，所以先查 `PRAGMA table_info`。
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
        CREATE TABLE IF NOT EXISTS api_tokens (
            token_hash    TEXT PRIMARY KEY,
            user_id       INTEGER NOT NULL,
            label         TEXT,
            created_at    TEXT DEFAULT (datetime('now')),
            last_used_at  TEXT,
            revoked       INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_token_user ON api_tokens(user_id);

        -- 会话：浏览器登录后凭 sid（HttpOnly Cookie）免密访问
        CREATE TABLE IF NOT EXISTS sessions (
            sid         TEXT PRIMARY KEY,
            user_id     INTEGER NOT NULL,
            created_at  TEXT DEFAULT (datetime('now')),
            expires_at  TEXT NOT NULL,
            last_seen   TEXT,
            ua          TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_sess_user ON sessions(user_id);
        CREATE INDEX IF NOT EXISTS idx_sess_exp  ON sessions(expires_at);

        -- 登录失败计数（防暴力猜密码）
        CREATE TABLE IF NOT EXISTS login_attempts (
            key         TEXT PRIMARY KEY,          -- 用户名 或 ip:xxx
            fails       INTEGER DEFAULT 0,
            first_at    TEXT DEFAULT (datetime('now')),
            locked_until TEXT
        );
        """
        with self._conn() as c:
            c.executescript(ddl)
            # 幂等增列（旧库升级路径）
            cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
            if "pw_hash" not in cols:
                c.execute("ALTER TABLE users ADD COLUMN pw_hash TEXT")
            if "db_key" not in cols:
                # ⚠️ 存下**归一化后的数据区键**，用于唯一性校验。
                #    不存的话就得每次扫全表算 user_db_key，
                #    而且没法用 UNIQUE 约束保证不撞。
                c.execute("ALTER TABLE users ADD COLUMN db_key TEXT")
                c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_dbkey "
                          "ON users(db_key) WHERE db_key IS NOT NULL")
            # 回填历史用户（如 screenshot_bot）
            for r in c.execute("SELECT id, external_id FROM users "
                               "WHERE db_key IS NULL").fetchall():
                c.execute("UPDATE users SET db_key=? WHERE id=?",
                          (user_db_key(r["external_id"]), r["id"]))

    # ---- 用户目录 -----------------------------------------------------
    def ensure_user(self, external_id: str, name: str = "",
                    role: str = "user") -> int:
        """
        确认用户存在（不存在则建）。**CLI 与令牌签发走这条路**——
        它不设密码（`pw_hash` 为空 → 该账号登录不了，但用令牌可以）。

        ⚠️ 必须同时写 `db_key`。漏写会让唯一索引对这条记录失效，
           后来者就能注册一个撞键的用户名，**两人共用一个数据区**。
        """
        with self._conn() as c:
            r = c.execute("SELECT id FROM users WHERE external_id = ?",
                          (external_id,)).fetchone()
            if r:
                # 补齐历史记录缺失的 db_key
                c.execute("UPDATE users SET db_key=? WHERE id=? AND db_key IS NULL",
                          (user_db_key(external_id), int(r["id"])))
                return int(r["id"])
            cur = c.execute(
                "INSERT INTO users (external_id, name, role, db_key) VALUES (?,?,?,?)",
                (external_id, name or external_id, role, user_db_key(external_id)))
            return int(cur.lastrowid)

    def get_user(self, external_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM users WHERE external_id = ?",
                          (external_id,)).fetchone()
            return dict(r) if r else None

    # ---- 注册 / 登录 --------------------------------------------------
    def register(self, username: str, password: str,
                 name: str = "") -> Dict[str, Any]:
        """
        注册。返回 `{ok, user_id, external_id}` 或 `{ok: False, error}`。

        ⚠️ 唯一性按 **`db_key`（归一化后的数据区键）** 查，不按 `external_id`。
           原因见文件上方 `validate_username` 的说明：
           `a/b` 与 `a_b` 是不同的用户名，却映射到同一个数据区。
           只查 external_id 会让它们同时注册成功，然后**共用一份数据**。
        """
        u = (username or "").strip()
        ok, err = validate_username(u)
        if not ok:
            return {"ok": False, "error": err}
        ok, err = validate_password(password)
        if not ok:
            return {"ok": False, "error": err}

        key = user_db_key(u)
        with self._conn() as c:
            dup = c.execute(
                "SELECT external_id FROM users WHERE db_key = ?", (key,)).fetchone()
            if dup:
                # 两种情况分开说：同名算"已存在"，不同名但撞键算"太相似"
                if dup["external_id"] == u:
                    return {"ok": False, "error": "该用户名已被注册"}
                return {"ok": False, "error":
                        f"该用户名与已有的「{dup['external_id']}」过于相似"
                        f"（会共用同一个数据区），请换一个"}
            cur = c.execute(
                "INSERT INTO users (external_id, name, role, pw_hash, db_key) "
                "VALUES (?,?,?,?,?)",
                (u, name or u, "user", hash_password(password), key))
            return {"ok": True, "user_id": int(cur.lastrowid), "external_id": u}

    def authenticate(self, username: str, password: str,
                     ip: str = "", ua: str = "") -> Dict[str, Any]:
        """
        校验用户名 + 密码。**带失败限流**。

        返回 `{ok, user_id, external_id, name, sid?}` 或 `{ok:False, error}`。

        ⚠️ 失败信息**不区分**"用户不存在"与"密码错误"——
           区分了就等于告诉攻击者"这个用户名是存在的"，
           可以拿来枚举账号。（限流也一并依赖这个统一出口。）
        """
        u = (username or "").strip()
        generic = "用户名或密码不正确"

        locked, wait = self._locked(minutes=LOGIN_LOCK_MINUTES, key=f"u:{u}")
        if locked:
            return {"ok": False, "error": f"失败次数过多，请 {wait} 分钟后再试",
                    "locked": True}

        with self._conn() as c:
            r = c.execute("SELECT id, external_id, name, pw_hash FROM users "
                          "WHERE external_id = ?", (u,)).fetchone()

        if not r or not r["pw_hash"] or not verify_password(password, r["pw_hash"]):
            self._note_fail(f"u:{u}")
            if ip:
                self._note_fail(f"ip:{ip}")
            return {"ok": False, "error": generic}

        self._clear_fails(f"u:{u}")
        if ip:
            self._clear_fails(f"ip:{ip}")
        return {"ok": True, "user_id": int(r["id"]),
                "external_id": r["external_id"], "name": r["name"] or r["external_id"]}

    def set_password(self, external_id: str, password: str) -> bool:
        ok, _ = validate_password(password)
        if not ok:
            return False
        with self._conn() as c:
            cur = c.execute("UPDATE users SET pw_hash=?, "
                            "updated_at=datetime('now') WHERE external_id=?",
                            (hash_password(password), external_id))
            return cur.rowcount > 0

    # ---- 失败限流 -----------------------------------------------------
    def _locked(self, *, minutes: int, key: str) -> Tuple[bool, int]:
        with self._conn() as c:
            r = c.execute("SELECT locked_until FROM login_attempts WHERE key=?",
                          (key,)).fetchone()
        if not r or not r["locked_until"]:
            return False, 0
        try:
            until = datetime.fromisoformat(r["locked_until"])
        except ValueError:
            return False, 0
        # 统一用 UTC（SQLite 的 datetime('now') 就是 UTC）
        now = datetime.utcnow()
        if until > now:
            return True, max(1, int((until - now).total_seconds() // 60) + 1)
        return False, 0

    def _note_fail(self, key: str) -> None:
        with self._conn() as c:
            r = c.execute("SELECT fails FROM login_attempts WHERE key=?",
                          (key,)).fetchone()
            n = (r["fails"] if r else 0) + 1
            lock = ((datetime.utcnow() + timedelta(minutes=LOGIN_LOCK_MINUTES))
                    .isoformat(timespec="seconds")) if n >= LOGIN_MAX_FAILS else None
            c.execute(
                "INSERT INTO login_attempts(key,fails,first_at,locked_until) "
                "VALUES(?,?,datetime('now'),?) "
                "ON CONFLICT(key) DO UPDATE SET fails=excluded.fails, "
                "  locked_until=excluded.locked_until",
                (key, n, lock))

    def _clear_fails(self, key: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM login_attempts WHERE key=?", (key,))

    # ---- 会话（HttpOnly Cookie）---------------------------------------
    def create_session(self, user_id: int, ua: str = "",
                       days: int = SESSION_DAYS) -> str:
        sid = secrets.token_urlsafe(32)
        exp = (datetime.utcnow() + timedelta(days=days)).isoformat(timespec="seconds")
        with self._conn() as c:
            c.execute("INSERT INTO sessions (sid,user_id,expires_at,last_seen,ua) "
                      "VALUES(?,?,?,datetime('now'),?)", (sid, user_id, exp, ua[:200]))
        return sid

    def resolve_session(self, sid: str) -> Optional[Dict[str, Any]]:
        """会话 id → 用户信息。过期/不存在返回 None，并顺手清掉过期行。"""
        if not sid:
            return None
        now = datetime.utcnow().isoformat(timespec="seconds")
        with self._conn() as c:
            c.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            r = c.execute(
                "SELECT s.user_id, u.external_id, u.name, u.role "
                "FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.sid = ? AND s.expires_at > ?", (sid, now)).fetchone()
            if not r:
                return None
            c.execute("UPDATE sessions SET last_seen=datetime('now') WHERE sid=?",
                      (sid,))
            return {"user_id": int(r["user_id"]), "external_id": r["external_id"],
                    "name": r["name"], "role": r["role"]}

    def revoke_session(self, sid: str) -> bool:
        if not sid:
            return False
        with self._conn() as c:
            return c.execute("DELETE FROM sessions WHERE sid=?", (sid,)).rowcount > 0

    def purge_sessions(self, external_id: str = "") -> int:
        with self._conn() as c:
            if external_id:
                r = c.execute("SELECT id FROM users WHERE external_id=?",
                              (external_id,)).fetchone()
                if not r:
                    return 0
                return c.execute("DELETE FROM sessions WHERE user_id=?",
                                 (int(r["id"]),)).rowcount
            return c.execute(
                "DELETE FROM sessions WHERE expires_at <= ?",
                (datetime.utcnow().isoformat(timespec="seconds"),)).rowcount

    def list_users(self) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT u.*, (SELECT COUNT(*) FROM api_tokens t "
                "  WHERE t.user_id = u.id AND t.revoked = 0) AS n_tokens "
                "FROM users u ORDER BY u.id").fetchall()
            return [dict(r) for r in rows]

    def delete_user(self, external_id: str) -> Dict[str, int]:
        """删目录项（令牌随外键级联）。⚠️ **不动用户库文件**——那是数据，另删。"""
        with self._conn() as c:
            r = c.execute("SELECT id FROM users WHERE external_id = ?",
                          (external_id,)).fetchone()
            if not r:
                return {"deleted": 0}
            uid = int(r["id"])
            n = c.execute("SELECT COUNT(*) FROM api_tokens WHERE user_id = ?",
                          (uid,)).fetchone()[0]
            c.execute("DELETE FROM api_tokens WHERE user_id = ?", (uid,))
            c.execute("DELETE FROM users WHERE id = ?", (uid,))
            return {"deleted": 1, "tokens_revoked": int(n)}

    # ---- 令牌 ---------------------------------------------------------
    def issue_token(self, user_id: int, label: str = "") -> str:
        """
        签发令牌。**返回明文，且仅此一次**——库里只存哈希，之后无法找回。
        """
        raw = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
        with self._conn() as c:
            c.execute("INSERT INTO api_tokens (token_hash, user_id, label) "
                      "VALUES (?,?,?)", (_hash(raw), user_id, label))
        return raw

    def resolve(self, token: str) -> Optional[Dict[str, Any]]:
        """
        令牌 → 用户信息。无效/已吊销返回 None。
        成功时顺手更新 `last_used_at`（便于排查"这个令牌还有人在用吗"）。
        """
        if not token:
            return None
        h = _hash(token.strip())
        with self._conn() as c:
            r = c.execute(
                "SELECT t.user_id, t.revoked, u.external_id, u.name, u.role "
                "FROM api_tokens t JOIN users u ON u.id = t.user_id "
                "WHERE t.token_hash = ?", (h,)).fetchone()
            if not r or r["revoked"]:
                return None
            c.execute("UPDATE api_tokens SET last_used_at = datetime('now') "
                      "WHERE token_hash = ?", (h,))
            return {"user_id": int(r["user_id"]), "external_id": r["external_id"],
                    "name": r["name"], "role": r["role"]}

    def revoke(self, token: str) -> bool:
        with self._conn() as c:
            cur = c.execute("UPDATE api_tokens SET revoked = 1 WHERE token_hash = ?",
                            (_hash(token.strip()),))
            return cur.rowcount > 0

    def list_tokens(self) -> List[Dict[str, Any]]:
        """列出令牌（**只有前 8 位哈希**，无法反推原文）。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT t.token_hash, t.label, t.created_at, t.last_used_at, "
                "       t.revoked, u.external_id, u.name "
                "FROM api_tokens t JOIN users u ON u.id = t.user_id "
                "ORDER BY t.created_at DESC").fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["token_fp"] = d.pop("token_hash")[:8] + "…"
                out.append(d)
            return out

    def stats(self) -> Dict[str, int]:
        with self._conn() as c:
            return {
                "users": c.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                "tokens": c.execute("SELECT COUNT(*) FROM api_tokens").fetchone()[0],
                "tokens_active": c.execute(
                    "SELECT COUNT(*) FROM api_tokens WHERE revoked = 0").fetchone()[0],
            }


# ---------------------------------------------------------------------------

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="认证目录（auth.db）")
    ap.add_argument("--users", action="store_true", help="列出用户")
    ap.add_argument("--add-user", metavar="ID", help="新增/确认一个用户")
    ap.add_argument("--name", default="", help="配合 --add-user 的显示名")
    ap.add_argument("--issue", metavar="ID", help="为某用户签发令牌")
    ap.add_argument("--label", default="", help="令牌备注")
    ap.add_argument("--list-tokens", action="store_true", help="列出令牌")
    ap.add_argument("--revoke", metavar="TOKEN", help="吊销令牌")
    ap.add_argument("--set-pw", metavar="ID", help="为已有用户设置/重置密码")
    ap.add_argument("--pw", default="", help="配合 --set-pw 的密码（不传则交互输入）")
    ap.add_argument("--db", help="auth.db 路径（默认取 paths.auth_db()）")
    a = ap.parse_args()

    au = Auth(Path(a.db) if a.db else None)

    if a.set_pw:
        import getpass
        pw = a.pw or getpass.getpass(f"为 {a.set_pw} 设置密码（≥8 位）: ")
        if not au.get_user(a.set_pw):
            print(f"⚠️ 用户 {a.set_pw} 不存在。先建：--add-user {a.set_pw}")
            return 1
        ok, err = validate_password(pw)
        if not ok:
            print(f"⚠️ {err}")
            return 1
        au.set_password(a.set_pw, pw)
        print(f"✅ 已为 {a.set_pw} 设置密码")
        return 0

    if a.add_user:
        uid = au.ensure_user(a.add_user, name=a.name)
        print(f"✅ 用户 {a.add_user} 就绪（id={uid}）")
        return 0

    if a.issue:
        uid = au.ensure_user(a.issue, name=a.name or a.issue)
        tok = au.issue_token(uid, label=a.label)
        print("=" * 72)
        print(f"已为用户 {a.issue} 签发令牌（id={uid}）：")
        print()
        print(f"    {tok}")
        print()
        print("⚠️ **本令牌只显示这一次**——库中只存哈希，之后无法找回。")
        print("   请立即保存。丢失只能重新签发。")
        print("=" * 72)
        return 0

    if a.list_tokens:
        for t in au.list_tokens():
            state = "已吊销" if t["revoked"] else "有效"
            print(f"  {t['token_fp']:<12} {t['external_id']:<12} [{state}] "
                  f"{t['label'] or '—':<14} 最后使用 {t['last_used_at'] or '从未'}")
        return 0

    if a.revoke:
        print("✅ 已吊销" if au.revoke(a.revoke) else "⚠️ 未找到该令牌")
        return 0

    if a.users:
        for u in au.list_users():
            print(f"  #{u['id']:<3} {u['external_id']:<14} {u['name'] or '':<12} "
                  f"{u['role']:<8} 有效令牌 {u['n_tokens']}")
        return 0

    print(json.dumps(au.stats(), ensure_ascii=False, indent=2))
    print(f"\n认证目录：{au.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
