"""SQLite 数据存储：账号、会话与参数预设。"""

import functools
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid

import config

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
PRESETS_FILE = os.path.join(DATA_DIR, "presets.json")
DB_FILE = os.path.join(DATA_DIR, "app.db")


# 进程级可重入锁：保护会话的「读取整行 → 修改 → 整行写回」序列。
# 每次改会话都是 get_session（读整列 messages JSON）→ 改内存 → _save_session（整列写回），
# 中间没有任何并发保护；waitress 单进程多线程下，前端落库与后台记忆维护线程会互相用
# 过期快照覆盖整行，导致刚写入的消息丢失。此锁把整段读改写串行化，消除丢失更新。
# 用可重入锁以支持 save_session_memory 等嵌套调用（外层已持锁时内层直接进入）。
_SESSION_LOCK = threading.RLock()


def _locked(fn):
    """装饰器：在该函数的会话整行读改写期间持有 _SESSION_LOCK。"""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _SESSION_LOCK:
            return fn(*args, **kwargs)

    return wrapper


def _ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)


def _connect():
    _ensure_dirs()
    # timeout 让写操作在锁冲突时等待（而非立刻抛 database is locked），
    # 缓解记忆维护后台线程与请求读库/写库的并发冲突。
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 数据库重构 P0：开启 WAL（提升读写并发，避免读被写阻塞）+ busy_timeout
    # （锁冲突时等待而非立刻抛 database is locked）。
    # journal_mode=WAL 为持久化设置（写入库文件，其后所有连接自动生效）；
    # busy_timeout 为连接级设置，需每次连接设置。
    conn.execute(f"PRAGMA journal_mode={config.SQLITE_JOURNAL_MODE}")
    conn.execute(f"PRAGMA busy_timeout={int(config.SQLITE_BUSY_TIMEOUT_MS)}")
    return conn


def _init_db():
    with _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                title TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                model TEXT,
                params TEXT,
                auto_title_generated INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user_updated
                ON sessions(username, updated_at DESC);
            CREATE TABLE IF NOT EXISTS messages (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id   TEXT    NOT NULL,
                username     TEXT    NOT NULL,
                role         TEXT    NOT NULL,
                content      TEXT    NOT NULL DEFAULT '',
                ts           REAL    NOT NULL,
                reasoning    TEXT,
                files        TEXT,
                interrupted  INTEGER NOT NULL DEFAULT 0,
                scene_break  INTEGER NOT NULL DEFAULT 0,
                scene_label  TEXT,
                annotation   TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session_id
                ON messages(session_id, id);
            CREATE INDEX IF NOT EXISTS idx_messages_session_role
                ON messages(session_id, role, id);
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        if "auto_title_generated" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN auto_title_generated INTEGER NOT NULL DEFAULT 0")
        if "vm" not in columns:
            # 向量记忆设置（每会话独立，JSON: {"enabled": bool, "model": str, "recent_n": int}）
            conn.execute("ALTER TABLE sessions ADD COLUMN vm TEXT")
        if "memory" not in columns:
            # 四层记忆（每会话独立，JSON）：card(核心设定卡) / facts(动态关键事实) /
            # summary(剧情摘要) / vector(向量记忆设置，迁移自旧 vm 字段)
            conn.execute("ALTER TABLE sessions ADD COLUMN memory TEXT")
            # 老会话：把已有的 vm 迁移进 memory.vector，避免新面板丢配置
            _migrate_vm_to_memory(conn)
        # P1/P4：旧列 sessions.messages → 独立 messages 表
        #  - 存在旧列：先幂等回填（若尚未回填），成功后归档删除该列（P4）
        #  - 不存在（全新库或已迁移）：跳过
        if "messages" in columns:
            _migrate_messages(conn)
            try:
                conn.execute("ALTER TABLE sessions DROP COLUMN messages")
                print("[migrate] P4: 已移除旧列 sessions.messages（数据由 messages 表承载）")
            except sqlite3.OperationalError as e:  # noqa: BLE001
                print(f"[migrate] P4: 删除旧列失败（SQLite 版本过低？）: {e}")
        # 注释列：user 消息可携带一次性提示词（仅当前轮注入，历史仅作展示）。
        # 必须在 _migrate_messages_fk 之前补齐，否则重建表时 SELECT 会引用不存在的列。
        msg_columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        if "annotation" not in msg_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN annotation TEXT")
        # P4-c：确保 messages.session_id 具备外键 ON DELETE CASCADE（幂等，无外键才重建）
        _migrate_messages_fk(conn)


def _migrate_vm_to_memory(conn):
    """把已有的 vm 字段迁移进 memory.vector（老会话数据兼容，不丢配置）。"""
    rows = conn.execute(
        "SELECT id, vm FROM sessions WHERE vm IS NOT NULL AND vm != '' AND "
        "(memory IS NULL OR memory = '')"
    ).fetchall()
    for row in rows:
        try:
            vm = json.loads(row["vm"])
        except (TypeError, ValueError):
            continue
        if not isinstance(vm, dict):
            continue
        memory = {"card": None, "facts": [], "summary": None, "vector": _parse_vm(vm)}
        conn.execute(
            "UPDATE sessions SET memory = ? WHERE id = ?",
            (json.dumps(memory, ensure_ascii=False), row["id"]),
        )


def _migrate_messages(conn):
    """P1：把旧列 sessions.messages（整块 JSON）幂等回填到独立 messages 表。

    幂等：只回填 messages 表中尚不存在的会话，可重复执行（重启不会重复插入）。
    顺序：按会话、按原数组顺序逐条插入，保证同一会话内 id 升序 == 原消息顺序。
    校验：每个会话回填后 COUNT(*) 必须等于旧 JSON 中有效消息条数，否则抛错；
          抛错会使 _init_db 的连接上下文回滚本次全部回填（表保留为空），下次重跑。
    读路径：本阶段不改任何读函数，仍走旧列（本函数只写 messages 表）。
    """
    rows = conn.execute("SELECT id, username, messages FROM sessions").fetchall()
    if not rows:
        return
    done = {r[0] for r in conn.execute("SELECT DISTINCT session_id FROM messages")}
    migrated_sessions = 0
    migrated_msgs = 0
    for r in rows:
        sid = r["id"]
        if sid in done:
            continue
        username, blob = r["username"], r["messages"]
        try:
            raw = json.loads(blob) if blob else []
        except (TypeError, ValueError):
            print(f"[migrate] 跳过无法解析的会话 {sid}（messages 非法 JSON）")
            raw = []
        if not isinstance(raw, list):
            raw = []
        valid = [m for m in raw if isinstance(m, dict)]
        if not valid:
            # 空会话无需回填，也不写入 messages（避免每次重启重复扫描同一批空会话）
            continue
        if len(valid) != len(raw):
            print(f"[migrate] 会话 {sid}: 忽略 {len(raw) - len(valid)} 条非对象消息")
        # 清理可能的上次半成品后重插（保证幂等可重跑）
        conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
        for m in valid:
            try:
                ts = float(m.get("ts"))
            except (TypeError, ValueError):
                ts = 0.0
            files = m.get("files")
            conn.execute(
                "INSERT INTO messages(session_id, username, role, content, ts, "
                "reasoning, files, interrupted, scene_break, scene_label) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sid,
                    username,
                    str(m.get("role") or ""),
                    m.get("content") or "",
                    ts,
                    m.get("reasoning"),
                    json.dumps(files, ensure_ascii=False) if files is not None else None,
                    1 if m.get("interrupted") else 0,
                    1 if m.get("scene_break") else 0,
                    m.get("scene_label"),
                ),
            )
        n = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?", (sid,)
        ).fetchone()[0]
        if n != len(valid):
            raise RuntimeError(
                f"消息回填校验失败: session={sid} 期望={len(valid)} 实际={n}"
            )
        migrated_sessions += 1
        migrated_msgs += n
    if migrated_sessions == 0:
        return
    conn.commit()
    print(f"[migrate] messages 表回填完成: {migrated_sessions} 个会话 / {migrated_msgs} 条消息")


def _migrate_messages_fk(conn):
    """P4-c：为 messages.session_id 添加外键 ON DELETE CASCADE（SQLite 需重建表）。

    幂等：已存在外键则跳过。重建前先清理孤儿行（否则 INSERT 进带外键的新表会失败）。
    重建无法用 ALTER 完成，故 CREATE 新表 → INSERT SELECT → DROP → RENAME（整体一个事务）。
    """
    if conn.execute("PRAGMA foreign_key_list(messages)").fetchall():
        return
    # 清理孤儿（新表带外键，孤儿会使 INSERT 违反约束）
    conn.execute(
        "DELETE FROM messages WHERE session_id NOT IN (SELECT id FROM sessions)"
    )
    conn.executescript("""
        DROP TABLE IF EXISTS messages_new;
        BEGIN;
        CREATE TABLE messages_new (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            username     TEXT    NOT NULL,
            role         TEXT    NOT NULL,
            content      TEXT    NOT NULL DEFAULT '',
            ts           REAL    NOT NULL,
            reasoning    TEXT,
            files        TEXT,
            interrupted  INTEGER NOT NULL DEFAULT 0,
            scene_break  INTEGER NOT NULL DEFAULT 0,
            scene_label  TEXT,
            annotation   TEXT
        );
        INSERT INTO messages_new(id, session_id, username, role, content, ts,
            reasoning, files, interrupted, scene_break, scene_label, annotation)
            SELECT id, session_id, username, role, content, ts,
            reasoning, files, interrupted, scene_break, scene_label, annotation FROM messages;
        DROP TABLE messages;
        ALTER TABLE messages_new RENAME TO messages;
        CREATE INDEX idx_messages_session_id ON messages(session_id, id);
        CREATE INDEX idx_messages_session_role ON messages(session_id, role, id);
        COMMIT;
    """)
    print("[migrate] P4-c: messages 表已重建并添加外键 ON DELETE CASCADE")


def _default_memory():
    """会话四层记忆的默认结构。"""
    return {
        "worlds": [],      # 世界卡列表 [{"id","name","content","updated_at"}]，先于人物卡注入
        "cards": [],       # 人物卡列表 [{"id","name","content","updated_at"}]，一角色一卡
        "facts": [],       # 动态关键事实 [{"text","ts"}]
        "facts_last_round": 0,  # 事实抽取进度（已处理到的轮次，切片抽取用）
        "facts_slice_rounds": config.FACT_SLICE_ROUNDS,  # ② 事实切片宽度（轮），与会话摘要切片分开
        "facts_max_per_slice": config.FACT_MAX_PER_SLICE,  # ② 每片最多抽取条数（0 = 不限制）
        "facts_enabled": True,  # ② 动态关键事实开关（关闭时保留内容但停止注入/维护）
        "facts_auto": True,     # ② 自动总结开关（总开关下一级；关闭时不触发后台抽取，仅手动）
        "summary": None,   # 剧情摘要 {"text","summarized_ts","last_round","enabled",...}
        "recent_n": config.MEMORY_RECENT_N_DEFAULT,  # 最近 N 轮（新会话默认 0 = 全量塞满）
        "vector": _parse_vm(None),  # 向量记忆设置（迁移自旧 vm）
    }


def _parse_recent_n(raw, default=None):
    default = config.VECTOR_MEMORY_RECENT_N if default is None else default
    if raw is None or raw == "":
        return default
    try:
        return max(0, min(int(raw), 1000))
    except (TypeError, ValueError):
        return default


def _parse_memory(raw):
    """规范化四层记忆结构。接收 dict 或 JSON 字符串或 None；缺省返回默认结构。"""
    mem = _default_memory()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return mem
    if not isinstance(raw, dict):
        return mem
    # 人物卡列表（多角色，一角色一卡）。兼容旧单卡结构：
    # card 为 dict（{"content",...}）或 str 时迁移为单元素 cards 列表。
    mem["cards"] = []
    if isinstance(raw.get("cards"), list):
        for c in raw["cards"]:
            if not isinstance(c, dict):
                continue
            mem["cards"].append({
                "id": str(c.get("id") or uuid.uuid4().hex),
                "name": str(c.get("name") or "").strip(),
                "content": str(c.get("content", "")).strip(),
                "main": bool(c.get("main", False)),  # 主角色标记：AI 第一人称扮演的角色
                "updated_at": float(c.get("updated_at") or 0),
            })
    # 兼容旧单卡结构：cards 为空且存在旧 card 字段（dict 或 str）时迁移为单元素列表
    if not mem["cards"] and raw.get("card") is not None:
        old = raw["card"]
        if isinstance(old, dict):
            content = str(old.get("content", "")).strip()
        else:
            content = str(old).strip()
        if content:
            mem["cards"].append({
                "id": uuid.uuid4().hex,
                "name": "",
                "content": content,
                "main": True,  # 旧单卡结构：唯一卡即主角色
                "updated_at": float(old.get("updated_at") or 0) if isinstance(old, dict) else 0,
            })
    # 注意：允许没有任何主角色卡（main 全为 False），不强制回退
    if not isinstance(raw.get("facts"), list):
        mem["facts"] = []
    else:
        mem["facts"] = [
            {
                "text": str(f.get("text", "")).strip(),
                "ts": float(f.get("ts", 0)),
                "locked": bool(f.get("locked", False)),  # 上锁条目不被重新生成影响
            }
            for f in raw["facts"] if isinstance(f, dict) and str(f.get("text", "")).strip()
        ]
    # 世界卡列表（多张，一卡一世界观设定，先于人物卡注入）。
    mem["worlds"] = []
    if isinstance(raw.get("worlds"), list):
        for w in raw["worlds"]:
            if not isinstance(w, dict):
                continue
            mem["worlds"].append({
                "id": str(w.get("id") or uuid.uuid4().hex),
                "name": str(w.get("name") or "").strip(),
                "content": str(w.get("content", "")).strip(),
                "updated_at": float(w.get("updated_at") or 0),
            })
    mem["facts_enabled"] = bool(raw.get("facts_enabled", True))
    mem["facts_auto"] = bool(raw.get("facts_auto", True))  # ② 自动总结开关（总开关的下一级）
    try:
        mem["facts_last_round"] = max(0, int(raw.get("facts_last_round") or 0))
    except (TypeError, ValueError):
        mem["facts_last_round"] = 0
    if raw.get("facts_slice_rounds") is not None and str(raw.get("facts_slice_rounds")).strip() != "":
        try:
            mem["facts_slice_rounds"] = max(1, min(200, int(raw["facts_slice_rounds"])))
        except (TypeError, ValueError):
            pass  # 无效值保留默认
    if raw.get("facts_max_per_slice") is not None and str(raw.get("facts_max_per_slice")).strip() != "":
        try:
            mem["facts_max_per_slice"] = max(0, min(50, int(raw["facts_max_per_slice"])))
        except (TypeError, ValueError):
            pass  # 无效值保留默认
    s = raw.get("summary")
    mem["summary"] = _parse_summary(s) if s is not None else None
    vec_cfg = raw.get("vector") if isinstance(raw.get("vector"), dict) else raw.get("vm")
    mem["vector"] = _parse_vm(vec_cfg) if isinstance(vec_cfg, dict) else _parse_vm(None)
    mem["recent_n"] = _parse_recent_n(raw.get("recent_n"), default=mem["vector"].get("recent_n", config.VECTOR_MEMORY_RECENT_N))
    if "recent_n" not in raw and isinstance(vec_cfg, dict) and "recent_n" in vec_cfg:
        mem["recent_n"] = _parse_recent_n(vec_cfg.get("recent_n"), default=mem["recent_n"])
    return mem


def _parse_summary_slices(raw):
    """规范化切片实体列表：[{id, name, text, start_round, end_round, edited, ts}]。

    start_round / end_round 为对话轮次（1-based，含两端）；非法条目跳过。
    """
    if not isinstance(raw, list):
        return []
    out = []
    for i, s in enumerate(raw):
        if not isinstance(s, dict):
            continue
        try:
            start = max(1, int(s.get("start_round") or 0))
            end = max(start, int(s.get("end_round") or 0))
        except (TypeError, ValueError):
            continue
        out.append({
            "id": str(s.get("id") or f"s{i + 1}_{int(time.time() * 1000)}"),
            "name": str(s.get("name") or "").strip()[:50],
            "text": str(s.get("text") or "").strip(),
            "start_round": start,
            "end_round": end,
            "edited": bool(s.get("edited")),
            "ts": float(s.get("ts") or time.time()),
        })
    return out


def _summary_slices_text(slices):
    """把切片实体列表拼接成整份摘要文本：一片一行，有名字的片加【场景名】前缀。"""
    lines = []
    for s in slices or []:
        t = str(s.get("text") or "").strip()
        if not t:
            continue
        name = str(s.get("name") or "").strip()
        lines.append(f"【{name}】{t}" if name else t)
    return "\n".join(lines)


def _parse_summary(s):
    """规范化剧情摘要结构。接收 dict 或 None；缺省返回带默认配置的结构。

    结构：{text, summarized_ts, last_round, slice_rounds, slices}
    slice_rounds 为用户可设置的切片宽度；自动总结以切片为单位（每积累一个完整切片触发一次）。
    slices 为持久化切片实体列表（落袋切片，按剧情顺序）：每片
    {id, name, text, start_round, end_round, edited, ts}；
    text 为派生字段（由各片拼接生成，见 _summary_slices_text），旧会话无 slices 时
    保留整份 text 直接注入，不强制迁移。
    """
    default = {
        "text": "",
        "summarized_ts": None,
        "last_round": 0,
        "slice_rounds": config.SUMMARY_SLICE_ROUNDS,
        "slices": [],     # 持久化切片实体（落袋切片）
        "active": "text", # 注入模式（前端摘要卡分页选择）：text=整份文本 / slices=切片拼接
        "slice_auto": True,   # 切片自动总结开关：开启后每打一个锚点自动总结前一个切片
        "enabled": True,  # ③ 剧情摘要开关（关闭时保留内容但停止注入/自动/手动总结）
        "auto": True,     # ③ 自动总结开关（总开关下一级；关闭时后台不自动总结，仅手动）
    }
    if not isinstance(s, dict):
        return default
    out = dict(default)
    out["text"] = str(s.get("text") or "").strip()
    out["enabled"] = bool(s.get("enabled", True))
    out["auto"] = bool(s.get("auto", True))
    out["slice_auto"] = bool(s.get("slice_auto", True))
    out["slices"] = _parse_summary_slices(s.get("slices"))
    act = str(s.get("active") or "text").strip()
    out["active"] = act if act in ("text", "slices") else "text"
    try:
        out["summarized_ts"] = float(s["summarized_ts"]) if s.get("summarized_ts") is not None else None
    except (TypeError, ValueError):
        out["summarized_ts"] = None
    try:
        out["last_round"] = int(s.get("last_round") or 0)
    except (TypeError, ValueError):
        out["last_round"] = 0
    # 切片宽度：>=1，上限给个安全值
    try:
        v = int(s.get("slice_rounds"))
        out["slice_rounds"] = max(1, min(v, 200))
    except (TypeError, ValueError):
        pass  # 保留默认
    return out


def _new_id():
    return uuid.uuid4().hex


def _hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120000)
    return salt, digest.hex()


def _migrate_legacy_sessions(username):
    if not os.path.isdir(SESSIONS_DIR):
        return
    with _connect() as conn:
        for name in os.listdir(SESSIONS_DIR):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(SESSIONS_DIR, name), "r", encoding="utf-8") as f:
                    session = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            conn.execute(
                """INSERT OR IGNORE INTO sessions
                   (id, username, title, created_at, updated_at, model, params)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (session["id"], username, session.get("title", "未命名"),
                 session.get("created_at", time.time()),
                 session.get("updated_at", session.get("created_at", time.time())),
                 session.get("model"), json.dumps(session.get("params"), ensure_ascii=False)),
            )
            # P4：消息行级写入 messages 表（旧列已废弃）
            for m in session.get("messages") or []:
                if isinstance(m, dict):
                    _insert_message_row(conn, session["id"], username, m)


def create_user(username, password):
    salt, password_hash = _hash_password(password)
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO users(username, salt, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (username, salt, password_hash, time.time()),
            )
    except sqlite3.IntegrityError:
        return False
    _migrate_legacy_sessions(username)
    return True


def verify_user(username, password):
    with _connect() as conn:
        user = conn.execute("SELECT salt, password_hash FROM users WHERE username = ?", (username,)).fetchone()
    if not user:
        return False
    _, password_hash = _hash_password(password, user["salt"])
    return hmac.compare_digest(password_hash, user["password_hash"])


def change_password(username, current_password, new_password):
    with _connect() as conn:
        user = conn.execute("SELECT salt, password_hash FROM users WHERE username = ?", (username,)).fetchone()
        if not user:
            return False
        _, current_hash = _hash_password(current_password, user["salt"])
        if not hmac.compare_digest(current_hash, user["password_hash"]):
            return False
        salt, password_hash = _hash_password(new_password)
        conn.execute("UPDATE users SET salt = ?, password_hash = ? WHERE username = ?", (salt, password_hash, username))
    return True


def _parse_vm(raw):
    """规范化向量记忆设置。

    结构：{"enabled": bool, "model": str|None, "recent_n": int, "top_k": int|None}。
    top_k 语义：None=未设置（用默认 config.VECTOR_MEMORY_TOP_K）；0=不限制（自动召回）；>0=固定 N 条。
    接收 dict 或 JSON 字符串或 None；缺省为新会话默认（关闭、无模型、N=默认）。
    """
    default = {
        "enabled": False,
        "model": None,
        "recent_n": config.VECTOR_MEMORY_RECENT_N,
        "top_k": None,
        "slice_recall": False,  # 切片召回：按切片摘要相似度召回整段切片对话
    }
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return default
    if not isinstance(raw, dict):
        return default
    recent_n = default["recent_n"]
    if "recent_n" in raw and raw.get("recent_n") is not None and raw.get("recent_n") != "":
        try:
            recent_n = max(0, min(int(raw["recent_n"]), 1000))
        except (TypeError, ValueError):
            recent_n = default["recent_n"]
    # top_k：必须区分「未提供(None)」与「设置为0(不限制)」。
    # 只有当键存在且值非空时才算显式设置。
    top_k = default["top_k"]
    if "top_k" in raw and raw.get("top_k") is not None and raw.get("top_k") != "":
        try:
            top_k = max(0, min(int(raw["top_k"]), 500))
        except (TypeError, ValueError):
            top_k = default["top_k"]
    return {
        "enabled": bool(raw.get("enabled", False)),
        "model": raw.get("model") or None,
        "recent_n": recent_n,
        "top_k": top_k,
        "slice_recall": bool(raw.get("slice_recall", False)),
    }


def _row_to_msg(r):
    """messages 表行 → 消息 dict（只还原存在的字段，保持与旧 JSON 结构一致）。"""
    m = {"role": r["role"], "content": r["content"], "ts": r["ts"]}
    if r["reasoning"] is not None:
        m["reasoning"] = r["reasoning"]
    if r["files"]:
        try:
            m["files"] = json.loads(r["files"])
        except (TypeError, ValueError):
            pass
    if r["interrupted"]:
        m["interrupted"] = True
    if r["scene_break"]:
        m["scene_break"] = True
    if r["scene_label"] is not None:
        m["scene_label"] = r["scene_label"]
    if r["annotation"] is not None:
        m["annotation"] = r["annotation"]
    return m


def _insert_message_row(conn, sid, username, m):
    """向 messages 表插入一条消息；ts 非数值时回退 0。"""
    try:
        ts = float(m.get("ts"))
    except (TypeError, ValueError):
        ts = 0.0
    files = m.get("files")
    conn.execute(
        "INSERT INTO messages(session_id, username, role, content, ts, "
        "reasoning, files, interrupted, scene_break, scene_label, annotation) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sid, username, str(m.get("role") or ""), m.get("content") or "", ts,
            m.get("reasoning"),
            json.dumps(files, ensure_ascii=False) if files is not None else None,
            1 if m.get("interrupted") else 0,
            1 if m.get("scene_break") else 0,
            m.get("scene_label"),
            m.get("annotation"),
        ),
    )


def _row_to_session(row, conn=None):
    """sessions 行 → 会话 dict；messages 从 messages 表按 id 升序读取（conn 必传）。"""
    memory = _parse_memory(row["memory"] if "memory" in row.keys() else None)
    messages = []
    if conn is not None:
        messages = [
            _row_to_msg(r)
            for r in conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id", (row["id"],)
            )
        ]
    return {"id": row["id"], "title": row["title"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "model": row["model"],
            "params": json.loads(row["params"]) if row["params"] else None,
            "vm": memory["vector"],   # 兼容旧前端：vm 从 memory.vector 权威读取
            "memory": memory,
            "messages": messages}


def create_session(username, title=None, model=None, params=None, vm=None, memory=None):
    now = time.time()
    mem = _parse_memory(memory) if memory is not None else _default_memory()
    if vm is not None:
        mem["vector"] = _parse_vm(vm)
    session = {"id": _new_id(), "title": title or "会话 " + time.strftime("%m-%d %H:%M"),
               "created_at": now, "updated_at": now, "model": model, "params": params,
               "vm": mem["vector"], "memory": mem, "messages": []}
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sessions(id, username, title, created_at, updated_at, model, params, vm, memory) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session["id"], username, session["title"], now, now, model,
             json.dumps(params, ensure_ascii=False),
             json.dumps(session["vm"], ensure_ascii=False),
             json.dumps(mem, ensure_ascii=False)),
        )
    return session


def get_session(username, sid):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ? AND username = ?", (sid, username)).fetchone()
        if row is None:
            return None
        return _row_to_session(row, conn)


def get_session_messages(username, sid, limit=30, before=None):
    """分页读取会话消息（长对话按需加载更早的消息）。

    before 为全局消息下标锚点：返回下标 < before 的最早 limit 条；
    不传时返回最新的 limit 条。消息列表按时间升序（最早在前）。
    返回 dict（含分页信息）或 None（会话不存在）。
    返回的 messages 每项带全局 index（会话内 user/assistant 消息从 0 起的下标，
    与 delete_message 的删除下标一致），供前端渲染与后续加载定位。
    P3：直接对 messages 表分页（COUNT + LIMIT/OFFSET），不再读整坨。
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ? AND username = ?", (sid, username)
        ).fetchone()
        if row is None:
            return None
        total = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ? AND role != 'system'",
            (sid,),
        ).fetchone()[0]
        if before is None:
            end = total
        else:
            try:
                end = max(0, min(int(before), total))
            except (TypeError, ValueError):
                end = total
        start = max(0, end - limit)
        page = conn.execute(
            "SELECT * FROM messages WHERE session_id = ? AND role != 'system' "
            "ORDER BY id LIMIT ? OFFSET ?",
            (sid, end - start, start),
        ).fetchall()
        session = _row_to_session(row)  # 仅取元信息（messages 留空，避免整坨读）
        # 全会话场景锚点下标（升序）：前端据此对「第 N 场」默认命名按位置排序，
        # 不能只依赖当前分页——长对话下早分页的锚点不在本页会导致编号全部从 1 重数。
        anchor_indices = []
        pos = 0
        for r in conn.execute(
            "SELECT role, scene_break FROM messages WHERE session_id = ? AND role != 'system' "
            "ORDER BY id",
            (sid,),
        ):
            if r["scene_break"] and r["role"] == "user":
                anchor_indices.append(pos)
            pos += 1
    return {
        "id": session["id"],
        "title": session["title"],
        "created_at": session["created_at"],
        "updated_at": session["updated_at"],
        "model": session["model"],
        "params": session["params"],
        "vm": session.get("vm", _parse_vm(None)),
        "messages": [{"index": start + i, **_row_to_msg(r)} for i, r in enumerate(page)],
        "scene_anchors": anchor_indices,
        "total": total,
        "has_more": start > 0,
    }


def _save_session(username, session):
    """保存会话的非消息字段（标题 / 模型 / 参数 / 记忆）。

    P3 起消息改由 messages 表行级维护，此处**不再写 messages 列**（旧列作为
    只读快照冻结在 P1 状态），从而消除整坨读写。
    """
    memory = _parse_memory(session.get("memory"))
    vm = memory["vector"]
    with _connect() as conn:
        conn.execute(
            "UPDATE sessions SET title = ?, updated_at = ?, model = ?, params = ?, vm = ?, memory = ? WHERE id = ? AND username = ?",
            (session["title"], session.get("updated_at", time.time()), session.get("model"),
             json.dumps(session.get("params"), ensure_ascii=False),
             json.dumps(vm, ensure_ascii=False),
             json.dumps(memory, ensure_ascii=False),
             session["id"], username),
        )


def set_auto_title(username, sid, title):
    title = title.strip()[:10]
    if not title:
        return None
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE sessions SET title = ?, auto_title_generated = 1 WHERE id = ? AND username = ? AND auto_title_generated = 0",
            (title, sid, username),
        )
        if cursor.rowcount != 1:
            return None
    return get_session(username, sid)


def auto_title_status(username, sid):
    with _connect() as conn:
        row = conn.execute(
            "SELECT title, auto_title_generated FROM sessions WHERE id = ? AND username = ?",
            (sid, username),
        ).fetchone()
    if not row:
        return None
    return {"title": row["title"], "generated": bool(row["auto_title_generated"])}


@_locked
def update_session_config(username, sid, model=None, params=None, vm=None, memory=None):
    session = get_session(username, sid)
    if session is None:
        return None
    if model is not None:
        session["model"] = model
    if params is not None:
        session["params"] = params
    if vm is not None:
        session["memory"]["vector"] = _parse_vm(vm)
    if memory is not None:
        session["memory"] = _parse_memory(memory)
    _save_session(username, session)
    return session


# ------------------------- 四层记忆读写（memory 列） -------------------------

def get_session_memory(username, sid):
    """读取会话的四层记忆结构；会话不存在返回 None。"""
    session = get_session(username, sid)
    return session["memory"] if session else None


@_locked
def save_session_memory(username, sid, memory):
    """整体覆盖会话的四层记忆（规范化后落库），返回新会话或 None。"""
    session = get_session(username, sid)
    if session is None:
        return None
    session["memory"] = _parse_memory(memory)
    _save_session(username, session)
    return session


@_locked
def set_session_cards_item(username, sid, op, card_id=None, name=None, content=None):
    """人物卡列表条目操作（多角色，一角色一卡）。

    op:
      "add"      新建卡（name/content 可为空），返回带 id 的新 memory
      "update"   按 card_id 更新 name / content（传哪个更新哪个）
      "delete"   按 card_id 删除（允许删除后没有任何主角色卡）
      "set-main" 按 card_id 设为主角色卡；card_id 为空时取消所有主角色标记（置空）
    返回新 memory 或 None（会话不存在 / update|delete|set-main 找不到卡返回原 memory）。
    """
    memory = get_session_memory(username, sid)
    if memory is None:
        return None
    cards = memory.get("cards") or []
    if op == "add":
        cards = cards + [{
            "id": uuid.uuid4().hex,
            "name": (name or "").strip(),
            "content": (content or "").strip(),
            "main": False,  # 新建卡不默认主角色，由用户手动点星标指定（允许置空）
            "updated_at": time.time(),
        }]
        memory["cards"] = cards
    elif op == "update":
        for c in cards:
            if c.get("id") == card_id:
                if name is not None:
                    c["name"] = name.strip()
                if content is not None:
                    c["content"] = content.strip()
                c["updated_at"] = time.time()
                memory["cards"] = cards
                break
        else:
            return memory
    elif op == "delete":
        # 删除后允许没有任何主角色卡，不做自动接任
        memory["cards"] = [c for c in cards if c.get("id") != card_id]
    elif op == "set-main":
        # card_id 为空：取消所有主角色标记（允许主角色置空）；否则切换到指定卡
        if card_id:
            found = False
            for c in cards:
                if c.get("id") == card_id:
                    c["main"] = True
                    found = True
                else:
                    c["main"] = False
            if not found:
                return memory
        else:
            for c in cards:
                c["main"] = False
        memory["cards"] = cards
    else:
        return memory
    save_session_memory(username, sid, memory)
    return memory


@_locked
def set_session_worlds_item(username, sid, op, world_id=None, name=None, content=None):
    """世界卡列表条目操作（多张，一卡一世界观设定）。

    op:
      "add"    新建卡（name/content 可为空），返回带 id 的新 memory
      "update" 按 world_id 更新 name / content（传哪个更新哪个）
      "delete" 按 world_id 删除
    返回新 memory 或 None（会话不存在 / update|delete 找不到卡返回原 memory）。
    """
    memory = get_session_memory(username, sid)
    if memory is None:
        return None
    worlds = memory.get("worlds") or []
    if op == "add":
        worlds = worlds + [{
            "id": uuid.uuid4().hex,
            "name": (name or "").strip(),
            "content": (content or "").strip(),
            "updated_at": time.time(),
        }]
        memory["worlds"] = worlds
    elif op == "update":
        for w in worlds:
            if w.get("id") == world_id:
                if name is not None:
                    w["name"] = name.strip()
                if content is not None:
                    w["content"] = content.strip()
                w["updated_at"] = time.time()
                memory["worlds"] = worlds
                break
        else:
            return memory
    elif op == "delete":
        memory["worlds"] = [w for w in worlds if w.get("id") != world_id]
    else:
        return memory
    save_session_memory(username, sid, memory)
    return memory


@_locked
def set_session_facts_config(username, sid, slice_rounds=None, max_per_slice=None):
    """设置动态关键事实的抽取配置。

    slice_rounds：切片宽度（轮），None=不更新；max_per_slice：每片最多抽取条数
    （0 = 不限制），None=不更新。返回新 memory 或 None（会话不存在 / 无有效字段）。
    """
    memory = get_session_memory(username, sid)
    if memory is None:
        return None
    updated = False
    if slice_rounds is not None:
        try:
            memory["facts_slice_rounds"] = max(1, min(200, int(slice_rounds)))
            updated = True
        except (TypeError, ValueError):
            pass
    if max_per_slice is not None:
        try:
            memory["facts_max_per_slice"] = max(0, min(50, int(max_per_slice)))
            updated = True
        except (TypeError, ValueError):
            pass
    if not updated:
        return memory
    save_session_memory(username, sid, memory)
    return memory


@_locked
def set_session_facts(username, sid, facts, last_round=None):
    """整体覆盖动态关键事实列表。facts 为 [{"text", ...}]，规范化后落库。

    last_round：事实抽取进度（已处理到的轮次）；None 不更新。
    返回新 memory 或 None。
    """
    memory = get_session_memory(username, sid)
    if memory is None:
        return None
    memory["facts"] = _parse_memory({"facts": facts, "summary": None, "vector": memory["vector"]})["facts"]
    if last_round is not None:
        try:
            memory["facts_last_round"] = max(0, int(last_round))
        except (TypeError, ValueError):
            pass
    save_session_memory(username, sid, memory)
    return memory


@_locked
def set_session_summary(username, sid, text, last_round=None):
    """写入剧情摘要整份文本。text 为空则清除（含切片实体）。last_round 为已总结到的轮次。

    保留已有的用户配置与切片实体（text 由切片派生时，直接写 text 属于临时覆盖，
    下次切片保存会重新派生覆盖）。返回新 memory 或 None。"""
    memory = get_session_memory(username, sid)
    if memory is None:
        return None
    text = (text or "").strip()
    old = memory.get("summary") or {}
    if text:
        s = _parse_summary(old)
        s["text"] = text
        s["summarized_ts"] = time.time()
        if last_round is not None:
            s["last_round"] = max(0, int(last_round))
        memory["summary"] = s
    else:
        memory["summary"] = None
    save_session_memory(username, sid, memory)
    return memory


@_locked
def set_session_summary_slices(username, sid, slices, last_round=None):
    """全量覆盖持久化切片实体列表（切片线专用，不改动整份 text）。

    slices 为 [{id, name, text, start_round, end_round, edited, ts}]（非法条目跳过）；
    last_round 为已处理到的轮次（None 不更新；整份 text 与 last_round 归文本线维护）。
    返回新 memory 或 None。
    """
    memory = get_session_memory(username, sid)
    if memory is None:
        return None
    old = memory.get("summary") or {}
    s = _parse_summary(old)
    s["slices"] = _parse_summary_slices(slices)
    s["summarized_ts"] = time.time()
    if last_round is not None:
        s["last_round"] = max(0, int(last_round))
    memory["summary"] = s
    save_session_memory(username, sid, memory)
    return memory


@_locked
def update_summary_slice(username, sid, slice_id, name=None, text=None):
    """更新单个切片实体的名称或摘要（面板编辑入口）。

    任一字段更新后该片标记 edited=True（不改动整份 text，文本归文本线维护）。
    name/text 传 None 表示不更新该项。返回 {"memory", "slice"} 或 None（会话/切片不存在）。
    """
    memory = get_session_memory(username, sid)
    if memory is None:
        return None
    s = _parse_summary(memory.get("summary") or {})
    target = next((x for x in s["slices"] if x["id"] == str(slice_id)), None)
    if target is None:
        return None
    if name is not None:
        target["name"] = str(name).strip()[:50]
        target["edited"] = True
    if text is not None:
        target["text"] = str(text).strip()
        target["edited"] = True
    target["ts"] = time.time()
    memory["summary"] = s
    save_session_memory(username, sid, memory)
    return {"memory": memory, "slice": target}


@_locked
def rerun_summary_slice(username, sid, slice_id, text):
    """用新总结替换指定切片的摘要（手动「重新总结」入口）。

    与 update_summary_slice 的区别：text 来自 LLM 重新总结而非用户手编，
    覆盖后 edited 重置为 False（重建/继承时不视为用户编辑保留）。
    返回 {"memory", "slice"} 或 None（会话/切片不存在）。
    """
    memory = get_session_memory(username, sid)
    if memory is None:
        return None
    s = _parse_summary(memory.get("summary") or {})
    target = next((x for x in s["slices"] if x["id"] == str(slice_id)), None)
    if target is None:
        return None
    target["text"] = str(text).strip()
    target["edited"] = False
    target["ts"] = time.time()
    memory["summary"] = s
    save_session_memory(username, sid, memory)
    return {"memory": memory, "slice": target}


@_locked
def set_session_summary_config(username, sid, slice_rounds=None, enabled=None, active=None, slice_auto=None):
    """设置会话剧情摘要的触发参数（slice_rounds 切片宽度 / enabled 开关 /
    active 注入模式 / slice_auto 切片自动总结开关）。

    只更新传入的参数，其余保留。active 取 "text"（整份文本）或 "slices"（切片拼接）。
    返回新 memory 或 None（会话不存在）。
    """
    memory = get_session_memory(username, sid)
    if memory is None:
        return None
    s = memory.get("summary") or {}
    changed = False
    if slice_rounds is not None:
        try:
            s = dict(s)
            s["slice_rounds"] = max(1, min(int(slice_rounds), 200))
            changed = True
        except (TypeError, ValueError):
            pass
    if active is not None:
        act = str(active).strip()
        if act in ("text", "slices"):
            s = dict(s)
            s["active"] = act
            changed = True
    if slice_auto is not None:
        s = dict(s)
        s["slice_auto"] = bool(slice_auto)
        changed = True
    if enabled is not None:
        s = dict(s)
        s["enabled"] = bool(enabled)
        changed = True
    if changed:
        memory["summary"] = _parse_summary(s)
        save_session_memory(username, sid, memory)
    return memory


@_locked
def set_session_memory_switches(username, sid, facts_enabled=None, summary_enabled=None,
                                vector_enabled=None, reset_values=False, facts_auto=None,
                                summary_auto=None):
    """批量设置记忆卡片开关（2/3/4 层），供「一键配置 / 关闭智能总结」与单卡开关调用。

    facts_auto：② 动态事实的自动总结开关（总开关下一级），None=不更新。
    summary_auto：③ 剧情摘要的自动总结开关（总开关下一级），None=不更新。
    reset_values=True（一键配置）时把数值恢复默认：最近 N 轮=10、
    摘要切片/自动间隔、向量 TopK 与 recent_n 恢复默认。
    只更新传入的字段，其余保留。返回新 memory 或 None（会话不存在）。
    """
    memory = get_session_memory(username, sid)
    if memory is None:
        return None
    if facts_enabled is not None:
        memory["facts_enabled"] = bool(facts_enabled)
    if facts_auto is not None:
        memory["facts_auto"] = bool(facts_auto)
    if summary_enabled is not None:
        s = dict(memory.get("summary") or {})
        s["enabled"] = bool(summary_enabled)
        memory["summary"] = _parse_summary(s)
    if summary_auto is not None:
        s = dict(memory.get("summary") or {})
        s["auto"] = bool(summary_auto)
        memory["summary"] = _parse_summary(s)
    if vector_enabled is not None:
        vec = dict(memory.get("vector") or _parse_vm(None))
        vec["enabled"] = bool(vector_enabled)
        memory["vector"] = _parse_vm(vec)
    if reset_values:
        # 最近 N 轮恢复为 10（用户指定的一键配置值）
        memory["recent_n"] = config.VECTOR_MEMORY_RECENT_N
        memory["facts_slice_rounds"] = config.FACT_SLICE_ROUNDS  # 事实切片宽度恢复默认
        memory["facts_max_per_slice"] = config.FACT_MAX_PER_SLICE  # 每片抽取上限恢复默认
        s = dict(memory.get("summary") or {})
        s["slice_rounds"] = config.SUMMARY_SLICE_ROUNDS
        memory["summary"] = _parse_summary(s)
        vec = dict(memory.get("vector") or _parse_vm(None))
        vec["top_k"] = None          # 恢复默认召回
        vec["recent_n"] = config.VECTOR_MEMORY_RECENT_N
        memory["vector"] = _parse_vm(vec)
    save_session_memory(username, sid, memory)
    return memory


# 哨兵：区分「参数未提供（不更新）」与「显式传入 None（清除/恢复默认）」
_UNSET = object()


@_locked
def set_session_vector_config(username, sid, top_k=_UNSET, recent_n=None, enabled=None, model=None, slice_recall=_UNSET):
    """设置会话的向量记忆配置。

    只更新传入的参数，其余保留。返回新 memory 或 None（会话不存在）。
    top_k 语义：_UNSET=不更新；None=清除（恢复默认，存储为 null）；0=不限制（自动召回）；>0=固定 N 条。
    slice_recall 语义：_UNSET=不更新；其余按 bool 保存（切片召回开关）。
    """
    memory = get_session_memory(username, sid)
    if memory is None:
        return None
    vec = dict(memory.get("vector") or _parse_vm(None))
    changed = False
    if top_k is not _UNSET:
        if top_k is None:
            vec["top_k"] = None
        else:
            try:
                vec["top_k"] = max(0, min(int(top_k), 500))
            except (TypeError, ValueError):
                pass
        changed = True
    if recent_n is not None:
        try:
            vec["recent_n"] = max(0, min(int(recent_n), 1000))
            changed = True
        except (TypeError, ValueError):
            pass
    if enabled is not None:
        vec["enabled"] = bool(enabled)
        changed = True
    if model is not None:
        vec["model"] = str(model).strip() or None
        changed = True
    if slice_recall is not _UNSET:
        vec["slice_recall"] = bool(slice_recall)
        changed = True
    if changed:
        memory["vector"] = _parse_vm(vec)
        memory["recent_n"] = _parse_recent_n(memory["vector"].get("recent_n"), default=memory.get("recent_n", config.VECTOR_MEMORY_RECENT_N))
        save_session_memory(username, sid, memory)
    return memory


@_locked
def set_session_recent_n(username, sid, recent_n):
    """设置独立的最近 N 轮上下文策略（0=全量模式，保留全部上下文）。"""
    memory = get_session_memory(username, sid)
    if memory is None:
        return None
    if recent_n is None or recent_n == "":
        memory["recent_n"] = config.VECTOR_MEMORY_RECENT_N
    else:
        try:
            memory["recent_n"] = max(0, min(int(recent_n), 1000))
        except (TypeError, ValueError):
            memory["recent_n"] = config.VECTOR_MEMORY_RECENT_N
    save_session_memory(username, sid, memory)
    return memory


@_locked
def clear_session_memory_layer(username, sid, layer):
    """清空某一层记忆（cards / facts / summary），vector 为配置不清。返回新 memory 或 None。"""
    memory = get_session_memory(username, sid)
    if memory is None:
        return None
    if layer in ("cards", "facts", "summary"):
        memory[layer] = []
        if layer == "facts":
            memory["facts_last_round"] = 0  # 清空事实同时重置抽取进度
        save_session_memory(username, sid, memory)
    return memory


def import_session(username, data):
    """按导出 JSON 的结构新建一个完整会话（标题、模型、参数、时间、消息）。
    返回新会话；vm 缺省为关闭，与前端导入按钮对应导出文件结构。
    """
    now = time.time()
    try:
        created_at = float(data.get("created_at") or now)
    except (TypeError, ValueError):
        created_at = now
    try:
        updated_at = float(data.get("updated_at") or created_at or now)
    except (TypeError, ValueError):
        updated_at = created_at
    title = data.get("title") or "会话 " + time.strftime("%m-%d %H:%M")
    messages = [m for m in (data.get("messages") or []) if m.get("role") in ("user", "assistant")]
    mem = _parse_memory(data.get("memory"))
    if data.get("vm") is not None:
        mem["vector"] = _parse_vm(data.get("vm"))
    session = {
        "id": _new_id(),
        "title": title,
        "created_at": created_at,
        "updated_at": updated_at,
        "model": data.get("model"),
        "params": data.get("params"),
        "vm": mem["vector"],
        "memory": mem,
        "messages": messages,
    }
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sessions(id, username, title, created_at, updated_at, model, params, vm, memory) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session["id"], username, title, created_at, updated_at, session["model"],
             json.dumps(session["params"], ensure_ascii=False),
             json.dumps(session["vm"], ensure_ascii=False),
             json.dumps(mem, ensure_ascii=False)),
        )
        # 消息行级写入 messages 表
        for m in messages:
            _insert_message_row(conn, session["id"], username, m)
    return session


def list_sessions(username):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT s.id, s.title, s.model, s.created_at, s.updated_at, "
            "(SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count "
            "FROM sessions s WHERE s.username = ? ORDER BY s.updated_at DESC",
            (username,),
        ).fetchall()
        return [{"id": row["id"], "title": row["title"], "model": row["model"],
                 "created_at": row["created_at"], "updated_at": row["updated_at"],
                 "message_count": row["message_count"]} for row in rows]


def delete_session(username, sid):
    """删除会话及其全部消息行。

    P3：messages 表未定义外键，故**显式清理**该会话的消息行，避免留下孤儿数据
    （会话已删但 messages 仍有行）。两个删除在同一连接内，异常整体回滚。
    """
    with _connect() as conn:
        conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM sessions WHERE id = ? AND username = ?", (sid, username))


@_locked
def rename_session(username, sid, title):
    session = get_session(username, sid)
    if session is None:
        return None
    session["title"] = title
    _save_session(username, session)
    return session


def _is_user_configured(session):
    # 仅按推理参数判断是否「已配置」；向量记忆设置（vm）不参与，
    # 因此只有向量设置、无消息的空会话仍会被 cleanup_empty_sessions 清理。
    params = session.get("params") or {}
    return any(params.get(key) != default for key, default in config.DEFAULT_PARAMS.items())


def cleanup_empty_sessions(username, exclude_id=None):
    removed = []
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM sessions WHERE username = ?", (username,)).fetchall()
        for row in rows:
            session = _row_to_session(row, conn)
            if session["id"] != exclude_id and not session["messages"] and not _is_user_configured(session):
                conn.execute("DELETE FROM messages WHERE session_id = ?", (session["id"],))
                conn.execute("DELETE FROM sessions WHERE id = ? AND username = ?", (session["id"], username))
                removed.append(session["id"])
    return removed


@_locked
def delete_message(username, sid, index):
    """删除第 index 条非 system 消息（P3 行级：OFFSET 定位 → DELETE 一行）。"""
    with _connect() as conn:
        row = conn.execute("SELECT id FROM sessions WHERE id = ? AND username = ?", (sid, username)).fetchone()
        if row is None:
            return None
        try:
            index = int(index)
        except (TypeError, ValueError):
            return None
        if index < 0:
            return None
        target = conn.execute(
            "SELECT id FROM messages WHERE session_id = ? AND role != 'system' "
            "ORDER BY id LIMIT 1 OFFSET ?",
            (sid, index),
        ).fetchone()
        if target is None:
            return None
        conn.execute("DELETE FROM messages WHERE id = ?", (target["id"],))
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ? AND username = ?",
                     (time.time(), sid, username))
    return get_session(username, sid)


@_locked
def set_message_scene_break(username, sid, index, scene_break, scene_label=None):
    """设置 / 清除指定 user 消息上的场景锚点（手动分场标记）。

    index 为 user/assistant 消息下标（不含 system，与 delete_message 一致）。
    scene_break=True 打锚点（scene_label 可空，空则前端显示默认「第 N 场」）；
    scene_break=False 清除锚点。锚点只允许打在 user 消息（轮首）上。
    返回更新后的消息 dict；会话不存在 / 下标越界 / 非 user 消息返回 None。
    P3：行级（OFFSET 定位 → UPDATE 一行）。
    """
    with _connect() as conn:
        row = conn.execute("SELECT id FROM sessions WHERE id = ? AND username = ?", (sid, username)).fetchone()
        if row is None:
            return None
        try:
            index = int(index)
        except (TypeError, ValueError):
            return None
        if index < 0:
            return None
        target = conn.execute(
            "SELECT * FROM messages WHERE session_id = ? AND role != 'system' "
            "ORDER BY id LIMIT 1 OFFSET ?",
            (sid, index),
        ).fetchone()
        if target is None or target["role"] != "user":
            return None
        if scene_break:
            label = str(scene_label or "").strip()
            conn.execute(
                "UPDATE messages SET scene_break = 1, scene_label = ? WHERE id = ?",
                (label[:50] if label else None, target["id"]),
            )
        else:
            conn.execute(
                "UPDATE messages SET scene_break = 0, scene_label = NULL WHERE id = ?",
                (target["id"],),
            )
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ? AND username = ?",
                     (time.time(), sid, username))
        updated = conn.execute("SELECT * FROM messages WHERE id = ?", (target["id"],)).fetchone()
    return _row_to_msg(updated)


@_locked
def ensure_first_anchor(username, sid):
    """确保会话存在「开场锚点」：首个锚点打在中间时，自动给首条 user 消息补一个
    场景锚点，使「首轮 → 第一个用户锚点」整段成一个切片（开场段不按切片轮数细分）。

    - 会话没有任何锚点：不处理（无锚点会话保持按切片轮数细分）
    - 首条 user 消息已带锚点（用户自己打的或此前已补）：不处理
    - 有锚点但首条 user 消息未带：补 scene_break=True（label 留空，前端显示默认「第 1 场」）

    返回 {"added": bool, "index": int}：added=True 表示本次补了锚点，index 为
    首条 user 消息下标（非 system，与前端 msgIndex 一致，供就地插入分割线）；
    会话不存在返回 None。
    """
    with _connect() as conn:
        row = conn.execute("SELECT id FROM sessions WHERE id = ? AND username = ?", (sid, username)).fetchone()
        if row is None:
            return None
        users = conn.execute(
            "SELECT * FROM messages WHERE session_id = ? AND role = 'user' ORDER BY id", (sid,)
        ).fetchall()
        if not users:
            return {"added": False, "index": -1}
        first = users[0]
        # 首条 user 消息在「非 system」列表中的下标 = 其前所有非 system 行数
        index = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ? AND role != 'system' AND id < ?",
            (sid, first["id"]),
        ).fetchone()[0]
        has_anchor = any(u["scene_break"] for u in users)
        if not has_anchor or first["scene_break"]:
            return {"added": False, "index": index}
        conn.execute(
            "UPDATE messages SET scene_break = 1, scene_label = NULL WHERE id = ?", (first["id"],)
        )
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ? AND username = ?",
                     (time.time(), sid, username))
        return {"added": True, "index": index}


@_locked
def get_scene_anchor_indices(username, sid):
    """返回会话内全部场景锚点（user 消息 scene_break=1）的全局下标（升序）。

    下标为「非 system 消息」中的位置（与前端 msgIndex / delete_message 一致），
    供前端对「第 N 场」默认命名按全局位置排序。会话不存在返回 None。
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM sessions WHERE id = ? AND username = ?", (sid, username)
        ).fetchone()
        if row is None:
            return None
        out = []
        pos = 0
        for r in conn.execute(
            "SELECT role, scene_break FROM messages WHERE session_id = ? AND role != 'system' "
            "ORDER BY id",
            (sid,),
        ):
            if r["scene_break"] and r["role"] == "user":
                out.append(pos)
            pos += 1
        return out


@_locked
def rename_scene_anchor(username, sid, round_no, label):
    """按轮次更新锚点消息的场景标签（切片面板改名 → 分割线同步）。

    round_no 为对话轮次（1-based）：定位第 round_no 轮的 user 消息；
    该消息带锚点时更新 scene_label（空串删除标签），未带锚点则不动（返回 ok 但 changed=False）。
    返回 {"ok": bool, "changed": bool}；会话不存在 / 轮次越界返回 None。
    """
    with _connect() as conn:
        row = conn.execute("SELECT id FROM sessions WHERE id = ? AND username = ?", (sid, username)).fetchone()
        if row is None:
            return None
        try:
            round_no = int(round_no)
        except (TypeError, ValueError):
            return None
        if round_no < 1:
            return None
        target = conn.execute(
            "SELECT * FROM messages WHERE session_id = ? AND role = 'user' "
            "ORDER BY id LIMIT 1 OFFSET ?",
            (sid, round_no - 1),
        ).fetchone()
        if target is None:
            return None
        if not target["scene_break"]:
            return {"ok": True, "changed": False}
        label = str(label or "").strip()
        conn.execute(
            "UPDATE messages SET scene_label = ? WHERE id = ?",
            (label[:50] if label else None, target["id"]),
        )
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ? AND username = ?",
                     (time.time(), sid, username))
        return {"ok": True, "changed": True}


@_locked
def append_message(username, sid, role, content, reasoning=None, files=None, interrupted=None, annotation=None):
    session = get_session(username, sid) or create_session(username)
    message = {"role": role, "content": content, "ts": time.time()}
    if reasoning is not None:
        message["reasoning"] = reasoning
    if annotation is not None:
        message["annotation"] = annotation
    if files is not None:
        # 保存 binary 标志：前端据此区分「文本文件(拼内容)」与「二进制附件(base64)」，
        # 否则另一设备/重试时从后端恢复会把二进制文件误当作文本拼接。
        message["files"] = [{"name": f.get("name", ""), "content": f.get("content", ""),
                             "binary": bool(f.get("binary", False))} for f in files]
    if interrupted is not None:
        message["interrupted"] = bool(interrupted)
    with _connect() as conn:
        _insert_message_row(conn, session["id"], username, message)
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ? AND username = ?",
                     (time.time(), session["id"], username))
    return get_session(username, session["id"])


def _read_presets():
    if not os.path.exists(PRESETS_FILE):
        return []
    with open(PRESETS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_presets(presets):
    _ensure_dirs()
    with open(PRESETS_FILE, "w", encoding="utf-8") as f:
        json.dump(presets, f, ensure_ascii=False, indent=2)


def list_presets():
    return _read_presets()


def save_preset(name, params):
    presets = _read_presets()
    for preset in presets:
        if preset["name"] == name:
            preset["params"] = params
            _write_presets(presets)
            return preset
    preset = {"name": name, "params": params}
    presets.append(preset)
    _write_presets(presets)
    return preset


def delete_preset(name):
    _write_presets([preset for preset in _read_presets() if preset["name"] != name])


# 所有辅助函数定义完毕后再初始化数据库（含旧 vm -> memory.vector 迁移）
_init_db()