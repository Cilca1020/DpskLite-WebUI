/* 发送/重试核心与消息操作（编辑/删除/重试） */

// 当前流式请求的中止控制器（供「停止生成」按钮使用）；null 表示无进行中的生成
let activeAbortController = null;
// 是否正在生成：生成期间忽略重复发送（防止 Enter 键并发触发第二个请求）
let isGenerating = false;

// ------------------------- 向量记忆（长对话）前端状态 -------------------------
// 模型选择随会话恢复（打开会话时以该会话保存的 model 覆盖浏览器缓存，见 applyVmToUI）；
// 启用开关与 N 按「对话」单独存储（后端 sessions.vm）。新建对话默认关闭（开关关、N=默认）。
const VM_DEFAULT_N = 0; // N 默认值：新会话默认 0（全量模式，窗口能塞多少塞多少）；一键配置恢复为 10
const VM_N_MAX = 1000; // N 输入上限（轮次会变化，不做动态钳制）
const VM_MODEL_KEY = "dsw_vm_model"; // 浏览器缓存键

// 当前会话的向量记忆设置（openSession / resetChatUI 时从会话加载/重置）
let vmEnabled = false;
let vmRecentN = VM_DEFAULT_N;
// top_k 召回数量：null=未设置（后端用默认）；0=自动召回（不限制数量）；>0=固定 N 条
let vmTopK = null;

function getRecentNField() {
  return document.getElementById("memoryContextN") || document.getElementById("vmRecentN") || null;
}
function vmLoadModel() { return localStorage.getItem(VM_MODEL_KEY) || ""; }
function vmSaveModel(model) {
  if (model) localStorage.setItem(VM_MODEL_KEY, model);
  else localStorage.removeItem(VM_MODEL_KEY);
}
function vmLoadEnabled() { return vmEnabled; }
// 供记忆页的「一键配置 / 关闭智能总结」在程序化改开关时同步内部状态（change 事件不触发）
function vmSetEnabled(v) { vmEnabled = !!v; }
function vmLoadN() {
  const v = parseInt(vmRecentN, 10);
  // N=0 合法：全量模式（保留全部上下文，按字数截断）
  return isNaN(v) ? VM_DEFAULT_N : Math.max(0, Math.min(VM_N_MAX, v));
}
function vmSaveN(v) {
  let n = parseInt(v, 10);
  if (isNaN(n)) n = VM_DEFAULT_N;
  vmRecentN = Math.max(0, Math.min(VM_N_MAX, n));
  const field = getRecentNField();
  if (field) field.value = vmRecentN;
  return vmRecentN;
}
function vmLoadTopK() { return vmTopK; }
function vmSaveTopK(v) {
  if (v === null || v === undefined) vmTopK = null;
  else vmTopK = Math.max(0, Math.min(500, parseInt(v, 10) || 0));
}

// 本次请求是否启用向量记忆：严格由「启用向量记忆」开关控制；需已选向量化模型。
// 关闭开关时完全不启用（N 不生效），长对话早期内容走「无向量记忆」的截断策略。
function vmUse() {
  return !!vmLoadModel() && vmLoadEnabled();
}

// 把会话中的向量记忆设置应用到前端状态与设置面板（openSession/resetChatUI 调用）。
// vm 为后端返回的 {enabled, model, recent_n}；null 表示新会话默认（关闭）。
// 模型选择随会话恢复：打开会话时以该会话保存的 model 覆盖浏览器缓存。
// 否则在另一会话切换为「无模型」清空全局缓存后，本会话的模型选择会丢失。
function applyVmToUI(vm) {
  vmEnabled = !!(vm && vm.enabled);
  vmRecentN = (vm && vm.recent_n) || VM_DEFAULT_N;
  vmTopK = (vm && vm.top_k !== undefined && vm.top_k !== null) ? vm.top_k : null;
  // vm 存在（含 model 为空的会话）即覆盖缓存；null（新会话）则保留全局缓存沿用上次选择
  if (vm && typeof vm.model === "string") vmSaveModel(vm.model);
  if (window.renderVmModelBox) window.renderVmModelBox();
  const en = $("#vmEnabled");
  if (en) en.checked = vmEnabled;
  const n = getRecentNField();
  if (n) {
    n.value = vmLoadN();
    n.max = VM_N_MAX;
    // N 是独立于向量记忆的上下文保留策略，输入框始终可用（N=0 = 全量模式）
  }
  const topK = $("#vmTopK");
  if (topK) {
    topK.value = (vmTopK === null) ? "" : vmTopK;
    topK.disabled = !vmEnabled;
  }
  // 切片召回开关随会话恢复（切换会话时立即同步 DOM，避免保存读到上一会话的状态）
  const sr = $("#vmSliceRecall");
  if (sr) sr.checked = !!(vm && vm.slice_recall);
  // 通知记忆页同步 top_k 的可见/禁用态
  if (window.syncMemoryVectorField) window.syncMemoryVectorField();
}
window.applyVmToUI = applyVmToUI;
window.vmSetEnabled = vmSetEnabled;

// 生成开始：发送按钮切换为「停止生成」按钮（仅生成期间显示）。
// 纯文字「停止」与「发送」同为二字，宽度自然保持一致。
function enterGeneratingState() {
  const btn = $("#sendBtn");
  btn.classList.add("stop");
  btn.title = "停止生成";
  btn.textContent = "停止";
  btn.disabled = false; // 停止按钮必须可点击
  btn.onclick = stopGeneration;
}

// 生成结束（成功/失败/用户停止）：恢复为发送按钮
function exitGeneratingState() {
  const btn = $("#sendBtn");
  btn.classList.remove("stop");
  btn.title = "";
  btn.textContent = "发送";
  btn.onclick = sendMessage;
  // disabled 由调用方（sendMessage/retryFrom）负责恢复
}

// 停止当前流式生成：只中断请求，已生成内容保留在气泡中
function stopGeneration() {
  if (activeAbortController) activeAbortController.abort();
}

/* ---------------- 流式输出的会话切换处理 ----------------
 *
 * 目标：某会话正在流式输出时切换到别的会话再切回来，原输出的状态不被
 * 中断/重置，并确保切换过程不产生数据混乱或重复输出。
 *
 * 核心设计：
 *  1. liveStreams 以「会话 id」为键保存每个进行中的流式请求状态
 *     （controller、已累积的 full/reasoning、缓冲、model、payload、待存历史等），
 *     会话归属在发起时用捕获的 sid 固化，绝不回读切换后的 currentSessionId。
 *  2. 累积与渲染解耦：文本（full/reasoning/buffer）无论会话是否可见都持续累积；
 *     DOM 渲染（气泡、增量 markdown、滚动、loading）仅在「该会话当前可见」时执行，
 *     切走后只静默累积，避免把内容画到已被销毁的节点上。
 *  3. 生命周期：openSession 切换会话时调用 syncLiveStreams(sid)：
 *     - 离开的会话：仅标记 unmount（不 abort，输出继续在后台累积）；
 *     - 回到的会话：根据已累积的 full/reasoning 重建气泡并继续渲染。
 *  4. 落库与内存态（conversation/sessionTotal）更新仅在 sid === currentSessionId
 *     时进行，防止把 A 的结果误写进 B；后端保存始终用捕获的 sid。
 *  5. 完成后删除 liveStreams[sid]（数据已入库），之后 openSession 从后端正常
 *     读取，不会重复追加气泡。
 */

// 进行中的流式请求注册表：sid -> liveStream 对象
const liveStreams = {};

// 助手气泡操作条显示（供初始渲染与恢复渲染共用），带滑入动画
function showActionsForEl(assistantEl) {
  const row = assistantEl && assistantEl.parentNode;
  const bar = row && row.querySelector(".msg-actions");
  if (bar) {
    bar.style.display = "flex";
    bar.classList.remove("msg-actions-in");
    void bar.offsetWidth; // 强制重排，确保动画从头播放
    bar.classList.add("msg-actions-in");
  }
  // 仅当用户处于「跟随置底」状态时才回底显示操作条，避免打扰正在上滑阅读的用户
  if (window.shouldFollowScroll && window.shouldFollowScroll()) {
    const box = $("#chatBox");
    if (box) box.scrollTop = box.scrollHeight;
  }
}

// 寻找 src[0, end) 中最后一个"块级安全断点"下标（不含则回退到 0）。
// 安全断点定义：其后内容即便孤立即使 markdown 重解析也不会影响前面已定型块。
function lastSafeBreak(src, end) {
  const seg = src.slice(0, end);
  // 代码围栏：未闭合的 ``` 之后不能断（避免把未闭合块提前定死）
  const fenceRe = /```/g;
  let fenceCount = 0, m;
  while ((m = fenceRe.exec(seg)) !== null) fenceCount++;
  if (fenceCount % 2 !== 0) {
    // 存在未闭合代码块：断点必须在开场 ``` 之前
    const open = seg.lastIndexOf("```");
    return Math.max(0, open);
  }
  // 优先在「空行（段落边界）」处断开
  let brk = seg.lastIndexOf("\n\n");
  if (brk !== -1) return brk + 2; // 包含空行，下一段为草稿
  // 其次在「行尾」断开（单行块如标题/列表项，整行写完整再定型）
  const nl = seg.lastIndexOf("\n");
  if (nl !== -1) return nl + 1;
  return 0; // 仅一行且未结束：整段作为草稿
}

// 在 st 上创建（或重建）流式助手气泡的 DOM：assistantEl / answerDiv / draftNode / loadingEl。
// 初始发送与恢复渲染共用；恢复时 renderedLen 归零，按当前累积的 full 整体重渲染。
function buildLiveBubbles(st) {
  st.assistantEl = addMsgEl("assistant", "", true, st.msgIndex, null, null, false);
  st.answerDiv = document.createElement("div");
  st.answerDiv.className = "msg-content";
  st.assistantEl.appendChild(st.answerDiv);
  st.draftNode = document.createElement("span");
  st.draftNode.className = "md-draft";
  st.draftNode.style.whiteSpace = "pre-wrap";
  st.draftNode.style.wordBreak = "break-word";
  st.draftAttached = false;
  st.renderedLen = 0;
  st.reasoningWrap = null;
  st.reasoningBody = null;
  st.loadingEl = document.createElement("div");
  st.loadingEl.className = "msg-loading";
  st.loadingEl.innerHTML = '<span class="spinner"></span><span class="msg-loading-text">思考中…</span>';
  st.assistantEl.insertBefore(st.loadingEl, st.answerDiv);
  const box = $("#chatBox");
  if (box) box.scrollTop = box.scrollHeight;
}

// 推理过程折叠块：按需创建
function liveEnsureReasoning(st) {
  if (st.reasoningWrap) return st.reasoningWrap;
  st.reasoningWrap = document.createElement("details");
  st.reasoningWrap.className = "reasoning";
  st.reasoningWrap.open = true;
  const summary = document.createElement("summary");
  summary.textContent = "思考过程";
  st.reasoningBody = document.createElement("div");
  st.reasoningBody.className = "reasoning-body";
  st.reasoningWrap.appendChild(summary);
  st.reasoningWrap.appendChild(st.reasoningBody);
  st.assistantEl.insertBefore(st.reasoningWrap, st.assistantEl.firstChild);
  return st.reasoningWrap;
}

function liveHideLoading(st) {
  if (st.loadingEl && st.loadingEl.parentNode) st.loadingEl.remove();
}

// 把 [renderedLen, upto) 这一段增量渲染并追加为块（不触碰已渲染部分）
function liveFlushRendered(st, upto) {
  if (upto <= st.renderedLen) return;
  const seg = st.full.slice(st.renderedLen, upto);
  const html = (window.marked ? marked.parse(seg) : seg);
  const tmp = document.createElement("div");
  tmp.innerHTML = html;
  attachCodeCopy(tmp); // 新定型的代码块立即附加"单独复制"按钮（幂等）
  while (tmp.firstChild) {
    if (st.draftAttached) st.answerDiv.insertBefore(tmp.firstChild, st.draftNode);
    else st.answerDiv.appendChild(tmp.firstChild);
  }
  st.renderedLen = upto;
}

function liveRenderAnswer(st) {
  if (!st.full) return;
  const brk = lastSafeBreak(st.full, st.full.length);
  liveFlushRendered(st, brk); // 把稳定前缀内的新完整块增量渲染
  const draft = st.full.slice(st.renderedLen);
  if (draft) {
    if (!st.draftAttached) {
      st.answerDiv.appendChild(st.draftNode);
      st.draftAttached = true;
    }
    st.draftNode.textContent = draft;
  } else if (st.draftAttached) {
    st.draftNode.remove();
    st.draftAttached = false;
  }
}

// 渲染当前累积内容到 DOM（仅当该会话当前可见）。累积已在 liveAccumulate 完成。
function liveRenderChunk(st) {
  if (!st.mounted || st.sid !== currentSessionId) return;
  liveHideLoading(st); // 收到首个内容块即移除加载动画
  if (st.mode === "reasoning") {
    liveEnsureReasoning(st);
    st.reasoningBody.textContent = st.reasoning; // 流式实时显示纯文本
  } else {
    // 思考过程是否随正文输出自动收起由设置决定（默认收起；关闭设置后保持展开）
    if (st.reasoning && st.reasoningWrap && st.reasoningWrap.open && window.getAutoCollapseReasoning()) {
      st.reasoningWrap.open = false;
    }
    liveRenderAnswer(st); // 增量渲染：实时成型，已定型块不重绘
  }
  // 仅当用户处于「跟随置底」状态时跟随最新内容；主动上滑阅读时不强制拉回底部
  if (window.shouldFollowScroll && window.shouldFollowScroll()) {
    const box = $("#chatBox");
    if (box) box.scrollTop = box.scrollHeight;
  }
}

// 累积文本：无论会话是否可见都追加到 full/reasoning（不触碰 DOM）
function liveAccumulate(st, text) {
  if (!text) return;
  if (st.mode === "reasoning") st.reasoning += text;
  else st.full += text;
}

// 缓冲按标记切分：先累积，可见时再渲染（尾部不完整部分留到下次）
function liveFlush(st) {
  const MARK = "<<REASONING>>";
  const MARK_A = "<<ANSWER>>";
  const MAX_PREFIX = Math.max(MARK.length, MARK_A.length) - 1; // 可能形成标记的最大残留长度
  // 在 buffer 中找最先出现的完整标记
  const idxR = st.buffer.indexOf(MARK);
  const idxA = st.buffer.indexOf(MARK_A);
  let next = -1, nextMode = null;
  if (idxR !== -1 && (idxA === -1 || idxR < idxA)) { next = idxR; nextMode = "reasoning"; }
  else if (idxA !== -1) { next = idxA; nextMode = "answer"; }
  if (next === -1) {
    // 没有完整标记：把不可能再拼接成标记的安全前缀先累积渲染，保留末尾最多 MAX_PREFIX 个字符
    if (st.buffer.length > MAX_PREFIX) {
      const safeLen = st.buffer.length - MAX_PREFIX;
      liveAccumulate(st, st.buffer.slice(0, safeLen));
      liveRenderChunk(st);
      st.buffer = st.buffer.slice(safeLen);
    }
    return;
  }
  // next 之前是上一段的延续文本（无新标记前缀），按当前 mode 处理
  const before = st.buffer.slice(0, next);
  if (before) {
    liveAccumulate(st, before);
    liveRenderChunk(st);
  }
  // 切换到新标记模式
  st.mode = nextMode;
  st.buffer = st.buffer.slice(next + (nextMode === "reasoning" ? MARK : MARK_A).length);
  // 递归处理剩余 buffer（可能还含下一个标记）
  liveFlush(st);
}

// 创建一次流式请求的状态对象。payload 在发起时（当前会话上下文下）构建并固化，
// 避免切换会话后读到其它会话的 model / 参数 / 上下文。
function createLiveStream({ sid, userText, files, writeUser, saveHistory, msgIndex, payload }) {
  return {
    sid,
    userText: userText || "",
    files: files || null,
    writeUser,
    saveHistory,
    msgIndex,
    payload,
    controller: new AbortController(),
    full: "",
    reasoning: "",
    buffer: "",
    mode: "reasoning",
    mounted: false,
    finished: false,
    saved: false,
    // DOM 引用（mount 时重建）
    assistantEl: null,
    answerDiv: null,
    draftNode: null,
    draftAttached: false,
    renderedLen: 0,
    reasoningWrap: null,
    reasoningBody: null,
    loadingEl: null,
  };
}

// 把进行中的流式状态重建为可见气泡（openSession 切回本会话时调用）。
// 该轮 user 消息尚未落库，需一并恢复；assistant 气泡按已累积内容渲染。
function mountLiveStream(st) {
  if (!st || st.finished) return;
  // 其它会话的流式请求标记为不可见（继续后台累积，不中断）
  for (const k in liveStreams) {
    if (k !== st.sid && liveStreams[k].mounted) {
      liveStreams[k].mounted = false;
      liveStreams[k].assistantEl = liveStreams[k].answerDiv = liveStreams[k].draftNode = liveStreams[k].loadingEl = null;
    }
  }
  st.mounted = true;
  const box = $("#chatBox");
  hideEmptyHint(box);
  // 恢复本轮的 user 气泡（尚未写入后端历史）
  lastUserMsgIndex = sessionTotal;
  addMsgEl("user", st.userText || "（已发送文件）", false, sessionTotal,
    st.files && st.files.length ? st.files : null, null, false);
  conversation.push({ role: "user", content: st.userText, files: st.files && st.files.length ? st.files : null });
  sessionTotal++;
  // 恢复 assistant 流式气泡
  lastAssistantMsgIndex = sessionTotal;
  st.msgIndex = sessionTotal;
  buildLiveBubbles(st);
  if (st.reasoning) { liveEnsureReasoning(st); renderMarkdown(st.reasoningBody, st.reasoning); }
  if (st.full) liveRenderAnswer(st);
  if (st.reasoning || st.full) liveHideLoading(st);
  box.scrollTop = box.scrollHeight;
}

// 会话切换后的流式状态同步：
// activeSid 为当前可见会话 id；为 null 表示未选中任何会话。
// 可见会话的进行中请求重建气泡；其它会话仅标记不可见（不中断）。
function syncLiveStreams(activeSid) {
  for (const k in liveStreams) {
    const st = liveStreams[k];
    if (st.finished) continue; // 已完成且已入库，openSession 从后端读取即可
    if (k === activeSid) {
      // 本会话成为当前会话：openSession 已清空容器并重绘历史，故始终重建其进行中气泡。
      // （即便此前已 mounted，也要重建——容器内容已被清空。）
      mountLiveStream(st);
    } else if (st.mounted) {
      st.mounted = false;
      st.assistantEl = st.answerDiv = st.draftNode = st.loadingEl = null;
    }
  }
}
window.syncLiveStreams = syncLiveStreams;

// 终止某个会话的进行中流式请求（删除会话时调用），并移除注册表项
function abortLiveStream(sid) {
  const st = liveStreams[sid];
  if (!st) return;
  try { st.controller.abort(); } catch (_) {}
  delete liveStreams[sid];
}
window.abortLiveStream = abortLiveStream;

// 在发起时固化请求 payload：model / 参数 / 会话上下文在调用时刻读取，
// 确保切换会话后本请求仍携带原会话的配置与上下文。
function buildChatPayload(sid) {
  return {
    api_key: loadApiKey(),
    model: $("#modelSelect").value,
    // 把用户消息携带的文本文件内容拼接到对应 user 的 content 中，
    // 使模型能看到文件内容（files 本身不作为独立消息）
    messages: conversation
      // 过滤空内容的助手消息（被停止时保存的空消息），避免污染模型上下文
      .filter((m) => !(m.role === "assistant" && !(m.content || "").trim()))
      .map((m) => {
        if (m.role === "user" && m.files && m.files.length) {
          const appended = m.files
            .map((f) => {
              if (f.binary) {
                // 二进制办公文档：以 base64 附件形式标注（文本模型仅能识别为附件）
                return `\n\n--- 附件（二进制，${f.name}，${f.content.length} 字节 base64）：请知悉该文件为二进制文档，无法在此直接解析内容 ---`;
              }
              return `\n\n--- 文件内容：${f.name} ---\n${f.content}`;
            })
            .join("");
          return { role: "user", content: (m.content || "") + appended };
        }
        return { role: m.role, content: m.content };
      }),
    ...readParamsFromUI(),
    // 向量记忆：仅由「启用向量记忆」开关控制；关闭时不传 recent_n，后端忽略 N
    // 后端负责「最近 N 轮 + 向量检索 Top-K」拼接（轮次 ≤ N 时全部添加，token 过多砍最早）
    session_id: sid || undefined,
    vector_memory: vmUse(),
    vector_memory_model: vmLoadModel() || undefined,
    // N 独立于向量记忆开关：总是上报（N=0 为全量模式，后端据此绕过向量检索）
    vector_memory_recent_n: vmLoadN(),
  };
}

// 执行流式请求：fetch + 缓冲切分 + 累积 + 可见时渲染 + 收尾落库。
// 返回 { full, reasoning, saved }。落库与会话计数用捕获的 sid，绝不回读切换后的会话。
async function runLiveStream(st) {
  const apiKey = loadApiKey();
  if (!apiKey) {
    if (st.mounted) st.assistantEl.textContent = "[错误] 请先点击右上角设置按钮，在设置面板中输入 API Key";
    st.finished = true;
    delete liveStreams[st.sid];
    return null;
  }
  // 停止控制：为本次流式请求绑定中止控制器，并把发送按钮切换为「停止」
  activeAbortController = st.controller;
  isGenerating = true;
  enterGeneratingState();
  let aborted = false; // 是否被用户停止生成（气泡将标记为截断）

  try {
    await streamChat(st.payload, (chunk) => {
      st.buffer += chunk;
      liveFlush(st);
    }, st.controller.signal);
  } catch (e) {
    // 用户主动停止（AbortError）不视为错误：保留已生成内容，走下方共用收尾
    if (e && e.name === "AbortError") {
      aborted = true;
    } else {
      // 异常时移除加载动画；输出已结束（失败），同样显示操作条
      if (st.mounted) { liveHideLoading(st); showActionsForEl(st.assistantEl); }
      st.finished = true;
      activeAbortController = null;
      isGenerating = false;
      exitGeneratingState();
      delete liveStreams[st.sid];
      throw e;
    }
  }
  // 生成结束（成功/失败/用户停止）：清除中止控制器并恢复发送按钮
  activeAbortController = null;
  isGenerating = false;
  exitGeneratingState();

  // 流结束后处理残留缓冲
  if (st.buffer) {
    liveAccumulate(st, st.buffer);
    st.buffer = "";
    liveRenderChunk(st);
  }
  // 收尾渲染（仅当本会话当前可见）：定型剩余草稿、显示操作条、标记截断
  if (st.mounted && st.sid === currentSessionId) {
    liveHideLoading(st); // 流结束兜底移除加载动画
    showActionsForEl(st.assistantEl); // 流式输出结束后再显示操作条
    liveFlushRendered(st, st.full.length);
    if (st.draftAttached) { st.draftNode.remove(); st.draftAttached = false; }
    if (st.reasoning && st.reasoningBody) renderMarkdown(st.reasoningBody, st.reasoning);
    st.assistantEl._rawText = st.full; // 供"复制为 Markdown"使用
    if (aborted) markInterrupted(st.assistantEl, !!(st.full || st.reasoning));
  }
  // 落库：始终写入发起时捕获的 sid
  let savedAssistant = false; // 本次是否把 assistant 消息写入了后端历史
  if (st.saveHistory && st.sid) {
    if (st.writeUser) {
      await apiPost("/api/sessions/" + st.sid + "/msg", {
        role: "user",
        content: st.userText,
        files: st.files && st.files.length ? st.files : undefined,
      });
    }
    // 保存助手消息：被停止且无输出时也保存空消息并标记 interrupted，
    // 使刷新/重开后仍能看到「回答已中断」提示（发送上下文时会被过滤）；
    // 正常结束但内容为空则不保存，避免产生无意义空气泡。
    if (st.full || st.reasoning || aborted) {
      await apiPost("/api/sessions/" + st.sid + "/msg", {
        role: "assistant",
        content: st.full,
        reasoning: st.reasoning || undefined,
        interrupted: aborted || undefined,
      });
      savedAssistant = true;
    }
    // 首轮问答完成后自动生成一次会话标题；失败不影响当前对话。
    if (st.writeUser) {
      autoTitleSession(st.sid, 2).then((updated) => {
        if (updated) refreshSessions();
      }).catch(() => {});
    }
    refreshSessions();
  }
  // 完成后移除注册表项：数据已入库，之后 openSession 从后端正常读取
  st.finished = true;
  st.saved = savedAssistant;
  st.aborted = aborted;
  delete liveStreams[st.sid];
  // saved 标记供调用方维护会话消息计数：full/reasoning 为空（如刚发送即点停止）时，
  // 也可能已把空 assistant 消息写入后端历史，调用方据此决定是否递增 sessionTotal。
  return { full: st.full, reasoning: st.reasoning, saved: savedAssistant };
}

// 为已有首轮问答且仍使用默认标题的会话补生成标题。
async function autoTitleSession(sessionId, retries = 0) {
  const apiKey = loadApiKey();
  if (!apiKey || !sessionId) return false;
  const result = await apiPost("/api/sessions/" + sessionId + "/auto-title", {
    api_key: apiKey,
    model: $("#modelSelect").value,
    system_prompt: readParamsFromUI().system_prompt,
  });
  if (result && result.title) return true;
  if (retries > 0) {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    return autoTitleSession(sessionId, retries - 1);
  }
  return false;
}

// 编辑最新一轮的提问：取出原文本填入输入框，并删除该轮（user+assistant）。
// 用户修改后发送，新的一轮即取代原位置。仅最新一轮可触发。
async function editMessage(index) {
  if (!currentSessionId || index !== lastUserMsgIndex) return;
  const s = await apiGet("/api/sessions/" + currentSessionId);
  const chat = (s.messages || []).filter((m) => m.role !== "system");
  if (index < 0 || index >= chat.length || chat[index].role !== "user") return;
  const original = chat[index].content;
  // 成对删除该轮（user + 紧跟的 assistant），不弹确认
  let second = -1;
  if (chat[index + 1] && chat[index + 1].role === "assistant") second = index + 1;
  const hi = Math.max(index, second);
  const lo = Math.min(index, second);
  await apiDelete("/api/sessions/" + currentSessionId + "/msg/" + hi);
  if (second !== -1) await apiDelete("/api/sessions/" + currentSessionId + "/msg/" + lo);
  lastUserMsgIndex = null; // 该轮即将被删除，临时清空
  // 抑制「最新一轮」标记重新计算：删除最后一条后倒数第二条会成为最后一条，
  // 若被重新标记，用户尚未发送修改内容时它也会误显「编辑」按钮，导致连续误改。
  // 待用户在输入框完成编辑并发送新消息后，sendMessage 会重新设置标记。
  await openSession(currentSessionId, { suppressLatestMarkers: true });
  // 预填输入框，并把原文件恢复回待发送队列（用户可保留或重新选择）
  const input = $("#userInput");
  input.value = original;
  if (window.FileReaderModule) {
    FileReaderModule.clearPendingFiles();
    const originalFiles = chat[index].files || [];
    if (originalFiles.length) FileReaderModule.addFilesFromData(originalFiles);
  }
  input.focus();
}

// 删除第 index 条消息（不含 system），并重新渲染会话。
// 按「轮次」成对删除：删除 user 时连同其后紧跟的 assistant 一起删，
// 删除 assistant 时连同其前紧邻的 user 一起删；不成对则单独删。
async function deleteMessage(index) {
  if (!currentSessionId) return;
  if (!confirm("确定删除这条消息（及其同轮消息）？")) return;
  // 读取当前会话，判断是否存在成对消息
  const s = await apiGet("/api/sessions/" + currentSessionId);
  const chat = (s.messages || []).filter((m) => m.role !== "system");
  if (index < 0 || index >= chat.length) return;
  const role = chat[index].role;
  let second = -1; // 成对消息的下标（-1 表示无配对）
  if (role === "user" && chat[index + 1] && chat[index + 1].role === "assistant") {
    second = index + 1;
  } else if (role === "assistant" && chat[index - 1] && chat[index - 1].role === "user") {
    second = index - 1;
  }
  // 先删较大的下标，避免删除前一条导致后一条下标前移而误删
  if (second !== -1) {
    const hi = Math.max(index, second);
    const lo = Math.min(index, second);
    await apiDelete("/api/sessions/" + currentSessionId + "/msg/" + hi);
    await apiDelete("/api/sessions/" + currentSessionId + "/msg/" + lo);
  } else {
    await apiDelete("/api/sessions/" + currentSessionId + "/msg/" + index);
  }
  // 用后端数据重新渲染，保证内存与磁盘一致
  await openSession(currentSessionId);
}

// 重试：删除第 msgIndex（助手消息）及其之后的所有消息，
// 以该助手对应的上文重新请求一次
async function retryFrom(msgIndex) {
  if (!currentSessionId) return;
  const sendBtn = $("#sendBtn");
  if (sendBtn.disabled) return; // 避免与正在进行的请求冲突
  // 删除该助手消息及其后所有消息：反复删除同一个下标直到该下标越界
  while (true) {
    const s = await apiDelete("/api/sessions/" + currentSessionId + "/msg/" + msgIndex);
    if (s && s.error) break; // 下标已越界或无会话
    // 判断是否还有消息，防止无限循环
    const list = await apiGet("/api/sessions/" + currentSessionId);
    const chatCount = (list.messages || []).filter((m) => m.role !== "system").length;
    if (chatCount <= msgIndex) break;
  }
  // 依据最新后端数据重建内存态与 UI（保留文件信息）
  const s = await apiGet("/api/sessions/" + currentSessionId);
  conversation = s.messages
    .filter((m) => m.role !== "system")
    .map((m) => ({ role: m.role, content: m.content, files: m.files || null }));
  // 该助手消息对应的用户提问在 conversation 中位于 msgIndex-1
  const userMsg = conversation[msgIndex - 1] || { content: "", files: null };
  const userText = userMsg.content || "";
  const userFiles = userMsg.files || null;
  // 重新渲染会话（此时尾部已被删掉）
  await openSession(currentSessionId);
  // 删除后剩余的"最后一条助手消息"只是暂时的，马上会被新回答取代，
  // 先清掉其重试按钮，避免出现"倒数第二条也能重试"的误标
  document.querySelectorAll(".msg-actions .retry-btn").forEach((b) => b.remove());
  // 在会话末尾追加一个新的助手占位并流式请求（user 提问仍在 conversation 中，作为上下文）
  const sid = currentSessionId;
  lastAssistantMsgIndex = msgIndex; // 新回答为最后一条助手消息，显示重试按钮（渲染前先设置）
  // 以发起时固化的 payload 创建流式请求（writeUser=false：重试时 user 已在历史中）
  const st = createLiveStream({
    sid,
    userText,
    files: userFiles,
    writeUser: false,
    saveHistory: true,
    msgIndex,
    payload: buildChatPayload(sid),
  });
  buildLiveBubbles(st);
  st.mounted = true;
  liveStreams[sid] = st;
  try {
    sendBtn.disabled = true;
    const res = await runLiveStream(st);
    if (res && (res.full || res.reasoning) && sid === currentSessionId) conversation.push({ role: "assistant", content: res.full });
    if (res && res.saved && sid === currentSessionId) sessionTotal++; // 后端已保存该 assistant 消息
  } catch (e) {
    activeAbortController = null;
    isGenerating = false;
    exitGeneratingState();
    if (e && e.body && e.body.vector_memory_error) {
      showToast((e.message || "向量记忆不可用") + "，已停止回复");
    }
    if (st.mounted) st.assistantEl.textContent = "[错误] " + e.message;
  } finally {
    sendBtn.disabled = false;
  }
}

// 发送后优雅收起虚拟键盘：
// 1) blur 让输入框失焦（iOS Safari 点击自定义按钮不会自动收起键盘，需显式 blur）
// 2) 键盘收起会改变视口高度，等待系统收起动画结束后再把聊天区平滑滚到底部，
//    避免 Android 上收起瞬间内容跳动突兀。编辑模式（editMessage）不调用此函数，
//    因为预填原文后需要保持键盘打开让用户继续编辑。
function dismissKeyboard() {
  const input = $("#userInput");
  if (document.activeElement !== input) return;
  input.blur();
  const chatBox = $("#chatBox");
  setTimeout(() => {
    if (chatBox.scrollTo) {
      chatBox.scrollTo({ top: chatBox.scrollHeight, behavior: "smooth" });
    } else {
      chatBox.scrollTop = chatBox.scrollHeight;
    }
  }, 320);
}

// 向量记忆不可用：toast 提示 + 停止回复 + 把用户发送的内容返回输入框，
// 并撤销乐观渲染的用户气泡与助手占位（不静默忽略）
function handleVmFailure(e, text, files) {
  showToast((e.message || "向量记忆不可用") + "，已停止回复，内容已退回输入框");
  const input = $("#userInput");
  input.value = text || "";
  if (window.FileReaderModule) {
    FileReaderModule.clearPendingFiles();
    if (files && files.length) FileReaderModule.addFilesFromData(files);
  }
  input.focus();
  // 撤销乐观渲染：移除刚追加的最后两条消息行（user 气泡 + assistant 占位）
  const box = $("#chatBox");
  const rows = Array.from(box.querySelectorAll(".msg-row"));
  rows.slice(-2).forEach((r) => r.remove());
  // 恢复内存态
  if (conversation.length && conversation[conversation.length - 1].role === "user") {
    conversation.pop();
  }
  if (sessionTotal > 0) sessionTotal--;
  lastUserMsgIndex = null;
  lastAssistantMsgIndex = null;
  if (!box.querySelector(".msg-row")) showEmptyHint(box);
}

// 发送消息：渲染用户消息与助手占位，触发流式请求
async function sendMessage() {
  if (isGenerating) return; // 生成期间忽略重复发送（防止 Enter 键并发触发）
  const input = $("#userInput");
  const text = input.value.trim();
  // 文本与文件至少有一种；都为空则忽略
  const files = window.FileReaderModule ? FileReaderModule.getPendingFiles() : [];
  if (!text && files.length === 0) return;

  const apiKey = loadApiKey();
  if (!apiKey) {
    alert("请先点击右上角设置按钮，在设置面板中输入 API Key");
    return;
  }

  // 发送消息即正式创建对话（未选中会话时在此创建，并出现在侧栏）
  await ensureSession();

  // 新消息是追加到 DOM 而非重渲染，旧消息上的「编辑/重试」按钮不会自动消失，
  // 会导致倒数第二条也残留按钮。先清掉所有旧按钮，再更新标记并渲染新消息。
  document
    .querySelectorAll(".msg-actions .edit-btn, .msg-actions .retry-btn")
    .forEach((b) => b.remove());

  // 渲染用户消息（传入全局下标，使操作条可见）；携带文件时在气泡上方渲染文件卡片
  // 用户消息强制纯文本：输入中若含 Markdown 语法则按原样显示，不渲染
  const userIdx = sessionTotal; // 新消息的全局下标 = 当前会话消息总数
  lastUserMsgIndex = userIdx; // 最新一轮，显示编辑按钮（渲染前先设置）
  addMsgEl("user", text || "（已发送文件）", false, userIdx, files);
  input.value = "";
  dismissKeyboard(); // 移动端发送后自动收起虚拟键盘
  conversation.push({ role: "user", content: text, files: files.length ? files : null });
  sessionTotal++; // user 消息已追加

  // 发送后立即清空输入框上方的待发送文件卡片（不等输出完成）
  if (window.FileReaderModule) FileReaderModule.clearPendingFiles();

  // 以发起时捕获的会话 id 与固化的 payload 创建流式请求（writeUser=true：首次发送需写 user）
  const sendBtn = $("#sendBtn");
  const sid = currentSessionId;
  lastAssistantMsgIndex = sessionTotal; // user 已 push 并 sessionTotal++，assistant 下标 = 新总数
  const st = createLiveStream({
    sid,
    userText: text,
    files,
    writeUser: true,
    saveHistory: true,
    msgIndex: sessionTotal,
    payload: buildChatPayload(sid),
  });
  buildLiveBubbles(st); // 渲染助手占位（含加载动画）
  st.mounted = true;
  liveStreams[sid] = st;
  sendBtn.disabled = true;
  try {
    const res = await runLiveStream(st);
    // 仅当本会话仍为当前会话时才更新内存态，避免把 A 的结果误写进 B
    if (res && (res.full || res.reasoning) && sid === currentSessionId) conversation.push({ role: "assistant", content: res.full });
    if (res && res.saved && sid === currentSessionId) sessionTotal++; // 后端已保存该 assistant 消息（含停止时的空消息）
  } catch (e) {
    // 异常收尾：任何失败都要恢复发送按钮与生成状态
    activeAbortController = null;
    isGenerating = false;
    exitGeneratingState();
    if (e && e.body && e.body.vector_memory_error) {
      // 向量记忆不可用：提示并停止回复，把内容退回输入框，撤销刚渲染的气泡
      handleVmFailure(e, text, files);
    } else if (st.mounted) {
      st.assistantEl.textContent = "[错误] " + e.message;
    }
  } finally {
    sendBtn.disabled = false;
  }
}
