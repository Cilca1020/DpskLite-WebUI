"""SQLite 数据存储：账号、会话与参数预设。"""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import uuid

import config

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
PRESETS_FILE = os.path.join(DATA_DIR, "presets.json")
DB_FILE = os.path.join(DATA_DIR, "app.db")


def _ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)


def _connect():
    _ensure_dirs()
    # timeout 让写操作在锁冲突时等待（而非立刻抛 database is locked），
    # 缓解记忆维护后台线程与请求读库/写库的并发冲突。
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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
                messages TEXT NOT NULL,
                auto_title_generated INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user_updated
                ON sessions(username, updated_at DESC);
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
        "max_slice_rounds": config.SUMMARY_MAX_SEGMENT_ROUNDS,  # 锚点段保留上限（轮）
        "slices": [],     # 持久化切片实体（落袋切片）
        "enabled": True,  # ③ 剧情摘要开关（关闭时保留内容但停止注入/自动/手动总结）
        "auto": True,     # ③ 自动总结开关（总开关下一级；关闭时后台不自动总结，仅手动）
    }
    if not isinstance(s, dict):
        return default
    out = dict(default)
    out["text"] = str(s.get("text") or "").strip()
    out["enabled"] = bool(s.get("enabled", True))
    out["auto"] = bool(s.get("auto", True))
    out["slices"] = _parse_summary_slices(s.get("slices"))
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
    # 锚点段保留上限：>=1，上限给个安全值
    try:
        v = int(s.get("max_slice_rounds"))
        out["max_slice_rounds"] = max(1, min(v, 200))
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
                   (id, username, title, created_at, updated_at, model, params, messages)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (session["id"], username, session.get("title", "未命名"),
                 session.get("created_at", time.time()),
                 session.get("updated_at", session.get("created_at", time.time())),
                 session.get("model"), json.dumps(session.get("params"), ensure_ascii=False),
                 json.dumps(session.get("messages", []), ensure_ascii=False)),
            )


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
    }


def _row_to_session(row):
    memory = _parse_memory(row["memory"] if "memory" in row.keys() else None)
    return {"id": row["id"], "title": row["title"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "model": row["model"],
            "params": json.loads(row["params"]) if row["params"] else None,
            "vm": memory["vector"],   # 兼容旧前端：vm 从 memory.vector 权威读取
            "memory": memory,
            "messages": json.loads(row["messages"])}


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
            "INSERT INTO sessions(id, username, title, created_at, updated_at, model, params, messages, vm, memory) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session["id"], username, session["title"], now, now, model,
             json.dumps(params, ensure_ascii=False), json.dumps([]),
             json.dumps(session["vm"], ensure_ascii=False),
             json.dumps(mem, ensure_ascii=False)),
        )
    return session


def get_session(username, sid):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ? AND username = ?", (sid, username)).fetchone()
    return _row_to_session(row) if row else None


def get_session_messages(username, sid, limit=30, before=None):
    """分页读取会话消息（长对话按需加载更早的消息）。

    before 为全局消息下标锚点：返回下标 < before 的最早 limit 条；
    不传时返回最新的 limit 条。消息列表按时间升序（最早在前）。
    返回 dict（含分页信息）或 None（会话不存在）。
    返回的 messages 每项带全局 index（会话内 user/assistant 消息从 0 起的下标，
    与 delete_message 的删除下标一致），供前端渲染与后续加载定位。
    """
    session = get_session(username, sid)
    if session is None:
        return None
    chat = [m for m in session["messages"] if m.get("role") != "system"]
    total = len(chat)
    if before is None:
        end = total
    else:
        try:
            end = max(0, min(int(before), total))
        except (TypeError, ValueError):
            end = total
    start = max(0, end - limit)
    page = chat[start:end]
    return {
        "id": session["id"],
        "title": session["title"],
        "created_at": session["created_at"],
        "updated_at": session["updated_at"],
        "model": session["model"],
        "params": session["params"],
        "vm": session.get("vm", _parse_vm(None)),
        "messages": [{"index": start + i, **m} for i, m in enumerate(page)],
        "total": total,
        "has_more": start > 0,
    }


def _save_session(username, session):
    memory = _parse_memory(session.get("memory"))
    vm = memory["vector"]
    with _connect() as conn:
        conn.execute(
            "UPDATE sessions SET title = ?, updated_at = ?, model = ?, params = ?, messages = ?, vm = ?, memory = ? WHERE id = ? AND username = ?",
            (session["title"], session.get("updated_at", time.time()), session.get("model"),
             json.dumps(session.get("params"), ensure_ascii=False),
             json.dumps(session.get("messages", []), ensure_ascii=False),
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


def save_session_memory(username, sid, memory):
    """整体覆盖会话的四层记忆（规范化后落库），返回新会话或 None。"""
    session = get_session(username, sid)
    if session is None:
        return None
    session["memory"] = _parse_memory(memory)
    _save_session(username, session)
    return session


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


def set_session_summary_config(username, sid, slice_rounds=None, enabled=None, max_slice_rounds=None):
    """设置会话剧情摘要的触发参数（slice_rounds 切片宽度 / enabled 开关 / max_slice_rounds 锚点段上限）。

    只更新传入的参数，其余保留。返回新 memory 或 None（会话不存在）。
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
    if max_slice_rounds is not None:
        try:
            s = dict(s)
            s["max_slice_rounds"] = max(1, min(int(max_slice_rounds), 200))
            changed = True
        except (TypeError, ValueError):
            pass
    if enabled is not None:
        s = dict(s)
        s["enabled"] = bool(enabled)
        changed = True
    if changed:
        memory["summary"] = _parse_summary(s)
        save_session_memory(username, sid, memory)
    return memory


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
        s["max_slice_rounds"] = config.SUMMARY_MAX_SEGMENT_ROUNDS
        memory["summary"] = _parse_summary(s)
        vec = dict(memory.get("vector") or _parse_vm(None))
        vec["top_k"] = None          # 恢复默认召回
        vec["recent_n"] = config.VECTOR_MEMORY_RECENT_N
        memory["vector"] = _parse_vm(vec)
    save_session_memory(username, sid, memory)
    return memory


# 哨兵：区分「参数未提供（不更新）」与「显式传入 None（清除/恢复默认）」
_UNSET = object()


def set_session_vector_config(username, sid, top_k=_UNSET, recent_n=None, enabled=None, model=None):
    """设置会话的向量记忆配置。

    只更新传入的参数，其余保留。返回新 memory 或 None（会话不存在）。
    top_k 语义：_UNSET=不更新；None=清除（恢复默认，存储为 null）；0=不限制（自动召回）；>0=固定 N 条。
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
    if changed:
        memory["vector"] = _parse_vm(vec)
        memory["recent_n"] = _parse_recent_n(memory["vector"].get("recent_n"), default=memory.get("recent_n", config.VECTOR_MEMORY_RECENT_N))
        save_session_memory(username, sid, memory)
    return memory


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
            "INSERT INTO sessions(id, username, title, created_at, updated_at, model, params, messages, vm, memory) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session["id"], username, title, created_at, updated_at, session["model"],
             json.dumps(session["params"], ensure_ascii=False),
             json.dumps(messages, ensure_ascii=False),
             json.dumps(session["vm"], ensure_ascii=False),
             json.dumps(mem, ensure_ascii=False)),
        )
    return session


def list_sessions(username):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, title, model, created_at, updated_at, messages FROM sessions WHERE username = ? ORDER BY updated_at DESC",
            (username,),
        ).fetchall()
    return [{"id": row["id"], "title": row["title"], "model": row["model"],
             "created_at": row["created_at"], "updated_at": row["updated_at"],
             "message_count": len(json.loads(row["messages"]))} for row in rows]


def delete_session(username, sid):
    with _connect() as conn:
        conn.execute("DELETE FROM sessions WHERE id = ? AND username = ?", (sid, username))


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
            session = _row_to_session(row)
            if session["id"] != exclude_id and not session["messages"] and not _is_user_configured(session):
                conn.execute("DELETE FROM sessions WHERE id = ? AND username = ?", (session["id"], username))
                removed.append(session["id"])
    return removed


def delete_message(username, sid, index):
    session = get_session(username, sid)
    if session is None:
        return None
    indices = [i for i, message in enumerate(session["messages"]) if message.get("role") != "system"]
    if index < 0 or index >= len(indices):
        return None
    del session["messages"][indices[index]]
    session["updated_at"] = time.time()
    _save_session(username, session)
    return session


def set_message_scene_break(username, sid, index, scene_break, scene_label=None):
    """设置 / 清除指定 user 消息上的场景锚点（手动分场标记）。

    index 为 user/assistant 消息下标（不含 system，与 delete_message 一致）。
    scene_break=True 打锚点（scene_label 可空，空则前端显示默认「第 N 场」）；
    scene_break=False 清除锚点。锚点只允许打在 user 消息（轮首）上。
    返回更新后的消息 dict；会话不存在 / 下标越界 / 非 user 消息返回 None。
    """
    session = get_session(username, sid)
    if session is None:
        return None
    indices = [i for i, message in enumerate(session["messages"]) if message.get("role") != "system"]
    if index < 0 or index >= len(indices):
        return None
    message = session["messages"][indices[index]]
    if message.get("role") != "user":
        return None
    if scene_break:
        message["scene_break"] = True
        label = str(scene_label or "").strip()
        if label:
            message["scene_label"] = label[:50]
        else:
            message.pop("scene_label", None)
    else:
        message.pop("scene_break", None)
        message.pop("scene_label", None)
    session["updated_at"] = time.time()
    _save_session(username, session)
    return message


def rename_scene_anchor(username, sid, round_no, label):
    """按轮次更新锚点消息的场景标签（切片面板改名 → 分割线同步）。

    round_no 为对话轮次（1-based）：定位第 round_no 轮的 user 消息；
    该消息带锚点时更新 scene_label（空串删除标签），未带锚点则不动（返回 ok 但 changed=False）。
    返回 {"ok": bool, "changed": bool}；会话不存在 / 轮次越界返回 None。
    """
    session = get_session(username, sid)
    if session is None:
        return None
    seen = 0
    for message in session["messages"]:
        if message.get("role") == "system":
            continue
        if message.get("role") == "user":
            seen += 1
            if seen == round_no:
                if not message.get("scene_break"):
                    return {"ok": True, "changed": False}
                label = str(label or "").strip()
                if label:
                    message["scene_label"] = label[:50]
                else:
                    message.pop("scene_label", None)
                session["updated_at"] = time.time()
                _save_session(username, session)
                return {"ok": True, "changed": True}
    return None


def append_message(username, sid, role, content, reasoning=None, files=None, interrupted=None):
    session = get_session(username, sid) or create_session(username)
    message = {"role": role, "content": content, "ts": time.time()}
    if reasoning is not None:
        message["reasoning"] = reasoning
    if files is not None:
        # 保存 binary 标志：前端据此区分「文本文件(拼内容)」与「二进制附件(base64)」，
        # 否则另一设备/重试时从后端恢复会把二进制文件误当作文本拼接。
        message["files"] = [{"name": f.get("name", ""), "content": f.get("content", ""),
                             "binary": bool(f.get("binary", False))} for f in files]
    if interrupted is not None:
        message["interrupted"] = bool(interrupted)
    session["messages"].append(message)
    session["updated_at"] = time.time()
    _save_session(username, session)
    return session


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