-- =============================================================================
-- llm-wiki.db — SQLite schema
-- -----------------------------------------------------------------------------
-- ★★ 三个库，按职责物理分开 ★★
--
--   【认证目录】auth.db        谁是谁（users）+ 谁能进（api_tokens）
--                              由 scripts/agent_auth.py 建表
--
--   【用户库】users/<id>.db    某人的对话/摘要/偏好
--                              由 scripts/agent_memory.py 建表
--
--   【知识库】shared/knowledge.db  本文件描述的**镜像区**（mirror_*）
--                              全体用户共享、只读、可由 db_build.py 重建
--
-- 为什么拆成三个文件（而不是一个库里按 user_id 分）
-- ------------------------------------------------
-- 原先所有用户数据在同一个 .db 里，靠 `WHERE user_id = ?` 隔离。
-- 那有两个问题：
--   1. "当前用户"由客户端自报的 `user` 参数决定 —— 改个参数就能读别人的对话
--   2. 就算加了认证，一次忘带 user_id 的查询仍会跨用户泄漏
--
-- 物理分开后，第 2 个问题在**文件层面**就不可能发生：
-- 服务 alice 时打开的压根不是 bob 的库。
--
-- ⭐ 而**知识库绝不能按用户复制**——它是全公司共享的成本事实源，
--    复制多份必然各自漂移（A 看到旧结论、B 看到新的），
--    那就违背了本库"唯一事实源"的根基。

-- =============================================================================

PRAGMA journal_mode = WAL;      -- 并发读不阻塞写
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

-- -----------------------------------------------------------------------------
-- 元信息：schema 版本、来源指纹、建库时间
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT DEFAULT (datetime('now'))
);

-- =============================================================================
-- 【镜像区】—— 可重建
-- =============================================================================

-- ---- 1. 文档（raw/ 下的每个文件一条）----------------------------------------
CREATE TABLE IF NOT EXISTS mirror_documents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    rel_path      TEXT NOT NULL UNIQUE,     -- 相对 wiki 根的路径
    file_name     TEXT NOT NULL,
    ext           TEXT,                     -- .csv/.pdf/.md/...
    topic         TEXT,                     -- csv/cost_data 等（由目录推断）
    size_bytes    INTEGER,
    sha256        TEXT,                     -- 内容哈希：检测"这个文件变了吗"
    is_binary     INTEGER DEFAULT 0,        -- 1 = 原始二进制进了 blob 表
    derived_text  TEXT,                     -- 提取后的可检索文本（PDF/Word 的 .txt 内容）
    extracted_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_doc_sha  ON mirror_documents(sha256);
CREATE INDEX IF NOT EXISTS idx_doc_topic ON mirror_documents(topic);

-- ---- 2. 原始二进制（**另表存放**，不塞进 documents）-------------------------
-- 为什么分开：blob 会让 documents 表膨胀、拖慢每次全表扫描。
-- 分开后，"查文档元信息"与"取原文"互不干扰。
CREATE TABLE IF NOT EXISTS mirror_blobs (
    document_id  INTEGER PRIMARY KEY,       -- 与 documents 一对一
    content      BLOB NOT NULL,             -- 原件字节
    FOREIGN KEY (document_id) REFERENCES mirror_documents(id) ON DELETE CASCADE
);

-- ---- 3. 词条（wiki/ 下的每篇，排除 index/log）-------------------------------
CREATE TABLE IF NOT EXISTS mirror_articles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    rel_path      TEXT NOT NULL UNIQUE,     -- 相对 wiki/ 的路径
    topic         TEXT,                     -- products/costs/...
    title         TEXT,
    info_cutoff   TEXT,                     -- 元数据「信息截点」
    last_compiled TEXT,                     -- 元数据「最后编译」
    raw_fields    TEXT,                     -- 元数据「Raw:」原文（JSON 数组）
    body          TEXT,                     -- 全文
    char_count    INTEGER,
    sha256        TEXT
);
CREATE INDEX IF NOT EXISTS idx_art_topic ON mirror_articles(topic);

-- ---- 4. 章节切块（供检索；与 scripts/retrieval.py 的切法一致）---------------
CREATE TABLE IF NOT EXISTS mirror_chunks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id    INTEGER NOT NULL,
    chunk_key     TEXT NOT NULL UNIQUE,     -- 稳定标识 `<rel>#<idx>`（与检索层对齐）
    section       TEXT,                     -- 章节标题
    section_path  TEXT,                     -- 人类可读定位
    start_line    INTEGER,
    text          TEXT,
    has_status    INTEGER DEFAULT 0,        -- 是否含 Status 块（已知冲突）
    FOREIGN KEY (article_id) REFERENCES mirror_articles(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chunk_art ON mirror_chunks(article_id);

-- ---- 5. 关键数值（state/metrics.json 的落库版）------------------------------
CREATE TABLE IF NOT EXISTS mirror_metrics (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_key    TEXT NOT NULL UNIQUE,     -- state 的稳定 ID（含 article，可 diff）
    entity        TEXT,
    metric        TEXT,
    period        TEXT,
    value         REAL,
    unit          TEXT,
    article       TEXT,                     -- 来源词条
    section       TEXT,
    line          INTEGER,
    coord         TEXT,                     -- 行内证据坐标
    disputed      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_met_em  ON mirror_metrics(entity, metric);
CREATE INDEX IF NOT EXISTS idx_met_per ON mirror_metrics(period);

-- ---- 6. 已知冲突（Status 块）-----------------------------------------------
CREATE TABLE IF NOT EXISTS mirror_conflicts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    article       TEXT NOT NULL,
    section       TEXT,
    kind          TEXT,                     -- Disputed / Outdated / Update
    block_text    TEXT,                     -- 冲突块原文
    line          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_conf_art ON mirror_conflicts(article);

-- ---- 7. 数值变更日志（state.verify 的落库版）-------------------------------
-- 每次重建镜像时，比对上次的 metrics，记下"哪个值变了"。
-- 这是审计轨迹：11.21 什么时候变成 11.25，查这里。
CREATE TABLE IF NOT EXISTS mirror_metric_changes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_key    TEXT NOT NULL,
    old_value     REAL,
    new_value     REAL,
    detected_at   TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chg_key ON mirror_metric_changes(metric_key);

-- ---- 8. 全文检索（FTS5，逐字分词以支持中文）---------------------------------
-- 中文没有空格，unicode61 会把整句当一个 token。
-- 解法：入库前在**每个汉字间插空格**（见 db_build.py 的 seg()），
-- 使每个字成为独立 token → 任意长度的中文子串都能命中。
CREATE VIRTUAL TABLE IF NOT EXISTS mirror_fts USING fts5(
    ref_kind UNINDEXED,     -- 'chunk' | 'document'
    ref_id   UNINDEXED,     -- 对应表的 id
    title,                  -- 词条标题 / 文档名（便于排序与显示）
    body,                   -- 已逐字分词的正文
    tokenize = 'unicode61'
);

-- （原【权威区】的表已迁出——见下方说明）

-- =============================================================================
-- 视图：把镜像区的常用查询封好，供报告侧直接调
-- =============================================================================

-- 每个 (实体, 指标) 的取值分布（含争议标记）
CREATE VIEW IF NOT EXISTS v_metric_lookup AS
SELECT entity, metric, period, value, unit, article, line, coord, disputed,
       metric_key
FROM mirror_metrics
ORDER BY entity, metric, period;

-- 哪些词条含已知冲突
CREATE VIEW IF NOT EXISTS v_articles_with_conflicts AS
SELECT DISTINCT a.rel_path, a.title, c.kind
FROM mirror_articles a
JOIN mirror_conflicts c ON c.article = a.rel_path;
