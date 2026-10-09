/* 参数读写 + 会话管理（新建/打开/清理/列表） */

// 默认/范围/说明信息由后端 /api/params/default 提供，前端据此做范围回退
let PARAM_DEFAULTS = {};
let PARAM_RANGES = {};
let PARAM_META = {};
let STOP_MAX_ITEMS = 4;
let STOP_MAX_LEN = 32;

// 长对话分段渲染：初始只渲染最近 PAGE_SIZE 条，上滑到顶部时按需加载更早消息。
// sessionTotal 记录会话 user/assistant 消息总数（新消息的全局下标 = 追加前总数）；
// sessionMinIndex 为已渲染最早一条消息的全局下标（作为加载更早消息的锚点）。
const PAGE_SIZE = 30;
let sessionTotal = 0;
let sessionHasMore = false;
let sessionMinIndex = 0;
let loadingOlder = false;

function saveApiKey(k) {
  if (k) localStorage.setItem(LS_KEY, k);
  else localStorage.removeItem(LS_KEY);
}

function loadApiKey() { return localStorage.getItem(LS_KEY) || ""; }

// 确保存在当前会话：未选中任何对话时，用当前 UI 的 model + 参数 + 向量记忆设置创建新会话。
// 创建后立即出现在侧栏，可在左侧跳转。已选中会话时直接返回。
async function ensureSession() {
  if (currentSessionId) return currentSessionId;
  const model = $("#modelSelect").value || ($("#modelSelect").options[0] || {}).value || "";
  const s = await apiPost("/api/sessions", {
    model: model,
    params: readParamsFromUI(),
    vm: {
      enabled: vmLoadEnabled(),
      model: vmLoadModel(),
      recent_n: vmLoadN(),
      top_k: vmLoadTopK(),
    },
  });
  currentSessionId = s.id;
  refreshSessions();
  return currentSessionId;
}

// 保存向量记忆设置到当前会话。仅改向量设置时**不新建对话**：
// 未选中会话则只更新内存/缓存状态，待会话真正创建（发消息或改参数）时一并落库。
async function saveVmSettings() {
  if (!currentSessionId) return;
  try {
    await apiPost("/api/sessions/" + currentSessionId + "/config", {
      model: $("#modelSelect").value,
      params: readParamsFromUI(),
      vm: {
        enabled: vmLoadEnabled(),
        model: vmLoadModel(),
        recent_n: vmLoadN(),
        top_k: vmLoadTopK(),
      },
    });
  } catch (_) {
    /* 保存失败不影响对话 */
  }
}

// 把当前 UI 上的 model + 推理参数保存到当前会话（每个对话一套独立配置）。
// 未选中对话时先创建会话再保存——修改配置即正式创建对话。
async function saveSessionConfig() {
  if (!currentSessionId) await ensureSession();
  if (!currentSessionId) return; // 创建失败则放弃保存
  const cfg = {
    model: $("#modelSelect").value,
    params: readParamsFromUI(),
    // 向量记忆设置随会话单独存储（不存浏览器）
    vm: {
      enabled: vmLoadEnabled(),
      model: vmLoadModel(),
      recent_n: vmLoadN(),
      top_k: vmLoadTopK(),
    },
  };
  try {
    await apiPost("/api/sessions/" + currentSessionId + "/config", cfg);
  } catch (_) {
    /* 保存失败不影响对话 */
  }
}

// 把会话配置（model + params）应用到 UI
function applyConfigToUI(model, params) {
  if (model) $("#modelSelect").value = model;
  if (params) {
    writeParamsToUI(params);
  } else {
    writeParamsToUI(PARAM_DEFAULTS);
  }
}

// 数值参数：超范围时回退到默认值；非数字也回退到默认
function clampParam(key, raw) {
  const def = PARAM_DEFAULTS[key];
  if (key === "stop") return def; // stop 单独处理
  const [lo, hi] = PARAM_RANGES[key] || [null, null];
  let v = parseFloat(raw);
  if (isNaN(v)) return def; // 非数字 -> 回退默认
  if (lo !== null && v < lo) v = lo; // 低于下限 -> 钳到下限
  if (hi !== null && v > hi) v = hi; // 高于上限 -> 钳到上限
  return key === "max_tokens" ? Math.round(v) : v;
}

// 处理 stop：逗号分隔，限制数量与单项长度，超长回退为截断（不丢弃）
function sanitizeStop(raw) {
  const s = (raw || "").toString();
  if (!s.trim()) return "";
  const items = s.split(",").map((x) => x.trim()).filter(Boolean);
  return items
    .slice(0, STOP_MAX_ITEMS)
    .map((x) => x.slice(0, STOP_MAX_LEN))
    .join(",");
}

function readParamsFromUI() {
  const p = { system_prompt: $("#system_prompt").value };
  document.querySelectorAll(".param-field input[data-key]").forEach((el) => {
    const key = el.dataset.key;
    p[key] = key === "stop" ? sanitizeStop(el.value) : clampParam(key, el.value);
  });
  return p;
}

function writeParamsToUI(p) {
  $("#system_prompt").value = p.system_prompt ?? PARAM_DEFAULTS.system_prompt ?? "";
  document.querySelectorAll(".param-field input[data-key]").forEach((el) => {
    const key = el.dataset.key;
    if (key in p && p[key] !== undefined && p[key] !== null) {
      el.value = p[key];
    } else if (key in PARAM_DEFAULTS) {
      el.value = PARAM_DEFAULTS[key];
    }
  });
}

// 渲染侧边栏会话列表
async function refreshSessions() {
  const list = await apiGet("/api/sessions");
  const ul = $("#sessionList");
  ul.innerHTML = "";
  list.forEach((s) => {
    const li = document.createElement("li");
    if (s.id === currentSessionId) li.className = "active";
    const title = document.createElement("span");
    title.textContent = s.title;
    title.style.flex = "1";
    li.style.cursor = "pointer";
    li.onclick = () => openSession(s.id);
    const exportBtn = document.createElement("button");
    exportBtn.className = "item-btn";
    exportBtn.title = "导出对话（JSON / Markdown）";
    exportBtn.innerHTML = svgIcon("download");
    exportBtn.onclick = (e) => {
      e.stopPropagation();
      onExportClick(exportBtn, s.id);
    };
    const renameBtn = document.createElement("button");
    renameBtn.className = "item-btn";
    renameBtn.title = "重命名";
    renameBtn.innerHTML = svgIcon("edit");
    renameBtn.onclick = async (e) => {
      e.stopPropagation();
      const name = prompt("重命名会话：", s.title);
      if (name === null) return;
      const t = name.trim();
      if (!t) return;
      await apiPost("/api/sessions/" + s.id + "/rename", { title: t });
      refreshSessions();
    };
    const del = document.createElement("button");
    del.className = "item-btn danger";
    del.title = "删除";
    del.innerHTML = svgIcon("trash");
    del.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm("确定删除会话「" + s.title + "」？")) return;
      // 会话将被删除：终止其仍在进行中的流式请求并清理注册表项
      if (window.abortLiveStream) window.abortLiveStream(s.id);
      await apiDelete("/api/sessions/" + s.id);
      if (currentSessionId === s.id) {
        // 删的是当前会话：优先切到最新会话，无其他会话才新建
        const list = await apiGet("/api/sessions");
        if (list.length > 0) {
          await openSession(list[0].id);
        } else {
          await newSession();
        }
      } else {
        refreshSessions();
      }
    };
    li.appendChild(title);
    li.appendChild(exportBtn);
    li.appendChild(renameBtn);
    li.appendChild(del);
    ul.appendChild(li);
  });
}

// 把界面重置为「未选中对话」的空白态：清空对话区、显示「你好」欢迎提示、
// 参数重置为默认值。不创建会话。模型沿用当前 UI 选中值（上一个浏览对话使用的模型）。
function resetChatUI() {
  currentSessionId = null;
  conversation = [];
  sessionTotal = 0;
  sessionHasMore = false;
  sessionMinIndex = 0;
  sceneAnchors = []; // 场景锚点登记随会话清空
  applyVmToUI(null); // 新建对话：向量记忆默认关闭
  if (window.resetMemoryPanel) window.resetMemoryPanel(); // 记忆页：未选中会话占位
  const box = $("#chatBox");
  box.innerHTML = "";
  showEmptyHint(box); // 未选中对话时居中显示「你好」欢迎提示
  const model = $("#modelSelect").value || ($("#modelSelect").options[0] || {}).value || "";
  applyConfigToUI(model, PARAM_DEFAULTS);
  // 未选中会话：所有进行中的流式请求标记为不可见（后台继续累积，不中断）
  if (window.syncLiveStreams) window.syncLiveStreams(null);
  // 内容已整体清空：重置「跟随置底」状态，避免方向判断误触发
  if (window.resetScrollFollow) window.resetScrollFollow();
  refreshSessions(); // 清除侧栏激活态
}

// 新建会话：已选中对话时重置为空白态（不创建会话）；
// 未选中对话时提示已是最新。只有修改配置参数、prompt 或发送消息才正式创建会话。
async function newSession() {
  if (!currentSessionId) {
    showToast("已经是最新对话");
    return;
  }
  resetChatUI();
}

// 导入会话：读取用户选择的 JSON 文件（对应导出的 JSON 格式），新建会话并打开
function onImportClick() {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".json,application/json";
  input.onchange = async () => {
    const file = input.files && input.files[0];
    if (!file) return;
    try {
      const data = JSON.parse(await file.text());
      if (!data.messages || !Array.isArray(data.messages)) {
        showToast("导入失败：JSON 缺少 messages 数组");
        return;
      }
      const s = await apiPost("/api/sessions/import", data);
      await openSession(s.id);
      showToast("已导入会话「" + s.title + "」");
    } catch (_) {
      showToast("导入失败：无法解析 JSON 文件");
    }
  };
  input.click();
}

// 打开会话：载入会话配置与历史消息并渲染
// opts.suppressLatestMarkers=true 时跳过「最新一轮」标记的重新计算（保持 null）。
// 用于编辑流程：删除最后一条后，倒数第二条会变成最后一条，若重新标记，
// 用户尚未发送修改内容时该条也会误显「编辑/重试」按钮。
async function openSession(id, opts = {}) {
  // 长对话分段加载：初始只取最近 PAGE_SIZE 条渲染；更早消息由上滑滚动按需加载
  const s = await apiGet("/api/sessions/" + id + "/messages?limit=" + PAGE_SIZE);
  if (!s || s.error) return;
  currentSessionId = id;
  // 载入该会话独立的 model + 推理参数 + 向量记忆设置
  applyConfigToUI(s.model, s.params);
  applyVmToUI(s.vm);
  // 记忆分页：切换到新会话时重新加载其四层记忆（设置面板开着时用户可见）
  if (window.loadMemoryPanel) window.loadMemoryPanel();
  // content 始终是纯回答，直接用于模型上下文；reasoning 仅渲染；files 仅 user 消息携带。
  // 上下文仅包含已加载的消息，上滑加载更早消息后自动扩充。
  conversation = (s.messages || []).map((m) => ({ role: m.role, content: m.content, files: m.files || null }));
  const box = $("#chatBox");
  box.innerHTML = "";
  // 分页状态：本次返回的是最新一段消息（全局下标最大的一段）
  sessionTotal = s.total || 0;
  sessionHasMore = !!s.has_more;
  sessionMinIndex = s.messages && s.messages.length ? s.messages[0].index : 0;
  // 最新一轮 user 消息与最后一条 assistant 消息（全局下标），仅这些显示编辑/重试按钮
  lastUserMsgIndex = null;
  lastAssistantMsgIndex = null;
  if (!opts.suppressLatestMarkers) {
    (s.messages || []).forEach((m) => {
      if (m.role === "user") lastUserMsgIndex = m.index;
      if (m.role === "assistant") lastAssistantMsgIndex = m.index;
    });
  }
  // 渲染（msgIndex 用全局下标，与后端 delete_message 对齐）
  // 场景锚点登记：本页消息中带 scene_break 的 user 消息下标（升序），
  // 供分场默认命名「第 N 场」计数
  sceneAnchors = (s.messages || [])
    .filter((m) => m.role === "user" && m.scene_break)
    .map((m) => m.index);
  (s.messages || []).forEach((m) => {
    if (m.role === "assistant") {
      renderAssistant(m.content, m.reasoning || null, m.index, !!m.interrupted);
    } else {
      // 用户消息强制纯文本：Markdown 语法按原样显示，不渲染
      addMsgEl(m.role, m.content, false, m.index, m.files || null, null, true,
        m.scene_break ? { label: m.scene_label || "" } : null);
    }
  });
  // 渲染完成后滚动到最底部
  box.scrollTop = box.scrollHeight;
  // 内容已整体替换：重置「跟随置底」状态并同步方向锚点，避免方向判断误触发
  if (window.resetScrollFollow) window.resetScrollFollow();
  // 空会话（无消息）时居中显示欢迎提示，否则移除
  if (!box.querySelector(".msg-row")) showEmptyHint(box);
  else hideEmptyHint(box);
  refreshSessions();
  // 会话切换后的流式状态同步：本会话若有进行中的流式输出则恢复其气泡继续渲染，
  // 其它会话的流式请求标记为不可见（后台继续累积，不中断、不重置）。
  if (window.syncLiveStreams) window.syncLiveStreams(id);
  // 切换后清理其他空会话（保留当前正在查看的）
  await cleanupEmptySessions(currentSessionId);
}

// 上滑到顶部时按需加载更早的消息：prepend 到列表顶部并保持当前视口位置。
// 用已渲染最早消息的全局下标作为锚点（before），对后续发送/追加新消息免疫。
async function loadOlderMessages() {
  if (loadingOlder || !sessionHasMore || !currentSessionId) return;
  loadingOlder = true;
  const box = $("#chatBox");
  const prevHeight = box.scrollHeight;
  const prevScrollTop = box.scrollTop;
  try {
    const s = await apiGet("/api/sessions/" + currentSessionId + "/messages?limit=" + PAGE_SIZE + "&before=" + sessionMinIndex);
    if (!s || s.error) return;
    const msgs = s.messages || [];
    if (!msgs.length) { sessionHasMore = false; return; }
    const firstRow = box.querySelector(".msg-row"); // 原列表第一条，作为插入锚点
    msgs.forEach((m) => {
      if (m.role === "assistant") {
        renderAssistant(m.content, m.reasoning || null, m.index, !!m.interrupted, firstRow, false);
      } else {
        addMsgEl(m.role, m.content, false, m.index, m.files || null, firstRow, false,
          m.scene_break ? { label: m.scene_label || "" } : null);
      }
    });
    // 扩充模型上下文与分页状态
    conversation = msgs.map((m) => ({ role: m.role, content: m.content, files: m.files || null })).concat(conversation);
    sessionHasMore = !!s.has_more;
    sessionMinIndex = msgs[0].index;
    // 更早分页中的场景锚点并入登记（保持升序，供分场默认命名计数）
    const olderAnchors = msgs
      .filter((m) => m.role === "user" && m.scene_break)
      .map((m) => m.index);
    if (olderAnchors.length) {
      sceneAnchors = Array.from(new Set(olderAnchors.concat(sceneAnchors))).sort((a, b) => a - b);
    }
    // 顶部插入了新内容，滚动条整体下移：补偿 scrollTop，保持当前可视区域不动
    box.scrollTop = prevScrollTop + (box.scrollHeight - prevHeight);
  } finally {
    loadingOlder = false;
  }
}

// 清理空会话（无消息），排除 excludeId 指向的当前会话
async function cleanupEmptySessions(excludeId) {
  try {
    await apiPost("/api/sessions/cleanup", { exclude: excludeId });
    refreshSessions();
  } catch (_) {
    /* 清理失败不影响主流程 */
  }
}
