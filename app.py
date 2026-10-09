"""Flask 后端入口。

接口一览：
  GET  /                       -> 返回前端页面 (static/index.html)
  GET  /api/models             -> 支持的模型列表
  GET  /api/params/default     -> 默认推理参数
  POST /api/chat               -> 对话（支持流式，前端用 fetch + ReadableStream 接收）
  GET  /api/sessions           -> 会话列表
  POST /api/sessions           -> 新建会话
  GET  /api/sessions/<id>      -> 单个会话内容
  DELETE /api/sessions/<id>    -> 删除会话
  POST /api/sessions/<id>/rename -> 重命名会话 ({"title": "..."})
  POST /api/sessions/cleanup   -> 删除空会话 ({"exclude": 当前会话id})
  POST /api/sessions/<id>/msg  -> 向会话追加一条消息（非流式，存历史用）
  DELETE /api/sessions/<id>/msg/<index> -> 删除会话中第 index 条消息（不含 system）
  POST /api/sessions/<id>/cards-item  -> 人物卡条目操作（add/update/delete）
    POST /api/sessions/<id>/worlds-item -> 世界卡条目操作（add/update/delete）
    POST /api/sessions/<id>/summary-slice/<slice_id> -> 编辑单个剧情切片（名称/摘要）
  GET  /api/presets            -> 参数预设列表
  POST /api/presets            -> 保存预设
  DELETE /api/presets/<name>   -> 删除预设
"""

import colorsys
import io
import json
import hmac
import os
import random
import re
import secrets
import string
import sys
import threading
import time
from datetime import timedelta

from flask import Flask, Response, jsonify, request, send_from_directory, session
from PIL import Image, ImageDraw, ImageFont
from werkzeug.middleware.proxy_fix import ProxyFix

import config
import llm_client
import memory_engine
import storage
import vector_memory

app = Flask(__name__, static_folder="static")
# 部署在 nginx 反向代理之后：信任其转发的 X-Forwarded-Proto / For / Host，
# 使 request.is_secure 与 Cookie 安全标记等能正确感知 HTTPS 请求。
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get("DPSKLITE_WEBUI_SECRET_KEY", config.SECRET_KEY)
app.config["SESSION_COOKIE_SECURE"] = config.SESSION_COOKIE_SECURE
# 持久化 session cookie：关闭浏览器 / 切换网络后仍保持登录 30 天
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)


@app.before_request
def make_session_persistent():
    # 让登录态跨浏览器会话 / 网络保持：session cookie 带 Max-Age（30 天），
    # 关闭浏览器或切换网络时不会丢失登录状态。
    session.permanent = True


@app.before_request
def protect_api():
    if request.path.startswith("/api/") and not request.path.startswith("/api/auth/") and "username" not in session:
        return jsonify({"error": "请先登录"}), 401


# ------------------------- 账号与验证码 -------------------------

@app.route("/api/auth/me")
def auth_me():
    return jsonify({"authenticated": "username" in session, "username": session.get("username")})


# 验证码字符集：剔除 0/O、1/I/L、2/Z、5/S、8/B 等易混淆字符
CAPTCHA_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CAPTCHA_LENGTH = 4
_CAPTCHA_FONT_CANDIDATES = [
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]


def _captcha_font(size):
    """加载粗体字体，找不到则回退到 Pillow 内置字体。"""
    for path in _CAPTCHA_FONT_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def _rand_color(rng, lo=30, hi=160):
    return (rng.randint(lo, hi), rng.randint(lo, hi), rng.randint(lo, hi))


def _char_color(rng):
    """深色调、低饱和、色相随机的字符色。

    用 HSV 控制：色相全色环随机（各字符颜色不同），但饱和度偏低、
    明度统一偏深。与浅色背景形成明度差，色盲用户也能区分字符与背景。
    """
    h = rng.random()
    s = rng.uniform(0.45, 0.7)
    v = rng.uniform(0.38, 0.55)
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return (int(r * 255), int(g * 255), int(b * 255))


def _render_captcha(code, width=220, height=64):
    """生成带彩色斑块背景、噪点、干扰曲线、字符旋转/抖动/随机大小的验证码图片。"""
    rng = random.Random()
    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img)
    # 彩色马赛克斑块背景：打散底色，压低字符与背景的对比度
    block = 8
    for y in range(0, height, block):
        for x in range(0, width, block):
            t = x / width
            base = (240 - int(10 * t), 243 - int(8 * t), 247 - int(14 * t))
            offset = rng.randint(-26, 26)
            fill = tuple(max(0, min(255, c + offset)) for c in base)
            draw.rectangle((x, y, x + block - 1, y + block - 1), fill=fill)
    # 随机噪点（颜色与字符相近，制造颗粒干扰）
    for _ in range(int(width * height * 0.12)):
        draw.point((rng.randint(0, width - 1), rng.randint(0, height - 1)),
                   fill=_rand_color(rng, 90, 200))
    # 贝塞尔干扰曲线
    for _ in range(5):
        color = _rand_color(rng, 100, 190)
        pts = [
            (rng.randint(-10, width // 4), rng.randint(0, height)),
            (rng.randint(width // 4, width // 2), rng.randint(0, height)),
            (rng.randint(width // 2, 3 * width // 4), rng.randint(0, height)),
            (rng.randint(3 * width // 4, width + 10), rng.randint(0, height)),
        ]
        draw.line(pts, fill=color, width=rng.randint(1, 2), joint="curve")
    # 随机小圆圈
    for _ in range(8):
        x, y = rng.randint(0, width), rng.randint(0, height)
        r = rng.randint(2, 5)
        draw.ellipse((x - r, y - r, x + r, y + r),
                     outline=_rand_color(rng, 110, 180), width=1)
    # 逐字符绘制：随机大小 / 旋转 / 上下抖动 / 深色调低饱和随机色
    step = width / (len(code) + 1)
    for i, ch in enumerate(code):
        size = rng.randint(27, 33)
        font = _captcha_font(size)
        bbox = draw.textbbox((0, 0), ch, font=font)
        mask = Image.new("L", (bbox[2] - bbox[0] + 10, bbox[3] - bbox[1] + 10), 0)
        ImageDraw.Draw(mask).text((5 - bbox[0], 5 - bbox[1]), ch, font=font, fill=255)
        mask = mask.rotate(rng.randint(-28, 28), expand=1, resample=Image.BICUBIC)
        cx = int(step * (i + 1) - mask.width / 2)
        cy = int(height / 2 - mask.height / 2 + rng.randint(-6, 6))
        img.paste(Image.new("RGB", mask.size, _char_color(rng)), (cx, cy), mask)
    return img


@app.route("/api/auth/captcha")
def auth_captcha():
    code = "".join(secrets.choice(CAPTCHA_CHARS) for _ in range(CAPTCHA_LENGTH))
    session["captcha"] = code
    buf = io.BytesIO()
    _render_captcha(code).save(buf, format="PNG")
    return Response(buf.getvalue(), mimetype="image/png",
                    headers={"Cache-Control": "no-store"})


def _auth_form():
    data = request.get_json(force=True, silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    captcha = str(data.get("captcha", "")).strip().upper()
    if not username or not password or not captcha:
        return None, (jsonify({"error": "请填写完整信息"}), 400)
    if len(captcha) != CAPTCHA_LENGTH:
        return None, (jsonify({"error": f"验证码需为 {CAPTCHA_LENGTH} 位"}), 400)
    if len(username) < 3 or len(username) > 32 or not username.replace("_", "").isalnum():
        return None, (jsonify({"error": "账号需为 3-32 位字母、数字或下划线"}), 400)
    if not hmac.compare_digest(captcha, session.pop("captcha", "")):
        return None, (jsonify({"error": "验证码错误或已过期"}), 400)
    return (username, password), None


@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    values, error = _auth_form()
    if error:
        return error
    username, password = values
    if len(password) < 6:
        return jsonify({"error": "密码至少 6 位"}), 400
    if not storage.create_user(username, password):
        return jsonify({"error": "账号已存在"}), 409
    session["username"] = username
    return jsonify({"authenticated": True, "username": username})


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    values, error = _auth_form()
    if error:
        return error
    username, password = values
    if not storage.verify_user(username, password):
        return jsonify({"error": "账号或密码错误"}), 401
    session["username"] = username
    return jsonify({"authenticated": True, "username": username})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/auth/password", methods=["POST"])
def auth_password():
    if "username" not in session:
        return jsonify({"error": "请先登录"}), 401
    data = request.get_json(force=True, silent=True) or {}
    current_password = str(data.get("current_password", ""))
    new_password = str(data.get("new_password", ""))
    if len(new_password) < 6:
        return jsonify({"error": "新密码至少 6 位"}), 400
    if current_password == new_password:
        return jsonify({"error": "新密码不能与当前密码相同"}), 400
    if not storage.change_password(session["username"], current_password, new_password):
        return jsonify({"error": "当前密码错误"}), 400
    return jsonify({"ok": True})


# ------------------------- 静态页面 -------------------------

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ------------------------- 模型与参数 -------------------------

@app.route("/api/models")
def api_models():
    models = [
        {"id": m["id"], "label": m["label"], "api_key_hint": m.get("api_key_hint", "")}
        for m in config.SUPPORTED_MODELS
    ]
    return jsonify(models)


@app.route("/api/params/default")
def api_default_params():
    return jsonify({
        "defaults": config.DEFAULT_PARAMS,
        "ranges": {k: list(v) for k, v in config.PARAM_RANGES.items()},
        "meta": config.PARAM_META,
        "stop_max_items": config.STOP_MAX_ITEMS,
        "stop_max_len": config.STOP_MAX_LEN,
    })


# ------------------------- 对话 -------------------------

def _validate_params(data):
    """校验并钳制参数到合法范围，返回清洗后的参数字典。"""
    out = {}
    for key, (lo, hi) in config.PARAM_RANGES.items():
        val = data.get(key, config.DEFAULT_PARAMS.get(key))
        try:
            val = float(val) if key != "max_tokens" else int(val)
        except (TypeError, ValueError):
            val = config.DEFAULT_PARAMS.get(key)
        out[key] = max(lo, min(hi, val))
    out["system_prompt"] = data.get("system_prompt", config.DEFAULT_PARAMS["system_prompt"])

    # stop 序列：逗号分隔，限制数量与单项长度
    raw_stop = data.get("stop", config.DEFAULT_PARAMS["stop"])
    if isinstance(raw_stop, str) and raw_stop.strip():
        items = [s.strip() for s in raw_stop.split(",") if s.strip()]
        items = [s[: config.STOP_MAX_LEN] for s in items[: config.STOP_MAX_ITEMS]]
        out["stop"] = items
    else:
        out["stop"] = []
    return out


@app.route("/api/vector-memory/models")
def api_vector_memory_models():
    """列出后端 models/ 目录下可用的向量化模型（用户选取后前端缓存）。"""
    return jsonify(vector_memory.list_available_models())


def _no_vm_char_budget(model, params):
    """估算「关闭向量记忆」时历史最多可占用的字符数。

    按所选模型 API 上下文窗口动态计算：可用 token = 上下文窗口 - 回复 max_tokens - system prompt，
    再换算为字符数并留出安全余量；未配置窗口的模型回退到默认值。
    """
    window = config.MODEL_CONTEXT_WINDOW.get(model or "", config.NO_VM_DEFAULT_WINDOW)
    max_tokens = int(params.get("max_tokens") or config.DEFAULT_PARAMS.get("max_tokens", 2048))
    sys_tokens = len(params.get("system_prompt") or "") / config.CHARS_PER_TOKEN
    avail_tokens = max(0, window - max_tokens - sys_tokens)
    return int(avail_tokens * config.CHARS_PER_TOKEN * config.NO_VM_CONTEXT_RATIO)


def _truncate_long(user_messages, model=None, params=None):
    """向量记忆关闭/不可用时：按所选模型上下文窗口动态保留最近对话（只留最近部分）。

    system prompt 单独组装，不在此列表中，因此绝不会被砍掉。
    """
    chat = [
        {"role": m["role"], "content": m["content"]}
        for m in user_messages
        if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()
    ]
    budget = _no_vm_char_budget(model, params) if params else config.NO_VM_MAX_CHARS
    while (sum(len(m["content"]) for m in chat) > budget
           or len(chat) > config.NO_VM_MAX_MESSAGES) and len(chat) > 1:
        chat.pop(0)
    return chat


def _build_chat_context(user_messages, sid, data, username=None, params=None):
    """拼接发给模型的对话上下文（四层记忆引擎）。

    - ① 核心设定（世界卡 + 人物卡）/ ② 动态关键事实 / ③ 剧情摘要：无条件常驻（会话 memory 列）。
    - ④ 向量记忆：按需召回细节（沿用旧前端 vector_memory 开关参数，兼容阶段二前的前端）。
    - 关闭 / 无 session_id：退化为纯最近窗口截断。
    """
    stored_msgs = None
    if username and sid:
        stored = storage.get_session(username, sid)
        if stored:
            stored_msgs = stored["messages"]
    return memory_engine.build_context(
        username,
        sid,
        user_messages,
        data=data,
        params=params,
        stored_msgs=stored_msgs,
    )


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json(force=True, silent=True) or {}
    api_key = data.get("api_key")
    model = data.get("model")
    user_messages = data.get("messages", [])  # 前端传来的对话上下文（不含 system）
    sid = data.get("session_id") or data.get("sessionId") or data.get("sid")

    if not api_key:
        return jsonify({"error": "缺少 api_key"}), 400
    if not model:
        return jsonify({"error": "缺少 model"}), 400
    if not user_messages:
        return jsonify({"error": "消息为空"}), 400

    params = _validate_params(data)

    # 在请求上下文内捕获 username（生成器在流式迭代时请求上下文可能已销毁，
    # 因此不能在生成器内部访问 flask.session）
    current_username = session.get("username")

    # 组装完整消息：system prompt 在前（为空时不注入 system 消息）
    try:
        context = _build_chat_context(user_messages, sid, data, current_username, params)
    except vector_memory.VectorMemoryError as e:
        app.logger.error("向量记忆不可用：%s", e)
        return jsonify({"error": f"向量记忆不可用：{e}", "vector_memory_error": True}), 502
    sys_prompt = (params.get("system_prompt") or "").strip()
    messages = ([{"role": "system", "content": sys_prompt}] if sys_prompt else []) + context

    # 调试日志：打印实际发给 LLM 的完整上下文（含四层记忆注入），方便边跑边验证
    try:
        _log_context(sid, messages)
    except Exception:  # noqa: BLE001
        pass

    # 流式响应：用 text/event-stream 把每个片段推给前端
    def generate():
        try:
            for piece in llm_client.chat(
                api_key=api_key,
                model=model,
                messages=messages,
                temperature=params["temperature"],
                top_p=params["top_p"],
                max_tokens=params["max_tokens"],
                frequency_penalty=params["frequency_penalty"],
                presence_penalty=params["presence_penalty"],
                stop=params["stop"],
                stream=True,
            ):
                yield piece
        except RuntimeError as e:
            yield f"\n[错误] {e}"
        finally:
            # 流式结束后触发记忆维护。注意：必须放到后台线程执行，
            # 因为事实抽取 / 剧情总结会发起真实的 LLM 调用（耗时可能数十秒），
            # 若在 finally 里同步执行会阻塞流式响应收尾，前端会一直等连接关闭。
            # 使用已捕获的 current_username 闭包变量，不访问 flask.session。
            if sid and current_username:
                _spawn_memory_maintenance(api_key, sid, current_username)

    return Response(generate(), mimetype="text/plain; charset=utf-8")


def _run_memory_maintenance(api_key, sid, username):
    """对话结束后触发记忆维护：事实抽取 + 自动剧情总结。异常在调用方捕获。

    注意：该函数在流式响应收尾时调用，此时请求上下文可能已销毁，
    因此 username 必须由调用方在上下文内捕获后传入，不能在内部访问 flask.session。
    """
    stored = storage.get_session(username, sid)
    stored_msgs = stored["messages"] if stored else []
    # ① 动态关键事实：每次对话后增量抽取（失败不阻塞）
    try:
        memory_engine.extract_facts_for_session(api_key, username, sid, stored_msgs)
    except Exception:  # noqa: BLE001
        app.logger.exception("动态关键事实抽取失败")
    # ② 剧情摘要：文本线达到阈值自动总结（N 轮滚动合并）；切片线独立增量落袋（无新片零调用）
    try:
        if memory_engine.should_auto_summary(username, sid, stored_msgs):
            memory_engine.run_summary(api_key, username, sid, stored_msgs)
        memory_engine.run_summary_slices(api_key, username, sid, stored_msgs)
    except Exception:  # noqa: BLE001
        app.logger.exception("剧情摘要自动总结失败")


# 记忆维护后台线程的去重集合：记录正在维护中的 (username, sid)，避免并发重复触发
_memory_maintenance_lock = threading.Lock()
_memory_maintenance_active = set()


def _spawn_memory_maintenance(api_key, sid, username):
    """在后台线程执行记忆维护，不阻塞流式响应收尾。

    用 set 去重：同一会话的维护任务已在运行时不重复启动，防止并发重复总结。
    线程为 daemon，随进程退出，不阻塞服务器关闭。
    """
    key = (username, sid)
    with _memory_maintenance_lock:
        if key in _memory_maintenance_active:
            return
        _memory_maintenance_active.add(key)

    def worker():
        try:
            _run_memory_maintenance(api_key, sid, username)
        except Exception:  # noqa: BLE001
            app.logger.exception("记忆维护失败")
        finally:
            with _memory_maintenance_lock:
                _memory_maintenance_active.discard(key)

    t = threading.Thread(target=worker, daemon=True, name=f"mem-maint-{sid[:8]}")
    t.start()


def _log_context(sid, messages):
    """把实际发给 LLM 的完整上下文打印到控制台，便于观察记忆注入效果。"""
    if not config.LOG_CONTEXT:
        return
    total_chars = sum(len(m.get("content", "")) for m in messages)
    print("\n" + "=" * 72, flush=True)
    print(f"[上下文] session={sid or '-'} 共 {len(messages)} 条，总字符 {total_chars}", flush=True)
    print("-" * 72, flush=True)

    # 分段打印：标记各记忆层边界，重点划清「向量记忆（第④层）」的范围
    section = None  # 当前所在区块：card/facts/summary/vector/recent/plain
    for i, m in enumerate(messages):
        role = str(m.get("role", "?")).ljust(9)
        content = m.get("content", "")
        src = m.get("_src")

        # 根据 _src 或内容判断当前区块
        if src == "vector":
            cur = "vector"
        elif src == "recent":
            cur = "recent"
        elif content.startswith("【世界设定"):
            cur = "world"
        elif content.startswith("【核心设定") or content.startswith("【角色设定"):
            cur = "card"
        elif content.startswith("【当前剧情中的关键事实"):
            cur = "facts"
        elif content.startswith("【剧情摘要"):
            cur = "summary"
        else:
            cur = "plain"

        # 区块切换时打印分界标题
        if cur != section:
            if section is not None:
                # 上一个区块结束
                print("  " + "-" * 68, flush=True)
            headers = {
                "world":  "▼ ① 核心设定·世界卡（无条件常驻）",
                "card":   "▼ ① 核心设定·人物卡（无条件常驻）",
                "facts":  "▼ ② 动态关键事实（无条件常驻）",
                "summary":"▼ ③ 剧情摘要（无条件常驻）",
                "vector": "▼ ④ 向量记忆检索片段（按需召回）",
                "recent": "▼ 最近对话窗口",
                "plain":  "▼ 对话内容",
            }
            print(f"  {headers.get(cur, cur)}", flush=True)
            section = cur

        # 给向量检索片段加行内标记，进一步划清范围
        tag = ""
        if cur == "vector":
            tag = " [向量检索]"
        elif cur == "recent":
            tag = " [最近对话]"

        # 打印完整文本（不做字符截断）；多行内容用「 / 」标记合并，便于单行阅读
        full = content.replace("\n", " / ")
        print(f"  {i:>2} [{role}]{tag} {full}", flush=True)
    print("=" * 72, flush=True)


# ------------------------- 四层记忆 API -------------------------

@app.route("/api/sessions/<sid>/memory", methods=["GET", "POST"])
def api_session_memory(sid):
    """读取 / 整体覆盖会话的四层记忆结构。"""
    username = session["username"]
    if request.method == "GET":
        mem = storage.get_session_memory(username, sid)
        if mem is None:
            return jsonify({"error": "会话不存在"}), 404
        return jsonify({"memory": mem})
    data = request.get_json(force=True, silent=True) or {}
    saved = storage.save_session_memory(username, sid, data.get("memory"))
    if saved is None:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify({"memory": saved["memory"]})


@app.route("/api/sessions/<sid>/cards-item", methods=["POST"])
def api_cards_item(sid):
    """人物卡列表条目操作（多角色，一角色一卡）。

    body: {"op": "add|update|delete|set-main", "id": "...", "name": "...", "content": "..."}
    add：新建卡（name/content 可为空）；update：按 id 更新 name/content（传哪个更新哪个）；
    delete：按 id 删除；set-main：按 id 设为主角色卡（AI 第一人称扮演的角色）。返回完整 memory。
    """
    username = session["username"]
    data = request.get_json(force=True, silent=True) or {}
    op = str(data.get("op") or "")
    if op not in ("add", "update", "delete", "set-main"):
        return jsonify({"error": "无效操作"}), 400
    mem = storage.set_session_cards_item(
        username, sid, op,
        card_id=str(data.get("id") or "") or None,
        name=data.get("name"),
        content=data.get("content"),
    )
    if mem is None:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify({"memory": mem})


@app.route("/api/sessions/<sid>/worlds-item", methods=["POST"])
def api_worlds_item(sid):
    """世界卡列表条目操作（多张，一卡一世界观设定，先于人物卡注入）。

    body: {"op": "add|update|delete", "id": "...", "name": "...", "content": "..."}
    add：新建卡（name/content 可为空）；update：按 id 更新 name/content（传哪个更新哪个）；
    delete：按 id 删除。返回完整 memory。
    """
    username = session["username"]
    data = request.get_json(force=True, silent=True) or {}
    op = str(data.get("op") or "")
    if op not in ("add", "update", "delete"):
        return jsonify({"error": "无效操作"}), 400
    mem = storage.set_session_worlds_item(
        username, sid, op,
        world_id=str(data.get("id") or "") or None,
        name=data.get("name"),
        content=data.get("content"),
    )
    if mem is None:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify({"memory": mem})


@app.route("/api/sessions/<sid>/facts", methods=["POST"])
def api_set_facts(sid):
    """整体覆盖动态关键事实。body: {"facts": [{"text": "...", "ts": ..., "locked": bool}]}。"""
    username = session["username"]
    data = request.get_json(force=True, silent=True) or {}
    facts = data.get("facts")
    if not isinstance(facts, list):
        return jsonify({"error": "facts 需为数组"}), 400
    mem = storage.set_session_facts(username, sid, facts)
    if mem is None:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify({"memory": mem})


@app.route("/api/sessions/<sid>/facts-item", methods=["POST"])
def api_facts_item(sid):
    """关键事实条目级操作（增删改 / 上锁解锁）。

    body: {"op": "add"|"update"|"delete"|"lock"|"move", "index": int, "text": str, "locked": bool, "to": int}
    - add:    追加一条 {"text", "locked"}（index 忽略）
    - update: 修改 index 处文本（上锁条目也可手动改，锁只防重新生成）
    - delete: 删除 index 处条目（上锁条目也可手动删）
    - lock:   设置 index 处 locked 标记（locked=true 上锁 / false 解锁）
    - move:   把 index 处条目移动到 to 处（to 为新列表中的目标下标，先弹出再插入；
              拖动排序与 ↑↓ 按钮均基于此实现）
    """
    username = session["username"]
    data = request.get_json(force=True, silent=True) or {}
    op = data.get("op")
    mem = storage.get_session_memory(username, sid)
    if mem is None:
        return jsonify({"error": "会话不存在"}), 404
    facts = [dict(f) for f in (mem.get("facts") or [])]
    text = str(data.get("text") or "").strip()
    try:
        idx = int(data.get("index"))
    except (TypeError, ValueError):
        idx = -1
    in_range = 0 <= idx < len(facts)

    if op == "add":
        if not text:
            return jsonify({"error": "内容不能为空"}), 400
        facts.append({"text": text, "ts": time.time(), "locked": bool(data.get("locked", False))})
    elif op == "update":
        if not in_range:
            return jsonify({"error": "索引越界"}), 400
        if not text:
            return jsonify({"error": "内容不能为空"}), 400
        facts[idx]["text"] = text
        facts[idx]["ts"] = time.time()
    elif op == "delete":
        if not in_range:
            return jsonify({"error": "索引越界"}), 400
        facts.pop(idx)
    elif op == "lock":
        if not in_range:
            return jsonify({"error": "索引越界"}), 400
        facts[idx]["locked"] = bool(data.get("locked", True))
    elif op == "move":
        if not in_range:
            return jsonify({"error": "索引越界"}), 400
        try:
            to = int(data.get("to"))
        except (TypeError, ValueError):
            to = idx  # 缺省视为不动
        # to 为新列表中的目标下标：先弹出再插入；越界就近收拢到两端
        if to < 0:
            to = 0
        elif to >= len(facts):
            to = len(facts) - 1
        item = facts.pop(idx)
        facts.insert(to, item)
    else:
        return jsonify({"error": "未知操作"}), 400

    mem = storage.set_session_facts(username, sid, facts)
    if mem is None:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify({"memory": mem})


@app.route("/api/sessions/<sid>/summary", methods=["POST"])
def api_run_summary(sid):
    """手动触发一次剧情摘要（文本线）。body: {"slice_rounds": int, "full": bool}。
    full=True（重新总结）：忽略上次总结点与旧摘要文本，从全部历史重新生成并覆盖
    整份摘要文本；不读写切片实体（切片与文本摘要完全独立）。"""
    username = session["username"]
    data = request.get_json(force=True, silent=True) or {}
    api_key = str(data.get("api_key") or "").strip()
    if not api_key:
        return jsonify({"error": "缺少 api_key"}), 400
    stored = storage.get_session(username, sid)
    if not stored:
        return jsonify({"error": "会话不存在"}), 404
    # 剧情摘要开关关闭：手动总结也拒绝
    mem = storage.get_session_memory(username, sid) or {}
    if not (mem.get("summary") or {}).get("enabled", True):
        return jsonify({"error": "剧情摘要已关闭，无法手动总结"}), 400
    slice_rounds = data.get("slice_rounds")
    try:
        slice_rounds = int(slice_rounds) if slice_rounds else None
    except (TypeError, ValueError):
        slice_rounds = None
    result = memory_engine.run_summary(
        api_key, username, sid, stored["messages"],
        slice_rounds=slice_rounds, full=bool(data.get("full")),
    )
    if not result["ok"]:
        return jsonify({"error": result.get("error", "总结失败")}), 502
    return jsonify(result)


@app.route("/api/sessions/<sid>/summary-config", methods=["GET", "POST"])
def api_summary_config(sid):
    """读取 / 设置会话剧情摘要的触发参数。

    GET 返回当前配置；POST body 可选 {"slice_rounds": int, "enabled": bool,
    "max_slice_rounds": int（锚点段保留上限，轮）}，传哪个更新哪个。
    自动总结以切片为单位：存在已闭合的未总结切片时触发。
    """
    username = session["username"]
    if request.method == "GET":
        mem = storage.get_session_memory(username, sid)
        if mem is None:
            return jsonify({"error": "会话不存在"}), 404
        s = mem.get("summary") or {}
        return jsonify({
            "slice_rounds": s.get("slice_rounds"),
            "max_slice_rounds": s.get("max_slice_rounds", config.SUMMARY_MAX_SEGMENT_ROUNDS),
            "enabled": s.get("enabled", True),
        })
    data = request.get_json(force=True, silent=True) or {}
    slice_rounds = data.get("slice_rounds")
    max_slice_rounds = data.get("max_slice_rounds")
    enabled = data.get("enabled")
    if slice_rounds is None and enabled is None and max_slice_rounds is None:
        return jsonify({"error": "至少提供 slice_rounds / max_slice_rounds / enabled 之一"}), 400
    mem = storage.set_session_summary_config(
        username, sid, slice_rounds=slice_rounds, enabled=enabled, max_slice_rounds=max_slice_rounds
    )
    if mem is None:
        return jsonify({"error": "会话不存在"}), 404
    s = mem.get("summary") or {}
    return jsonify({
        "slice_rounds": s.get("slice_rounds"),
        "max_slice_rounds": s.get("max_slice_rounds", config.SUMMARY_MAX_SEGMENT_ROUNDS),
        "enabled": s.get("enabled", True),
    })


@app.route("/api/sessions/<sid>/facts-config", methods=["GET", "POST"])
def api_facts_config(sid):
    """读取 / 设置动态关键事实的抽取配置。

    GET 返回当前配置；POST body 可选 {"slice_rounds": int（1~200 轮）,
    "max_per_slice": int（每片最多抽取条数，0 = 不限制）}。
    """
    username = session["username"]
    if request.method == "GET":
        mem = storage.get_session_memory(username, sid)
        if mem is None:
            return jsonify({"error": "会话不存在"}), 404
        return jsonify({
            "slice_rounds": mem.get("facts_slice_rounds", config.FACT_SLICE_ROUNDS),
            "max_per_slice": mem.get("facts_max_per_slice", config.FACT_MAX_PER_SLICE),
        })
    data = request.get_json(force=True, silent=True) or {}
    slice_rounds = data.get("slice_rounds")
    max_per_slice = data.get("max_per_slice")
    if slice_rounds is None and max_per_slice is None:
        return jsonify({"error": "至少提供 slice_rounds / max_per_slice 之一"}), 400
    mem = storage.set_session_facts_config(
        username, sid, slice_rounds=slice_rounds, max_per_slice=max_per_slice
    )
    if mem is None:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify({
        "slice_rounds": mem.get("facts_slice_rounds", config.FACT_SLICE_ROUNDS),
        "max_per_slice": mem.get("facts_max_per_slice", config.FACT_MAX_PER_SLICE),
    })


@app.route("/api/sessions/<sid>/summary-text", methods=["POST"])
def api_summary_text(sid):
    """手动保存编辑后的剧情摘要文本。

    body: {"text": str}；空文本视为清除摘要。总结点（last_round）与各项配置保留。
    """
    username = session["username"]
    data = request.get_json(force=True, silent=True) or {}
    mem = storage.set_session_summary(username, sid, str(data.get("text") or ""))
    if mem is None:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify({"memory": mem})


@app.route("/api/sessions/<sid>/summary-slice/<slice_id>", methods=["POST"])
def api_summary_slice(sid, slice_id):
    """编辑单个剧情切片实体（切片管理面板入口）。

    body 可选 {"name": str, "text": str}，传哪个更新哪个；更新后该片标记 edited，
    整份摘要 text 重派生。改名时若该片起点轮次带场景锚点，同步更新锚点
    scene_label（前端分割线显示名跟随）。返回 {"memory", "slice"}。
    """
    username = session["username"]
    data = request.get_json(force=True, silent=True) or {}
    name = data.get("name") if "name" in data else None
    text = data.get("text") if "text" in data else None
    if name is None and text is None:
        return jsonify({"error": "至少提供 name / text 之一"}), 400
    result = storage.update_summary_slice(username, sid, slice_id, name=name, text=text)
    if result is None:
        return jsonify({"error": "会话不存在或切片不存在"}), 404
    # 改名同步：该片起点轮次上的锚点 scene_label 跟随更新（无锚点则跳过）
    if name is not None:
        storage.rename_scene_anchor(username, sid, result["slice"]["start_round"], name)
    return jsonify({"memory": result["memory"], "slice": result["slice"]})


@app.route("/api/sessions/<sid>/memory-switches", methods=["POST"])
def api_memory_switches(sid):
    """批量设置记忆卡片开关（2/3/4 层）与数值恢复，供「一键配置 / 关闭智能总结 / 单卡开关」调用。

    body 可选：
      facts_enabled:   bool  ② 动态关键事实开关（卡片总开关）
      facts_auto:      bool  ② 自动总结开关（总开关下一级；关闭时后台不自动抽取）
      summary_enabled: bool  ③ 剧情摘要开关
      summary_auto:    bool  ③ 自动总结开关（总开关下一级；关闭时后台不自动总结）
      vector_enabled:  bool  ④ 向量记忆开关
      reset_values:    bool  一键配置：恢复数值默认（最近 N 轮=10、摘要切片/自动间隔、向量 TopK/recent_n）
    返回完整 memory 结构。
    """
    username = session["username"]
    data = request.get_json(force=True, silent=True) or {}
    if not any(k in data for k in ("facts_enabled", "facts_auto", "summary_enabled", "summary_auto", "vector_enabled", "reset_values")):
        return jsonify({"error": "缺少开关参数"}), 400
    mem = storage.set_session_memory_switches(
        username,
        sid,
        facts_enabled=data.get("facts_enabled"),
        facts_auto=data.get("facts_auto"),
        summary_enabled=data.get("summary_enabled"),
        summary_auto=data.get("summary_auto"),
        vector_enabled=data.get("vector_enabled"),
        reset_values=bool(data.get("reset_values")),
    )
    if mem is None:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify({"memory": mem})


@app.route("/api/sessions/<sid>/vector-config", methods=["GET", "POST"])
def api_vector_config(sid):
    """读取 / 设置会话向量记忆配置.

    GET 返回当前配置（enabled/model/recent_n/top_k）。
    POST body 可选 {"enabled": bool, "model": str, "recent_n": int, "top_k": int}，
    传哪个更新哪个。

    top_k 语义：0 = 不限制数量（自动按相似度阈值召回）；>0 = 固定返回 N 条；
    不传 / 传 None = 使用全局默认。响应里用 "auto" 表示自动召回（即配置值为 0）。
    """
    username = session["username"]
    if request.method == "GET":
        mem = storage.get_session_memory(username, sid)
        if mem is None:
            return jsonify({"error": "会话不存在"}), 404
        v = mem.get("vector") or {}
        return jsonify({
            "enabled": bool(v.get("enabled")),
            "model": v.get("model"),
            "recent_n": mem.get("recent_n", v.get("recent_n")),
            "top_k": v.get("top_k"),
            "auto": v.get("top_k") == 0,  # 是否为自动召回
        })
    data = request.get_json(force=True, silent=True) or {}
    # 支持 "auto": true 快捷方式 -> top_k=0
    top_k = data.get("top_k")
    if data.get("auto") is True:
        top_k = 0
    elif "top_k" not in data:
        top_k = storage._UNSET  # 未传 top_k：不更新该字段
    mem = storage.set_session_vector_config(
        username, sid,
        top_k=top_k,
        recent_n=data.get("recent_n"),
        enabled=data.get("enabled"),
        model=data.get("model"),
    )
    if mem is None:
        return jsonify({"error": "会话不存在"}), 404
    v = mem.get("vector") or {}
    return jsonify({
        "enabled": bool(v.get("enabled")),
        "model": v.get("model"),
        "recent_n": mem.get("recent_n", v.get("recent_n")),
        "top_k": v.get("top_k"),
        "auto": v.get("top_k") == 0,
    })


@app.route("/api/sessions/<sid>/context-config", methods=["GET", "POST"])
def api_context_config(sid):
    """读取 / 设置独立上下文保留策略 N。N=0 表示全量模式。"""
    username = session["username"]
    mem = storage.get_session_memory(username, sid)
    if mem is None:
        return jsonify({"error": "会话不存在"}), 404
    if request.method == "GET":
        return jsonify({"recent_n": mem.get("recent_n", (mem.get("vector") or {}).get("recent_n", config.VECTOR_MEMORY_RECENT_N))})
    data = request.get_json(force=True, silent=True) or {}
    if "recent_n" not in data:
        return jsonify({"error": "缺少 recent_n"}), 400
    mem = storage.set_session_recent_n(username, sid, data.get("recent_n"))
    if mem is None:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify({"recent_n": mem.get("recent_n", config.VECTOR_MEMORY_RECENT_N)})


@app.route("/api/sessions/<sid>/facts-refresh", methods=["POST"])
def api_refresh_facts(sid):
    """手动触发一次动态关键事实抽取。body: {"api_key": "...", "full": bool}。
    full=True（重新总结）：用全部历史重新抽取并合并覆盖；否则只增量处理最近片段。"""
    username = session["username"]
    data = request.get_json(force=True, silent=True) or {}
    api_key = str(data.get("api_key") or "").strip()
    if not api_key:
        return jsonify({"error": "缺少 api_key"}), 400
    stored = storage.get_session(username, sid)
    if not stored:
        return jsonify({"error": "会话不存在"}), 404
    new_facts = memory_engine.extract_facts_for_session(
        api_key, username, sid, stored["messages"], full=bool(data.get("full")), auto=False
    )
    if new_facts is None:
        return jsonify({"error": "抽取失败或没有新增事实"}), 502
    return jsonify({"facts": new_facts})


@app.route("/api/sessions/<sid>/refresh", methods=["POST"])
def api_refresh_memory(sid):
    """一键手动刷新全部记忆层：动态关键事实抽取 + 剧情摘要总结。

    与自动维护不同，手动刷新会【强制】执行两个动作（不依赖自动触发阈值），
    把会话的记忆同步到当前对话状态。body: {"api_key": "..."}。

    返回 {facts: 更新后的事实列表或 null, summary: {...} 或 null, errors: [...]}。
    单层失败不阻塞另一层，失败信息放入 errors。
    """
    username = session["username"]
    data = request.get_json(force=True, silent=True) or {}
    api_key = str(data.get("api_key") or "").strip()
    if not api_key:
        return jsonify({"error": "缺少 api_key"}), 400
    stored = storage.get_session(username, sid)
    if not stored:
        return jsonify({"error": "会话不存在"}), 404

    errors = []
    facts_result = None
    summary_result = None

    # ① 动态关键事实：强制增量抽取（用户手动触发，不受自动总结开关影响）
    try:
        new_facts = memory_engine.extract_facts_for_session(
            api_key, username, sid, stored["messages"], auto=False
        )
        if new_facts is not None:
            facts_result = new_facts
    except Exception as e:  # noqa: BLE001
        app.logger.exception("手动刷新：事实抽取失败")
        errors.append(f"事实抽取失败: {e}")

    # ② 剧情摘要：强制增量总结（手动刷新不依赖自动阈值；文本线 + 切片线独立运行）
    try:
        r = memory_engine.run_summary(api_key, username, sid, stored["messages"])
        if r.get("ok"):
            summary_result = r.get("summary")
        else:
            errors.append(r.get("error", "总结失败"))
    except Exception as e:  # noqa: BLE001
        app.logger.exception("手动刷新：剧情总结失败")
        errors.append(f"剧情总结失败: {e}")
    # 切片线：增量落袋新切片（与文本摘要独立）
    try:
        memory_engine.run_summary_slices(api_key, username, sid, stored["messages"])
    except Exception as e:  # noqa: BLE001
        app.logger.exception("手动刷新：切片总结失败")
        errors.append(f"切片总结失败: {e}")

    return jsonify({
        "facts": facts_result,
        "summary": summary_result,
        "errors": errors,
    })


# ------------------------- 会话管理 -------------------------

@app.route("/api/sessions")
def api_list_sessions():
    return jsonify(storage.list_sessions(session["username"]))


@app.route("/api/sessions", methods=["POST"])
def api_create_session():
    data = request.get_json(force=True, silent=True) or {}
    title = data.get("title")
    model = data.get("model")
    params = data.get("params")
    vm = data.get("vm")
    return jsonify(storage.create_session(session["username"], title, model=model, params=params, vm=vm))


@app.route("/api/sessions/<sid>/config", methods=["POST"])
def api_update_session_config(sid):
    data = request.get_json(force=True, silent=True) or {}
    saved_session = storage.update_session_config(
        session["username"],
        sid,
        model=data.get("model"),
        params=data.get("params"),
        vm=data.get("vm"),
        memory=data.get("memory"),
    )
    if saved_session is None:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify(saved_session)


@app.route("/api/sessions/import", methods=["POST"])
def api_import_session():
    """按导出的 JSON 结构新建一个完整会话（与导出功能对应）。"""
    data = request.get_json(force=True, silent=True) or {}
    s = storage.import_session(session["username"], data)
    return jsonify(s)


@app.route("/api/sessions/<sid>")
def api_get_session(sid):
    s = storage.get_session(session["username"], sid)
    if s is None:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify(s)


@app.route("/api/sessions/<sid>/messages")
def api_get_session_messages(sid):
    """分页读取会话消息（长对话按需加载）。limit 为单次条数；before 为消息下标锚点，
    返回该锚点之前最近的 limit 条；不传 before 时返回最新的 limit 条。"""
    try:
        limit = max(1, min(int(request.args.get("limit", 30)), 200))
    except (TypeError, ValueError):
        limit = 30
    before = request.args.get("before")
    if before is not None:
        try:
            before = max(0, int(before))
        except (TypeError, ValueError):
            before = None
    data = storage.get_session_messages(session["username"], sid, limit=limit, before=before)
    if data is None:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify(data)


@app.route("/api/sessions/<sid>", methods=["DELETE"])
def api_delete_session(sid):
    storage.delete_session(session["username"], sid)
    return jsonify({"ok": True})


@app.route("/api/sessions/<sid>/rename", methods=["POST"])
def api_rename_session(sid):
    data = request.get_json(force=True, silent=True) or {}
    title = data.get("title", "").strip()
    if not title:
        return jsonify({"error": "缺少 title"}), 400
    saved_session = storage.rename_session(session["username"], sid, title)
    if saved_session is None:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify(saved_session)


@app.route("/api/sessions/cleanup", methods=["POST"])
def api_cleanup_sessions():
    # 删除空会话，可排除当前正在查看的那个
    exclude = (request.get_json(force=True, silent=True) or {}).get("exclude")
    removed = storage.cleanup_empty_sessions(session["username"], exclude_id=exclude)
    return jsonify({"removed": removed})


@app.route("/api/sessions/<sid>/msg/<int:index>", methods=["DELETE"])
def api_delete_msg(sid, index):
    """删除会话中指定下标的消息（从 0 开始，仅统计 user/assistant 消息，不含 system）。"""
    saved_session = storage.delete_message(session["username"], sid, index)
    if saved_session is None:
        return jsonify({"error": "会话不存在或下标越界"}), 404
    return jsonify(saved_session)


@app.route("/api/sessions/<sid>/msg/<int:index>/scene-break", methods=["POST"])
def api_scene_break(sid, index):
    """设置 / 清除指定 user 消息上的场景锚点（手动分场标记）。

    body: {"scene_break": bool, "scene_label": str}。
    scene_break=True 打锚点（scene_label 可空，空则前端显示默认「第 N 场」）；
    scene_break=False 清除锚点。锚点只允许打在 user 消息（轮首）上。
    """
    username = session["username"]
    data = request.get_json(force=True, silent=True) or {}
    scene_break = bool(data.get("scene_break"))
    scene_label = data.get("scene_label")
    message = storage.set_message_scene_break(
        username, sid, index, scene_break=scene_break, scene_label=scene_label
    )
    if message is None:
        return jsonify({"error": "会话不存在、下标越界或该消息不是用户消息"}), 404
    return jsonify({"ok": True, "message": message})


@app.route("/api/sessions/<sid>/msg", methods=["POST"])
def api_append_msg(sid):
    data = request.get_json(force=True, silent=True) or {}
    role = data.get("role")
    content = data.get("content", "")
    reasoning = data.get("reasoning")
    files = data.get("files")
    interrupted = data.get("interrupted")
    if role not in ("user", "assistant"):
        return jsonify({"error": "role 必须为 user 或 assistant"}), 400
    saved_session = storage.append_message(
        session["username"], sid, role, content, reasoning=reasoning, files=files, interrupted=interrupted
    )
    return jsonify(saved_session)


# 默认标题格式：create_session 生成的 "会话 MM-DD HH:MM"
_DEFAULT_TITLE_RE = re.compile(r"^会话 \d{2}-\d{2} \d{2}:\d{2}$")


def _is_default_title(title):
    return bool(_DEFAULT_TITLE_RE.match(title or ""))


@app.route("/api/sessions/<sid>/auto-title", methods=["POST"])
def api_auto_title(sid):
    data = request.get_json(force=True, silent=True) or {}
    api_key = str(data.get("api_key", "")).strip()
    system_prompt = data.get("system_prompt", config.DEFAULT_PARAMS["system_prompt"])
    current_user = session["username"]
    title_status = storage.auto_title_status(current_user, sid)
    if title_status is None:
        return jsonify({"error": "会话不存在"}), 404
    # 以标题是否仍为默认格式作为唯一判据：非默认标题（用户已手动命名或已自动生成过）
    # 一律直接返回，不重新生成，防止覆盖自定义标题（不依赖 auto_title_generated 标志，
    # 避免旧数据中标志为 0 的手动命名会话被误覆盖）。
    # generated=1 但标题仍是默认格式（历史脏数据）时继续生成实现自愈。
    if not _is_default_title(title_status["title"]):
        return jsonify({"title": title_status["title"]})
    saved_session = storage.get_session(current_user, sid)
    if not api_key or not saved_session:
        return jsonify({"error": "参数不完整或会话不存在"}), 400
    # 标题生成固定使用轻量 chat 模型，不跟随 UI 选中模型，
    # 避免 reasoner 因 token 预算输不出正文、OpenAI 模型等导致的生成失败
    model = config.AUTO_TITLE_MODEL
    messages = [m for m in saved_session["messages"] if m.get("role") in ("user", "assistant")]
    if len(messages) < 2 or messages[0]["role"] != "user" or messages[1]["role"] != "assistant":
        return jsonify({"error": "首轮对话尚未完成"}), 400
    prompt = (
        "请根据系统提示词和下面的第一轮问答，为这个会话生成一个简洁标题。"
        "只返回标题文字，不要引号、标点或解释，最多10个字。\n"
        f"用户：{messages[0].get('content', '')}\n助手：{messages[1].get('content', '')}"
    )
    try:
        title = llm_client.chat(
            api_key=api_key,
            model=model,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
            temperature=0.2,
            top_p=1.0,
            # 推理模型（reasoner）的思考过程也计入 max_tokens，32 容易输不出正文
            max_tokens=1024,
            stream=False,
        )
    except (RuntimeError, ValueError, KeyError, IndexError):
        return jsonify({"error": "标题生成失败"}), 502
    title = "".join(str(title or "").replace("\n", " ").split()).strip(" \"'`。，、：:；;！!？?《》[]()（）")[:10]
    if not title:
        return jsonify({"error": "标题为空"}), 502
    updated = storage.set_auto_title(current_user, sid, title)
    if updated is None:
        # 并发下已被其他请求生成：返回最新标题
        latest = storage.auto_title_status(current_user, sid)
        return jsonify({"title": latest["title"] if latest else title})
    return jsonify(updated)


# ------------------------- 预设管理 -------------------------

@app.route("/api/presets")
def api_list_presets():
    return jsonify(storage.list_presets())


@app.route("/api/presets", methods=["POST"])
def api_save_preset():
    data = request.get_json(force=True, silent=True) or {}
    name = data.get("name")
    params = data.get("params")
    if not name:
        return jsonify({"error": "缺少 name"}), 400
    if not params:
        return jsonify({"error": "缺少 params"}), 400
    preset = storage.save_preset(name, params)
    return jsonify(preset)


@app.route("/api/presets/<name>", methods=["DELETE"])
def api_delete_preset(name):
    storage.delete_preset(name)
    return jsonify({"ok": True})


if __name__ == "__main__":
    # Windows 控制台默认 GBK，重配置为 UTF-8，保证上下文日志里的中文/特殊字符正常打印
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    # 生产环境使用 waitress（支持 Windows 的多线程 WSGI 服务器）。
    # 关闭 debug 重载器：waitress 不支持 werkzeug 的 reloader。
    from waitress import serve
    # _quiet=False 让 waitress 实时打印访问日志（IP、方法、路径、状态码）。
    # threads 提高并发上限，缓解流式对话占用线程导致的请求排队（Task queue depth 告警）。
    serve(app, host=config.HOST, port=config.PORT, _quiet=False, threads=16)
