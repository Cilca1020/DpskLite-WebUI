/* 渲染逻辑：markdown、消息气泡、文件卡片、助手消息 */

// 将文本以 Markdown 渲染进元素（思考与正文通用）
function renderMarkdown(el, text) {
  const src = (text || "").trim();
  // marked 输出末尾带一个格式化 \n，在 white-space:pre-wrap 容器（如思考块）中会被渲染成多余空行，故 trim
  // 兼容 marked 对「中文/全角字符紧贴 ** 加粗符号」的边界识别失败（如 “主题为**内容**” 不渲染）：
  // 在两者之间插入零宽空格 \u200B 让 marked 识别 strong，渲染后再移除。
  const cjk = "\\u4e00-\\u9fff\\u3000-\\u303f\\uFF00-\\uFFEF\\u2018-\\u201D";
  const srcSafe = src
    .replace(new RegExp("([" + cjk + "])(\\*\\*)", "g"), "$1\u200B$2")
    .replace(new RegExp("(\\*\\*)([" + cjk + "])", "g"), "$1\u200B$2");
  const html = (window.marked ? marked.parse(srcSafe) : src).replace(/\u200B/g, "");
  el.innerHTML = html.trim();
  if (!src) el.textContent = "";
  attachCodeCopy(el); // 渲染完成后为每个代码块附加"单独复制"按钮
}

// 为容器内所有代码块（pre code）附加独立的复制按钮。
// 已有按钮的代码块跳过（幂等）：流式增量渲染时会对新增块重复调用。
function attachCodeCopy(container) {
  if (!container || !container.querySelectorAll) return;
  container.querySelectorAll("pre").forEach((pre) => {
    if (pre.querySelector(":scope > .code-copy-btn")) return;
    const code = pre.querySelector("code");
    if (!code) return;
    // 语法高亮：仅对带语言标签的代码块高亮（无语言时不自动检测，避免误判）
    if (window.hljs) {
      const langMatch = (code.className || "").match(/language-([\w+#.-]+)/);
      if (langMatch) {
        try {
          hljs.highlightElement(code);
        } catch (_) {
          /* 未知语言等异常忽略，代码保持原样 */
        }
      }
    }
    // 语言标签：从 code 类名提取（如 language-python），无则显示"plain text"
    const langEl = document.createElement("span");
    langEl.className = "code-lang";
    const langMatch = (code.className || "").match(/language-([\w+#.-]+)/);
    langEl.textContent = langMatch ? langMatch[1] : "plain text";
    pre.appendChild(langEl);
    const btn = document.createElement("button");
    btn.className = "code-copy-btn";
    btn.type = "button";
    btn.title = "复制代码";
    btn.innerHTML = svgIcon("copy");
    btn.onclick = (e) => {
      e.stopPropagation();
      const txt = code.innerText;
      const done = () => {
        btn.innerHTML = svgIcon("check");
        setTimeout(() => (btn.innerHTML = svgIcon("copy")), 1200);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(txt).then(done).catch(() => fallbackCopy(txt, done));
      } else {
        fallbackCopy(txt, done);
      }
    };
    pre.appendChild(btn);
  });
}

// 空会话欢迎提示：对话区无消息时居中显示，有消息时移除
function showEmptyHint(box) {
  if (!box) box = $("#chatBox");
  if (box.querySelector(".chat-empty")) return;
  const empty = document.createElement("div");
  empty.className = "chat-empty";
  empty.innerHTML =
    '<div class="chat-empty-icon">' + svgIcon("chat") + "</div>" +
    '<div class="chat-empty-title">你好，想聊点什么？</div>' +
    '<div class="chat-empty-sub">我可以帮你写代码、整理思路、解答疑问，直接输入问题即可开始。</div>';
  box.appendChild(empty);
}

function hideEmptyHint(box) {
  if (!box) box = $("#chatBox");
  const el = box.querySelector(".chat-empty");
  if (el) el.remove();
}

// 渲染一条消息气泡。返回气泡元素（div），供上层插入 reasoning 折叠块。
// role: "user" | "assistant"；markdown 控制是否渲染 markdown（用户消息强制纯文本）；
// msgIndex 非 null 且当前会话存在时，渲染气泡外的操作条；
// files：用户消息携带的文件（渲染为气泡上方的文件卡片）；
// anchorEl：非 null 时把消息行插入到该元素之前（加载更早消息用），否则追加到末尾；
// autoScroll：插入后是否自动滚动到底部（加载更早消息时应关闭，保持视口位置）。
// scene：非 null 表示该 user 消息带场景锚点（{label}），在气泡行之前渲染分场分割线。
function addMsgEl(role, text, markdown = true, msgIndex = null, files = null, anchorEl = null, autoScroll = true, scene = null) {
  const box = $("#chatBox");
  // 每行容器：包裹气泡 + 气泡外的操作条
  const row = document.createElement("div");
  row.className = "msg-row " + role;
  const div = document.createElement("div");
  div.className = "msg " + role;
  if (markdown && window.marked) {
    renderMarkdown(div, text);
  } else {
    div.textContent = text || "";
  }
  // 保存原始 markdown 源，供"复制为 Markdown"使用
  div._rawText = text || "";
  // 文件卡片（仅用户消息携带文件时存在）：渲染在气泡上方
  if (role === "user" && files && files.length) {
    row.appendChild(buildFileCardsEl(files));
  }
  // 消息工具条：常驻显示在气泡外右下方（不依赖 hover，便于移动端）
  let bar = null;
  if (msgIndex !== null && currentSessionId) {
    bar = document.createElement("div");
    bar.className = "msg-actions";
    // 编辑按钮：仅最新一轮的 user 消息显示（置于最左）
    if (role === "user" && msgIndex === lastUserMsgIndex) {
      const editBtn = document.createElement("button");
      editBtn.className = "msg-action edit-btn";
      editBtn.title = "编辑这条提问";
      editBtn.innerHTML = svgIcon("edit");
      editBtn.onclick = (e) => {
        e.stopPropagation();
        editMessage(msgIndex);
      };
      bar.appendChild(editBtn);
    }
    // 复制纯文本按钮：复制气泡纯文本（排除思考过程折叠块与文件卡片）
    const copyBtn = document.createElement("button");
    copyBtn.className = "msg-action";
    copyBtn.title = "复制为纯文本";
    copyBtn.innerHTML = svgIcon("copy");
    copyBtn.onclick = (e) => {
      e.stopPropagation();
      const clone = div.cloneNode(true);
      const r = clone.querySelector(".reasoning");
      if (r) r.remove();
      // 复制只包含用户输入文本，不含文件内容
      const txt = clone.innerText.trim();
      const done = () => {
        copyBtn.innerHTML = svgIcon("check");
        setTimeout(() => (copyBtn.innerHTML = svgIcon("copy")), 1200);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(txt).then(done).catch(() => fallbackCopy(txt, done));
      } else {
        fallbackCopy(txt, done);
      }
    };
    bar.appendChild(copyBtn);
    // 复制为 Markdown 按钮：使用消息原始 markdown 源（排除思考过程折叠块）
    // 仅助手消息提供（用户提问通常无需 Markdown 源码）
    if (role === "assistant") {
      const copyMdBtn = document.createElement("button");
      copyMdBtn.className = "msg-action md-btn";
      copyMdBtn.title = "复制为 Markdown";
      copyMdBtn.textContent = "MD";
      copyMdBtn.onclick = (e) => {
        e.stopPropagation();
        const md = (div._rawText || div.innerText || "").trim();
        const done = () => {
          copyMdBtn.innerHTML = svgIcon("check");
          setTimeout(() => (copyMdBtn.textContent = "MD"), 1200);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(md).then(done).catch(() => fallbackCopy(md, done));
        } else {
          fallbackCopy(md, done);
        }
      };
      bar.appendChild(copyMdBtn);
    }
    // 分场按钮：仅 user 消息显示。未打锚点 = 在此分场；已打锚点 = 编辑分场名称
    if (role === "user") {
      const sceneBtn = document.createElement("button");
      sceneBtn.className = "msg-action scene-btn" + (scene ? " active" : "");
      sceneBtn.title = scene ? "编辑分场名称" : "在此分场（场景锚点，用于剧情总结分段）";
      sceneBtn.innerHTML = svgIcon("scene");
      sceneBtn.onclick = (e) => {
        e.stopPropagation();
        addSceneBreak(row, msgIndex, scene ? (scene.label || "") : undefined);
      };
      bar.appendChild(sceneBtn);
    }
    const delBtn = document.createElement("button");
    delBtn.className = "msg-action";
    delBtn.title = "删除这条消息（连同同轮另一条）";
    delBtn.innerHTML = svgIcon("trash");
    delBtn.onclick = (e) => {
      e.stopPropagation();
      deleteMessage(msgIndex);
    };
    bar.appendChild(delBtn);
    // 重试仅对最后一条助手消息有意义：重新请求其对应上文（置于最右）
    if (role === "assistant" && msgIndex === lastAssistantMsgIndex) {
      const retryBtn = document.createElement("button");
      retryBtn.className = "msg-action retry-btn";
      retryBtn.title = "重试这条回答";
      retryBtn.innerHTML = svgIcon("retry");
      retryBtn.onclick = (e) => {
        e.stopPropagation();
        retryFrom(msgIndex);
      };
      bar.appendChild(retryBtn);
    }
  }
  row.appendChild(div);
  if (msgIndex !== null && currentSessionId) row.appendChild(bar);
  // 流式占位的助手气泡（空文本）在输出结束前隐藏操作条，
  // 结束后由 streamAssistant 以滑入动画显示（display:none 不占位，气泡更紧凑）
  if (bar && role === "assistant" && !text) bar.style.display = "none";
  if (anchorEl) box.insertBefore(row, anchorEl); // 加载更早消息：插入到指定元素之前
  else box.appendChild(row);
  // 场景锚点分割线：紧贴本消息行之前（就地插入，顺序不受 loadOlderMessages 前插影响）
  if (scene && role === "user") box.insertBefore(buildSceneDivider(scene.label, msgIndex), row);
  hideEmptyHint(box); // 有新消息时移除空会话欢迎提示
  if (autoScroll) box.scrollTop = box.scrollHeight;
  return div; // 返回气泡，供上层插入 reasoning 折叠块
}

/* ---------------- 场景锚点（手动分场） ----------------
 * user 消息上打「场景锚点」（scene_break / scene_label，随消息存储），
 * 后端剧情总结/事实抽取按锚点硬边界切片：锚点段不超上限时整段总结（场景级），
 * 摘要行带【场景标签】前缀。分割线渲染为像素风虚线 + 标签，可改名/删除。
 */

// 「第 N 场」默认命名：N = 该消息之前已有的锚点数 + 1
function defaultSceneName(msgIndex) {
  const before = (typeof sceneAnchors !== "undefined" ? sceneAnchors : []).filter((i) => i < msgIndex).length;
  return "第 " + (before + 1) + " 场";
}

// 弹出分场命名输入（原生 prompt，显式确认）。取消返回 null；返回裁剪后的名称（可空串）。
function promptSceneLabel(msgIndex, current) {
  const tip = current === undefined
    ? "分场名称（留空 = " + defaultSceneName(msgIndex) + "）："
    : "修改分场名称（留空 = " + defaultSceneName(msgIndex) + "）：";
  const name = prompt(tip, current === undefined ? "" : current);
  if (name === null) return null;
  return name.trim().slice(0, 50);
}

// 打 / 改某条 user 消息的场景锚点。existingLabel 传 undefined 表示新打锚点。
// 就地插入/更新分割线，不整页重绘（避免长对话滚动位置丢失）。
async function addSceneBreak(row, msgIndex, existingLabel) {
  if (!currentSessionId || msgIndex === null) return;
  const label = promptSceneLabel(msgIndex, existingLabel);
  if (label === null) return; // 用户取消
  try {
    const r = await apiPost(
      "/api/sessions/" + currentSessionId + "/msg/" + msgIndex + "/scene-break",
      { scene_break: true, scene_label: label }
    );
    if (r && r.error) return showToast(r.error);
    // 更新锚点登记（供后续默认命名计数）
    if (!sceneAnchors.includes(msgIndex)) {
      sceneAnchors.push(msgIndex);
      sceneAnchors.sort((a, b) => a - b);
    }
    // 移除旧分割线（编辑场景）后在消息行前插入新的
    const old = document.querySelector('.scene-divider[data-index="' + msgIndex + '"]');
    if (old) old.remove();
    row.parentNode.insertBefore(buildSceneDivider(label, msgIndex), row);
    showToast("已分场");
    // 切片自动总结（管理面板开关开启时生效）：打锚点即自动总结前一个切片。
    // fire-and-forget 静默执行：开关关闭后端返回 400，失败不打扰用户。
    const ak = loadApiKey();
    if (ak) {
      fetch("/api/sessions/" + currentSessionId + "/summary-slices-run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: ak }),
      }).catch(() => {});
    }
  } catch (_) {
    showToast("分场失败");
  }
}

// 删除场景锚点：相邻两段重新合并为一个分段
async function removeSceneBreak(msgIndex) {
  if (!currentSessionId) return;
  if (!confirm("删除该分场标记？相邻两段将合并为一个分段。")) return;
  try {
    const r = await apiPost(
      "/api/sessions/" + currentSessionId + "/msg/" + msgIndex + "/scene-break",
      { scene_break: false }
    );
    if (r && r.error) return showToast(r.error);
    sceneAnchors = sceneAnchors.filter((i) => i !== msgIndex);
    const dv = document.querySelector('.scene-divider[data-index="' + msgIndex + '"]');
    if (dv) dv.remove();
    showToast("已删除分场");
  } catch (_) {
    showToast("删除失败");
  }
}

// 构建分场分割线元素：像素风虚线 + 场景标签（短名完整显示，超长省略号 + 悬停完整提示），
// 标签右侧提供改名 / 删除按钮（常驻显示，兼容移动端）
function buildSceneDivider(label, msgIndex) {
  const dv = document.createElement("div");
  dv.className = "scene-divider";
  dv.dataset.index = msgIndex;
  const lineBefore = document.createElement("span");
  lineBefore.className = "scene-divider-line";
  const name = document.createElement("span");
  name.className = "scene-divider-label";
  const shown = (label || "").trim() || defaultSceneName(msgIndex);
  name.textContent = shown;
  name.title = shown;
  const lineAfter = document.createElement("span");
  lineAfter.className = "scene-divider-line";
  const actions = document.createElement("span");
  actions.className = "scene-divider-actions";
  const editBtn = document.createElement("button");
  editBtn.className = "scene-action";
  editBtn.type = "button";
  editBtn.title = "修改分场名称";
  editBtn.innerHTML = svgIcon("edit");
  editBtn.onclick = async (e) => {
    e.stopPropagation();
    const nl = promptSceneLabel(msgIndex, name.textContent);
    if (nl === null) return;
    try {
      const r = await apiPost(
        "/api/sessions/" + currentSessionId + "/msg/" + msgIndex + "/scene-break",
        { scene_break: true, scene_label: nl }
      );
      if (r && r.error) return showToast(r.error);
      const updated = nl || defaultSceneName(msgIndex);
      name.textContent = updated;
      name.title = updated;
      // 双向同步：把新名字写回已落袋切片实体（切片管理面板显示名跟随）
      if (window.syncSliceNameFromAnchor) window.syncSliceNameFromAnchor(msgIndex, nl);
      showToast("已保存");
    } catch (_) {
      showToast("保存失败");
    }
  };
  const delBtn = document.createElement("button");
  delBtn.className = "scene-action danger";
  delBtn.type = "button";
  delBtn.title = "删除分场标记";
  delBtn.innerHTML = svgIcon("trash");
  delBtn.onclick = (e) => {
    e.stopPropagation();
    removeSceneBreak(msgIndex);
  };
  actions.appendChild(editBtn);
  actions.appendChild(delBtn);
  dv.appendChild(lineBefore);
  dv.appendChild(name);
  dv.appendChild(lineAfter);
  dv.appendChild(actions);
  return dv;
}

// 构建"文件卡片"元素（代表文件的气泡），供用户消息渲染使用
function buildFileCardsEl(files) {
  const wrap = document.createElement("div");
  wrap.className = "file-cards";
  files.forEach((f) => {
    const card = document.createElement("div");
    card.className = "file-card";
    const icon = document.createElement("span");
    icon.className = "file-card-icon";
    icon.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>';
    const name = document.createElement("span");
    name.className = "file-card-name";
    name.textContent = f.name;
    name.title = f.name;
    card.appendChild(icon);
    card.appendChild(name);
    wrap.appendChild(card);
  });
  return wrap;
}

// 在被截断（用户停止生成）的助手气泡上追加灰色提示。幂等：同一气泡不重复添加。
// hasContent=true：回答已有部分内容，提示显示在气泡下方；
// hasContent=false：流式输出开始前即被停止（气泡为空），提示显示在气泡内部作为占位。
function markInterrupted(assistantEl, hasContent) {
  if (!assistantEl) return;
  if (!hasContent) {
    if (assistantEl.querySelector(".msg-interrupted-empty")) return;
    const hint = document.createElement("div");
    hint.className = "msg-interrupted-empty";
    hint.textContent = "回答已中断";
    assistantEl.appendChild(hint);
    // 标记空气泡：CSS 据此收窄气泡内边距，避免占位提示把气泡撑得过高
    assistantEl.classList.add("msg-empty-interrupted");
    return;
  }
  const row = assistantEl.parentNode;
  if (!row || row.querySelector(".msg-interrupted")) return;
  const hint = document.createElement("div");
  hint.className = "msg-interrupted";
  hint.textContent = "回答已中断";
  row.insertBefore(hint, assistantEl.nextSibling);
}

// 渲染一条助手消息（支持思考折叠块），返回元素
function renderAssistant(text, reasoning, msgIndex = null, interrupted = false, anchorEl = null, autoScroll = true) {
  const el = addMsgEl("assistant", text, true, msgIndex, null, anchorEl, autoScroll); // 正文已按 markdown 渲染
  if (reasoning) {
    const wrap = document.createElement("details");
    wrap.className = "reasoning";
    const summary = document.createElement("summary");
    summary.textContent = "思考过程";
    const body = document.createElement("div");
    body.className = "reasoning-body";
    renderMarkdown(body, reasoning); // 思考过程也渲染 markdown
    wrap.appendChild(summary);
    wrap.appendChild(body);
    el.insertBefore(wrap, el.firstChild);
  }
  if (interrupted) markInterrupted(el, !!text);
  return el;
}
