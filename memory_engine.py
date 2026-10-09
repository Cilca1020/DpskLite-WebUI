"""四层记忆引擎：核心设定卡 / 动态关键事实 / 剧情摘要 / 向量记忆。

针对「角色扮演、剧情动态推进、关键设定不能忘」的场景，按优先级从高到低
无条件或按需注入上下文：

    [system prompt]
    [① 核心设定]     世界卡（世界观/背景，先注入）+ 人物卡（角色设定），均静态常驻
    [② 动态关键事实] LLM 切片抽取（自动+手动），本地合并去重，随剧情更新（改名/恋爱等）
    [③ 剧情摘要]     LLM 切片分段滚动总结，n（切片宽度）可调，自动+手动触发；
                     支持用户手动场景锚点（分场）：锚点为硬边界，锚点段整段总结
                     （不超上限时），摘要行带【场景标签】前缀
    [④ 向量检索片段] 按需召回对话细节（复用 vector_memory 机制）
    [最近 N 轮对话]

与 vector_memory 的关系：本模块持有 vector_memory 实例，负责第④层，
并把四层按顺序拼成一份可直接喂给 LLM 的消息列表。

设计原则：
- 数据层读写统一走 storage 模块（sessions.memory 列 + character_cards 表）。
- 记忆维护（事实抽取 / 切片总结 / 汇总）统一用固定轻量模型
  config.MEMORY_MAINTENANCE_MODEL（默认 deepseek-chat），参考 auto-title 做法，
  稳定且便宜；调用方（路由层）传入 api_key。
- 所有 LLM 调用失败都不阻塞主对话：记录日志并优雅降级（跳过该层），
  绝不因记忆维护失败而让用户的提问失败。
"""

import json
import time

import difflib

import config
import llm_client
import storage
import vector_memory

# 各层在拼接消息时的 role
# ① 核心设定（世界卡 / 人物卡）/ ② 动态关键事实 / ③ 剧情摘要：用 system role
# （背景性内容，优先级高于对话）；④ 向量检索片段沿用其原始 role（user/assistant）。
_WORLD_ROLE = "system"
_CARD_ROLE = "system"
_FACTS_ROLE = "system"
_SUMMARY_ROLE = "system"


# ------------------------- 四层读取 -------------------------

def _get_memory(username, sid):
    """读取会话四层记忆（规范化后的 dict）；会话不存在返回 None。"""
    mem = storage.get_session_memory(username, sid)
    return mem if mem is not None else None


def _vector_enabled(memory, data, sid):
    """判定第④层向量记忆是否启用。

    优先级：请求参数 vector_memory（兼容旧前端开关）> 会话 memory.vector.enabled。
    需带 session_id 才真正生效。
    """
    enabled = bool((memory or {}).get("vector", {}).get("enabled"))
    if "vector_memory" in (data or {}):
        enabled = bool((data or {}).get("vector_memory"))
    return enabled and bool(sid)


def _context_n(memory, data):
    """返回最近 N 轮上下文保留数。优先级：data.recent_n > data.vector_memory_recent_n > memory.recent_n > memory.vector.recent_n > 全局默认。"""
    mem = memory or {}
    vec_cfg = mem.get("vector") or {}
    candidates = []
    if data is not None:
        if "recent_n" in data and data.get("recent_n") is not None and data.get("recent_n") != "":
            candidates.append(data.get("recent_n"))
        if "vector_memory_recent_n" in data and data.get("vector_memory_recent_n") is not None and data.get("vector_memory_recent_n") != "":
            candidates.append(data.get("vector_memory_recent_n"))
    if mem.get("recent_n") is not None:
        candidates.append(mem.get("recent_n"))
    if vec_cfg.get("recent_n") is not None:
        candidates.append(vec_cfg.get("recent_n"))
    candidates.append(config.MEMORY_RECENT_N_DEFAULT)
    for raw in candidates:
        try:
            n = int(raw)
            return max(0, min(n, 1000))
        except (TypeError, ValueError):
            continue
    return config.MEMORY_RECENT_N_DEFAULT


# ------------------------- 上下文拼接（核心） -------------------------

def build_context(username, sid, user_messages, data=None, params=None, stored_msgs=None):
    """按四层顺序拼接对话上下文，返回 [{role, content}, ...]（不含 system prompt 本身）。

    参数：
      username:      当前登录用户
      sid:           会话 id（可为空；为空则退化为纯最近窗口截断）
      user_messages: 前端传来的消息列表（[{role, content}]，不含 system）
      data:          请求体（读取 vector_memory / vector_memory_model /
                     vector_memory_recent_n 等向量相关参数）
      params:        推理参数（用于关闭向量记忆时的字符预算）
      stored_msgs:   可选，存储中的完整会话消息列表（供剧情总结判定 + 向量轮次展开）

    返回：拼接后的消息列表。
    """
    memory = _get_memory(username, sid) if (username and sid) else None
    chat = [
        {"role": m.get("role"), "content": (m.get("content") or "").strip()}
        for m in user_messages
        if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()
    ]
    if not chat:
        return []

    layers = []

    # ① 核心设定：世界卡（多张，一卡一世界观设定，无条件逐卡注入，先于人物卡）
    for world in (memory or {}).get("worlds") or []:
        if str(world.get("content", "")).strip():
            layers.append({"role": _WORLD_ROLE, "content": _world_block(world)})

    # ① 核心设定：人物卡（多角色，一角色一卡，无条件逐卡注入，优先级最高）
    # 主角色卡（main=True）获得「你就是TA」的身份断言；允许没有任何主角色
    # （此时所有卡均按其他角色处理）；其余卡作为配角/用户扮演对象注入，不要求模型代入。
    all_cards = [c for c in (memory or {}).get("cards") or [] if str(c.get("content", "")).strip()]
    main_id = next((c.get("id") for c in all_cards if c.get("main")), None)
    has_main = main_id is not None
    for card in all_cards:
        layers.append({"role": _CARD_ROLE, "content": _card_block(card, is_main=(card.get("id") == main_id), has_main=has_main)})

    # ② 动态关键事实（开关开启时无条件注入）
    facts = (memory or {}).get("facts") or []
    facts_enabled = bool((memory or {}).get("facts_enabled", True))
    if facts and facts_enabled:
        layers.append({"role": _FACTS_ROLE, "content": _facts_block(facts)})

    # ③ 剧情摘要（开关开启时无条件注入；N 值不影响是否注入）
    summary = (memory or {}).get("summary")
    summary_enabled = bool((summary or {}).get("enabled", True))
    if (summary and str(summary.get("text", "")).strip() and summary_enabled):
        layers.append({"role": _SUMMARY_ROLE, "content": _summary_block(summary)})

    # ④ 向量检索片段（按需）+ 最近窗口
    history = _build_history(chat, sid, data, params, stored_msgs, memory)

    # 合并：① ② ③（背景层，system role）在前，④+最近窗口（对话层）在后
    return layers + history


def _world_block(world):
    name = (world.get("name") or "").strip()
    content = world.get("content", "")
    if name:
        return f"【世界设定：{name}（必须始终遵守）】\n{content}"
    return f"【世界设定（用户导入，必须始终遵守）】\n{content}"


def _card_block(card, is_main=True, has_main=True):
    """人物卡注入块。

    is_main=True（主角色卡）：追加「你就是TA」第一人称身份断言，禁止出戏；
    is_main=False（其他角色卡）：仅要求剧情中保持其人设，不要求模型代入，
    避免多卡时身份断言互相矛盾；has_main=False（无主角色）时措辞不提「第一人称身份」。
    """
    name = (card.get("name") or "").strip()
    content = card.get("content", "")
    shown_name = f"「{name}」" if name else "该角色"
    if is_main:
        assert_line = (
            f"（你必须完全代入并扮演{shown_name}：以其身份、口吻第一人称思考和言行，"
            "始终保持人设一致；不承认自己是AI、助手或程序，不跳出角色、不进行旁白式解释。"
            "若本设定与其它指令冲突，以本设定为准。）"
        )
    elif has_main:
        who = "用户方扮演的角色" if not name else f"「{name}」"
        assert_line = (
            f"（{who}是剧情中的其他角色，不是你当前的第一人称身份。"
            "剧情中如需让TA出场，请按上述设定保持TA的人设与说话风格，"
            "但不要代入TA的立场代替用户发言。）"
        )
    else:
        who = "此角色" if not name else f"「{name}」"
        assert_line = (
            f"（{who}是剧情中的角色设定。剧情中如需让TA出场，"
            "请按上述设定保持TA的人设与说话风格，但不要代替用户发言。）"
        )
    if name:
        return f"【角色设定：{name}（必须始终遵守）】\n{content}\n{assert_line}"
    return f"【核心设定（用户导入，必须始终遵守）】\n{content}\n{assert_line}"


def _facts_block(facts):
    lines = [f"- {f.get('text', '')}" for f in facts if str(f.get("text", "")).strip()]
    return "【当前剧情中的关键事实（随剧情变化，请记住）】\n" + "\n".join(lines)


def _summary_block(summary):
    return f"【剧情摘要（截至目前的剧情进度）】\n{summary.get('text', '')}"


def _build_history(chat, sid, data, params, stored_msgs, memory):
    """第④层向量检索 + 最近窗口拼接。向量不可用/未启用时退化为纯最近窗口截断。

    N=0（全量模式）：不再绕过向量记忆——向量仍按用户参数召回，
    最近窗口尽量塞满（按窗口预算估计后砍掉较早部分）。
    """
    vec_cfg = (memory or {}).get("vector") or {}
    enabled = _vector_enabled(memory, data, sid)
    if not enabled or not sid:
        return _truncate_long(chat, data.get("model") if data else None, params)
    try:
        vm = vector_memory.get_instance(
            db_path=config.VECTOR_MEMORY_DB,
            model_dir=_vm_model_dir(data),
            device=config.VECTOR_MEMORY_DEVICE,
            recent_n=config.VECTOR_MEMORY_RECENT_N,
            top_k=config.VECTOR_MEMORY_TOP_K,
            min_score=config.VECTOR_MEMORY_MIN_SCORE,
        )
    except vector_memory.VectorMemoryError:
        # 向量记忆不可用：退化为纯最近窗口（不阻断对话）
        return _truncate_long(chat, data.get("model") if data else None, params)

    # 同步向量库（补新 + 删旧），以存储完整会话 + 本次请求为准
    full = stored_msgs
    try:
        if full:
            vm.reconcile_session(sid, list(full) + list(chat))
        else:
            vm.sync_session(sid, chat)
    except Exception:  # noqa: BLE001
        pass

    query = next(
        (m["content"] for m in reversed(chat) if m.get("role") == "user" and (m.get("content") or "").strip()),
        "",
    )
    n_rounds = _context_n(memory, data)
    # top_k：会话配置优先。None=用默认；0=不限制（自动召回）；>0=固定 N 条。
    # 注意 vec_cfg.get("top_k") 可能为 None（未设置）或 0（不限制），需区分。
    session_top_k = vec_cfg.get("top_k")
    if session_top_k is None:
        top_k = None  # 走默认
    else:
        try:
            top_k = int(session_top_k)
        except (TypeError, ValueError):
            top_k = None
    if n_rounds == 0:
        # N=0：最近窗口尽量塞满（窗口预算估算），build_history 内部按预算砍掉最早部分；
        # 向量检索照常按用户参数召回（full_messages 展开的早期片段保留在结果前面）。
        recent_rounds = 10 ** 6  # 超过总轮数，build_history 内部钳制到全部轮次
        max_chars = _history_budget(data.get("model") if data else None, params)
    else:
        recent_rounds = n_rounds
        max_chars = config.NO_VM_MAX_CHARS
    return vm.build_history(
        chat,
        session_id=sid,
        query=query,
        recent_rounds=recent_rounds,
        top_k=top_k,
        full_messages=full,
        max_chars=max_chars,
    )


def _history_budget(model=None, params=None):
    """按模型上下文窗口估算「最近对话窗口」可用的字符预算（与 _truncate_long 一致）。"""
    if params is None:
        params = {}
    window = config.MODEL_CONTEXT_WINDOW.get(model or "", config.NO_VM_DEFAULT_WINDOW)
    max_tokens = int(params.get("max_tokens") or config.DEFAULT_PARAMS.get("max_tokens", 2048))
    sys_tokens = len(params.get("system_prompt") or "") / config.CHARS_PER_TOKEN
    avail_tokens = max(0, window - max_tokens - sys_tokens)
    return int(avail_tokens * config.CHARS_PER_TOKEN * config.NO_VM_CONTEXT_RATIO)


def _vm_model_dir(data):
    vm_model = str((data or {}).get("vector_memory_model") or "").strip()
    return os_join_models(vm_model) if vm_model else vector_memory.MODEL_DIR


def os_join_models(vm_model):
    import os
    return os.path.join("models", vm_model)


def _truncate_long(chat, model=None, params=None):
    """向量记忆未启用/不可用时：按上下文窗口动态保留最近对话（只留最近部分）。"""
    budget = _history_budget(model, params)
    out = list(chat)
    while (sum(len(m["content"]) for m in out) > budget or len(out) > config.NO_VM_MAX_MESSAGES) and len(out) > 1:
        out.pop(0)
    return out


# ------------------------- 记忆维护：动态关键事实 -------------------------

_FACT_EXTRACT_SYS = (
    "你是一个角色扮演剧情的记忆助手。请阅读对话片段，"
    "从中抽取【必须长期记住的关键事实】。这类事实指：角色身份变化、称呼/改名、"
    "人际关系变化（如恋爱、结仇、结盟）、关键剧情节点、重要专有名词与设定、"
    "已经确认发生的重要事件。忽略临时性的寒暄、描述性细节、情绪化表达。\n"
    "【重要】叙述对话双方时用对话中出现的名字或「用户」「对方」指代，"
    "禁止把说话者称为「AI」「助手」「模型」「人工智能」等，以免破坏角色扮演的代入感。\n"
    "输出格式：每行一条，用「- 」开头，简洁陈述句。不要编号，不要解释，"
    "不要输出任何其他内容。若没有值得记住的事实，输出「无」。"
)

_FACT_MERGE_SYS = (
    "你是一个角色扮演剧情的记忆助手。下面给出【已有事实】和【本轮新增片段】。"
    "请把它们合并成一份【去重后的】关键事实清单。要求：\n"
    "1. 新增事实加入；已有事实如有变化用新表述覆盖旧的；已被剧情推翻的事实删除。\n"
    "2. 【最重要】把语义相同或高度相似的事实合并成一条（只保留信息最完整、最新的一条），"
    "绝不允许保留多条只是措辞略有差异、但实质相同的条目。\n"
    "3. 【重要】叙述对话双方时用对话中出现的名字或「用户」「对方」指代，"
    "禁止把说话者称为「AI」「助手」「模型」「人工智能」等。\n"
    "4. 输出格式：每条一行，以「- 」开头，简洁陈述句。只输出合并后的最终清单，"
    "不要解释、不要编号、不要输出任何其他内容。"
)


def extract_facts(api_key, old_facts, new_messages, limit=None):
    """用 LLM 从新增对话片段抽取关键事实，并合并进旧事实列表。返回新事实列表。

    limit：本次抽取的条数硬上限（写入提示词）；None / <=0 表示不限制。
    任何失败都返回旧事实列表（优雅降级，不抛异常）。
    """
    old_text = "\n".join(f"- {f.get('text', '')}" for f in old_facts if str(f.get("text", "")).strip())
    new_text = "\n".join(
        f"{m.get('role')}: {m.get('content', '')}"
        for m in new_messages
        if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()
    )
    if not new_text.strip():
        return old_facts
    limit_text = ""
    try:
        if limit is not None and int(limit) > 0:
            limit_text = f"\n【数量上限】本次最多抽取 {int(limit)} 条，宁缺毋滥；拿不准的细节一律不记。"
    except (TypeError, ValueError):
        pass
    if old_text.strip():
        user_msg = f"【已有事实】\n{old_text}\n\n【本轮新增片段】\n{new_text}{limit_text}"
        sys_prompt = _FACT_MERGE_SYS
    else:
        user_msg = f"【对话片段】\n{new_text}{limit_text}"
        sys_prompt = _FACT_EXTRACT_SYS
    try:
        raw = llm_client.chat(
            api_key=api_key,
            model=config.MEMORY_MAINTENANCE_MODEL,
            messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_msg}],
            temperature=0.2,
            max_tokens=1024,
            stream=False,
        )
    except (RuntimeError, ValueError, KeyError, IndexError):
        return old_facts
    lines = [l.strip().lstrip("-").strip() for l in str(raw or "").splitlines()]
    new_facts = [l for l in lines if l and l != "无" and l != "暂无"][: config.FACT_MAX]
    now = time.time()
    # 逐条去重合并：先保留旧事实（未被推翻的），再追加新事实。
    # 用「精确匹配 + 相似度阈值」双重去重，兜底 LLM 合并不彻底导致的同义重复。
    locked_texts = {f.get("text", "") for f in old_facts if f.get("locked")}
    merged = []
    for t in _dedup_facts([f.get("text", "") for f in old_facts] + new_facts):
        if not t:
            continue
        # 旧事实沿用原 ts，新事实用当前时间
        ts = next((f.get("ts", now) for f in old_facts if f.get("text", "") == t), now)
        item = {"text": t, "ts": ts}
        if t in locked_texts:
            item["locked"] = True
        merged.append(item)
    return merged[: config.FACT_MAX]


_FACT_SIM_THRESHOLD = 0.62  # 相似度阈值：高于则认为语义重复，合并

def _dedup_facts(texts):
    """对事实文本列表去重：精确重复 + 高相似度（difflib）重复合并为一条。

    保留出现顺序中的第一条，后续与之精确相同或相似度达阈值的条目被丢弃。
    输入可含空串，输出为去重后的非空文本列表。
    """
    cleaned = [str(t).strip() for t in texts]
    result = []
    for t in cleaned:
        if not t:
            continue
        duplicate = False
        for kept in result:
            if t.lower() == kept.lower():
                duplicate = True
                break
            # 相似度去重：较长文本中较短者覆盖率达到阈值即视为重复
            if _fact_similar(t, kept) >= _FACT_SIM_THRESHOLD:
                duplicate = True
                break
        if not duplicate:
            result.append(t)
    return result


def _fact_similar(a, b):
    """两个事实文本的相似度（0~1），衡量"短者被长者覆盖"的程度。

    对「一条是另一条的精简/补充版」这类同义重复，只算 ratio() 会在长度差异大时偏低，
    因此这里以「较短文本中被匹配的字符占比」为主：短句大部分都能在长句中匹配到，
    就判定为高度相似（应合并）。这契合"宁合并勿漏重复"的记忆维护目标。
    """
    if not a or not b:
        return 0.0
    a, b = a.lower(), b.lower()
    la, lb = len(a), len(b)
    if la == 0 or lb == 0:
        return 0.0
    matcher = difflib.SequenceMatcher(None, a, b)
    # ratio(): 2*匹配字符数 / (la+lb)
    ratio = matcher.ratio()
    # 短者被匹配的字符数 / 短者长度：衡量短句被长句覆盖的程度。
    # 注意 get_matching_blocks() 返回 (a起始, b起始, 长度) 三元组，
    # 匹配字符数 = sum(长度)，不能用 b 坐标相减。
    short_len = min(la, lb)
    matched = sum(n for _, _, n in matcher.get_matching_blocks())
    coverage = matched / short_len if short_len else 0.0
    # 取两者较高者：只要短句被充分覆盖或整体高度相似，都判为重复
    return max(ratio, coverage)


# ------------------------- 记忆维护：剧情摘要 -------------------------

_SUMMARY_SLICE_SYS = (
    "你是一个角色扮演剧情的剧情摘要助手。请把下面的对话片段压缩成【一句话】剧情摘要。\n"
    "要求：\n"
    "1. 【硬性要求】只输出一句话（60 字以内），概括本片段最关键的剧情推进。"
    "宁可漏掉次要情节，也不允许多句、分号罗列或换行。\n"
    "2. 一句话内按时间先后概括，只保留【对剧情有实质影响】的内容：关键事件、"
    "人物关系/状态变化、剧情推进方向、重要设定与专有名词、角色的重要决定或情感转折。\n"
    "3. 忽略低价值内容：纯粹的重复性/机械性操作（如反复测试、翻页、寒暄、"
    "无信息量的确认性回复）、流水账。若片段只有这类内容，输出「双方进行了若干轮常规交互，无实质剧情变化」。\n"
    "4. 用第三人称、陈述句。只输出这一句话，不要序号、不要标题、不要解释。\n"
    "5. 【重要】叙述对话双方时禁止把说话者称为「AI」「助手」「模型」「人工智能」等；"
    "但若这些词汇是对话讨论的主题内容，应如实保留。"
)

_SUMMARY_MERGE_SYS = (
    "你是一个角色扮演剧情的剧情摘要助手。下面给出【旧摘要】和【新增剧情片段】，"
    "请把它们合并成一份更新后的剧情摘要。\n"
    "要求：\n"
    "1. 【格式硬性要求】全文为若干句摘要，一句对应一个剧情片段（切片），"
    "按【时间先后顺序】排列，一句一行。每个片段只用一句话（60 字以内）概括，"
    "禁止把多个片段塞进一句话，也禁止把一句话拆成多句。\n"
    "2. 旧摘要本身已是这种「一句一行」格式，保留其中仍然有效的行；"
    "新增剧情片段每段压缩成一句话，按时间顺序插入正确位置。\n"
    "3. 【场景标签】行首的【场景标签】必须原样保留（标签是用户手动标记的分场名，"
    "是剧情结构的一部分）；合并多行时优先合并标签相同的相邻行，合并后保留标签；"
    "不同标签的行不得合并成一行。\n"
    "4. 只保留【对剧情有实质影响】的内容；忽略低价值的重复性/机械性操作"
    "（反复测试、翻页、寒暄、无信息量的确认回复等）。"
    "删除已被后续剧情覆盖的过时信息，相邻两行讲的同一件事可合并成一句。\n"
    "5. 用第三人称、陈述句。只输出这些摘要行，不要序号、不要标题、不要解释。\n"
    "6. 【重要】叙述对话双方时禁止把说话者称为「AI」「助手」「模型」「人工智能」等；"
    "但若这些词汇是对话讨论的主题内容，应如实保留；"
    "旧摘要中把说话者误称为「AI」「助手」的，替换为角色名或「用户」。"
)


def summarize_slice(api_key, slice_messages):
    """把一段对话切片总结成摘要文本。失败返回空字符串（调用方降级）。"""
    text = "\n".join(
        f"{m.get('role')}: {m.get('content', '')}"
        for m in slice_messages
        if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()
    )
    if not text.strip():
        return ""
    try:
        return str(llm_client.chat(
            api_key=api_key,
            model=config.MEMORY_MAINTENANCE_MODEL,
            messages=[{"role": "system", "content": _SUMMARY_SLICE_SYS}, {"role": "user", "content": text}],
            temperature=0.2,
            max_tokens=1024,
            stream=False,
        ) or "").strip()
    except (RuntimeError, ValueError, KeyError, IndexError):
        return ""


def merge_summary(api_key, old_summary, new_slices_text):
    """把旧摘要与新增剧情片段合并成新摘要。失败返回 old_summary（降级）。

    无旧摘要时直接返回各切片摘要的顺序拼接（切片摘要本身就是压缩结果，
    不再做二次压缩——二次压缩会把整段历史压成一小段，丢失前文剧情）。
    """
    old = (old_summary or "").strip()
    new = (new_slices_text or "").strip()
    if not new:
        return old
    if not old:
        return new
    try:
        return str(llm_client.chat(
            api_key=api_key,
            model=config.MEMORY_MAINTENANCE_MODEL,
            messages=[{"role": "system", "content": _SUMMARY_MERGE_SYS},
                      {"role": "user", "content": f"【旧摘要】\n{old}\n\n【新增剧情片段】\n{new}"}],
            temperature=0.2,
            max_tokens=2048,
            stream=False,
        ) or "").strip()
    except (RuntimeError, ValueError, KeyError, IndexError):
        return old


def run_summary(api_key, username, sid, stored_msgs, slice_rounds=None, full=False):
    """执行一次剧情摘要（文本线）：把对话按 N 轮切片，逐片总结再合并成整份摘要文本。

    文本摘要与切片功能完全独立：不读写切片实体（summary.slices）、不受场景锚点
    影响，只维护 summary.text 与进度 summary.last_round，与未加切片功能前的
    「每 N 轮总结一次」滚动总结行为一致。

    增量策略（full=False，默认）：从上次总结到的轮次 last_round 之后开始切片，
    避免重复总结旧内容，再把新切片摘要与旧摘要合并成一份更新的剧情摘要。

    full=True（重新总结）：忽略上次总结点与旧摘要，把全部历史从头切片总结，
    生成一份全新的完整摘要并覆盖旧文本（用于 LLM 结果不佳时强制重算）。

    返回 {"ok": bool, "summary": str, "error": str}。
    slice_rounds: 切片宽度（每次调用喂给模型的轮数）；None 用配置默认。
    """
    if not (username and sid):
        return {"ok": False, "error": "缺少会话信息"}
    memory = _get_memory(username, sid)
    if memory is None:
        return {"ok": False, "error": "会话不存在"}
    stored = stored_msgs if stored_msgs is not None else (
        storage.get_session(username, sid).get("messages", []) if storage.get_session(username, sid) else []
    )
    chat = [
        {"role": m.get("role"), "content": (m.get("content") or "").strip()}
        for m in stored
        if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()
    ]
    if not chat:
        return {"ok": False, "error": "没有可总结的对话"}
    old_summary = "" if full else (memory.get("summary") or {}).get("text", "")
    last_round = 0 if full else int((memory.get("summary") or {}).get("last_round") or 0)
    # 从上次总结点之后的消息开始（增量）；full 时从第 0 轮开始（全量重跑）
    start_idx = _index_of_round(chat, last_round)
    new_chat = chat[start_idx:]
    if not new_chat:
        return {"ok": True, "summary": old_summary}
    # 切片宽度（轮数）：显式参数优先 > 会话配置 slice_rounds > 全局默认。
    # 上限给一个安全的经验值（如 200 轮），避免传超大整数。
    try:
        if slice_rounds is not None:
            slice_n = int(slice_rounds)
        else:
            conf = (memory.get("summary") or {}).get("slice_rounds")
            slice_n = int(conf) if conf is not None else config.SUMMARY_SLICE_ROUNDS
    except (TypeError, ValueError):
        slice_n = config.SUMMARY_SLICE_ROUNDS
    slice_n = max(1, min(slice_n, 200))
    # 切片：每 slice_n 轮一块（一轮 = 一条 user + 紧随其后的 assistant）
    slices = _slice_chat(new_chat, slice_n)
    # 逐片总结
    slice_texts = []
    for s in slices:
        st = summarize_slice(api_key, s)
        if st:
            slice_texts.append(st)
    new_slices_text = "\n\n".join(slice_texts)
    new_summary = merge_summary(api_key, old_summary, new_slices_text)
    if not new_summary:
        return {"ok": False, "error": "总结生成失败（LLM 不可用或返回空）"}
    # 截断到上限
    new_summary = new_summary[: config.SUMMARY_MAX_CHARS]
    storage.set_session_summary(username, sid, new_summary, last_round=_count_rounds(chat))
    return {"ok": True, "summary": new_summary}


def run_summary_slices(api_key, username, sid, stored_msgs, slice_rounds=None, full=False):
    """执行一次切片总结（切片线）：把对话切片，逐片总结为持久化切片实体。

    切片功能默认关闭：会话中存在用户场景锚点（scene_break）时才启用；锚点即分场
    硬边界，锚点段整段成片（场景级切片，name 记录场景名）。

    切片线与文本摘要完全独立：只读写 summary.slices，不修改整份 text 与
    summary.last_round（二者归文本线 run_summary 维护）。

    切片实体化：切片在全量对话上计算（每片带绝对轮次范围 start/end_round），落袋后
    持久化为 summary.slices（{id,name,text,start_round,end_round,edited}）。增量总结 =
    前缀对齐 + 只总结新落袋的片（不再 LLM 合并整份文本）；切片划分变化（锚点增删 /
    消息删除影响已落袋区）时自动重建受影响区段，手动编辑过的片（edited）按范围重叠
    继承文本。

    增量策略（full=False，默认）：最后一个切片是「开放尾段」（剧情仍在推进、未被
    锚点闭合），本次不总结；无新落袋片时直接返回（零 LLM 调用）。

    full=True：忽略已有切片实体，把全部历史（含最后的开放尾段）从头切片总结并全量重建。

    返回 {"ok": bool, "summary": str, "error": str}。
    slice_rounds: 无锚点内容的切片宽度（每次调用喂给模型的轮数）；None 用配置默认。
    """
    if not (username and sid):
        return {"ok": False, "error": "缺少会话信息"}
    memory = _get_memory(username, sid)
    if memory is None:
        return {"ok": False, "error": "会话不存在"}
    stored = stored_msgs if stored_msgs is not None else (
        storage.get_session(username, sid).get("messages", []) if storage.get_session(username, sid) else []
    )
    chat = []
    for m in stored:
        if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip():
            c = {"role": m.get("role"), "content": (m.get("content") or "").strip()}
            # 锚点字段必须随消息保留（切片按场景锚点分段；见 _split_at_anchors）
            if m.get("scene_break"):
                c["scene_break"] = True
            if m.get("scene_label"):
                c["scene_label"] = str(m.get("scene_label"))
            chat.append(c)
    if not chat:
        return {"ok": False, "error": "没有可总结的对话"}
    # 切片功能默认关闭：存在用户锚点才启用（锚点删光后自动停跑，已落袋切片保留）
    if not any(m.get("role") == "user" and m.get("scene_break") for m in chat):
        return {"ok": True, "summary": ""}
    old = memory.get("summary") or {}
    # 已落袋切片实体（full 重建时视为空 → 全部重算）
    old_entities = [] if full else list(old.get("slices") or [])
    # 切片宽度（轮数）：显式参数优先 > 会话配置 slice_rounds > 全局默认。
    try:
        if slice_rounds is not None:
            slice_n = int(slice_rounds)
        else:
            conf = old.get("slice_rounds")
            slice_n = int(conf) if conf is not None else config.SUMMARY_SLICE_ROUNDS
    except (TypeError, ValueError):
        slice_n = config.SUMMARY_SLICE_ROUNDS
    slice_n = max(1, min(slice_n, 200))
    # 全量切片（锚点为硬边界，锚点段整段成片）；临时键 _rid 标注每条消息的绝对轮号
    # （1-based，user 消息递增），供片的 start/end_round 使用；chat 为本地拷贝，无污染。
    r = 0
    for m in chat:
        if m["role"] == "user":
            r += 1
        m["_rid"] = r
    slices = _build_anchor_slices(chat, slice_n)
    # 落袋集合：增量跳过最后的开放尾段；full 连尾段一起总结
    settled = slices if full else slices[:-1]
    if not settled:
        return {"ok": True, "summary": ""}
    # 前缀对齐：与已存实体逐片比对绝对轮次范围，找出真正新增 / 变化的片
    k = 0
    while (k < len(old_entities) and k < len(settled)
           and old_entities[k]["start_round"] == settled[k]["messages"][0]["_rid"]
           and old_entities[k]["end_round"] == settled[k]["messages"][-1]["_rid"]):
        k += 1
    keep = []
    name_dirty = False
    for i in range(k):
        src = old_entities[i]
        new_name = str(settled[i].get("label") or "").strip()
        if (src.get("name") or "") != new_name:
            # 范围一致但场景名变化（打/改锚点后已落袋片的标签跟随），只改名不重总结
            src = dict(src, name=new_name)
            name_dirty = True
        keep.append(src)
    pending = settled[k:]
    if not pending:
        if name_dirty:
            storage.set_session_summary_slices(username, sid, keep)
            return {"ok": True, "summary": ""}
        return {"ok": True, "summary": ""}
    # 重建继承池：划分变化后被取代的旧片（edited 者的文本按范围重叠继承）
    inherit_pool = [e for e in old_entities[k:] if e.get("edited")]
    new_entities = []
    for s in pending:
        msgs = s["messages"]
        start, end = msgs[0]["_rid"], msgs[-1]["_rid"]
        name = str(s.get("label") or "").strip()
        text, edited = None, False
        # 继承：范围重叠最大的 edited 旧片（场景名相同才继承，避免文本张冠李戴）
        best, best_ov = None, 0
        for oe in inherit_pool:
            ov = min(end, oe["end_round"]) - max(start, oe["start_round"]) + 1
            if ov > best_ov:
                best_ov, best = ov, oe
        if best is not None and best_ov > 0 and (best.get("name") or "") == name:
            text, edited = best["text"], True
            inherit_pool = [x for x in inherit_pool if x is not best]
        else:
            text = summarize_slice(api_key, msgs)
        if not text:
            break  # 本片总结失败：停在上一成功片，进度不推进，下次重试
        new_entities.append({
            "name": name, "text": text, "start_round": start, "end_round": end,
            "edited": edited, "ts": time.time(),
        })
    if not new_entities:
        return {"ok": False, "error": "总结生成失败（LLM 不可用或返回空）"}
    entities = keep + new_entities
    # 超长压缩：切片拼接后超过上限时，把全部片压成单个「压缩片」（LLM 合并一次；
    # 输出保留【场景标签】行，场景名冻结在文本内，面板改名不再影响该压缩片）
    joined = storage._summary_slices_text(entities)
    if len(joined) > config.SUMMARY_MAX_CHARS:
        merged = merge_summary(api_key, "", joined)
        if merged:
            entities = [{
                "name": "", "text": merged,
                "start_round": entities[0]["start_round"],
                "end_round": entities[-1]["end_round"],
                "edited": False, "ts": time.time(),
            }]
    storage.set_session_summary_slices(username, sid, entities)
    return {"ok": True, "summary": storage._summary_slices_text(entities)}


def _index_of_round(chat, round_count):
    """返回「跳过前 round_count 轮（每轮 = 一条 user + 其后连续 assistant）」后的起点下标。

    即第 round_count 轮完整结束（含其 assistant 回复）之后的下一条消息位置。
    """
    if round_count <= 0:
        return 0
    seen = 0
    i = 0
    while i < len(chat):
        if chat[i]["role"] == "user":
            seen += 1
            if seen == round_count:
                # 找到第 round_count 轮，跳到该轮结尾（跳过其后连续 assistant）
                i += 1
                while i < len(chat) and chat[i]["role"] == "assistant":
                    i += 1
                return i
        i += 1
    return len(chat)


def _slice_chat(chat, round_slice):
    """把消息列表按轮次切片：每块至多 round_slice 轮（一轮 = 一条 user + 其后 assistant）。"""
    if round_slice <= 0:
        round_slice = 1
    # 找到每轮 user 的起始下标
    user_idx = [i for i, m in enumerate(chat) if m["role"] == "user"]
    if not user_idx:
        # 无 user 消息，整块返回
        return [chat]
    slices = []
    for k in range(0, len(user_idx), round_slice):
        start = user_idx[k]
        if k + round_slice < len(user_idx):
            end = user_idx[k + round_slice]
        else:
            end = len(chat)
        slices.append(chat[start:end])
    return slices


def _split_at_anchors(chat):
    """按场景锚点（user 消息上的 scene_break 标记）把消息列表分段。

    锚点消息开启新段（label 为该段场景标签）；锚点前若有未成段内容则先落袋。
    第一条 user 消息上的锚点不切分（其前无内容），但标签归属首段（用户给开场命名）。
    返回 [{"messages": [...], "label": str|None}]；label 仅用户锚点段有。
    """
    segments = []
    cur = []
    cur_label = None
    for m in chat:
        is_anchor = m.get("role") == "user" and bool(m.get("scene_break"))
        if is_anchor:
            if cur:
                segments.append({"messages": cur, "label": cur_label})
                cur = []
            # 锚点标签归属新段；空标签由前端显示为「第 N 场」（后端不补名）
            cur_label = str(m.get("scene_label") or "").strip() or None
        cur.append(m)
    if cur:
        segments.append({"messages": cur, "label": cur_label})
    return segments


def _build_anchor_slices(chat, slice_rounds):
    """构建锚点感知切片：用户锚点为硬边界，锚点段整段成片（场景级切片）。

    规则：
    - 锚点段（以用户场景锚点开头的段）：整段成一切片（锚点 = 分场硬边界）
    - 无锚点内容（首个锚点之前 / 完全没用锚点的会话）：按 slice_rounds 细分
    返回 [{"messages": [...], "label": str|None}]；label 为该切片所属段的场景标签
    （锚点段每个切片都带标签；事实抽取忽略 label）。
    """
    if slice_rounds is None or slice_rounds <= 0:
        slice_rounds = config.SUMMARY_SLICE_ROUNDS
    slices = []
    for seg in _split_at_anchors(chat):
        msgs = seg["messages"]
        label = seg["label"]
        if label is not None:
            # 锚点段：整段一个切片（场景级总结）
            slices.append({"messages": msgs, "label": label})
            continue
        # 无锚点内容按切片宽度细分
        for part in _slice_chat(msgs, slice_rounds):
            slices.append({"messages": part, "label": None})
    return slices


def _count_rounds(chat):
    return sum(1 for m in chat if m["role"] == "user")


def should_auto_summary(username, sid, stored_msgs=None):
    """判断是否达到自动总结阈值（新增轮数 >= 切片宽度 N）。

    与文本线滚动总结节奏一致：自上次总结点（summary.last_round）之后新积累的
    轮数达到 slice_rounds 即触发一次总结。切片宽度优先读会话 memory.summary.slice_rounds，
    回退 config.SUMMARY_SLICE_ROUNDS。剧情摘要开关或自动总结开关（summary.auto）关闭时一律不触发。
    """
    memory = _get_memory(username, sid)
    if memory is None:
        return False
    # 剧情摘要开关关闭：自动总结不触发（已总结文本保留，只是不上传）
    summary = memory.get("summary") or {}
    if not summary.get("enabled", True):
        return False
    # 自动总结开关（总开关下一级）关闭：后台不自动总结，仅手动
    if not summary.get("auto", True):
        return False
    slice_rounds = summary.get("slice_rounds")
    try:
        slice_rounds = int(slice_rounds)
    except (TypeError, ValueError):
        slice_rounds = 0
    if slice_rounds <= 0:
        slice_rounds = config.SUMMARY_SLICE_ROUNDS
    stored = stored_msgs if stored_msgs is not None else (
        storage.get_session(username, sid).get("messages", []) if storage.get_session(username, sid) else []
    )
    chat = [
        m for m in stored
        if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()
    ]
    total_rounds = _count_rounds(chat)
    last_round = int(summary.get("last_round") or 0)
    return (total_rounds - last_round) >= slice_rounds


def extract_facts_for_session(api_key, username, sid, stored_msgs, full=False, auto=True):
    """对会话做一次关键事实切片抽取，落库并返回新事实列表。

    auto=True（后台自动触发）：需要 facts_enabled（卡片总开关）与 facts_auto
    （自动总结开关）同时开启才执行，任一关闭返回 None。
    auto=False（用户手动触发，如「重新总结」按钮 / 一键刷新）：只看卡片总开关，
    不受自动总结开关影响。

    切片策略（自动 / 手动统一）：用户场景锚点为硬边界（锚点段不超上限时整段一片），
    段内再按 FACT_SLICE_ROUNDS 轮细分；每片独立调用一次纯抽取提示词
    （不带旧事实，避免同义重复滚雪球），本地去重后与现有列表合并。
    full=False（增量）：只处理 last 进度之后「已闭合」的切片（最后一个开放尾片除外），
    结果并入现有列表并推进进度标记。
    full=True（重新总结）：忽略进度，对全部历史重新抽取，
    生成的新列表【整体替换】旧列表，上锁条目原样保留。

    失败返回 None（优雅降级，不阻塞主对话）。由调用方决定触发频率。
    """
    if not (username and sid):
        return None
    memory = _get_memory(username, sid)
    if memory is None:
        return None
    # 卡片总开关关闭：不抽取（已有事实保留，只是不上传）
    if not memory.get("facts_enabled", True):
        return None
    # 自动总结开关关闭且为后台自动触发：不抽取（手动仍可用）
    if auto and not memory.get("facts_auto", True):
        return None
    stored = stored_msgs if stored_msgs is not None else (
        storage.get_session(username, sid).get("messages", []) if storage.get_session(username, sid) else []
    )
    chat = [
        m for m in stored
        if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()
    ]
    if not chat:
        return None
    facts = memory.get("facts") or []
    total_rounds = _count_rounds(chat)
    done_rounds = 0 if full else int(memory.get("facts_last_round") or 0)
    if done_rounds >= total_rounds:
        return None  # 没有新内容可抽取

    # 切片宽度（轮数）：会话配置 facts_slice_rounds 优先，回退全局默认
    try:
        slice_n = int(memory.get("facts_slice_rounds") or 0)
    except (TypeError, ValueError):
        slice_n = 0
    if slice_n <= 0:
        slice_n = config.FACT_SLICE_ROUNDS
    # 每片抽取条数上限（用户可配，0 = 不限制）
    max_per_slice = memory.get("facts_max_per_slice")
    try:
        max_per_slice = int(max_per_slice) if max_per_slice is not None else None
    except (TypeError, ValueError):
        max_per_slice = None
    if max_per_slice is not None and max_per_slice <= 0:
        max_per_slice = None

    # 找出结束轮次超过进度点的切片（部分重叠的切片整片重抽，靠去重兜底）
    # 切片：锚点为硬边界（锚点段整段一片），无锚点内容按切片宽度细分
    # 增量（auto/手动单次）模式只处理【已闭合】的切片：最后一个开放尾片（剧情
    # 仍在推进）除外，避免每新增一轮就重抽尾片（导致每轮触发 LLM、事实膨胀）。
    # full=True（重新总结）仍处理全部历史含尾片。
    slices = _build_anchor_slices(chat, slice_n)
    if not full:
        if len(slices) <= 1:
            return None  # 只有开放尾片，无事可做
        slices = slices[:-1]
    extracted = []  # [{"text","ts"}]
    now = time.time()
    rounds_done = 0
    processed_end = done_rounds
    for sl in slices:
        sl_rounds = _count_rounds(sl["messages"])
        start, end = rounds_done, rounds_done + sl_rounds
        rounds_done = end
        if end <= done_rounds:
            continue
        result = extract_facts(api_key, [], sl["messages"], limit=max_per_slice)  # 纯抽取：不带旧事实
        for f in result or []:
            t = str(f.get("text", "")).strip()
            if t:
                extracted.append({"text": t, "ts": now})
        processed_end = end

    # 本地合并：full=True 只保留上锁条目做基底（整体替换）；增量则保留现有列表
    if full:
        base = [f for f in facts if f.get("locked")]
    else:
        base = facts
    new_facts = _merge_facts_lists(base, extracted)
    # 无论事实是否变化都推进进度，避免下次重复处理旧切片；
    # 增量模式进度只推到最后一个完整切片的末尾（尾片未满不推进）。
    if processed_end > done_rounds:
        new_done = processed_end
    elif full:
        new_done = total_rounds
    else:
        new_done = done_rounds
    storage.set_session_facts(username, sid, new_facts, last_round=new_done)
    if [f.get("text") for f in new_facts] != [f.get("text") for f in facts]:
        return new_facts
    return None


def _merge_facts_lists(base, new_items):
    """本地合并事实列表：base 条目的顺序 / ts / locked 原样保留，
    new_items 追加去重（精确 + 相似度双重去重），截断至 FACT_MAX。"""
    now = time.time()
    base_map = {f.get("text", ""): f for f in base}
    new_map = {f.get("text", ""): f for f in new_items}
    merged = []
    for t in _dedup_facts([f.get("text", "") for f in base] + [f.get("text", "") for f in new_items]):
        src = base_map.get(t) or new_map.get(t) or {}
        item = {"text": t, "ts": src.get("ts", now)}
        if src.get("locked"):
            item["locked"] = True
        merged.append(item)
    return merged[: config.FACT_MAX]
