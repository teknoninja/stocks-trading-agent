// TradingView sidebar bot — Chrome extension content script.
// Same UI as tradingview_bot/sidebar.js, but all API calls go through the
// background service worker (see background.js).
(function () {
  function __tvBotStart() {
  if (window.__tvBotLoaded) return;
  window.__tvBotLoaded = true;

  const FLAG_COLORS = { BUY: "#16a34a", SELL: "#dc2626", HOLD: "#d97706" };

  // ---------- API via background service worker ----------
  function api(path, body) {
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage({ path, body }, (resp) => {
        if (chrome.runtime.lastError) return reject(chrome.runtime.lastError.message);
        if (!resp || !resp.ok) return reject((resp && resp.error) || "no response");
        resolve(resp.data);
      });
    });
  }

  // ---------- symbol detection ----------
  function detectSymbol() {
    try {
      const u = new URL(window.location.href);
      const q = u.searchParams.get("symbol");
      if (q) return decodeURIComponent(q);
      // symbol overview pages: /symbols/NASDAQ-AAPL/ -> NASDAQ:AAPL
      const sm = u.pathname.match(/^\/symbols\/([A-Z0-9.]+)-([A-Z0-9.!&_]+)\/?/);
      if (sm) return `${sm[1]}:${sm[2]}`;
      const sp = u.pathname.match(/^\/symbols\/([A-Z0-9.]+)\/?/);
      if (sp) return sp[1];
      // only parse the title on chart pages (list/screener titles aren't symbols)
      if (!u.pathname.startsWith("/chart")) return null;
    } catch (e) {}
    // Title looks like: "AAPL 211.16 ▲ +0.53% Unusual ..."
    const t = document.title || "";
    const m = t.match(/^([A-Z0-9.\-:_!&]{1,20})[\s,]/);
    if (m) return m[1];
    return null;
  }

  // ---------- UI ----------
  const panel = document.createElement("div");
  panel.id = "tv-bot-panel";
  panel.style.cssText = [
    "position:fixed", "top:0", "right:0", "width:330px", "height:100vh",
    "background:#0f1117", "color:#e5e7eb", "z-index:2147483647",
    "font-family:-apple-system,Segoe UI,Roboto,sans-serif", "font-size:13px",
    "display:flex", "flex-direction:column", "box-shadow:-4px 0 18px rgba(0,0,0,.5)",
    "transition:transform .25s ease",
  ].join(";");

  panel.innerHTML = `
    <div style="padding:10px 14px;display:flex;align-items:center;gap:8px;border-bottom:1px solid #262a35;">
      <span style="font-size:16px">🤖</span>
      <b style="flex:1">Stock Flag Bot</b>
      <span id="tvb-llm" style="font-size:10px;color:#9ca3af"></span>
      <button id="tvb-hide" style="background:none;border:none;color:#9ca3af;cursor:pointer;font-size:15px">✕</button>
    </div>
    <div style="padding:12px 14px;border-bottom:1px solid #262a35">
      <div style="display:flex;align-items:center;gap:10px">
        <div>
          <div style="display:flex;align-items:center;gap:6px">
            <div id="tvb-symbol" style="font-weight:700;font-size:15px">—</div>
            <button id="tvb-watch" title="Add/remove from scanner watchlist" style="background:none;border:none;color:#6b7280;font-size:17px;cursor:pointer;padding:0;line-height:1">☆</button>
          </div>
          <div id="tvb-price" style="color:#9ca3af;font-size:12px"></div>
        </div>
        <div style="flex:1"></div>
        <div id="tvb-flag" style="padding:6px 14px;border-radius:8px;font-weight:800;font-size:15px;background:#374151">…</div>
      </div>
      <div id="tvb-conf" style="margin-top:6px;color:#9ca3af;font-size:11px">Browse the list and open any stock — I'll follow it and generate a flag.</div>
      <div id="tvb-reasons" style="margin-top:8px;font-size:11.5px;line-height:1.45;max-height:150px;overflow-y:auto"></div>
      <div style="margin-top:10px;display:flex;gap:6px">
        <button id="tvb-buy" style="flex:1;background:#16a34a;border:none;color:#fff;border-radius:8px;padding:7px 8px;cursor:pointer;font-size:12px;font-weight:700">📄 Buy</button>
        <button id="tvb-sell" style="flex:1;background:#dc2626;border:none;color:#fff;border-radius:8px;padding:7px 8px;cursor:pointer;font-size:12px;font-weight:700">📄 Sell</button>
        <button id="tvb-auto" style="flex:1.2;background:#1b1f2a;border:1px solid #2d3342;color:#9ca3af;border-radius:8px;padding:7px 8px;cursor:pointer;font-size:12px">⏻ Auto: …</button>
      </div>
      <div class="note" style="margin-top:5px;color:#6b7280;font-size:10px">Paper only — your call, even against the flag</div>
    </div>
    <div id="tvb-chat" style="flex:1;overflow-y:auto;padding:12px 14px;display:flex;flex-direction:column;gap:8px"></div>
    <div style="padding:10px 12px;border-top:1px solid #262a35;display:flex;gap:6px">
      <input id="tvb-input" placeholder="Ask about this stock…" style="flex:1;background:#1b1f2a;border:1px solid #2d3342;border-radius:8px;color:#e5e7eb;padding:8px 10px;outline:none"/>
      <button id="tvb-send" style="background:#2962ff;border:none;color:#fff;border-radius:8px;padding:8px 12px;cursor:pointer;font-weight:600">➤</button>
    </div>
    <div style="padding:4px 12px 8px;color:#6b7280;font-size:10px;text-align:center">Educational only — not financial advice ·
      <a href="http://127.0.0.1:8765/performance" target="_blank" style="color:#60a5fa">performance</a> ·
      <a href="http://127.0.0.1:8765/watchlists" target="_blank" style="color:#60a5fa">watchlists</a></div>`;
  document.body.appendChild(panel);

  const toggle = document.createElement("button");
  toggle.textContent = "🤖";
  toggle.style.cssText =
    "position:fixed;bottom:22px;right:22px;width:46px;height:46px;border-radius:50%;border:none;background:#2962ff;color:#fff;font-size:20px;cursor:pointer;z-index:2147483646;box-shadow:0 4px 14px rgba(0,0,0,.4);display:none";
  document.body.appendChild(toggle);

  const $ = (id) => document.getElementById(id);
  $("tvb-hide").onclick = () => { panel.style.transform = "translateX(100%)"; toggle.style.display = "block"; };
  toggle.onclick = () => { panel.style.transform = "translateX(0)"; toggle.style.display = "none"; };

  function addMsg(text, who) {
    const div = document.createElement("div");
    div.style.cssText =
      who === "user"
        ? "align-self:flex-end;background:#2962ff;color:#fff;padding:7px 10px;border-radius:10px 10px 2px 10px;max-width:85%;white-space:pre-wrap"
        : "align-self:flex-start;background:#1b1f2a;padding:7px 10px;border-radius:10px 10px 10px 2px;max-width:92%;white-space:pre-wrap";
    div.textContent = text;
    $("tvb-chat").appendChild(div);
    $("tvb-chat").scrollTop = $("tvb-chat").scrollHeight;
    return div;
  }

  // ---------- data ----------
  let currentSymbol = null;
  let analyzing = false;

  async function refreshAnalysis(symbol) {
    if (analyzing) return;
    analyzing = true;
    $("tvb-symbol").textContent = symbol;
    $("tvb-flag").textContent = "…";
    $("tvb-flag").style.background = "#374151";
    $("tvb-conf").textContent = "analyzing (structure, zones, VWAP, volume profile, divergences…)";
    $("tvb-reasons").innerHTML = "";
    try {
      const a = await api(`/analyze?symbol=${encodeURIComponent(symbol)}`);
      if (a.error) {
        $("tvb-flag").textContent = "N/A";
        $("tvb-conf").textContent = a.error;
        return;
      }
      $("tvb-flag").textContent = a.flag;
      $("tvb-flag").style.background = FLAG_COLORS[a.flag] || "#374151";
      $("tvb-price").textContent = `${a.price} (${a.change_pct >= 0 ? "+" : ""}${a.change_pct}%)`;
      $("tvb-conf").textContent = `score ${a.score >= 0 ? "+" : ""}${a.score} · confidence ${Math.round(a.confidence * 100)}% · ${a.signal_counts.bullish}▲ / ${a.signal_counts.bearish}▼ signals`;
      const mk = (arr, color, sign) =>
        (arr || []).slice(0, 3).map((x) => `<div style="color:${color};margin-bottom:3px">${sign} ${x}</div>`).join("");
      $("tvb-reasons").innerHTML =
        mk(a.bullish_reasons, "#4ade80", "▲") + mk(a.bearish_reasons, "#f87171", "▼");
    } catch (e) {
      $("tvb-flag").textContent = "OFF";
      $("tvb-conf").textContent = "Local bot server unreachable — run: python run_tradingview_bot.py --no-browser";
    } finally {
      analyzing = false;
    }
  }

  async function send() {
    const q = $("tvb-input").value.trim();
    if (!q || !currentSymbol) return;
    $("tvb-input").value = "";
    addMsg(q, "user");
    const pending = addMsg("thinking…", "bot");
    try {
      const d = await api("/ask", { symbol: currentSymbol, question: q });
      pending.textContent = d.answer;
    } catch (e) {
      pending.textContent = "Server unreachable — run: python run_tradingview_bot.py --no-browser";
    }
  }
  $("tvb-send").onclick = send;
  $("tvb-input").addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
  // keep TradingView keyboard shortcuts from hijacking typing
  ["keyup", "keypress"].forEach((ev) =>
    $("tvb-input").addEventListener(ev, (e) => e.stopPropagation()));
  $("tvb-input").addEventListener("keydown", (e) => e.stopPropagation());

  api("/health").then((h) => {
    $("tvb-llm").textContent = h.llm ? `LLM: ${h.model}` : "LLM: off (rule-based)";
  }).catch(() => { $("tvb-llm").textContent = "server off"; });

  // ---------- paper trading (explicit side — your call, even against the flag) ----------
  async function paperTrade(side) {
    if (!currentSymbol) return;
    const pending = addMsg(`submitting paper ${side} for ${currentSymbol}…`, "bot");
    try {
      const d = await api("/papertrade", { symbol: currentSymbol, side });
      pending.textContent = d.ok ? `✅ ${d.action}` : `⚠️ ${d.error}`;
    } catch (e) { pending.textContent = "Server unreachable."; }
  }
  $("tvb-buy").onclick = () => paperTrade("buy");
  $("tvb-sell").onclick = () => paperTrade("sell");

  // ---------- scanner watchlist toggle ----------
  function paintWatch(st) {
    const b = $("tvb-watch");
    if (!st || !st.supported) { b.textContent = "☆"; b.style.color = "#3a3f4d"; b.title = "Not supported (US/NSE only)"; return; }
    b.textContent = st.watching ? "★" : "☆";
    b.style.color = st.watching ? "#eda100" : "#6b7280";
    b.title = st.watching ? `On the ${st.market.toUpperCase()} scanner watchlist — click to remove`
                          : `Add to the ${st.market.toUpperCase()} scanner watchlist`;
  }
  function refreshWatch(sym) {
    api(`/watchlist/status?symbol=${encodeURIComponent(sym)}`).then(paintWatch).catch(() => paintWatch(null));
  }
  $("tvb-watch").onclick = async () => {
    if (!currentSymbol) return;
    try {
      const d = await api("/watchlist/toggle", { symbol: currentSymbol });
      if (d.error) { addMsg(`⚠️ ${d.error}`, "bot"); return; }
      paintWatch({ supported: true, watching: d.watching, market: d.market });
      addMsg(`${d.watching ? "⭐" : "☆"} ${d.action}${d.note ? " — " + d.note : ""}. ` +
        (d.watching ? "The auto-scanner will now trade it when Auto is ON." : "The auto-scanner will ignore it now."), "bot");
    } catch (e) { addMsg("Server unreachable.", "bot"); }
  };

  // ---------- after-hours auto-trading toggle ----------
  let autoState = null; // null=unknown/unconfigured, true/false=state
  function paintAuto() {
    const b = $("tvb-auto");
    if (autoState === null) {
      b.textContent = "⏻ Auto: n/a";
      b.style.color = "#6b7280";
    } else if (autoState) {
      b.textContent = "⏻ Auto: ON";
      b.style.color = "#4ade80"; b.style.borderColor = "#16a34a";
    } else {
      b.textContent = "⏻ Auto: OFF";
      b.style.color = "#9ca3af"; b.style.borderColor = "#2d3342";
    }
  }
  api("/autotrade").then((s) => { autoState = s.configured ? s.enabled : null; paintAuto(); })
    .catch(() => paintAuto());
  $("tvb-auto").onclick = async () => {
    if (autoState === null) {
      addMsg("Auto-trading toggle isn't configured on the server (needs GITHUB_TOKEN + GITHUB_REPO). You can also flip the AUTO_TRADING variable on github.com → repo Settings → Actions variables.", "bot");
      return;
    }
    try {
      const d = await api("/autotrade", { on: !autoState });
      if (d.ok) {
        autoState = d.enabled; paintAuto();
        addMsg(autoState
          ? "🟢 Auto-trading ON — the GitHub scanner will trade your watchlist during US market hours, even with this laptop off."
          : "⚪ Auto-trading OFF — no scheduled trades will run.", "bot");
      } else { addMsg(`⚠️ ${d.error}`, "bot"); }
    } catch (e) { addMsg("Server unreachable.", "bot"); }
  };

  // watch for symbol changes as user navigates TradingView (SPA)
  setInterval(() => {
    const s = detectSymbol();
    if (s && s !== currentSymbol) {
      currentSymbol = s;
      $("tvb-chat").innerHTML = "";
      addMsg(`Now watching ${s}. Ask me anything — e.g. "why this flag?", "where are the demand zones?", "is there divergence?"`, "bot");
      refreshAnalysis(s);
      refreshWatch(s);
    }
  }, 1500);
  }

  if (document.body) __tvBotStart();
  else document.addEventListener("DOMContentLoaded", __tvBotStart);
})();
