/* API 封装：后端接口请求 + 流式对话 */

async function apiGet(url) {
  const r = await fetch(url);
  return r.json();
}

// 非 2xx 响应统一构造异常：带上后端返回的 error 文本（风格与 streamChat 一致）。
// 原先 apiPost / apiDelete 不检查 r.ok，任何 4xx/5xx 只要返回合法 JSON 就被静默忽略，
// 会把「写库失败」掩盖成「发送成功」，进而出现 user 消息没落库只剩 assistant 的现象。
async function apiError(r) {
  const body = await r.json().catch(() => ({}));
  const e = new Error((body && body.error) || "请求失败（HTTP " + r.status + "）");
  e.status = r.status;
  e.body = body;
  return e;
}

async function apiPost(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw await apiError(r);
  return r.json();
}

async function apiDelete(url) {
  const r = await fetch(url, { method: "DELETE" });
  if (!r.ok) throw await apiError(r);
  return r.json();
}

// 流式对话：返回后端逐块文本，通过 onChunk 回调渲染。
// signal 可选：传入 AbortSignal 后可被「停止生成」中断（中断抛 AbortError）。
async function streamChat(payload, onChunk, signal) {
  const r = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal,
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    // 附带 status 与响应体，便于调用方识别向量记忆不可用等特定错误
    const e = new Error(err.error || "请求失败");
    e.status = r.status;
    e.body = err;
    throw e;
  }
  const reader = r.body.getReader();
  const decoder = new TextDecoder("utf-8");
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    onChunk(decoder.decode(value, { stream: true }));
  }
}
