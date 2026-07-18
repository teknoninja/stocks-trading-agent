// Service worker: proxies API calls to the local analysis server.
// Content scripts on https pages can't reliably reach http://127.0.0.1
// (CSP + local-network access rules), but the extension background can,
// thanks to host_permissions in the manifest.
const API = "http://127.0.0.1:8765";

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || !msg.path) return;
  const opts = { method: msg.method || "GET" };
  if (msg.body) {
    opts.method = "POST";
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(msg.body);
  }
  fetch(API + msg.path, opts)
    .then((r) => r.json())
    .then((data) => sendResponse({ ok: true, data }))
    .catch((e) => sendResponse({ ok: false, error: String(e) }));
  return true; // keep the message channel open for the async response
});
