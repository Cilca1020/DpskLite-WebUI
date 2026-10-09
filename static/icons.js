/* 基础工具与全局状态：DOM 快捷函数、内联 SVG 图标、剪贴板降级、全局会话状态 */
const $ = (sel) => document.querySelector(sel);
const LS_KEY = "dsw_api_key";

// 内联 SVG 图标（用 currentColor 描边，自动跟随主题文字色）。
// 内联可避免 Flask 对 .svg 的 MIME 类型不完整（image/svg 而非 image/svg+xml）
// 导致 CSS mask 不生效的问题。
const ICONS = {
  plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg>',
  save: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2Z"/><path d="M17 21v-8H7v8M7 3v5h8"/></svg>',
  moon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79Z"/></svg>',
  sun: '<svg class="icon-sun" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M12,7.25A4.75,4.75,0,1,0,16.75,12,4.756,4.756,0,0,0,12,7.25Zm0,8A3.25,3.25,0,1,1,15.25,12,3.254,3.254,0,0,1,12,15.25ZM11.25,5V3a.75.75,0,0,1,1.5,0V5a.75.75,0,0,1-1.5,0Zm1.5,14v2a.75.75,0,0,1-1.5,0V19a.75.75,0,0,1,1.5,0ZM5,12.75H3a.75.75,0,0,1,0-1.5H5a.75.75,0,0,1,0,1.5ZM21.75,12a.75.75,0,0,1-.75.75H19a.75.75,0,0,1,0-1.5h2A.75.75,0,0,1,21.75,12ZM5.106,6.166a.75.75,0,0,1,1.061-1.06L7.581,6.52A.75.75,0,0,1,6.52,7.581ZM18.894,17.834a.75.75,0,1,1-1.061,1.06L16.419,17.48a.75.75,0,0,1,1.061-1.061ZM7.581,16.419a.75.75,0,0,1,0,1.061L6.167,18.894a.75.75,0,0,1-1.061-1.06L6.52,16.419A.75.75,0,0,1,7.581,16.419Zm8.838-8.838a.75.75,0,0,1,0-1.061l1.414-1.414a.75.75,0,0,1,1.061,1.06L17.48,7.581a.752.752,0,0,1-1.061,0Z"/></svg>',
  edit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>',
  trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/></svg>',
  retry: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 4v6h-6"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>',
  copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
  download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>',
  copyMd: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2" ry="2"/><path d="M6 9v6M6 15l2-2 2 2M14 9v6M14 9h3a1.5 1.5 0 0 1 0 3h-3" stroke-width="1.8"/></svg>',
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>',
  chat: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5Z"/></svg>',
  // star：取自 static/assets/svg/star.svg，改为 currentColor 填充以跟随主题
  star: '<svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M12.962,4.6,14.9,8.513a1,1,0,0,0,.755.546l4.482.649a1,1,0,0,1,.555,1.705l-3.241,3.145a1,1,0,0,0-.289.886l.741,4.3a1.07,1.07,0,0,1-1.553,1.127L12.466,18.84a1.009,1.009,0,0,0-.932,0L7.649,20.874a1.073,1.073,0,0,1-1.556-1.13l.741-4.3a1,1,0,0,0-.289-.886L3.3,11.413a1,1,0,0,1,.555-1.7l4.482-.649A1,1,0,0,0,9.1,8.513L11.038,4.6A1.074,1.074,0,0,1,12.962,4.6Z"/></svg>',
  // scene：场景锚点（手动分场）按钮图标（羽毛旗 flag）
  scene: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/></svg>',
  // slice：切片管理入口（堆叠分层切片）
  slice: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2 2 7.5 12 13l10-5.5L12 2z"/><path d="M2 12.5 12 18l10-5.5"/><path d="M2 17.5 12 23l10-5.5"/></svg>',
};

// 生成图标按钮内部的 svg 元素（统一尺寸类）
function svgIcon(name) {
  return ICONS[name] || "";
}

// 轻量 toast 提示：居中显示于顶部，自动淡出后移除
function showToast(msg) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  document.body.appendChild(el);
  requestAnimationFrame(() => el.classList.add("toast-show"));
  setTimeout(() => {
    el.classList.remove("toast-show");
    setTimeout(() => el.remove(), 220); // 等待淡出动画结束
  }, 2000);
}

// 复制文本的降级方案（clipboard API 不可用时的 textarea + execCommand）
function fallbackCopy(text, onDone) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try {
    document.execCommand("copy");
    if (onDone) onDone();
  } catch (_) {
    /* 复制失败静默处理 */
  }
  document.body.removeChild(ta);
}

// 当前会话状态（前端内存态，持久化交给后端 storage）
let currentSessionId = null;
// 当前对话上下文：[{role, content}]，不含 system（system 由参数面板提供）
let conversation = [];
// 当前会话最新一轮 user 消息的下标（仅这一轮显示「编辑」按钮）；null 表示无
let lastUserMsgIndex = null;
// 当前会话最后一条 assistant 消息的下标（仅这一条显示「重试」按钮）；null 表示无
let lastAssistantMsgIndex = null;
// 当前会话已打场景锚点（手动分场）的 user 消息全局下标列表（升序），
// 供「第 N 场」默认命名计数；openSession/loadOlderMessages 重建，分场操作就地维护
let sceneAnchors = [];
