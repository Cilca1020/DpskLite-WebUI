"""长对话向量记忆模块（独立模块，不依赖前端）。

功能：
- 使用本地 sentence-transformers 加载 Qwen/Qwen3-Embedding-0.6B（models 目录内，相对路径），
  不依赖任何外部 API / Ollama。
- 对话消息向量化后存入 SQLite（data/vector_memory.db）。
- 检索时在本地用 numpy 计算余弦相似度，不调用任何远程服务。
- 拼接策略：开启后，对话历史 = 最近 recent_n 条 + 向量检索 top_k 条（自动去重）。

设备回退：
- 优先 CUDA -> MPS -> CPU。
- 若 torch 完全无法工作（导入失败 / 模型加载失败），抛出 VectorMemoryError 并提示，
  不会静默降级继续（由调用方决定如何处理）。

用法示例：
    from vector_memory import VectorMemory
    vm = VectorMemory()                      # 首次使用时才加载模型（懒加载）
    vm.sync_session(session_id, messages)    # 增量写入向量库（内容哈希去重，幂等）
    history = vm.build_history(messages, session_id=session_id)  # 拼接历史

后端接入：
    vm = vector_memory.get_instance(...)     # 全局单例，供 Flask 路由复用
    vm.sync_session(sid, payload_messages)   # 每次请求增量索引
    history = vm.build_history(payload_messages, session_id=sid, query=最新提问)
"""

import hashlib
import os
import sqlite3
import time
import uuid
from glob import glob

import numpy as np

# 模型在 models/ 目录内的相对路径（HF Hub 缓存目录结构：
# models/models--Qwen--Qwen3-Embedding-0.6B/snapshots/<commit>/）
MODEL_DIR = os.path.join("models", "models--Qwen--Qwen3-Embedding-0.6B")

# 向量库 SQLite 文件（与主存储共用 data/ 目录）
DEFAULT_DB = os.path.join("data", "vector_memory.db")

# 默认拼接参数
DEFAULT_RECENT_N = 10      # 最近 n 条完整消息（user/assistant）
DEFAULT_TOP_K = 5          # 向量检索返回条数
DEFAULT_MIN_SCORE = 0.3    # 低于该相似度的检索结果直接丢弃
DEFAULT_MAX_SEQ_LEN = 8192  # 编码最大序列长度（模型原生支持 32k，取 8k 平衡内存/速度）
SLICE_TOP_K = 2            # 切片召回：最多召回的切片数（每片含多轮完整对话，2 个足够）


class VectorMemoryError(RuntimeError):
    """向量记忆模块的致命错误：torch/模型不可用时应抛出并终止。"""


_instance = None


def get_instance(**kwargs):
    """获取全局单例（模型懒加载，首次实际使用时才加载）。

    db_path / model_dir / device 与缓存实例不一致时自动重建，便于前端切换向量化模型。
    kwargs 同 VectorMemory.__init__。
    """
    global _instance
    if _instance is None:
        _instance = VectorMemory(**kwargs)
        return _instance
    attrs = {"db_path": "db_path", "model_dir": "model_dir", "device": "device_name"}
    for key, attr in attrs.items():
        if kwargs.get(key) is not None and getattr(_instance, attr) != kwargs[key]:
            _instance.close()
            _instance = VectorMemory(**kwargs)
            break
    return _instance


def list_available_models(models_dir="models"):
    """列出 models/ 目录下可用的 sentence-transformers 模型，返回 [{id, label}]。

    支持 HF Hub 缓存结构（models/models--org--name/snapshots/<commit>/）与直接平铺目录。
    """
    if not os.path.isdir(models_dir):
        return []
    found = []
    for name in sorted(os.listdir(models_dir)):
        path = os.path.join(models_dir, name)
        if not os.path.isdir(path):
            continue
        valid = os.path.exists(os.path.join(path, "config_sentence_transformers.json"))
        if not valid:
            valid = any(
                os.path.exists(os.path.join(snap, "config_sentence_transformers.json"))
                for snap in glob(os.path.join(path, "snapshots", "*"))
            )
        if valid:
            label = name.replace("models--", "", 1).replace("--", "/", 1)
            found.append({"id": name, "label": label})
    return found


def _chunk_hash(session_id, role, content):
    """消息内容指纹：用于增量同步时判断某条消息是否已入库（幂等）。"""
    return hashlib.sha256(f"{session_id}\n{role}\n{content}".encode("utf-8")).hexdigest()


def _slice_summary_hash(session_id, slice_id, name, summary):
    """切片内容指纹：场景名或摘要变化时重新编码入库。"""
    return hashlib.sha256(
        f"{session_id}\n{slice_id}\n{name}\n{summary}".encode("utf-8")
    ).hexdigest()


def _pick_device(device="auto"):
    """确定推理设备：auto -> CUDA -> MPS -> CPU。torch 不可用则抛错。"""
    if device and device != "auto":
        return device
    try:
        import torch
    except Exception as e:  # noqa: BLE001
        raise VectorMemoryError(
            f"无法导入 torch（{e}）。请先安装 PyTorch：\n"
            "  pip install torch\n"
            "如需 CUDA 加速请按 https://pytorch.org/get-started/locally/ 安装对应版本。"
        ) from e
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _find_model_path(model_dir=MODEL_DIR):
    """在 HF Hub 缓存目录结构下定位模型目录，找不到则抛出带下载指引的错误。"""
    candidates = []
    # HF Hub 缓存结构：models/.../snapshots/<commit>/
    for snap in sorted(glob(os.path.join(model_dir, "snapshots", "*"))):
        if os.path.isdir(snap) and os.path.exists(
            os.path.join(snap, "config_sentence_transformers.json")
        ):
            candidates.append(snap)
    # 直接平铺的目录结构
    if os.path.exists(os.path.join(model_dir, "config_sentence_transformers.json")):
        candidates.append(model_dir)
    if not candidates:
        raise VectorMemoryError(
            f"在 {model_dir} 下找不到 Qwen3-Embedding-0.6B 模型目录。\n"
            "请先下载模型（二选一）：\n"
            "  huggingface-cli download Qwen/Qwen3-Embedding-0.6B "
            "--local-dir models/models--Qwen--Qwen3-Embedding-0.6B\n"
            "或 git clone：\n"
            "  git clone https://huggingface.co/Qwen/Qwen3-Embedding-0.6B "
            "models/models--Qwen--Qwen3-Embedding-0.6B\n"
            "然后重试。"
        )
    return candidates[0]


class VectorMemory:
    """长对话向量记忆：向量化 -> SQLite 存储 -> 本地余弦检索 -> 历史拼接。

    __init__ 只做初始化（默认不加载模型），首次 encode / add / search 时才加载模型，
    因此不装 torch 时导入本模块不会报错。
    """

    def __init__(
        self,
        db_path=DEFAULT_DB,
        model_dir=MODEL_DIR,
        device="auto",
        recent_n=DEFAULT_RECENT_N,
        top_k=DEFAULT_TOP_K,
        min_score=DEFAULT_MIN_SCORE,
        max_seq_len=DEFAULT_MAX_SEQ_LEN,
        lazy=True,
    ):
        self.db_path = db_path
        self.model_dir = model_dir
        self.device_name = device
        self.recent_n = recent_n
        self.top_k = top_k
        self.min_score = min_score
        self.max_seq_len = max_seq_len
        self._model = None
        self._device = None
        self._dtype = None
        if not lazy:
            self._load_model()
        self._init_db()

    # ------------------------- 基础设施 -------------------------

    def _connect(self):
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        # timeout 缓解并发写（记忆维护后台线程与请求 reconcile 同时写）的锁冲突
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_chunks (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    dim INTEGER NOT NULL,
                    ts REAL NOT NULL,
                    hash TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_memory_session ON memory_chunks(session_id);
                -- 剧情切片向量：摘要向量化，检索命中后展开整段切片对话
                CREATE TABLE IF NOT EXISTS slice_chunks (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    slice_id TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    summary_hash TEXT NOT NULL DEFAULT '',
                    embedding BLOB NOT NULL,
                    dim INTEGER NOT NULL,
                    ts REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_slice_session ON slice_chunks(session_id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_slice_session_slice
                    ON slice_chunks(session_id, slice_id);
                """
            )
            # 旧库迁移：补充 hash 列；旧数据无 hash 无法去重，清空重建
            columns = {r[1] for r in conn.execute("PRAGMA table_info(memory_chunks)")}
            if "hash" not in columns:
                conn.execute(
                    "ALTER TABLE memory_chunks ADD COLUMN hash TEXT NOT NULL DEFAULT ''"
                )
            conn.execute("DELETE FROM memory_chunks WHERE hash = ''")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_session_hash "
                "ON memory_chunks(session_id, hash)"
            )

    @property
    def device(self):
        if self._device is None:
            self._device = _pick_device(self.device_name)
        return self._device

    @property
    def model(self):
        return self._load_model()

    def _load_model(self):
        """加载 embedding 模型（懒加载 + 设备/精度回退）。

        精度策略：CUDA/MPS 用 bfloat16（Qwen3 原生格式，省显存），CPU 用 float32。
        模型加载失败说明 torch/模型本身不可用，直接抛 VectorMemoryError，不降级继续。
        """
        if self._model is not None:
            return self._model
        try:
            import torch
        except Exception as e:  # noqa: BLE001
            raise VectorMemoryError(
                f"无法导入 torch（{e}）。请先安装 PyTorch：pip install torch"
            ) from e

        device = self.device
        model_path = _find_model_path(self.model_dir)
        if self._dtype is None:
            self._dtype = torch.bfloat16 if device != "cpu" else torch.float32

        try:
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(
                model_path,
                device=device,
                model_kwargs={"torch_dtype": self._dtype},
                processor_kwargs={"padding_side": "left"},
            )
            model.max_seq_length = self.max_seq_len
        except Exception as e:  # noqa: BLE001
            raise VectorMemoryError(
                f"加载 embedding 模型失败（device={device}）：{e}\n"
                "请检查 torch 是否与显卡/驱动匹配（可运行 nvidia-smi 确认 CUDA 状态），"
                "或强制使用 CPU：VectorMemory(device='cpu')。"
            ) from e

        self._model = model
        return model

    def close(self):
        """释放模型（可选调用）。"""
        if self._model is not None:
            try:
                self._model = None
                import gc

                gc.collect()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------- 向量化 -------------------------

    def encode(self, texts, query=False, batch_size=16):
        """向量化文本。query=True 时套用模型的 query prompt（提升检索效果）。"""
        model = self._load_model()
        texts = [str(t) for t in texts if str(t).strip()]
        if not texts:
            return np.zeros((0,), dtype=np.float32)
        kwargs = dict(
            batch_size=batch_size,
            show_progress_bar=False,
            normalize_embeddings=True,
        )
        if query:
            kwargs["prompt_name"] = "query"
        vecs = model.encode(texts, **kwargs)
        return np.asarray(vecs, dtype=np.float32)

    # ------------------------- 写入向量库 -------------------------

    def add(self, session_id, role, content, ts=None):
        """单条消息入库。返回 chunk id；空内容返回 None。"""
        content = (content or "").strip()
        if not content:
            return None
        h = _chunk_hash(session_id, role, content)
        vec = self.encode([content], query=False)[0]
        cid = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO memory_chunks(id, session_id, role, content, embedding, dim, ts, hash) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (cid, session_id, role, content, vec.tobytes(), int(vec.size),
                 ts if ts is not None else time.time(), h),
            )
        return cid

    def sync_session(self, session_id, messages):
        """增量写入会话消息：只编码/入库本会话尚未出现过的消息（内容哈希去重），幂等。

        messages 为 [{role, content, ...}]。返回新入库条数。
        """
        items = [
            {"role": m.get("role", "user"), "content": (m.get("content") or "").strip()}
            for m in messages
            if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()
        ]
        if not items:
            return 0
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT hash FROM memory_chunks WHERE session_id = ?", (session_id,)
            ).fetchall()
        existing = {r["hash"] for r in rows}
        # 过滤时动态维护已见集合：同一条消息在同一批次里出现多次也只入库一次，
        # 避免撞 (session_id, hash) 唯一索引。
        seen = set(existing)
        new_items = []
        for it in items:
            h = _chunk_hash(session_id, it["role"], it["content"])
            if h in seen:
                continue
            seen.add(h)
            new_items.append(it)
        if not new_items:
            return 0
        vecs = self.encode([it["content"] for it in new_items], query=False)
        now = time.time()
        with self._connect() as conn:
            for it, vec in zip(new_items, vecs):
                conn.execute(
                    "INSERT INTO memory_chunks(id, session_id, role, content, embedding, dim, ts, hash) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, session_id, it["role"], it["content"],
                     np.asarray(vec, dtype=np.float32).tobytes(), int(vec.size), now,
                     _chunk_hash(session_id, it["role"], it["content"])),
                )
        return len(new_items)

    def reconcile_session(self, session_id, messages):
        """以 messages（应为存储中的完整会话 + 本次请求消息）为准双向同步向量库：
        补入新增消息，并删除已不在 messages 中的 chunk（如被删除/重试掉的消息）。

        这样编辑、重试、删除会话消息后，旧内容不会被检索进新上下文。
        返回新增条数；删除操作不需要加载模型。
        """
        added = self.sync_session(session_id, messages)
        items = [
            {"role": m.get("role", "user"), "content": (m.get("content") or "").strip()}
            for m in messages
            if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()
        ]
        hashes = {_chunk_hash(session_id, it["role"], it["content"]) for it in items}
        with self._connect() as conn:
            if hashes:
                placeholders = ",".join("?" * len(hashes))
                conn.execute(
                    f"DELETE FROM memory_chunks WHERE session_id = ? AND hash NOT IN ({placeholders})",
                    [session_id, *sorted(hashes)],
                )
            else:
                conn.execute(
                    "DELETE FROM memory_chunks WHERE session_id = ?", (session_id,)
                )
        return added

    def add_messages(self, session_id, messages):
        """批量入库（等价于 sync_session，保留兼容旧接口）。返回实际入库条数。"""
        return self.sync_session(session_id, messages)

    def clear_session(self, session_id):
        """清空某个会话的向量记忆（含切片向量）。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM memory_chunks WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM slice_chunks WHERE session_id = ?", (session_id,))

    def count(self, session_id=None):
        """向量库条数统计。"""
        with self._connect() as conn:
            if session_id is None:
                row = conn.execute("SELECT COUNT(*) AS n FROM memory_chunks").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM memory_chunks WHERE session_id = ?", (session_id,)
                ).fetchone()
        return int(row["n"])

    # ------------------------- 切片向量 -------------------------

    def sync_slices(self, session_id, slices):
        """把剧情切片（summary.slices 实体）的摘要同步进切片向量库，幂等。

        slices 为 [{id, name, text, ...}]。与库内现有切片按 slice_id 对齐：
        - 已消失的切片（重建/合并）删除向量；
        - name 或摘要变化的切片重新编码入库；
        - 其余不动（零编码开销）。
        返回 {added, updated, removed}。
        """
        items = []
        for sl in slices or []:
            text = (sl.get("text") or "").strip()
            if not text:
                continue
            items.append({
                "slice_id": str(sl.get("id") or ""),
                "name": str(sl.get("name") or ""),
                "summary": text,
            })
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT slice_id, summary_hash FROM slice_chunks WHERE session_id = ?",
                (session_id,),
            ).fetchall()
        by_id = {r["slice_id"]: r["summary_hash"] for r in rows}
        stats = {"added": 0, "updated": 0, "removed": 0}
        keep_ids = {it["slice_id"] for it in items if it["slice_id"]}
        # 删除已消失的切片向量
        stale = [sid_ for sid_ in by_id if sid_ not in keep_ids]
        if stale:
            with self._connect() as conn:
                conn.executemany(
                    "DELETE FROM slice_chunks WHERE session_id = ? AND slice_id = ?",
                    [(session_id, sid_) for sid_ in stale],
                )
            stats["removed"] = len(stale)
        # 找出新增 / 内容变化的切片，批量编码
        pending = []
        for it in items:
            if not it["slice_id"]:
                continue
            h = _slice_summary_hash(session_id, it["slice_id"], it["name"], it["summary"])
            if by_id.get(it["slice_id"]) == h:
                continue
            it["summary_hash"] = h
            pending.append(it)
        if pending:
            vecs = self.encode([it["summary"] for it in pending], query=False)
            now = time.time()
            with self._connect() as conn:
                for it, vec in zip(pending, vecs):
                    conn.execute(
                        "INSERT INTO slice_chunks(id, session_id, slice_id, name, summary, "
                        "summary_hash, embedding, dim, ts) VALUES (?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(session_id, slice_id) DO UPDATE SET "
                        "name=excluded.name, summary=excluded.summary, "
                        "summary_hash=excluded.summary_hash, embedding=excluded.embedding, "
                        "dim=excluded.dim, ts=excluded.ts",
                        (uuid.uuid4().hex, session_id, it["slice_id"], it["name"], it["summary"],
                         it["summary_hash"], np.asarray(vec, dtype=np.float32).tobytes(),
                         int(vec.size), now),
                    )
            for it in pending:
                stats["updated" if it["slice_id"] in by_id else "added"] += 1
        return stats

    # ------------------------- 检索 -------------------------

    def search(self, query_text, session_id=None, top_k=None, exclude_contents=()):
        """本地余弦相似度检索，返回按分数降序的命中列表。

        top_k 语义：
          - None：用实例默认 self.top_k
          - 0：不限制数量，全部按相似度阈值召回（自动召回）
          - >0：最多返回 N 条
        exclude_contents: 需要排除的内容集合（如最近窗口内的消息，避免重复）。
        """
        if not (query_text or "").strip():
            return []
        if top_k is None:
            top_k = self.top_k
        qvec = self.encode([query_text], query=True)[0]
        exclude = set(exclude_contents or ())
        sql = (
            "SELECT id, session_id, role, content, embedding, ts FROM memory_chunks"
            + (" WHERE session_id = ?" if session_id else "")
            + " ORDER BY ts ASC"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, (session_id,) if session_id else ()).fetchall()

        qnorm = np.linalg.norm(qvec)
        results = []
        for row in rows:
            content = row["content"]
            if content in exclude:
                continue
            vec = np.frombuffer(row["embedding"], dtype=np.float32)
            if vec.size == 0:
                continue
            denom = qnorm * np.linalg.norm(vec) + 1e-12
            score = float(np.dot(qvec, vec) / denom)
            results.append(
                {
                    "id": row["id"],
                    "session_id": row["session_id"],
                    "role": row["role"],
                    "content": content,
                    "ts": row["ts"],
                    "score": score,
                }
            )
        results.sort(key=lambda r: r["score"], reverse=True)
        # 按内容去重（保留相似度最高的一条），避免同一消息重复进入上下文
        seen = set()
        dedup = []
        for r in results:
            if r["content"] in seen:
                continue
            seen.add(r["content"])
            dedup.append(r)
        # top_k=0 表示不限制数量（自动召回）；>0 时返回前 N 条
        return dedup if top_k == 0 else dedup[:top_k]

    def search_slices(self, query_text, session_id=None, top_k=SLICE_TOP_K, min_score=None):
        """切片摘要向量检索：返回相似度最高的切片 [{slice_id, name, summary, score}]。"""
        if not (query_text or "").strip():
            return []
        qvec = self.encode([query_text], query=True)[0]
        sql = (
            "SELECT slice_id, name, summary, embedding FROM slice_chunks"
            + (" WHERE session_id = ?" if session_id else "")
        )
        with self._connect() as conn:
            rows = conn.execute(sql, (session_id,) if session_id else ()).fetchall()
        qnorm = np.linalg.norm(qvec)
        results = []
        for row in rows:
            vec = np.frombuffer(row["embedding"], dtype=np.float32)
            if vec.size == 0:
                continue
            denom = qnorm * np.linalg.norm(vec) + 1e-12
            score = float(np.dot(qvec, vec) / denom)
            results.append({
                "slice_id": row["slice_id"],
                "name": row["name"],
                "summary": row["summary"],
                "score": score,
            })
        results.sort(key=lambda r: r["score"], reverse=True)
        min_score = self.min_score if min_score is None else min_score
        results = [r for r in results if r["score"] >= min_score]
        return results[:top_k] if top_k else results

    # ------------------------- 历史拼接 -------------------------

    def build_history(
        self,
        messages,
        session_id=None,
        recent_n=None,
        top_k=None,
        query=None,
        min_score=None,
        recent_rounds=None,
        full_messages=None,
        max_chars=None,
        slice_recall=False,
        slices=None,
    ):
        """按“最近 N 轮对话 + 更早的向量检索结果（按轮次）”拼接对话历史。

        对话轮次 ≤ N 时最近窗口包含全部消息（全部添加）；
        max_chars 非空且拼接结果超出时，从最早的消息开始砍，直到字符数达标。

        messages:       本次请求的消息列表（含 role/content），用于最近窗口与输出。
        full_messages:  可选，存储中的完整会话消息列表。检索命中会按“轮次”
                       （一轮 = 一次提问 + 一次回答）展开，需要完整的会话结构，
                       因此传它可让前端分页窗口之外的早期轮次也被完整召回。
        session_id:     向量检索范围（缺省为 None 即全部会话）。
        recent_n:       最近窗口的消息条数（与 recent_rounds 二选一，recent_rounds 优先）。
        recent_rounds:  最近窗口的对话轮数（一轮 = 一次提问 + 一次回答），
                       不得超过会话总轮数；内部换算成消息条数。
        query:          检索用查询，缺省取最后一条 user 消息内容。
        min_score:      相似度阈值，低于该值丢弃（None 用默认）。

        返回可直接喂给 LLM 的 [{role, content}, ...]：
        检索命中的完整轮次（按时间升序）在前，最近窗口在后；
        命中内容若已在最近窗口内会自动去重。
        """
        chat = [
            {"role": m.get("role"), "content": (m.get("content") or "").strip()}
            for m in messages
            if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()
        ]
        if not chat:
            return []
        recent_n = recent_n or self.recent_n
        if recent_rounds is not None:
            user_idx = [i for i, m in enumerate(chat) if m["role"] == "user"]
            rounds_total = len(user_idx)
            if rounds_total == 0:
                recent_n = len(chat)
            else:
                nr = max(1, min(int(recent_rounds), rounds_total))
                # 最近 nr 轮 = 从倒数第 nr 条 user 消息开始的所有消息
                start = user_idx[rounds_total - nr] if nr < rounds_total else 0
                recent_n = len(chat) - start
        # top_k: None -> 用默认 self.top_k；0 -> 不限制（自动召回）；>0 -> 固定 N 条
        if top_k is None:
            top_k = self.top_k
        min_score = self.min_score if min_score is None else min_score

        recent = chat[-recent_n:]
        qtext = (query or "").strip() or chat[-1]["content"]
        recent_contents = {m["content"] for m in recent}
        hits = self.search(
            qtext, session_id=session_id, top_k=top_k, exclude_contents=recent_contents
        )
        hits = [h for h in hits if h["score"] >= min_score]
        # 轮次展开的基准：优先用完整会话（可能超出本次请求的分页窗口），
        # 否则退化为本次消息列表。
        if full_messages is not None:
            base = [
                {"role": m.get("role"), "content": (m.get("content") or "").strip()}
                for m in full_messages
                if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()
            ]
        else:
            base = chat
        memory = self._expand_rounds(base, hits, recent_contents)
        # 切片召回：摘要向量命中 -> 展开该切片覆盖的完整轮次对话，与单轮召回合并
        slice_pairs = []
        if slice_recall and slices:
            try:
                slice_hits = self.search_slices(qtext, session_id=session_id)
            except Exception:  # noqa: BLE001
                slice_hits = []
            s_by_id = {str(s.get("id") or ""): s for s in slices}
            for sh in slice_hits:
                sl = s_by_id.get(str(sh["slice_id"]))
                if not sl:
                    continue
                slice_pairs += self._slice_round_pairs(
                    base, sl.get("start_round"), sl.get("end_round"), recent_contents
                )
        # 合并去重（同一下标只保留一次），按会话时间顺序输出；来源分别标记便于日志区分
        merged = {}
        for i, m in memory:
            if i not in merged:
                merged[i] = (m, "vector")
        for i, m in slice_pairs:
            if i not in merged:
                merged[i] = (m, "vector_slice")  # 切片召回优先级低于单轮（先到先得）
        vector_msgs = [
            dict(m, _src=src) for _, (m, src) in sorted(merged.items())
        ]
        recent_msgs = [{**m, "_src": "recent"} for m in recent]
        result = vector_msgs + recent_msgs
        # token 过多时砍掉最早的部分（至少保留最后一条，即当前提问）
        if max_chars and max_chars > 0:
            while len(result) > 1 and sum(len(m["content"]) for m in result) > max_chars:
                result.pop(0)
        return result

    def _expand_rounds(self, chat, hits, recent_contents):
        """把命中的单条消息展开为完整轮次（一轮 = 一条提问 + 紧随其后的回答）。

        返回 [(base下标, 消息)]，按时间升序；已在最近窗口内的消息被剔除。
        """
        if not hits:
            return []
        # 命中内容 -> 在 chat 中的下标（同内容取首个）
        used = set()
        chosen = []
        for h in hits:
            for i, m in enumerate(chat):
                if m["content"] == h["content"] and i not in used:
                    used.add(i)
                    chosen.append(i)
                    break
        # 展开：找到每条命中所属轮次的 user 消息及其后连续 assistant 回答
        round_idxs = set()
        for i in chosen:
            k = i
            while k > 0 and chat[k]["role"] != "user":
                k -= 1
            if chat[k]["role"] != "user":
                round_idxs.add(i)
                continue
            round_idxs.add(k)
            j = k + 1
            while j < len(chat) and chat[j]["role"] == "assistant":
                round_idxs.add(j)
                j += 1
        return [
            (i, {"role": chat[i]["role"], "content": chat[i]["content"]})
            for i in sorted(round_idxs)
            if chat[i]["content"] not in recent_contents
        ]

    def _slice_round_pairs(self, base, start_round, end_round, recent_contents):
        """按切片的轮次范围 [start_round, end_round] 展开完整对话（含范围外紧邻回答）。

        轮次编号与非 system 消息中第 N 条 user 对齐（与切片实体一致）。
        返回 [(base下标, 消息)]；已在最近窗口内的消息被剔除。
        """
        try:
            lo_r = max(1, int(start_round or 0))
            hi_r = max(1, int(end_round or 0))
        except (TypeError, ValueError):
            return []
        if hi_r < lo_r:
            return []
        user_idx = [i for i, m in enumerate(base) if m["role"] == "user"]
        if not user_idx:
            return []
        total = len(user_idx)
        if lo_r > total:
            return []
        hi_r = min(hi_r, total)
        lo = user_idx[lo_r - 1]                                  # 首轮 user 消息下标
        hi_end = user_idx[hi_r] if hi_r < total else len(base)   # 末轮的下一个 user 前
        return [
            (i, {"role": base[i]["role"], "content": base[i]["content"]})
            for i in range(lo, hi_end)
            if base[i]["content"] not in recent_contents
        ]
