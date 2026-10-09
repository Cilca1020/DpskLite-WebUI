/* 记忆分页：设置面板「记忆」页的四层记忆管理
 *
 * 层级顺序（与后端注入顺序一致）：
 *   ⓪ 系统提示词        —— 无条件注入，作为 system 消息最先发送
 *   ① 核心设定          —— 世界卡（worlds，先注入）+ 人物卡（cards），
 *                          均无条件注入，随每次请求作为 system 前缀
 *   ② 动态关键事实      —— 无条件注入，自动抽取 + 可手动触发
 *   ③ 剧情摘要          —— 无条件注入，增量总结 + 可手动触发
 *   ④ 向量记忆          —— 按需召回，检索相似历史片段
 *
 * 记忆按会话单独存储；未选中会话时显示占位提示。
 */

// 当前记忆数据缓存（openSession / resetChatUI 时刷新）
let MEMORY_STATE = null;

// 读取记忆面板上的元素（可能尚未渲染，统一用安全取值）
const memEl = (id) => document.getElementById(id);

// 把「记忆」页各卡片恢复为未选中会话的占位态（顶部批量按钮一并隐藏）
function resetMemoryPanel() {
  MEMORY_STATE = null;
  MEMORY_CARD_SELECTED = null;
  MEMORY_WORLD_SELECTED = null;
  MEMORY_SLICE_OPEN = null;
  const empty = memEl("memoryEmptyHint");
  if (empty) empty.classList.remove("hidden");
  const bar = memEl("memoryMasterBar");
  if (bar) bar.classList.add("hidden");
  ["memorySystemGroup", "memoryCardGroup", "memoryFactsGroup", "memorySummaryGroup", "memoryVectorGroup"].forEach((id) => {
    const el = memEl(id);
    if (el) el.classList.add("hidden");
  });
}

// 异步加载当前会话的四层记忆并渲染到「记忆」页。
// 打开设置面板 / 切换会话时调用；无会话时显示占位提示。
async function loadMemoryPanel() {
  const empty = memEl("memoryEmptyHint");
  const groups = ["memorySystemGroup", "memoryCardGroup", "memoryFactsGroup", "memorySummaryGroup", "memoryVectorGroup"].map(memEl);

  if (!currentSessionId) {
    resetMemoryPanel();
    return;
  }
  if (empty) empty.classList.add("hidden");
  const bar = memEl("memoryMasterBar");
  if (bar) bar.classList.remove("hidden");
  groups.forEach((el) => el && el.classList.remove("hidden"));

  try {
    const r = await apiGet("/api/sessions/" + currentSessionId + "/memory");
    if (!r || r.error) {
      showToast(r && r.error ? r.error : "读取记忆失败");
      return;
    }
    MEMORY_STATE = r.memory || {};
    renderMemoryPanel();
  } catch (_) {
    showToast("读取记忆失败");
  }
}

// 把记忆数据渲染到各卡片
function renderMemoryPanel() {
  const mem = MEMORY_STATE || {};

  // ① 核心设定：世界卡（先） + 人物卡（后）；本地选中态在重渲染后保持
  renderMemoryWorlds();
  renderMemoryCards();

  // ② 动态关键事实
  renderFacts();
  const factsSlice = memEl("memoryFactsSlice");
  if (factsSlice && mem.facts_slice_rounds !== undefined && mem.facts_slice_rounds !== null) {
    factsSlice.value = mem.facts_slice_rounds;
  }
  const factsLimit = memEl("memoryFactsLimit");
  if (factsLimit && mem.facts_max_per_slice !== undefined && mem.facts_max_per_slice !== null) {
    factsLimit.value = mem.facts_max_per_slice;
  }

  // 最近 N 轮上下文策略
  const recentN = (mem.recent_n !== undefined && mem.recent_n !== null)
    ? mem.recent_n
    : ((mem.vector || {}).recent_n !== undefined ? (mem.vector || {}).recent_n : 10);
  const recentField = memEl("memoryContextN") || memEl("vmRecentN");
  if (recentField) recentField.value = recentN;
  if (window.vmSaveN) window.vmSaveN(recentN);

  // ③ 剧情摘要
  const summary = mem.summary || {};
  const summaryText = memEl("memorySummaryText");
  if (summaryText) summaryText.value = summary.text || "";
  const meta = memEl("memorySummaryMeta");
  if (meta) {
    const last = summary.last_round;
    const sliceCount = Array.isArray(summary.slices) ? summary.slices.length : 0;
    meta.textContent = (last
      ? "已总结至第 " + last + " 轮" + (summary.slice_rounds ? "（切片 " + summary.slice_rounds + " 轮）" : "") + (sliceCount ? " · " + sliceCount + " 个切片" : "")
      : "尚未总结");
  }
  const sliceInput = memEl("memorySummarySlice");
  if (sliceInput && summary.slice_rounds !== undefined && summary.slice_rounds !== null) {
    sliceInput.value = summary.slice_rounds;
  }
  // 切片管理面板（持久化切片实体列表；无实体时隐藏）
  renderSummarySlices(summary);
  // 摘要卡「切片摘要」分页：只读列表 + 恢复当前分页选择
  renderSliceMiniList(summary);
  setSummaryTab(SUMMARY_TAB);

  // ④ 向量记忆：模型/开关/N 由 applyVmToUI 管理，这里同步 top_k 输入框
  const vec = mem.vector || {};
  const topK = memEl("vmTopK");
  if (topK) {
    // 0 表示自动召回，作为合法值保留；null/undefined 显示为空（后端用默认）
    topK.value = (vec.top_k === null || vec.top_k === undefined) ? "" : vec.top_k;
    topK.disabled = !vec.enabled;
  }
  refreshMemoryCardStates();
}

// 向量记忆层：模型选择/开关/N 的可见性与禁用由 main.js（syncVmUi）管理，
// 这里只保证 top_k 输入框跟随启用状态
function syncMemoryVectorField() {
  const en = memEl("vmEnabled");
  const topK = memEl("vmTopK");
  const topKField = memEl("vmTopKField");
  const hasModel = !!(window.vmLoadModel && window.vmLoadModel());
  if (topKField) topKField.classList.toggle("vm-hidden", !hasModel);
  if (topK) topK.disabled = !hasModel || !(en && en.checked);
}

// 同步卡片 2/3/4 的开关状态与置灰（内容保留但不可编辑）。
// 在渲染完成、开关变化、一键配置/关闭智能总结后即时调用；
// 通过操作 checkbox 的 checked 触发 CSS 过渡，让开关动画即时生效（无需刷新）。
function refreshMemoryCardStates() {
  const mem = MEMORY_STATE || {};

  // ② 动态关键事实
  const factsEnabled = mem.facts_enabled !== false;
  const factsSwitch = memEl("memoryFactsEnabled");
  if (factsSwitch) factsSwitch.checked = factsEnabled;
  const factsGroup = memEl("memoryFactsGroup");
  if (factsGroup) factsGroup.classList.toggle("memory-off", !factsEnabled);
  // 自动总结开关（总开关下一级）：默认开启
  const factsAuto = memEl("memoryFactsAuto");
  if (factsAuto) factsAuto.checked = mem.facts_auto !== false;

  // ③ 剧情摘要
  const summary = mem.summary || {};
  const summaryEnabled = summary.enabled !== false;
  const summarySwitch = memEl("memorySummaryEnabled");
  if (summarySwitch) summarySwitch.checked = summaryEnabled;
  const summaryGroup = memEl("memorySummaryGroup");
  if (summaryGroup) summaryGroup.classList.toggle("memory-off", !summaryEnabled);
  // 自动总结开关（总开关下一级）：默认开启；关闭时切片轮数输入置灰不可编辑
  const summaryAutoSwitch = memEl("memorySummaryAutoSwitch");
  const summaryAutoOn = summary.auto !== false;
  if (summaryAutoSwitch) summaryAutoSwitch.checked = summaryAutoOn;
  const summarySlice = memEl("memorySummarySlice");
  if (summarySlice) summarySlice.disabled = !summaryAutoOn;

  // ④ 向量记忆：开关由 main.js / chat.js（vmEnabled）管理，这里按可见开关态同步置灰与 top_k 禁用态
  const vectorSwitch = memEl("vmEnabled");
  const vecEnabled = vectorSwitch ? !!vectorSwitch.checked : !!(mem.vector || {}).enabled;
  const vectorGroup = memEl("memoryVectorGroup");
  if (vectorGroup) vectorGroup.classList.toggle("memory-off", !vecEnabled);
  syncMemoryVectorField();
}

/* ---------------- ③ 切片管理面板 ---------------- */

// 切片面板本地状态：当前展开的切片 id
let MEMORY_SLICE_OPEN = null;

// 摘要卡二选一分页：text = 文本摘要（原功能），slice = 切片摘要（只读）
let SUMMARY_TAB = "text";

// 切换摘要卡分页：按钮高亮 + 两个页面容器即时显隐（无动画）
function setSummaryTab(tab) {
  SUMMARY_TAB = tab === "slice" ? "slice" : "text";
  const tabText = memEl("summaryTabText");
  const tabSlice = memEl("summaryTabSlice");
  const pageText = memEl("summaryPageText");
  const pageSlice = memEl("summaryPageSlice");
  if (tabText) tabText.classList.toggle("active", SUMMARY_TAB === "text");
  if (tabSlice) tabSlice.classList.toggle("active", SUMMARY_TAB === "slice");
  if (pageText) pageText.classList.toggle("hidden", SUMMARY_TAB !== "text");
  if (pageSlice) pageSlice.classList.toggle("hidden", SUMMARY_TAB !== "slice");
}

// 渲染持久化切片列表（summary.slices）。每片：折叠头（名称 + 轮次范围）+
// 展开体（场景名输入 / 本片摘要 textarea / 本片对话内容，均为失焦自动保存）。
function renderSummarySlices(summary) {
  const list = memEl("memorySliceList");
  if (!list) return;
  const slices = Array.isArray(summary.slices) ? summary.slices : [];
  if (!slices.length) {
    list.classList.remove("hidden");
    list.innerHTML = "";
    const empty = document.createElement("p");
    empty.className = "memory-empty-box";
    empty.textContent = "暂无切片，添加锚点后可新建切片，用于区分不同话题/场景等。";
    list.appendChild(empty);
    return;
  }
  list.innerHTML = "";
  slices.forEach((sl, i) => {
    const isOpen = MEMORY_SLICE_OPEN === sl.id;
    const item = document.createElement("div");
    item.className = "memory-slice-item" + (isOpen ? " open" : "");
    item.dataset.id = sl.id;

    const head = document.createElement("div");
    head.className = "memory-slice-head";
    head.title = "点击展开 / 收起";
    const name = document.createElement("span");
    name.className = "memory-slice-name";
    name.textContent = sl.name || ("第" + (i + 1) + "片");
    const range = document.createElement("span");
    range.className = "memory-slice-range";
    range.textContent = "第" + sl.start_round + "–" + sl.end_round + "轮" + (sl.edited ? " · 已编辑" : "");
    head.appendChild(name);
    head.appendChild(range);
    head.addEventListener("click", () => toggleSliceItem(sl.id));
    item.appendChild(head);

    const body = document.createElement("div");
    body.className = "memory-slice-body";
    // 展开动画：外层 grid 0fr/1fr 过渡裁剪，内容放 inner（padding/border 在 inner 上过渡）
    const inner = document.createElement("div");
    inner.className = "memory-slice-body-inner";
    body.appendChild(inner);
    const nameField = document.createElement("label");
    nameField.className = "memory-slice-field";
    const nameSpan = document.createElement("span");
    nameSpan.textContent = "场景名";
    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.value = sl.name || "";
    nameInput.placeholder = "留空 = 第N片；改名同步消息分割线";
    nameInput.addEventListener("change", () => {
      const v = (nameInput.value || "").trim();
      if (v === (sl.name || "")) return;
      saveSliceField(sl, { name: v });
    });
    nameField.appendChild(nameSpan);
    nameField.appendChild(nameInput);

    const textField = document.createElement("label");
    textField.className = "memory-slice-field";
    const textSpan = document.createElement("span");
    textSpan.textContent = "本片摘要";
    const textArea = document.createElement("textarea");
    textArea.rows = 3;
    textArea.value = sl.text || "";
    textArea.placeholder = "本切片的剧情摘要（失焦自动保存）";
    textArea.addEventListener("change", () => {
      const v = (textArea.value || "").trim();
      if (v === (sl.text || "")) return;
      saveSliceField(sl, { text: v });
    });
    textField.appendChild(textSpan);
    textField.appendChild(textArea);

    const msgsBox = document.createElement("div");
    msgsBox.className = "memory-slice-msgs";
    msgsBox.dataset.loaded = "0";
    msgsBox.textContent = "（对话内容加载中…）";

    inner.appendChild(nameField);
    inner.appendChild(textField);
    inner.appendChild(msgsBox);
    item.appendChild(body);
    list.appendChild(item);
    if (isOpen) loadSliceMessages(sl.id, msgsBox);
  });
}

// 摘要卡「切片摘要」分页：只读渲染切片名 + 本片摘要（编辑请点「管理切片」）
function renderSliceMiniList(summary) {
  const list = memEl("memorySliceMiniList");
  if (!list) return;
  const slices = Array.isArray(summary.slices) ? summary.slices : [];
  list.innerHTML = "";
  if (!slices.length) {
    const empty = document.createElement("p");
    empty.className = "memory-empty-box";
    empty.textContent = "暂无切片，添加锚点后可新建切片，用于区分不同话题/场景等。";
    list.appendChild(empty);
    return;
  }
  slices.forEach((sl, i) => {
    const item = document.createElement("div");
    item.className = "slice-mini-item";

    const head = document.createElement("div");
    head.className = "slice-mini-head";
    const name = document.createElement("span");
    name.className = "slice-mini-name";
    name.textContent = sl.name || ("第" + (i + 1) + "片");
    const range = document.createElement("span");
    range.className = "slice-mini-range";
    range.textContent = "第" + sl.start_round + "–" + sl.end_round + "轮" + (sl.edited ? " · 已编辑" : "");
    head.appendChild(name);
    head.appendChild(range);

    const text = document.createElement("div");
    text.className = "slice-mini-text";
    text.textContent = sl.text || "（本片暂无摘要）";

    item.appendChild(head);
    item.appendChild(text);
    list.appendChild(item);
  });
}

// 展开 / 收起某个切片（grid 0fr/1fr 过渡动画）；首次展开时懒加载该片的对话内容
async function toggleSliceItem(sliceId) {
  const item = document.querySelector('#memorySliceList .memory-slice-item[data-id="' + sliceId + '"]');
  if (!item) return;
  const opening = !item.classList.contains("open");
  MEMORY_SLICE_OPEN = opening ? sliceId : null;
  // 单开互斥：收起其它已展开项
  document.querySelectorAll("#memorySliceList .memory-slice-item.open").forEach((el) => {
    if (el !== item) el.classList.remove("open");
  });
  item.classList.toggle("open", opening);
  if (opening) {
    const box = item.querySelector(".memory-slice-msgs");
    if (box && box.dataset.loaded === "0") await loadSliceMessages(sliceId, box);
  }
}

// 拉取会话消息并渲染该片轮次范围内的对话内容（长文本截断展示）
async function loadSliceMessages(sliceId, box) {
  const slices = ((MEMORY_STATE || {}).summary || {}).slices || [];
  const sl = slices.find((x) => x.id === sliceId);
  if (!sl) return;
  box.dataset.loaded = "1";
  try {
    const s = await apiGet("/api/sessions/" + currentSessionId);
    if (!s || s.error) { box.textContent = "（读取对话失败）"; return; }
    const all = (s.messages || []).filter((m) => m.role !== "system");
    const out = [];
    let seen = 0;
    for (const m of all) {
      if (m.role === "user") {
        seen++;
        if (seen > sl.end_round) break;
      }
      if (seen >= sl.start_round && seen <= sl.end_round) out.push(m);
    }
    box.textContent = "";
    if (!out.length) {
      box.textContent = "（未找到对应轮次的消息，可能已被删除）";
      return;
    }
    out.forEach((m) => {
      const row = document.createElement("div");
      row.className = "memory-slice-msg " + m.role;
      const who = document.createElement("span");
      who.className = "memory-slice-msg-role";
      who.textContent = m.role === "user" ? "用户" : "AI";
      const txt = document.createElement("span");
      txt.className = "memory-slice-msg-text";
      const c = m.content || "";
      txt.textContent = c.length > 300 ? c.slice(0, 300) + "…" : c;
      txt.title = c;
      row.appendChild(who);
      row.appendChild(txt);
      box.appendChild(row);
    });
  } catch (_) {
    box.textContent = "（读取对话失败）";
  }
}

// 保存单片编辑（名称 / 摘要）。改名后同步前端分割线文本（就地，不重绘会话）。
async function saveSliceField(sl, patch) {
  if (!currentSessionId) return;
  try {
    const r = await apiPost(
      "/api/sessions/" + currentSessionId + "/summary-slice/" + encodeURIComponent(sl.id),
      patch
    );
    if (r && r.error) { showToast(r.error); await loadMemoryPanel(); return; }
    MEMORY_STATE = r.memory || MEMORY_STATE;
    showToast("已保存");
    if ("name" in patch) syncSceneDividerText(sl.start_round, patch.name);
    renderSummarySlices((MEMORY_STATE || {}).summary || {});
  } catch (_) {
    showToast("保存失败");
  }
}

// 面板改名 → 分割线同步：按轮次定位锚点分割线并更新其显示文本（空名回退默认「第 N 场」）
function syncSceneDividerText(startRound, name) {
  if (typeof conversation === "undefined" || !Array.isArray(conversation)) return;
  let seen = 0;
  for (let i = 0; i < conversation.length; i++) {
    if (conversation[i].role === "user") {
      seen++;
      if (seen === startRound) {
        const div = document.querySelector('.scene-divider[data-index="' + i + '"]');
        if (div) {
          const lbl = div.querySelector(".scene-divider-label");
          if (lbl) {
            const shown = (name || "").trim() || (typeof defaultSceneName === "function" ? defaultSceneName(i) : "");
            lbl.textContent = shown;
            lbl.title = shown;
          }
        }
        return;
      }
    }
  }
}

// 分割线改名 → 切片实体同步（render.js 分场编辑回调）：
// 找起点轮次与该锚点一致的切片，把新名字写回实体（面板列表刷新）。
window.syncSliceNameFromAnchor = async function (msgIndex, label) {
  if (!currentSessionId || !MEMORY_STATE) return;
  if (typeof conversation === "undefined" || !Array.isArray(conversation)) return;
  let seen = 0;
  for (let i = 0; i <= msgIndex && i < conversation.length; i++) {
    if (conversation[i].role === "user") seen++;
  }
  const slices = ((MEMORY_STATE.summary || {}).slices) || [];
  const sl = slices.find((x) => x.start_round === seen);
  if (!sl) return;
  if ((sl.name || "") === (label || "")) return;
  try {
    const r = await apiPost(
      "/api/sessions/" + currentSessionId + "/summary-slice/" + encodeURIComponent(sl.id),
      { name: label || "" }
    );
    if (r && r.error) return;
    MEMORY_STATE = r.memory || MEMORY_STATE;
    renderSummarySlices((MEMORY_STATE || {}).summary || {});
  } catch (_) { /* 分割线已更新，切片同步失败静默 */ }
};

/* ---------------- 事件绑定 ---------------- */

// 人物卡模块本地状态：当前在编辑器中打开的卡 id
let MEMORY_CARD_SELECTED = null;

// 会话人物卡列表
function sessionCards() {
  const mem = MEMORY_STATE || {};
  return Array.isArray(mem.cards) ? mem.cards : [];
}

// 人物卡条目操作 API（增删改）
async function cardsItemOp(op, extra = {}) {
  if (!currentSessionId) { showToast("请先选择对话"); return null; }
  try {
    const r = await apiPost("/api/sessions/" + currentSessionId + "/cards-item", Object.assign({ op }, extra));
    if (r && r.error) { showToast(r.error); return null; }
    if (r && r.memory) MEMORY_STATE = r.memory;
    return r;
  } catch (_) {
    showToast("操作失败");
    return null;
  }
}

// 添加角色：建一张空卡并打开编辑（入口为列表末尾「+ 新建」）
async function addNewCard() {
  const r = await cardsItemOp("add", { name: "", content: "" });
  if (r) {
    const cards = sessionCards();
    MEMORY_CARD_SELECTED = cards.length ? cards[cards.length - 1].id : null;
    renderMemoryCards();
    const nameInput = memEl("memoryCardName");
    if (nameInput) nameInput.focus();
  }
}

// ① 渲染人物卡列表 + 编辑器（选中态跨重渲染保持）
function renderMemoryCards() {
  const cards = sessionCards();
  const list = memEl("memoryCardsList");
  const editor = memEl("memoryCardEditor");
  if (!list || !editor) return;

  // 选中卡若已被删除则回落到第一张
  if (MEMORY_CARD_SELECTED && !cards.some((c) => c.id === MEMORY_CARD_SELECTED)) {
    MEMORY_CARD_SELECTED = null;
  }
  if (!MEMORY_CARD_SELECTED && cards.length) MEMORY_CARD_SELECTED = cards[0].id;
  const selected = cards.find((c) => c.id === MEMORY_CARD_SELECTED) || null;

  // 列表：一角色一项，点击选中编辑；当前选中的高亮
  // 主角色卡（main=true）：AI 第一人称扮演的角色，带 ★ 标记；允许没有任何主角色
  const mainId = (cards.find((c) => c.main) || {}).id || null;
  list.innerHTML = "";
  cards.forEach((c) => {
    // ★ 星标规则：主角色始终显示（主题色）；非主角色未选中时隐藏，选中后显示淡灰色
    // 星标本身即切换按钮：点非主角色的星标设为主角色；点主角色的星标取消（允许置空）
    const isSelected = selected && c.id === selected.id;
    const isMain = c.id === mainId;
    const item = document.createElement("div");
    item.className = "memory-card-item" + (isSelected ? " selected" : "");
    const star = document.createElement("button");
    star.type = "button";
    star.className = "memory-card-item-star"
      + (isMain ? " is-main is-clickable" : (isSelected ? " is-dim is-clickable" : " is-hidden"));
    star.innerHTML = svgIcon("star");
    star.title = isMain ? "取消主角色（置空后 AI 不固定扮演任何角色）"
      : (isSelected ? "设为主角色（AI 第一人称扮演的角色）" : "");
    if (isSelected) {
      star.addEventListener("click", async (e) => {
        e.stopPropagation();
        const r = await cardsItemOp("set-main", isMain ? { id: "" } : { id: c.id });
        if (r) {
          renderMemoryCards();
          showToast(isMain ? "已取消主角色" : "已设为主角色");
        }
      });
    }
    item.appendChild(star);
    const name = document.createElement("span");
    name.className = "memory-card-item-name";
    name.textContent = c.name || "未命名";
    name.title = c.content ? c.content.slice(0, 80) : "（空卡）";
    item.appendChild(name);
    if (isSelected) {
      const del = document.createElement("button");
      del.className = "memory-card-item-del";
      del.type = "button";
      // 叉号用 SVG 绘制，避免字形垂直偏移导致的不对齐
      del.innerHTML =
        '<svg viewBox="0 0 10 10" width="10" height="10" aria-hidden="true">' +
        '<path d="M1.5 1.5 L8.5 8.5 M8.5 1.5 L1.5 8.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" fill="none"/></svg>';
      del.title = "删除该角色卡";
      del.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm("删除角色卡「" + (c.name || "未命名") + "」？")) return;
        const r = await cardsItemOp("delete", { id: c.id });
        if (r) {
          MEMORY_CARD_SELECTED = null;
          renderMemoryCards();
          showToast("已删除");
        }
      });
      item.appendChild(del);
    }
    item.addEventListener("click", () => {
      if (MEMORY_CARD_SELECTED === c.id) return;
      MEMORY_CARD_SELECTED = c.id;
      renderMemoryCards();
    });
    list.appendChild(item);
  });
  if (!cards.length) {
    const empty = document.createElement("div");
    empty.className = "memory-cards-empty";
    empty.textContent = "暂无角色卡，点击「+ 新建」或「导入」创建。";
    list.appendChild(empty);
  }

  // 新建入口：始终排在列表最右侧（+ 图标 + 新建）
  const add = document.createElement("button");
  add.type = "button";
  add.className = "memory-card-item memory-card-item-new";
  add.title = "新建角色卡";
  const plus = document.createElement("span");
  plus.className = "memory-card-item-plus";
  plus.textContent = "+";
  const addLabel = document.createElement("span");
  addLabel.className = "memory-card-item-name";
  addLabel.textContent = "新建";
  add.appendChild(plus);
  add.appendChild(addLabel);
  add.addEventListener("click", () => addNewCard());
  list.appendChild(add);

  // 编辑器：只展示当前选中的卡
  const nameInput = memEl("memoryCardName");
  const contentInput = memEl("memoryCardContent");
  if (!selected) {
    editor.classList.add("hidden");
    if (nameInput) nameInput.value = "";
    if (contentInput) contentInput.value = "";
    return;
  }
  editor.classList.remove("hidden");
  // 失焦自动保存期间会禁用输入框，此时避免覆盖用户正在输入的内容
  if (nameInput && document.activeElement !== nameInput) nameInput.value = selected.name || "";
  if (contentInput && document.activeElement !== contentInput) contentInput.value = selected.content || "";
}

// ① 人物卡：添加 / 导入 / 导出 / 编辑器失焦自动保存
function bindMemoryCards() {
  const nameInput = memEl("memoryCardName");
  const contentInput = memEl("memoryCardContent");

  const saveSelected = async (field) => {
    const selected = sessionCards().find((c) => c.id === MEMORY_CARD_SELECTED);
    if (!selected || !currentSessionId) return;
    const name = nameInput ? nameInput.value : selected.name;
    const content = contentInput ? contentInput.value : selected.content;
    if (name === selected.name && content === selected.content) return; // 无变化不保存
    if (field) field.disabled = true;
    const r = await cardsItemOp("update", { id: selected.id, name, content });
    if (field) field.disabled = false;
    if (r) {
      renderMemoryCards();
      showToast("已保存");
    }
  };
  if (nameInput) nameInput.addEventListener("change", () => saveSelected(nameInput));
  if (contentInput) contentInput.addEventListener("change", () => saveSelected(contentInput));

  // 添加角色：建一张空卡并打开编辑（入口为列表末尾「+ 新建」，见 addNewCard）

  // 导入：JSON（{name,content} / 数组 / {cards:[...]}) 或纯文本；全部作为新卡追加
  const importBtn = memEl("memoryCardImportBtn");
  const fileInput = memEl("memoryCardImportFile");
  if (importBtn && fileInput) {
    importBtn.onclick = () => fileInput.click();
    fileInput.addEventListener("change", async () => {
      const file = fileInput.files && fileInput.files[0];
      fileInput.value = "";
      if (!file || !currentSessionId) return;
      let text = "";
      try { text = await file.text(); } catch (_) { return showToast("读取文件失败"); }
      // 识别内容：JSON 对象/数组优先，失败则整个文件当纯文本正文
      let imported = null; // [{name, content}]
      const baseName = file.name.replace(/\.[^.]+$/, "");
      try {
        const data = JSON.parse(text);
        const arr = Array.isArray(data) ? data : (Array.isArray(data.cards) ? data.cards : [data]);
        imported = arr
          .map((c) => ({
            name: String((c && c.name) || "").trim(),
            content: String((c && (c.content || c.text || c.card)) || "").trim(),
          }))
          .filter((c) => c.content);
      } catch (_) { /* 非 JSON，按纯文本处理 */ }
      if (!imported || !imported.length) {
        const content = text.trim();
        if (!content) return showToast("文件为空");
        imported = [{ name: baseName, content }];
      }
      // 逐卡追加（name 缺省用文件名）
      let lastId = null;
      for (const c of imported) {
        const r = await cardsItemOp("add", { name: c.name || baseName, content: c.content });
        if (!r) return;
        const cards = sessionCards();
        lastId = cards.length ? cards[cards.length - 1].id : null;
      }
      MEMORY_CARD_SELECTED = lastId;
      renderMemoryCards();
      showToast("已导入 " + imported.length + " 张角色卡");
    });
  }

  // 导出：全部卡片打包为 JSON 下载
  const exportBtn = memEl("memoryCardExportBtn");
  if (exportBtn) {
    exportBtn.onclick = () => {
      const cards = sessionCards().filter((c) => c.content);
      if (!cards.length) return showToast("没有可导出的角色卡");
      const payload = {
        exported_at: new Date().toISOString(),
        cards: cards.map((c) => ({ name: c.name || "未命名", content: c.content })),
      };
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "人物卡-" + (cards.length === 1 ? (cards[0].name || "未命名") : "全部") + ".json";
      a.click();
      URL.revokeObjectURL(a.href);
    };
  }
}

/* ---------------- 世界卡（① 核心设定·世界卡，先于人物卡注入） ---------------- */

// 世界卡模块本地状态：当前在编辑器中打开的卡 id
let MEMORY_WORLD_SELECTED = null;

// 会话世界卡列表
function sessionWorlds() {
  const mem = MEMORY_STATE || {};
  return Array.isArray(mem.worlds) ? mem.worlds : [];
}

// 世界卡条目操作 API（增删改）
async function worldsItemOp(op, extra = {}) {
  if (!currentSessionId) { showToast("请先选择对话"); return null; }
  try {
    const r = await apiPost("/api/sessions/" + currentSessionId + "/worlds-item", Object.assign({ op }, extra));
    if (r && r.error) { showToast(r.error); return null; }
    if (r && r.memory) MEMORY_STATE = r.memory;
    return r;
  } catch (_) {
    showToast("操作失败");
    return null;
  }
}

// 添加世界卡：建一张空卡并打开编辑（入口为列表末尾「+ 新建」）
async function addNewWorld() {
  const r = await worldsItemOp("add", { name: "", content: "" });
  if (r) {
    const worlds = sessionWorlds();
    MEMORY_WORLD_SELECTED = worlds.length ? worlds[worlds.length - 1].id : null;
    renderMemoryWorlds();
    const nameInput = memEl("memoryWorldName");
    if (nameInput) nameInput.focus();
  }
}

// ① 渲染世界卡列表 + 编辑器（选中态跨重渲染保持）
function renderMemoryWorlds() {
  const worlds = sessionWorlds();
  const list = memEl("memoryWorldsList");
  const editor = memEl("memoryWorldEditor");
  if (!list || !editor) return;

  // 选中卡若已被删除则回落到第一张
  if (MEMORY_WORLD_SELECTED && !worlds.some((w) => w.id === MEMORY_WORLD_SELECTED)) {
    MEMORY_WORLD_SELECTED = null;
  }
  if (!MEMORY_WORLD_SELECTED && worlds.length) MEMORY_WORLD_SELECTED = worlds[0].id;
  const selected = worlds.find((w) => w.id === MEMORY_WORLD_SELECTED) || null;

  // 列表：一张卡一项，点击选中编辑；当前选中的高亮
  list.innerHTML = "";
  worlds.forEach((w) => {
    const item = document.createElement("div");
    item.className = "memory-card-item" + (selected && w.id === selected.id ? " selected" : "");
    const name = document.createElement("span");
    name.className = "memory-card-item-name";
    name.textContent = w.name || "未命名";
    name.title = w.content ? w.content.slice(0, 80) : "（空卡）";
    item.appendChild(name);
    if (selected && w.id === selected.id) {
      const del = document.createElement("button");
      del.className = "memory-card-item-del";
      del.type = "button";
      // 叉号用 SVG 绘制，避免字形垂直偏移导致的不对齐
      del.innerHTML =
        '<svg viewBox="0 0 10 10" width="10" height="10" aria-hidden="true">' +
        '<path d="M1.5 1.5 L8.5 8.5 M8.5 1.5 L1.5 8.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" fill="none"/></svg>';
      del.title = "删除该世界卡";
      del.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm("删除世界卡「" + (w.name || "未命名") + "」？")) return;
        const r = await worldsItemOp("delete", { id: w.id });
        if (r) {
          MEMORY_WORLD_SELECTED = null;
          renderMemoryWorlds();
          showToast("已删除");
        }
      });
      item.appendChild(del);
    }
    item.addEventListener("click", () => {
      if (MEMORY_WORLD_SELECTED === w.id) return;
      MEMORY_WORLD_SELECTED = w.id;
      renderMemoryWorlds();
    });
    list.appendChild(item);
  });
  if (!worlds.length) {
    const empty = document.createElement("div");
    empty.className = "memory-cards-empty";
    empty.textContent = "暂无世界卡，点击「+ 新建」或「导入」创建。";
    list.appendChild(empty);
  }

  // 新建入口：始终排在列表最右侧（+ 图标 + 新建）
  const add = document.createElement("button");
  add.type = "button";
  add.className = "memory-card-item memory-card-item-new";
  add.title = "新建世界卡";
  const plus = document.createElement("span");
  plus.className = "memory-card-item-plus";
  plus.textContent = "+";
  const addLabel = document.createElement("span");
  addLabel.className = "memory-card-item-name";
  addLabel.textContent = "新建";
  add.appendChild(plus);
  add.appendChild(addLabel);
  add.addEventListener("click", () => addNewWorld());
  list.appendChild(add);

  // 编辑器：只展示当前选中的卡
  const nameInput = memEl("memoryWorldName");
  const contentInput = memEl("memoryWorldContent");
  if (!selected) {
    editor.classList.add("hidden");
    if (nameInput) nameInput.value = "";
    if (contentInput) contentInput.value = "";
    return;
  }
  editor.classList.remove("hidden");
  if (nameInput && document.activeElement !== nameInput) nameInput.value = selected.name || "";
  if (contentInput && document.activeElement !== contentInput) contentInput.value = selected.content || "";
}

// ① 世界卡：添加 / 导入 / 导出 / 编辑器失焦自动保存
function bindMemoryWorlds() {
  const nameInput = memEl("memoryWorldName");
  const contentInput = memEl("memoryWorldContent");

  const saveSelected = async (field) => {
    const selected = sessionWorlds().find((w) => w.id === MEMORY_WORLD_SELECTED);
    if (!selected || !currentSessionId) return;
    const name = nameInput ? nameInput.value : selected.name;
    const content = contentInput ? contentInput.value : selected.content;
    if (name === selected.name && content === selected.content) return; // 无变化不保存
    if (field) field.disabled = true;
    const r = await worldsItemOp("update", { id: selected.id, name, content });
    if (field) field.disabled = false;
    if (r) {
      renderMemoryWorlds();
      showToast("已保存");
    }
  };
  if (nameInput) nameInput.addEventListener("change", () => saveSelected(nameInput));
  if (contentInput) contentInput.addEventListener("change", () => saveSelected(contentInput));

  // 导入：JSON（{name,content} / 数组 / {worlds:[...]}) 或纯文本；全部作为新卡追加
  const importBtn = memEl("memoryWorldImportBtn");
  const fileInput = memEl("memoryWorldImportFile");
  if (importBtn && fileInput) {
    importBtn.onclick = () => fileInput.click();
    fileInput.addEventListener("change", async () => {
      const file = fileInput.files && fileInput.files[0];
      fileInput.value = "";
      if (!file || !currentSessionId) return;
      let text = "";
      try { text = await file.text(); } catch (_) { return showToast("读取文件失败"); }
      // 识别内容：JSON 对象/数组优先，失败则整个文件当纯文本正文
      let imported = null; // [{name, content}]
      const baseName = file.name.replace(/\.[^.]+$/, "");
      try {
        const data = JSON.parse(text);
        const arr = Array.isArray(data) ? data : (Array.isArray(data.worlds) ? data.worlds : [data]);
        imported = arr
          .map((w) => ({
            name: String((w && w.name) || "").trim(),
            content: String((w && (w.content || w.text || w.card)) || "").trim(),
          }))
          .filter((w) => w.content);
      } catch (_) { /* 非 JSON，按纯文本处理 */ }
      if (!imported || !imported.length) {
        const content = text.trim();
        if (!content) return showToast("文件为空");
        imported = [{ name: baseName, content }];
      }
      // 逐卡追加（name 缺省用文件名）
      let lastId = null;
      for (const w of imported) {
        const r = await worldsItemOp("add", { name: w.name || baseName, content: w.content });
        if (!r) return;
        const worlds = sessionWorlds();
        lastId = worlds.length ? worlds[worlds.length - 1].id : null;
      }
      MEMORY_WORLD_SELECTED = lastId;
      renderMemoryWorlds();
      showToast("已导入 " + imported.length + " 张世界卡");
    });
  }

  // 导出：全部世界卡打包为 JSON 下载
  const exportBtn = memEl("memoryWorldExportBtn");
  if (exportBtn) {
    exportBtn.onclick = () => {
      const worlds = sessionWorlds().filter((w) => w.content);
      if (!worlds.length) return showToast("没有可导出的世界卡");
      const payload = {
        exported_at: new Date().toISOString(),
        worlds: worlds.map((w) => ({ name: w.name || "未命名", content: w.content })),
      };
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "世界卡-" + (worlds.length === 1 ? (worlds[0].name || "未命名") : "全部") + ".json";
      a.click();
      URL.revokeObjectURL(a.href);
    };
  }
}

// ② 动态关键事实：条目级操作 API（增删改 / 上锁解锁）
async function factsItemOp(op, index, extra = {}) {
  if (!currentSessionId) { showToast("请先选择对话"); return null; }
  try {
    const r = await apiPost("/api/sessions/" + currentSessionId + "/facts-item", Object.assign({ op, index }, extra));
    if (r && r.error) { showToast(r.error); return null; }
    if (r && r.memory) {
      MEMORY_STATE = r.memory;
      renderFacts();
    }
    return r;
  } catch (_) {
    showToast("操作失败");
    return null;
  }
}

// 小图标按钮（拖动手柄 / 锁 / 编辑 / 删除）；onClick 可省略（纯手柄按钮）
function factIconBtn(title, svgPath, onClick) {
  const btn = document.createElement("button");
  btn.className = "fact-icon-btn";
  btn.type = "button";
  btn.title = title;
  btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + svgPath + "</svg>";
  if (onClick) btn.addEventListener("click", onClick);
  return btn;
}

// ② 渲染事实条目列表：每条带 拖动手柄(拖拽排序)/上锁/编辑/删除 操作
function renderFacts() {
  const mem = MEMORY_STATE || {};
  const facts = Array.isArray(mem.facts) ? mem.facts : [];
  const factsList = memEl("memoryFactsList");
  if (!factsList) return;
  factsList.innerHTML = "";
  if (!facts.length) {
    const p = document.createElement("p");
    p.className = "memory-empty-text";
    p.textContent = "暂无事实。对话中会自动抽取，也可手动添加或点击「重新总结」。";
    factsList.appendChild(p);
    return;
  }
  const GRIP_SVG = '<circle cx="9" cy="5" r="1.3"/><circle cx="15" cy="5" r="1.3"/><circle cx="9" cy="12" r="1.3"/><circle cx="15" cy="12" r="1.3"/><circle cx="9" cy="19" r="1.3"/><circle cx="15" cy="19" r="1.3"/>';
  const LOCK_SVG = '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>';               // 闭锁
  const UNLOCK_SVG = '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V7a4 4 0 0 1 7.9-.9"/>';            // 开锁
  const EDIT_SVG = '<path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>';
  const DEL_SVG = '<path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/>';
  // 清理所有拖拽过程中的标记（源条目置灰 + 放置指示线）
  const clearDropMarks = () => {
    factsList.querySelectorAll(".fact-dragging, .fact-drop-before, .fact-drop-after").forEach((el) => {
      el.classList.remove("fact-dragging", "fact-drop-before", "fact-drop-after");
    });
  };
  facts.forEach((f, i) => {
    const locked = !!(f && f.locked);
    const row = document.createElement("div");
    row.className = "memory-fact-row" + (locked ? " fact-locked" : "");

    // 拖动手柄：按住可整行拖拽排序（桌面端）
    const grip = factIconBtn("拖动排序", GRIP_SVG);
    grip.className += " memory-fact-grip";
    grip.draggable = true;
    grip.addEventListener("dragstart", (ev) => {
      ev.dataTransfer.effectAllowed = "move";
      ev.dataTransfer.setData("text/plain", String(i));
      try { ev.dataTransfer.setDragImage(row, 16, 16); } catch (_) { /* 个别浏览器不支持时忽略 */ }
      row.classList.add("fact-dragging");
    });
    grip.addEventListener("dragend", clearDropMarks);
    // 放置目标：悬停在条目上半部分=插入其前，下半部分=插入其后
    row.addEventListener("dragover", (ev) => {
      if (!ev.dataTransfer || !ev.dataTransfer.types || !Array.from(ev.dataTransfer.types).includes("text/plain")) return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "move";
      const r = row.getBoundingClientRect();
      const before = ev.clientY < r.top + r.height / 2;
      row.classList.toggle("fact-drop-before", before);
      row.classList.toggle("fact-drop-after", !before);
    });
    row.addEventListener("dragleave", (ev) => {
      if (ev.relatedTarget && row.contains(ev.relatedTarget)) return; // 仍在行内（子元素间）移动时不清理
      row.classList.remove("fact-drop-before", "fact-drop-after");
    });
    row.addEventListener("drop", (ev) => {
      if (!ev.dataTransfer) return;
      ev.preventDefault();
      const from = parseInt(ev.dataTransfer.getData("text/plain"), 10);
      if (!Number.isInteger(from) || from < 0 || from >= facts.length || from === i) { clearDropMarks(); return; }
      const r = row.getBoundingClientRect();
      const before = ev.clientY < r.top + r.height / 2;
      let to = before ? i : i + 1;
      if (from < to) to -= 1;   // 源条目在前：弹出后目标下标左移一位
      clearDropMarks();
      if (to === from) return;  // 位置未变，不发请求
      factsItemOp("move", from, { to });
    });

    const idx = document.createElement("span");
    idx.className = "memory-fact-index";
    idx.textContent = String(i + 1).padStart(2, "0");

    const text = document.createElement("span");
    text.className = "memory-fact-text";
    text.textContent = (f && f.text) ? String(f.text) : "";

    const actions = document.createElement("span");
    actions.className = "memory-fact-actions";

    // 上锁/解锁：即时生效；图标显示当前状态（锁定=闭锁，未锁=开锁），悬停提示为动作
    const lockBtn = factIconBtn(locked ? "解锁（重新总结时可被更新）" : "上锁（不被重新总结覆盖）", locked ? LOCK_SVG : UNLOCK_SVG, async (e) => {
      const r = await factsItemOp("lock", i, { locked: !locked });
      if (r) showToast(!locked ? "已上锁" : "已解锁");
    });
    lockBtn.classList.add("fact-lock-btn");
    actions.appendChild(lockBtn);

    // 编辑：文本变输入框，Enter/失焦保存，Esc 取消
    actions.appendChild(factIconBtn("编辑", EDIT_SVG, () => {
      if (row.querySelector("input")) return;
      const input = document.createElement("input");
      input.className = "memory-fact-edit";
      input.type = "text";
      input.maxLength = 200;
      input.value = text.textContent;
      row.replaceChild(input, text);
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
      let done = false;
      const finish = (save) => {
        if (done) return;
        done = true;
        const v = input.value.trim();
        row.replaceChild(text, input);
        if (save && v && v !== text.textContent) factsItemOp("update", i, { text: v });
      };
      input.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") finish(true);
        else if (ev.key === "Escape") finish(false);
      });
      input.addEventListener("blur", () => finish(true));
    }));

    // 删除
    actions.appendChild(factIconBtn("删除", DEL_SVG, async () => {
      const factText = (f.text || "").trim();
      if (!confirm("删除关键事实「" + (factText.length > 20 ? factText.slice(0, 20) + "…" : factText) + "」？")) return;
      const r = await factsItemOp("delete", i);
      if (r) showToast("已删除");
    }));

    row.appendChild(grip);
    row.appendChild(idx);
    row.appendChild(text);
    row.appendChild(actions);
    factsList.appendChild(row);
  });
}

// ② 绑定手动添加条目
function bindFactAdd() {
  const input = memEl("memoryFactAddInput");
  const btn = memEl("memoryFactAddBtn");
  if (!input || !btn) return;
  const add = async () => {
    const v = input.value.trim();
    if (!v) return showToast("内容不能为空");
    const r = await factsItemOp("add", -1, { text: v });
    if (r) { input.value = ""; showToast("已添加"); }
  };
  btn.addEventListener("click", add);
  input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") add(); });
}

// ② 动态关键事实：手动重新总结（从全部历史重新抽取并合并覆盖）
function bindMemoryFacts() {
  const btn = memEl("memoryFactsRefreshBtn");
  if (!btn) return;
  btn.onclick = async () => {
    if (!currentSessionId) return showToast("请先选择对话");
    const apiKey = loadApiKey();
    if (!apiKey) return showToast("请先在「参数」页填写 API Key");
    btn.disabled = true;
    btn.textContent = "总结中…";
    try {
      const r = await apiPost("/api/sessions/" + currentSessionId + "/facts-refresh", { api_key: apiKey, full: true });
      if (r && r.error) return showToast(r.error || "没有新增事实");
      showToast("事实已更新");
      await loadMemoryPanel();
    } catch (_) {
      showToast("总结失败");
    } finally {
      btn.disabled = false;
      btn.textContent = "重新总结";
    }
  };
  // 切片轮数 / 每片上限：事实抽取配置（与会话摘要的切片互相独立，失焦自动保存）
  const saveFactsConfig = async (body, input) => {
    if (!currentSessionId) return;
    input.disabled = true;
    try {
      const r = await apiPost("/api/sessions/" + currentSessionId + "/facts-config", body);
      if (r && r.error) return showToast(r.error);
      if (MEMORY_STATE) {
        MEMORY_STATE.facts_slice_rounds = r.slice_rounds;
        MEMORY_STATE.facts_max_per_slice = r.max_per_slice;
      }
      showToast("已保存");
    } catch (_) {
      showToast("保存失败");
    } finally {
      input.disabled = false;
    }
  };
  const sliceInput = memEl("memoryFactsSlice");
  if (sliceInput) {
    sliceInput.addEventListener("change", () => {
      const v = parseInt(sliceInput.value, 10);
      if (isNaN(v)) return loadMemoryPanel(); // 无效输入回读
      const body = { slice_rounds: Math.max(1, Math.min(200, v)) };
      sliceInput.value = body.slice_rounds;
      saveFactsConfig(body, sliceInput);
    });
  }
  const limitInput = memEl("memoryFactsLimit");
  if (limitInput) {
    limitInput.addEventListener("change", () => {
      const v = parseInt(limitInput.value, 10);
      if (isNaN(v)) return loadMemoryPanel(); // 无效输入回读
      const body = { max_per_slice: Math.max(0, Math.min(50, v)) };
      limitInput.value = body.max_per_slice;
      saveFactsConfig(body, limitInput);
    });
  }
}

// 最近 N 轮：独立上下文保留策略（修改后自动保存）
function bindMemoryContext() {
  const field = memEl("memoryContextN");
  if (!field) return;
  const saveCurrent = async () => {
    if (!currentSessionId) return;
    const raw = field.value;
    let recent_n = null;
    if (raw !== "" && raw !== null && raw !== undefined) {
      recent_n = Math.max(0, Math.min(1000, parseInt(raw, 10) || 0));
      field.value = recent_n;
    }
    try {
      const r = await apiPost("/api/sessions/" + currentSessionId + "/context-config", { recent_n: recent_n });
      if (r && r.error) return showToast(r.error);
      if (window.vmSaveN) window.vmSaveN(r.recent_n);
      showToast("已保存");
      await loadMemoryPanel();
    } catch (_) {
      showToast("保存失败");
    }
  };
  field.addEventListener("change", saveCurrent);
}

// ③ 剧情摘要：触发参数自动保存 / 摘要文本失焦自动保存 / 重新总结
function bindMemorySummary() {
  // 摘要触发参数（切片轮数 / 自动间隔）：修改后自动保存，无需单独的保存按钮
  const saveSummaryConfig = async (body, input) => {
    if (!currentSessionId) return;
    input.disabled = true;
    try {
      const r = await apiPost("/api/sessions/" + currentSessionId + "/summary-config", body);
      if (r && r.error) return showToast(r.error);
      MEMORY_STATE = MEMORY_STATE || {};
      MEMORY_STATE.summary = Object.assign({}, MEMORY_STATE.summary || {}, body);
      showToast("已保存");
    } catch (_) {
      showToast("保存失败");
    } finally {
      // 恢复输入框可用态（禁用与否由自动总结开关状态决定）
      refreshMemoryCardStates();
    }
  };
  const sliceInput = memEl("memorySummarySlice");
  if (sliceInput) {
    sliceInput.addEventListener("change", () => {
      const v = parseInt(sliceInput.value, 10);
      if (isNaN(v)) return loadMemoryPanel(); // 无效输入回读
      const body = { slice_rounds: Math.max(1, Math.min(200, v)) };
      sliceInput.value = body.slice_rounds;
      saveSummaryConfig(body, sliceInput);
    });
  }

  // 摘要文本：编辑后失焦自动保存；清空并失焦 = 清除摘要（需确认）
  const summaryTextArea = memEl("memorySummaryText");
  if (summaryTextArea) {
    summaryTextArea.addEventListener("change", async () => {
      if (!currentSessionId) return;
      const text = (summaryTextArea.value || "").trim();
      const oldText = ((MEMORY_STATE || {}).summary || {}).text || "";
      if (text === oldText) return; // 无变化不保存
      if (!text && !confirm("摘要为空将清除当前摘要，确定？")) {
        summaryTextArea.value = oldText;
        return;
      }
      summaryTextArea.disabled = true;
      try {
        const r = await apiPost("/api/sessions/" + currentSessionId + "/summary-text", { text });
        if (r && r.error) return showToast(r.error);
        MEMORY_STATE = r.memory || MEMORY_STATE;
        showToast("已保存");
        await loadMemoryPanel();
      } catch (_) {
        showToast("保存失败");
      } finally {
        summaryTextArea.disabled = false;
      }
    });
  }

  const runBtn = memEl("memorySummaryRunBtn");
  if (runBtn) {
    runBtn.onclick = async () => {
      if (!currentSessionId) return showToast("请先选择对话");
      const apiKey = loadApiKey();
      if (!apiKey) return showToast("请先在「参数」页填写 API Key");
      // 重新总结：整份摘要文本全量重建（N 轮滚动总结），与切片实体完全无关
      runBtn.disabled = true;
      runBtn.textContent = "总结中…";
      try {
        const r = await apiPost("/api/sessions/" + currentSessionId + "/summary", { api_key: apiKey, full: true });
        if (r && r.error) return showToast(r.error || "总结失败");
        showToast("摘要已重新生成");
        await loadMemoryPanel();
      } catch (_) {
        showToast("总结失败");
      } finally {
        runBtn.disabled = false;
        runBtn.textContent = "重新总结";
      }
    };
  }

  // 摘要卡二选一分页：文本摘要 / 切片摘要
  const tabText = memEl("summaryTabText");
  const tabSlice = memEl("summaryTabSlice");
  if (tabText) tabText.addEventListener("click", () => setSummaryTab("text"));
  if (tabSlice) tabSlice.addEventListener("click", () => setSummaryTab("slice"));
}

// ④ 向量记忆：top_k 输入保存（模型/开关/N 的保存逻辑在 main.js / sessions.js）
function bindMemoryVectorTopK() {
  const topK = memEl("vmTopK");
  if (!topK) return;
  topK.addEventListener("change", async () => {
    if (!currentSessionId) return;
    let v = parseInt(topK.value, 10);
    // 空值 -> 不传（后端用默认）；否则钳制到 0~500
    const body = {};
    if (topK.value === "" || isNaN(v)) {
      body.top_k = null;
    } else {
      v = Math.max(0, Math.min(500, v));
      topK.value = v;
      body.top_k = v;
    }
    try {
      const r = await apiPost("/api/sessions/" + currentSessionId + "/vector-config", body);
      if (r && r.error) return showToast(r.error);
      // 同步到内存态，保证后续请求/新建会话使用最新值
      if (window.vmSaveTopK) window.vmSaveTopK(body.top_k);
      showToast("已保存");
    } catch (_) {
      showToast("保存失败");
    }
  });
}

// 顶部批量操作：一键配置（打开 2/3/4 开关 + 恢复默认值）/ 关闭智能总结（关闭 2/3/4，0/1 继续生效）
function bindMemoryMasterButtons() {
  const oneClick = memEl("memoryOneClickBtn");
  if (oneClick) {
    oneClick.onclick = async () => {
      if (!currentSessionId) return showToast("请先选择对话");
      oneClick.disabled = true;
      try {
        const r = await apiPost("/api/sessions/" + currentSessionId + "/memory-switches", {
          facts_enabled: true,
          summary_enabled: true,
          vector_enabled: true,
          facts_auto: true,
          summary_auto: true,
          reset_values: true,
        });
        if (r && r.error) return showToast(r.error);
        MEMORY_STATE = r.memory || MEMORY_STATE;
        // 程序化改开关不触发 change 事件，手动同步内部状态与开关动画
        if (window.vmSetEnabled) window.vmSetEnabled(true);
        if (window.vmSaveN) window.vmSaveN((r.memory || {}).recent_n);
        const en = memEl("vmEnabled");
        if (en) en.checked = true;
        // 数值输入框同步为新默认值（facts / summary 切片宽度等）
        const memNew = r.memory || {};
        const fSlice = memEl("memoryFactsSlice");
        if (fSlice && memNew.facts_slice_rounds != null) fSlice.value = memNew.facts_slice_rounds;
        const fLimit = memEl("memoryFactsLimit");
        if (fLimit && memNew.facts_max_per_slice != null) fLimit.value = memNew.facts_max_per_slice;
        const sCfg = memNew.summary || {};
        const sSlice = memEl("memorySummarySlice");
        if (sSlice && sCfg.slice_rounds != null) sSlice.value = sCfg.slice_rounds;
        refreshMemoryCardStates();
        showToast("一键配置已应用");
      } catch (_) {
        showToast("配置失败");
      } finally {
        oneClick.disabled = false;
      }
    };
  }

  const disableBtn = memEl("memoryDisableSmartBtn");
  if (disableBtn) {
    disableBtn.onclick = async () => {
      if (!currentSessionId) return showToast("请先选择对话");
      disableBtn.disabled = true;
      try {
        const r = await apiPost("/api/sessions/" + currentSessionId + "/memory-switches", {
          facts_enabled: false,
          summary_enabled: false,
          vector_enabled: false,
        });
        if (r && r.error) return showToast(r.error);
        MEMORY_STATE = r.memory || MEMORY_STATE;
        if (window.vmSetEnabled) window.vmSetEnabled(false);
        const en = memEl("vmEnabled");
        if (en) en.checked = false;
        refreshMemoryCardStates();
        showToast("已关闭智能总结");
      } catch (_) {
        showToast("操作失败");
      } finally {
        disableBtn.disabled = false;
      }
    };
  }
}

// 卡片 ②③ 右上角开关：点击即时带动画并持久化到后端（④ 由 main.js 处理）
function bindMemoryCardSwitches() {
  // ② 自动总结开关（总开关下一级）：关闭时后台不自动抽取，手动仍可用
  const factsAuto = memEl("memoryFactsAuto");
  if (factsAuto) {
    factsAuto.addEventListener("change", async () => {
      if (!currentSessionId) { refreshMemoryCardStates(); return showToast("请先选择对话"); }
      const on = factsAuto.checked;
      try {
        const r = await apiPost("/api/sessions/" + currentSessionId + "/memory-switches", { facts_auto: on });
        if (r && r.error) throw new Error(r.error);
        MEMORY_STATE = r.memory || MEMORY_STATE;
        showToast(on ? "已开启自动总结" : "已关闭自动总结");
      } catch (e) {
        factsAuto.checked = !on; // 失败回滚
        showToast(e && e.message ? e.message : "设置失败");
      }
    });
  }

  // ② 动态关键事实
  const factsSwitch = memEl("memoryFactsEnabled");
  if (factsSwitch) {
    factsSwitch.addEventListener("change", async () => {
      if (!currentSessionId) { refreshMemoryCardStates(); return showToast("请先选择对话"); }
      const on = factsSwitch.checked; // CSS 过渡即时生效
      const group = memEl("memoryFactsGroup");
      if (group) group.classList.toggle("memory-off", !on);
      MEMORY_STATE.facts_enabled = on;
      try {
        const r = await apiPost("/api/sessions/" + currentSessionId + "/memory-switches", { facts_enabled: on });
        if (r && r.error) throw new Error(r.error);
        MEMORY_STATE = r.memory || MEMORY_STATE;
      } catch (e) {
        factsSwitch.checked = !on;
        if (group) group.classList.toggle("memory-off", on);
        showToast(e && e.message ? e.message : "设置失败");
      }
    });
  }

  // ③ 自动总结开关（总开关下一级）：关闭时后台不自动总结，两个数值输入置灰不可编辑
  const summaryAutoSwitch = memEl("memorySummaryAutoSwitch");
  if (summaryAutoSwitch) {
    summaryAutoSwitch.addEventListener("change", async () => {
      if (!currentSessionId) { refreshMemoryCardStates(); return showToast("请先选择对话"); }
      const on = summaryAutoSwitch.checked;
      // 即时生效：切片轮数输入随开关置灰/恢复
      const sliceInput = memEl("memorySummarySlice");
      if (sliceInput) sliceInput.disabled = !on;
      MEMORY_STATE = MEMORY_STATE || {};
      MEMORY_STATE.summary = Object.assign({}, MEMORY_STATE.summary || {}, { auto: on });
      try {
        const r = await apiPost("/api/sessions/" + currentSessionId + "/memory-switches", { summary_auto: on });
        if (r && r.error) throw new Error(r.error);
        MEMORY_STATE = r.memory || MEMORY_STATE;
        showToast(on ? "已开启自动总结" : "已关闭自动总结");
      } catch (e) {
        summaryAutoSwitch.checked = !on; // 失败回滚
        if (sliceInput) sliceInput.disabled = on;
        if (autoInput) autoInput.disabled = on;
        showToast(e && e.message ? e.message : "设置失败");
      }
    });
  }

  // ③ 剧情摘要
  const summarySwitch = memEl("memorySummaryEnabled");
  if (summarySwitch) {
    summarySwitch.addEventListener("change", async () => {
      if (!currentSessionId) { refreshMemoryCardStates(); return showToast("请先选择对话"); }
      const on = summarySwitch.checked;
      const group = memEl("memorySummaryGroup");
      if (group) group.classList.toggle("memory-off", !on);
      MEMORY_STATE = MEMORY_STATE || {};
      MEMORY_STATE.summary = Object.assign({}, MEMORY_STATE.summary || {}, { enabled: on });
      try {
        const r = await apiPost("/api/sessions/" + currentSessionId + "/memory-switches", { summary_enabled: on });
        if (r && r.error) throw new Error(r.error);
        MEMORY_STATE = r.memory || MEMORY_STATE;
      } catch (e) {
        summarySwitch.checked = !on;
        if (group) group.classList.toggle("memory-off", on);
        showToast(e && e.message ? e.message : "设置失败");
      }
    });
  }
}

/* ---------------- ③ 切片管理模态面板 ---------------- */

// 切片管理模态：入口有两个——记忆页顶部批量操作栏「切片管理」按钮 +
// 摘要卡切片分页里的「管理切片」按钮。面板居中显示 + 背景遮罩。
// 按用户交互约定：显式点 ✕ 关闭（点遮罩不关闭），display 直接切换、无动画。
function bindSliceModal() {
  // 图标注入（顶部按钮 + 模态标题 + 卡内管理按钮）
  const ic1 = memEl("memorySlicePanelIc");
  if (ic1) ic1.innerHTML = svgIcon("slice");
  const ic2 = memEl("memorySliceModalIc");
  if (ic2) ic2.innerHTML = svgIcon("slice");
  const ic3 = memEl("memorySliceManageIc");
  if (ic3) ic3.innerHTML = svgIcon("slice");

  const modal = memEl("memorySliceModal");
  const openBtn = memEl("memorySlicePanelBtn");
  const openBtn2 = memEl("memorySliceManageBtn");
  const closeBtn = memEl("memorySliceModalClose");
  if (!modal || !closeBtn) return;

  const openModal = () => {
    if (!currentSessionId) return showToast("请先选择对话");
    // 打开时重渲染一次，确保内容与会话数据同步
    renderSummarySlices(((MEMORY_STATE || {}).summary) || {});
    modal.classList.remove("hidden");
  };
  if (openBtn) openBtn.addEventListener("click", openModal);
  if (openBtn2) openBtn2.addEventListener("click", openModal);
  closeBtn.addEventListener("click", () => modal.classList.add("hidden"));
}

/* ---------------- 初始化 ---------------- */

function initMemoryPanel() {
  bindMemoryWorlds();
  bindMemoryCards();
  bindFactAdd();
  bindMemoryFacts();
  bindMemoryContext();
  bindMemorySummary();
  bindSliceModal();
  bindMemoryVectorTopK();
  bindMemoryMasterButtons();
  bindMemoryCardSwitches();
  // 向量开关变化时同步 top_k 禁用态（main.js 的 syncVmUi 也会调用）
  const en = memEl("vmEnabled");
  if (en) en.addEventListener("change", syncMemoryVectorField);
  resetMemoryPanel();
}

// 暴露给 main.js（打开设置）、sessions.js（切换会话）与 chat.js（applyVmToUI）调用
window.loadMemoryPanel = loadMemoryPanel;
window.resetMemoryPanel = resetMemoryPanel;
window.syncMemoryVectorField = syncMemoryVectorField;
window.refreshMemoryCardStates = refreshMemoryCardStates;

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initMemoryPanel);
} else {
  initMemoryPanel();
}
