"""全局配置：支持的模型、默认推理参数、服务端口。

新增模型只需在 SUPPORTED_MODELS 里加一项，前端会自动出现可选项。
"""

import os

# Flask 服务配置
# 0.0.0.0 允许局域网内其他设备（如手机）访问；仅本机访问可改回 127.0.0.1
HOST = "0.0.0.0"
PORT = 5000
SECRET_KEY = "dpsklite-webui-change-this-secret"

# 是否要求 session cookie 仅在 HTTPS 下传输。
# 默认关闭，保证「克隆后直接 python app.py 以明文 http://<ip>:5000 访问」也能登录。
# 若部署在 nginx 等 HTTPS 反向代理之后，请改为 True。
SESSION_COOKIE_SECURE = False

# 自动生成会话标题使用的固定模型（不跟随 UI 选择，保证标题生成轻量稳定）
AUTO_TITLE_MODEL = "deepseek-chat"

# 支持的模型列表。每个模型包含：
#   id        -> 实际传给 API 的 model 名
#   label     -> 前端展示名
#   base_url  -> API 基址（兼容 OpenAI 协议）
#   default_model -> 该厂商默认 model 名（label 等于它时直接用它）
SUPPORTED_MODELS = [
    {
        "id": "deepseek-chat",
        "label": "DeepSeek Chat",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_hint": "sk-...（DeepSeek 控制台获取）",
    },
    {
        "id": "deepseek-reasoner",
        "label": "DeepSeek Reasoner",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_hint": "sk-...（DeepSeek 控制台获取）",
    },
]

# 调试开关：在控制台打印实际发给 LLM 的完整上下文（含各记忆层注入边界）。
# 默认关闭；排查记忆注入问题时可临时改为 True。
LOG_CONTEXT = True

# 默认推理参数（前端初始值也来自这里）
DEFAULT_PARAMS = {
    "temperature": 0.7,
    "top_p": 1.0,
    "max_tokens": 2048,
    "frequency_penalty": 0.0,
    "presence_penalty": 0.0,
    "stop": "",
    # 新对话默认不预填系统提示词，由用户自行填写（留空 = 不注入 system 消息）
    "system_prompt": "",
}

# 数值型参数的合法范围，用于后端校验（前端也会据此外做回退）
PARAM_RANGES = {
    "temperature": (0.0, 2.0),
    "top_p": (0.0, 1.0),
    "max_tokens": (1, 8192),
    "frequency_penalty": (-2.0, 2.0),
    "presence_penalty": (-2.0, 2.0),
}

# 每个参数的中文说明（前端展示用）
PARAM_META = {
    "temperature": "采样随机性。越高越发散有创意，越低越确定保守。",
    "top_p": "核采样阈值。只从累计概率达此值的词集中采样。",
    "max_tokens": "单次回复最大 token 数（含推理与回答）。",
    "frequency_penalty": "按词频惩罚重复词，降低重复。",
    "presence_penalty": "按是否出现过惩罚，鼓励引入新话题。",
    "stop": "停止词，最多 4 个，用逗号分隔；命中即停止生成。",
}

# stop 序列限制（非数值型，单独约束）
STOP_MAX_ITEMS = 4
STOP_MAX_LEN = 32

# 请求模型 API 的超时时间（秒）
REQUEST_TIMEOUT = 60

# 模型 API 请求的网络层重试策略（应对 DNS 抖动、连接超时、瞬时 5xx / 限流）
REQUEST_MAX_RETRIES = 3          # 最多重试次数（含首次）
REQUEST_RETRY_BACKOFF = 1.0      # 重试指数退避基数（秒）：1s, 2s, 4s
# 哪些状态码需要重试（网络错误 / 连接问题总是重试；这里补充服务器端错误与限流）
REQUEST_RETRY_STATUS = (429, 500, 502, 503, 504)

# ------------------------- 向量记忆（长对话） -------------------------
# 开启后，/api/chat 发给模型的对话历史 = 最近 VECTOR_MEMORY_RECENT_N 轮对话
# + 向量检索 VECTOR_MEMORY_TOP_K 条（本地 sentence-transformers + SQLite，无需外部 API）。
# 「一轮」= 一次提问 + 一次回答；N 由用户自定义，不得超过当前会话总轮数。
# 前端在 /api/chat 请求里带 session_id 后才会真正生效。
VECTOR_MEMORY_ENABLED = True
VECTOR_MEMORY_DB = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "vector_memory.db"
)
VECTOR_MEMORY_DEVICE = "auto"  # auto / cuda / mps / cpu
VECTOR_MEMORY_RECENT_N = 10  # 最近 N 轮对话
VECTOR_MEMORY_TOP_K = 5
VECTOR_MEMORY_MIN_SCORE = 0.3

# 向量记忆关闭/不可用时：上下文最多保留的消息条数与总字符数，
# 超出的更早消息直接丢弃（system prompt 不在此限制内，始终保留）。
# 注意：字符数上限在 app.py 中会按所选模型上下文窗口动态估算（见 _no_vm_char_budget），
# 这里的 NO_VM_MAX_CHARS 仅作未传入参数时的兜底值；NO_VM_MAX_MESSAGES 为安全条数硬上限。
NO_VM_MAX_MESSAGES = 500
NO_VM_MAX_CHARS = 24000

# 各模型上下文窗口（token 数）。用于「关闭向量记忆」时，按所选模型 API 上下文窗口
# 动态估算历史可保留的字符数（而非固定 30 条/24000 字符）。
MODEL_CONTEXT_WINDOW = {
    "deepseek-chat": 65536,
    "deepseek-reasoner": 65536,
}
# 未在 MODEL_CONTEXT_WINDOW 配置的模型使用的默认上下文窗口（token 数）
NO_VM_DEFAULT_WINDOW = 65536
# token 到字符的保守估算：1 token ≈ 2 字符（中文场景偏保守，避免估算过头导致超出窗口报错）
CHARS_PER_TOKEN = 2.0
# 历史内容占「可用上下文」的比例，预留回复与 system prompt 及安全余量
NO_VM_CONTEXT_RATIO = 0.75

# ------------------------- 四层记忆（记忆设置面板） -------------------------
# ① 核心设定卡（用户导入，静态常驻，可跨会话复用）
# ② 动态关键事实（LLM 自动抽取 + 手动增删，随剧情增量更新）
# ③ 剧情摘要（LLM 切片分段滚动总结，达到阈值自动触发 + 手动按钮）
# ④ 向量记忆（按需召回细节，复用上方 VECTOR_MEMORY_* 配置）

# 记忆维护（事实抽取 / 切片总结 / 汇总）统一使用的轻量模型
MEMORY_MAINTENANCE_MODEL = "deepseek-chat"

# 用户侧「最近 N 轮对话」的默认值：新会话默认 0（全量模式，窗口能塞多少塞多少）。
# 注意与 VECTOR_MEMORY_RECENT_N（向量检索内部默认窗口）区分开。
MEMORY_RECENT_N_DEFAULT = 0

# ② 动态关键事实：上限条数（防止无限膨胀）
FACT_MAX = 50

# 关键事实切片抽取宽度（轮数）：自动/手动抽取统一按此宽度切片，
# 每片独立调用一次抽取，避免长对话一次性喂给模型（对齐 SUMMARY_SLICE_ROUNDS 的做法）
FACT_SLICE_ROUNDS = 8

# 每个切片最多抽取的事实条数（写入抽取提示词作为硬上限，0/负数 = 不限制）
FACT_MAX_PER_SLICE = 5

# ③ 剧情摘要
# 默认切片宽度 n：每次总结多少「轮」（一轮 = 一次提问 + 一次回答），可由用户设置。
# 自动总结以切片为单位：每积累一个完整切片（新增轮数 >= 切片宽度）触发一次
SUMMARY_SLICE_ROUNDS = 8
# 摘要最大字符数（超出的部分提示截断，避免摘要本身过长挤占上下文）
SUMMARY_MAX_CHARS = 2000

# 四层注入到上下文的总字符硬上限（防止卡片/事实/摘要过长挤占完整对话）
MEMORY_CONTEXT_MAX_CHARS = 12000
