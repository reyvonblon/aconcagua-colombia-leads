async function apiJson(url, options = {}) {
  const opts = { ...options };
  if (opts.body && typeof opts.body !== "string") {
    opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    opts.body = JSON.stringify(opts.body);
  }
  const resp = await fetch(url, opts);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.error || `Error HTTP ${resp.status}`);
  }
  return data;
}

function showNotice(el, message, isError = false) {
  if (!el) return;
  el.textContent = message;
  el.classList.remove("hidden", "error");
  if (isError) el.classList.add("error");
}

function escapeHtml(str) {
  return String(str ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}
