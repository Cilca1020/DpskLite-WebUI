# DpskLite-WebUI

一个网页端程序，用于与 DeepSeek 等兼容 OpenAI 协议的大模型对话，支持便捷的参数调整与多层记忆管理，适合长对话与角色扮演场景。

测试网页：https://dpsklite-webui.top   欢迎骚扰（bushi

## 目录

- [DpskLite-WebUI](#dpsklite-webui)
  - [目录](#目录)
  - [功能](#功能)
  - [四层记忆（上下文管理）](#四层记忆上下文管理)
  - [场景锚点与剧情切片](#场景锚点与剧情切片)
  - [向量记忆（长对话）](#向量记忆长对话)
  - [目录结构](#目录结构)
  - [快速开始](#快速开始)
    - [一键重启脚本](#一键重启脚本)
  - [说明](#说明)

## 功能

- 与 DeepSeek / OpenAI 等模型对话（支持流式输出，生成过程中可随时「停止生成」，已生成内容保留）
- 实时调整推理参数：temperature、top_p、max_tokens、system prompt
- 多模型切换（通过配置）
- 多会话保存与历史记录
- 会话导出：支持 JSON（完整结构化数据）、Markdown（可读对话记录）、纯文本（txt，无标记符号）三种格式下载
- 会话导入：从 JSON 备份恢复完整会话（含消息与场景锚点）
- 文件上传：附加文本类文件（代码 / Markdown / PDF / Office 文档等）内容随消息发送
- 参数预设模板
- 深色 / 浅色主题切换（自动记忆偏好）
- 四层记忆系统：核心设定卡、动态关键事实、剧情摘要、向量记忆（详见下文）
- 用户输入 API Key 后，首轮问答完成时自动总结会话标题（每个会话仅一次，最多 10 字）
- API Key 在网页端输入，存于浏览器 localStorage（后端不持久化 Key）
- 账号登录与注册，登录时使用图形验证码

## 四层记忆（上下文管理）

设置面板「记忆」页按会话独立管理四层记忆，按优先级从高到低拼入每次请求的上下文：

| 层级 | 内容 | 注入方式 |
| --- | --- | --- |
| ⓪ 系统提示词 | 会话级 system prompt | 无条件，最先发送 |
| ① 核心设定 | 世界卡（世界观）+ 人物卡（角色设定，可设主角色） | 无条件，system 前缀 |
| ② 动态关键事实 | LLM 自动抽取（可手动触发），随剧情更新合并去重 | 无条件，system 前缀 |
| ③ 剧情摘要 | LLM 滚动总结（每 N 轮增量一次），超长自动压缩 | 无条件，system 前缀 |
| ④ 向量记忆 | 超长对话的早期内容按需召回 | 检索相似片段拼入 |

- 记忆维护（事实抽取 / 剧情总结 / 标题总结）使用固定轻量模型（`config.py` 的 `MEMORY_MAINTENANCE_MODEL`），稳定且便宜；所有 LLM 维护调用失败都不阻塞主对话。
- 剧情摘要支持两种注入模式（摘要卡分页切换，会话级记忆）：**文本摘要**（整份滚动文本）与**切片摘要**（按场景切片拼接，带场景名前缀）。
- 各层开关独立，关闭后保留内容但停止注入与自动维护。

## 场景锚点与剧情切片

面向角色扮演 / 长剧情对话的场景管理功能：

- **场景锚点（分场）**：在任意 user 消息上打锚点，标记新一场剧情的开头，可命名（如「酒馆初遇」）。锚点在消息流中渲染为分割线，双击分割线可改名，名称与切片双向同步。
- **剧情切片**：开启切片功能后（打锚点即启用），剧情按锚点分段总结——每个锚点段整段成为一个切片（无名字时显示「第 N 片」，可在切片管理面板改名），无锚点内容按摘要卡的「切片轮数」细分。锚点段未结束（剧情还在推进）时不落袋，打下个锚点后自动总结前一段。
- **切片管理面板**：记忆页「切片管理」打开，可查看每个切片包含的对话内容、编辑场景名与本片摘要（失焦自动保存）；支持单片「重新总结」与自动总结总开关。
- 切片摘要与整份文本摘要相互独立：文本线「重新总结」不影响切片，切片编辑不影响文本线。

## 向量记忆（长对话）

用于超长会话的历史召回：把消息用**本地嵌入模型**向量化后存入 SQLite，每次提问时在本地检索最相关的早期内容拼入上下文，避免上下文一长就「忘掉开头」。

对向量化模型的要求：

- 必须是 **sentence-transformers 兼容的本地嵌入模型**（不依赖任何远程 API / Ollama）。
- 模型目录内需包含 `config_sentence_transformers.json`（程序以此识别可用模型）。
- 模型文件统一放在 `models/` 目录下，支持两种目录结构：
  - HF Hub 缓存结构：`models/models--组织--模型名/snapshots/<commit>/`
  - 直接平铺目录：`models/模型名/`
- 默认模型为 **Qwen3-Embedding-0.6B**（目录 `models/models--Qwen--Qwen3-Embedding-0.6B`，**1024 维**，float32）。检索时使用 `prompt_name="query"` 套用模型的 query 指令，建议选用支持 query prompt 的 Qwen3-Embedding 系列。
- 对向量**维数无固定要求**：向量一律按 `float32` 存储与读取，入库时记录各自的实际维度即可。但同一向量库（`data/vector_memory.db`）内所有向量维度必须**一致**，因为检索用点积要求维度匹配；更换成不同维度的模型前需先清空旧向量库，否则会维度不匹配报错。

安装向量记忆所需的依赖（普通对话不装也可用，仅开启向量记忆时才需要）：

```bash
pip install torch sentence-transformers
```

如需 CUDA 加速，请按 <https://pytorch.org/get-started/locally/> 安装与显卡/驱动匹配的版本。

下载默认模型（二选一）：

```bash
# 方式一：huggingface-cli
huggingface-cli download Qwen/Qwen3-Embedding-0.6B --local-dir models/models--Qwen--Qwen3-Embedding-0.6B

# 方式二：git clone
git clone https://huggingface.co/Qwen/Qwen3-Embedding-0.6B models/models--Qwen--Qwen3-Embedding-0.6B
```

运行说明：

- 设备自动回退：CUDA → MPS → CPU；CUDA/MPS 使用 `bfloat16`，CPU 使用 `float32`。
- 模型为**懒加载**：未安装 torch 或未下载模型时不影响普通对话，仅当开启向量记忆并实际使用时才加载；若 torch 或模型不可用，程序会明确报错并提示，不会静默降级。
- 更换模型：把任意符合上述要求的 sentence-transformers 模型放入 `models/` 目录，即可在设置面板中选择。**维度相同的模型**可直接替换、复用旧向量；**维度不同**则需先清空向量库再换。

## 目录结构

```
DpskLite-WebUI/
├── app.py            # Flask 后端入口，注册所有接口
├── llm_client.py     # 调用大模型的封装（兼容 OpenAI 协议）
├── storage.py        # SQLite 账号与会话存储，JSON 参数预设存储
├── memory_engine.py  # 四层记忆引擎：核心设定 / 关键事实 / 剧情摘要（含切片）注入与维护
├── config.py         # 配置（端口、默认模型、支持的模型列表、记忆参数）
├── vector_memory.py  # 向量记忆：本地嵌入模型向量化 + SQLite 存储 + 本地检索
├── models/           # 本地向量化模型（sentence-transformers，默认 Qwen3-Embedding-0.6B，运行时下载）
├── static/           # 前端
│   ├── index.html
│   ├── style-light.css / style-dark.css / style-responsive.css
│   ├── icons.js      # SVG 图标库
│   ├── api.js        # 网络请求封装
│   ├── render.js     # 渲染逻辑（markdown / 消息气泡 / 场景分割线）
│   ├── sessions.js   # 参数读写与会话管理
│   ├── export.js     # 会话导出（JSON / Markdown / 纯文本）
│   ├── chat.js       # 流式输出与消息操作
│   ├── memory.js     # 记忆设置面板（四层记忆 + 切片管理）
│   ├── main.js       # 初始化入口
│   ├── file_reader.js  # 文件读取模块
│   └── vendor/       # 第三方库（marked 等）
├── data/             # 运行时生成（app.db：账号/会话/记忆，向量库，预设，已被 .gitignore 忽略）
├── restart.py        # 一键重启脚本（跨平台，自动清理占用端口的旧进程）
├── restart.bat       # Windows 快捷重启入口（双击即可）
└── requirements.txt
```

## 快速开始

1. 安装依赖：

```bash
pip install -r requirements.txt
```

2. 启动（ waitress 服务器）：

```bash
python app.py
```

3. 浏览器打开 http://127.0.0.1:5000

4. 首次打开页面先注册账号，之后使用账号、密码和图形验证码登录。
5. 登录后在页面右上角填入你的 API Key（存于浏览器本地，不会发给除模型服务外的任何地方）。

> 服务器已从 Flask 自带的开发服务器切换到 **waitress**（多线程、原生支持 Windows，可处理流式对话）。如需调整并发线程数，修改 `app.py` 末尾 `serve(...)` 的 `threads` 参数。

### 一键重启脚本

改完代码后 **waitress 不会自动重载**，需重启服务才能生效。提供了两个脚本：

- **Windows**：双击 `restart.bat` —— 自动安装缺失依赖、停掉占用 5000 端口的旧进程并启动服务。
- **跨平台 / 命令行**：

```bash
python restart.py                # 重启服务（自动清理占用端口的旧进程）
python restart.py --install      # 先安装依赖再重启
python restart.py --port 5001    # 自定义端口（默认读 config.py）
```

> 脚本会检测并结束占用端口的旧进程再启动，避免「旧进程占着端口导致新进程起不来」。



## 说明

- 后端仅作为代理转发，每次请求由前端携带 Key，后端不落盘保存 Key。
- 账号和会话数据（含四层记忆）保存在 `data/app.db`，会话通过 `username` 外键绑定账号，密码以 PBKDF2 哈希保存；部署到生产环境时请设置 `DPSKLITE_WEBUI_SECRET_KEY` 环境变量覆盖默认 Flask session 密钥。
- 支持的模型在 `config.py` 中配置，默认包含 DeepSeek 与 OpenAI 兼容端点。
