/* 消息注释模块：为「本轮」用户消息附加一次性提示词（仅当前轮注入，不写入历史上下文）。
 * 全局暴露 window.AnnotationModule，供 chat.js / render.js / sessions.js 调用。
 * - 输入区「注释」按钮：编辑入口（挂载方式类比文件，但只对本次发送生效）。
 * - 已发送消息操作条注释按钮：只读查看入口。
 * - 面板为独立模态（与「切片管理」面板一致），非输入区内联展开。
 */
(function () {
  "use strict";

  function el(id) {
    return document.getElementById(id);
  }

  // 待发送注释以 #noteInput 文本域为唯一真源，避免状态不同步
  function getPending() {
    const ta = el("noteInput");
    return ta ? ta.value : "";
  }

  function hasPending() {
    return !!getPending().trim();
  }

  function setPending(text) {
    const ta = el("noteInput");
    if (ta) ta.value = text || "";
    syncComposer();
  }

  function clearPending() {
    setPending("");
  }

  // 从历史消息恢复注释（编辑提问时复用）：有内容则打开面板，便于修改后重发
  function restore(text) {
    setPending(text || "");
    if ((text || "").trim()) openPanel();
  }

  /* ---------------- 模态面板 ---------------- */

  // mode: "edit"（输入区挂载，可编辑） | "view"（已发送消息，只读）
  function openModal(mode, text) {
    const modal = el("noteModal");
    if (!modal) return;
    const isView = mode === "view";
    const title = el("noteModalTitle");
    const hint = el("noteModalHint");
    const input = el("noteInput");
    const viewBody = el("noteViewBody");
    const foot = el("noteModalFoot");

    if (title) title.textContent = isView ? "本轮注释（仅本次对话生效）" : "本轮注释";
    if (hint) hint.hidden = isView;
    if (input) input.hidden = isView;
    if (viewBody) {
      viewBody.hidden = !isView;
      if (isView) viewBody.textContent = text || "";
    }
    if (foot) foot.hidden = isView; // 只读时无「清除/完成」

    modal.classList.remove("hidden");
    if (!isView && input) input.focus();
  }

  function closeModal() {
    const modal = el("noteModal");
    if (modal) modal.classList.add("hidden");
    syncComposer();
  }

  // 输入区「注释」按钮：打开编辑模态
  function openPanel() {
    openModal("edit");
  }

  // 已发送消息的注释查看按钮：打开只读模态
  function openViewer(text) {
    openModal("view", text);
  }

  function isPanelOpen() {
    const modal = el("noteModal");
    return !!modal && !modal.classList.contains("hidden");
  }

  /* ---------------- 输入区按钮态 ---------------- */

  // 刷新「注释」按钮高亮态：有注释或面板展开时高亮（不在输入区额外渲染卡片）
  function syncComposer() {
    const btn = el("noteBtn");
    if (btn) btn.classList.toggle("active", hasPending() || isPanelOpen());
  }

  function init() {
    const ic = el("noteModalIc");
    if (ic) ic.innerHTML = svgIcon("note");

    const btn = el("noteBtn");
    if (btn) btn.onclick = openPanel;

    const input = el("noteInput");
    if (input) input.addEventListener("input", syncComposer);

    const done = el("noteDoneBtn");
    if (done) done.onclick = closeModal;

    const close = el("noteModalClose");
    if (close) close.onclick = closeModal;

    const clear = el("noteClearBtn");
    if (clear) {
      clear.onclick = () => {
        clearPending();
        const ta = el("noteInput");
        if (ta) ta.focus();
      };
    }

    // 点击遮罩区域关闭（与切片管理面板一致：仅当点击目标就是遮罩层本身）
    const modal = el("noteModal");
    if (modal) {
      modal.addEventListener("click", (e) => {
        if (e.target === modal) closeModal();
      });
    }

    syncComposer();
  }

  window.AnnotationModule = {
    init,
    getPending,
    setPending,
    clearPending,
    hasPending,
    restore,
    openPanel,
    closePanel: closeModal,
    openViewer,
    syncComposer,
  };

  // 自动初始化（脚本位于 body 末尾，DOM 已就绪）
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
