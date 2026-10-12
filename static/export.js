/* 会话导出：支持 JSON / Markdown / 纯文本（txt）三种格式。
 * - JSON：完整结构化数据（标题、模型、参数、时间、全部消息含思考过程与附件），便于程序处理/未来导入。
 * - Markdown：人类可读的对话记录，思考过程折叠展示，可直接粘贴到笔记软件。
 * - 纯文本（txt）：无任何标记符号的纯文本，适合直接粘贴到任何地方。
 * 依赖：icons.js（svgIcon/showToast）、api.js（apiGet）。
 */

// 文件名清洗：剔除 Windows/各平台非法字符，超长截断
function sanitizeFilename(name) {
  const s = String(name || "对话").trim() || "对话";
  return s.replace(/[\\/:*?"<>|\n\r\t]/g, "_").slice(0, 60) || "对话";
}

// 秒级时间戳 -> "YYYY-MM-DD HH:MM"
function formatDateTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// 模型 id -> 前端展示名
function modelLabel(modelId) {
  const sel = $("#modelSelect");
  const opt = sel && Array.from(sel.options).find((o) => o.value === modelId);
  return opt ? opt.textContent : modelId || "未知模型";
}

// 触发浏览器下载
function downloadFile(filename, content, mime) {
  const blob = new Blob([content], { type: mime + ";charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// 清理导出文本：历史数据可能混入旧版 UI 渲染残留（<details>/</details> 等 HTML 标签）
// 或复制粘贴产生的零宽不可见字符，统一在此剔除，保证导出的是纯净 Markdown。
function cleanExportText(text) {
  return String(text || "")
    .replace(/<[^>]*>/g, "") // 移除一切 HTML 标签
    .replace(/[\u200b\u200c\u200d\u2060\ufeff]/g, "") // 零宽空格/连接符等不可见字符
    .replace(/\n{3,}/g, "\n\n") // 压缩多余空行
    .trim();
}

// 会话 -> Markdown 文本
function buildMarkdown(session) {  const lines = [];
  const roleNames = { user: "用户", assistant: "助手" };

  lines.push(`# ${session.title || "未命名会话"}`);
  lines.push("");
  lines.push(`- **模型**：${modelLabel(session.model)}`);
  lines.push(`- **创建时间**：${formatDateTime(session.created_at)}`);
  lines.push(`- **消息数**：${(session.messages || []).filter((m) => m.role !== "system").length}`);
  lines.push("");
  lines.push("---");
  lines.push("");

  (session.messages || [])
    .filter((m) => m.role !== "system")
    .forEach((m) => {
      lines.push(`## ${roleNames[m.role] || m.role}`);
      lines.push("");
      // 附件
      if (m.files && m.files.length) {
        lines.push(`> 附件：${m.files.map((f) => f.name).join("、")}`);
        lines.push("");
      }
      // 思考过程：引用块形式与正文区分（纯 Markdown，不含 HTML 标签）
      if (m.reasoning) {
        // 历史数据可能自带「思考过程」标记行（旧版渲染残留），去掉避免与标题重复
        const reasoningText = cleanExportText(m.reasoning)
          .replace(/^[💭🤔🧠]*\s*思考过程\s*[：:。.．]*\s*$/gm, "")
          .replace(/\n{3,}/g, "\n\n")
          .trim();
        lines.push("> **思考过程**");
        lines.push(">");
        lines.push(
          reasoningText
            .split("\n")
            .map((l) => "> " + l)
            .join("\n")
        );
        lines.push("");
      }
      lines.push(cleanExportText(m.content) || "（无内容）");
      lines.push("");
    });

  return lines.join("\n");
}

// 会话 -> 纯文本（无任何 Markdown/HTML 标记）
function buildPlainText(session) {
  const lines = [];
  const roleNames = { user: "用户", assistant: "助手" };
  const messages = (session.messages || []).filter((m) => m.role !== "system");

  lines.push(session.title || "未命名会话");
  lines.push("=".repeat(Math.min(30, String(session.title || "未命名会话").length || 4)));
  lines.push("");
  lines.push(`模型：${modelLabel(session.model)}`);
  lines.push(`创建时间：${formatDateTime(session.created_at)}`);
  lines.push(`消息数：${messages.length}`);
  lines.push("");

  messages.forEach((m) => {
    lines.push(`【${roleNames[m.role] || m.role}】`);
    // 附件
    if (m.files && m.files.length) {
      lines.push(`附件：${m.files.map((f) => f.name).join("、")}`);
    }
    // 思考过程：独立小节，与正文区分
    if (m.reasoning) {
      const reasoningText = cleanExportText(m.reasoning)
        .replace(/^[💭🤔🧠]*\s*思考过程\s*[：:。.．]*\s*$/gm, "")
        .replace(/\n{3,}/g, "\n\n")
        .trim();
      if (reasoningText) {
        lines.push("（思考过程）");
        lines.push(reasoningText);
        lines.push(""); // 思考过程与正文之间留空行（与 Markdown 导出一致）
      }
    }
    lines.push(cleanExportText(m.content) || "（无内容）");
    lines.push("");
    lines.push("----------");
    lines.push("");
  });

  return lines.join("\n").replace(/\n{3,}/g, "\n\n").trim();
}

// 导出为 JSON：完整会话数据（含四层记忆，保证导入导出无损往返）
// 结构：{ format, version, 元信息, memory（四层记忆）, messages（含分场锚点/思考/附件）}
// 说明：memory 是整个会话的功能数据核心（世界卡/人物卡/动态事实/剧情摘要含切片/向量记忆设置），
//       缺少它会导致导入后角色设定与记忆全部丢失；vm 为旧版兼容字段（权威值在 memory.vector）。
function exportSessionAsJSON(session) {
  const data = {
    format: "dpsklite-session",
    version: 2,
    title: session.title,
    model: session.model,
    params: session.params || null,
    created_at: session.created_at,
    updated_at: session.updated_at,
    exported_at: Math.floor(Date.now() / 1000),
    // 四层记忆：worlds / cards / facts / summary(含 slices) / vector / recent_n
    memory: session.memory || null,
    // 旧版（v1）导入入口只认顶层 vm：这里冗余一份，向后兼容更早的导入逻辑
    vm: (session.memory && session.memory.vector) || session.vm || null,
    // 消息原样导出：reasoning 思考、files 附件、interrupted、scene_break/scene_label 分场锚点均保留
    messages: (session.messages || []).filter((m) => m.role !== "system"),
  };
  const stamp = formatDateTime(session.created_at).replace(/[-: ]/g, "");
  downloadFile(`${sanitizeFilename(session.title)}_${stamp}.json`, JSON.stringify(data, null, 2), "application/json");
}

// 导出为 Markdown：对话记录
function exportSessionAsMarkdown(session) {
  const stamp = formatDateTime(session.created_at).replace(/[-: ]/g, "");
  downloadFile(`${sanitizeFilename(session.title)}_${stamp}.md`, buildMarkdown(session), "text/markdown");
}

// 导出为纯文本：对话记录（txt）
function exportSessionAsPlainText(session) {
  const stamp = formatDateTime(session.created_at).replace(/[-: ]/g, "");
  downloadFile(`${sanitizeFilename(session.title)}_${stamp}.txt`, buildPlainText(session), "text/plain");
}

// 关闭当前打开的导出菜单（全局唯一，避免多个叠开）
function closeExportMenu() {
  if (window._exportMenu) {
    window._exportMenu.menu.remove();
    document.removeEventListener("click", window._exportMenu.onDocClick);
    window._exportMenu = null;
  }
}

// 在导出按钮旁弹出格式选择菜单
function openExportMenu(btn, session) {
  closeExportMenu();
  const menu = document.createElement("div");
  menu.className = "export-menu";
  menu.innerHTML = `
    <button type="button" class="export-menu-item" data-fmt="md">导出为 Markdown（.md）</button>
    <button type="button" class="export-menu-item" data-fmt="txt">导出为纯文本（.txt）</button>
    <button type="button" class="export-menu-item" data-fmt="json">导出为 JSON（.json）</button>
  `;
  document.body.appendChild(menu);

  // 定位：菜单水平居中于按钮（菜单中心 = 按钮中心），并确保不超出视口；
  // 垂直方向放在按钮下方，空间不足时翻到上方
  const rect = btn.getBoundingClientRect();
  const menuW = menu.offsetWidth || 200;
  const left = Math.max(8, Math.min(rect.left + rect.width / 2 - menuW / 2, window.innerWidth - menuW - 8));
  menu.style.left = left + "px";
  menu.style.top = rect.bottom + 6 + "px";
  if (rect.bottom + 6 + menu.offsetHeight > window.innerHeight) {
    menu.style.top = Math.max(8, rect.top - menu.offsetHeight - 6) + "px";
  }

  const onDocClick = (e) => {
    if (menu.contains(e.target)) return; // 菜单项自己的点击处理
    closeExportMenu();
  };
  menu.addEventListener("click", (e) => {
    const item = e.target.closest(".export-menu-item");
    if (!item) return;
    const fmt = item.dataset.fmt;
    closeExportMenu();
    if (fmt === "md") {
      exportSessionAsMarkdown(session);
      showToast("已导出 Markdown");
    } else if (fmt === "txt") {
      exportSessionAsPlainText(session);
      showToast("已导出纯文本");
    } else {
      exportSessionAsJSON(session);
      showToast("已导出 JSON");
    }
  });
  document.addEventListener("click", onDocClick);
  window._exportMenu = { menu, onDocClick };
}

// 会话项导出按钮的点击入口：拉取完整会话数据后打开菜单
async function onExportClick(btn, sessionId) {
  try {
    const session = await apiGet("/api/sessions/" + sessionId);
    openExportMenu(btn, session);
  } catch (_) {
    showToast("导出失败：无法获取会话");
  }
}
