/* SIGNAL-X trading-partner drawer.
   Server-side Ollama Cloud call; the API key never reaches this file.
   Model output is rendered through a tiny escape-first Markdown subset. */

"use strict";

const Chat = (() => {
  const $ = (id) => document.getElementById(id);
  const ui = {
    open: false,
    busy: false,
    configured: false,
    model: "minimax-m3.1-flash",
    skills: 0,
    history: [],
    ctxKey: null,
    abort: null,
  };
  const el = {};

  function appState() {
    try {
      return typeof state !== "undefined" ? state : null;
    } catch {
      return null;
    }
  }

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function inline(value) {
    return value
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  }

  function mdLite(text) {
    const lines = String(text || "").split(/\r?\n/);
    let html = "";
    let list = null;
    for (const raw of lines) {
      const line = raw.trimEnd();
      const bullet = line.match(/^\s*[-*]\s+(.*)$/);
      const numbered = line.match(/^\s*\d+\.\s+(.*)$/);
      if (bullet || numbered) {
        const kind = bullet ? "ul" : "ol";
        if (list !== kind) {
          if (list) html += `</${list}>`;
          html += `<${kind}>`;
          list = kind;
        }
        html += `<li class="md-li">${inline(esc((bullet || numbered)[1]))}</li>`;
        continue;
      }
      if (list) { html += `</${list}>`; list = null; }
      const heading = line.match(/^\s*#{1,3}\s+(.*)$/);
      if (heading) {
        html += `<span class="md-h">${inline(esc(heading[1]))}</span>`;
      } else if (!line.trim()) {
        html += "<br>";
      } else {
        html += `<span>${inline(esc(line))}</span><br>`;
      }
    }
    if (list) html += `</${list}>`;
    return html || esc(text);
  }

  function scrollBottom() {
    if (el.log) el.log.scrollTop = el.log.scrollHeight;
  }

  function fillModels(models) {
    if (!el.modelList) return;
    el.modelList.innerHTML = "";
    for (const item of models || []) {
      const id = typeof item === "string" ? item : item.id;
      if (!id) continue;
      const option = document.createElement("option");
      option.value = id;
      option.label = (typeof item === "object" && item.label) || id;
      el.modelList.appendChild(option);
    }
  }

  function setStatus(text, cls) {
    if (!el.status) return;
    el.status.textContent = text;
    el.status.className = "chat-status" + (cls ? " " + cls : "");
  }

  function resetConversation() {
    ui.history = [];
    if (!el.log) return;
    el.log.innerHTML = "";
    const empty = document.createElement("div");
    empty.className = "chat-empty";
    empty.textContent =
      "I'm your chart partner. Ask about structure, patterns, EMA alignment, RSI/divergence, " +
      "volume, funding/OI or order flow. I can also pull gainers, other symbols, journal " +
      "signals, SNIPER diagnostics and paper state through read-only tools. " +
      "The model thinks before answering; complex charts can take 1–3 minutes.";
    el.log.appendChild(empty);
  }

  function toolLabel(call) {
    const labels = {
      get_gainers: "Gainers",
      get_snapshot: "Snapshot",
      get_signals: "Signals",
      get_sniper: "SNIPER",
      get_sniper_watchlist: "SNIPER watchlist",
      get_paper: "Paper",
    };
    const name = call && call.name ? String(call.name) : "tool";
    const args = call && call.arguments;
    const symbol = args && typeof args === "object" && args.symbol ? String(args.symbol) : "";
    return (labels[name] || name) + (symbol ? " · " + symbol : "");
  }

  function addMessage(role, text, skills, tools) {
    if (!el.log) return null;
    const wrap = document.createElement("div");
    wrap.className = "chat-msg " + role;
    const label = document.createElement("span");
    label.className = "role";
    label.textContent = role === "user" ? "You" : role === "assistant" ? "SIGNAL-X" : "Error";
    const body = document.createElement("div");
    body.className = "text";
    body.innerHTML = mdLite(text);
    wrap.appendChild(label);
    wrap.appendChild(body);
    if (Array.isArray(skills) && skills.length) {
      const foot = document.createElement("div");
      foot.className = "skills";
      const names = skills.slice(0, 5).map((s) => s.name || s.id).filter(Boolean);
      foot.textContent = skills.length + " lenses · " + names.join(" · ") +
        (skills.length > names.length ? " · +" + (skills.length - names.length) : "");
      wrap.appendChild(foot);
    }
    if (Array.isArray(tools) && tools.length) {
      const foot = document.createElement("div");
      foot.className = "tools";
      for (const call of tools.slice(0, 6)) {
        const chip = document.createElement("span");
        chip.className = "chat-tool" + (call && call.ok === false ? " bad" : "");
        chip.textContent = toolLabel(call);
        foot.appendChild(chip);
      }
      wrap.appendChild(foot);
    }
    el.log.appendChild(wrap);
    scrollBottom();
    return wrap;
  }

  function setBusy(busy) {
    ui.busy = busy;
    if (!el.send || !el.input) return;
    el.send.disabled = busy || !ui.configured;
    el.input.disabled = !ui.configured;
    el.model.disabled = busy;
    el.input.placeholder = busy ? "Reading the chart…" : "Ask your trading partner…";
  }

  function errorDetail(data, status) {
    if (data && Array.isArray(data.detail)) {
      const parts = data.detail.map((item) => item && (item.msg || item.message)).filter(Boolean);
      if (parts.length) return parts.join("; ");
    }
    if (data && typeof data.detail === "string") return data.detail;
    return "Chat request failed (HTTP " + status + ")";
  }

  async function loadStatus() {
    try {
      const response = await fetch("/api/chat/status", { cache: "no-store" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorDetail(data, response.status));
      ui.configured = !!data.configured;
      ui.model = data.model || "minimax-m3.1-flash";
      ui.skills = data.skills || 0;
      if (el.model) el.model.value = ui.model;
      fillModels(data.models);
      updateFoot();
      if (ui.configured) {
        setStatus(ui.skills + " skill lenses · " + ui.model + " · deep thinking may take 1–3 min", "ok");
        setBusy(false);
      } else {
        setStatus(
          "Chat offline — set OLLAMA_API_KEY or ~/.config/signal-x/ollama.key, then reload the dashboard.",
          "warn"
        );
        setBusy(false);
      }
    } catch (error) {
      ui.configured = false;
      setStatus("Chat status unavailable: " + (error.message || "API error"), "warn");
      setBusy(false);
    }
  }

  async function switchModel() {
    const model = el.model.value.trim();
    if (!model || model === ui.model) {
      el.model.value = ui.model;
      return;
    }
    const previous = ui.model;
    el.model.disabled = true;
    setStatus("Switching model to " + model + "…", "");
    try {
      const response = await fetch("/api/chat/model", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model }),
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) throw new Error(errorDetail(data, response.status));
      ui.model = data.model || model;
      ui.configured = !!data.configured;
      ui.skills = data.skills || ui.skills;
      el.model.value = ui.model;
      fillModels(data.models);
      updateFoot();
      resetConversation();
      updateSuggestions();
      if (ui.configured) {
        setStatus(ui.skills + " skill lenses · " + ui.model + " · deep thinking may take 1–3 min", "ok");
      } else {
        setStatus("Model saved, but chat is offline — add an Ollama API key.", "warn");
      }
    } catch (error) {
      el.model.value = previous;
      setStatus("Model switch failed: " + (error.message || "API error"), "warn");
    } finally {
      setBusy(false);
    }
  }

  function updateSuggestions() {
    if (!el.suggest) return;
    const prompts = [
      "What pattern is forming here?",
      "Walk me through this chart",
      "What would invalidate this read?",
      "Is order flow confirming price?",
    ];
    el.suggest.innerHTML = "";
    for (const prompt of prompts) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = prompt;
      button.addEventListener("click", () => {
        if (ui.busy) return;
        el.input.value = prompt;
        send(prompt);
      });
      el.suggest.appendChild(button);
    }
  }

  function updateFoot() {
    if (!el.foot) return;
    el.foot.textContent =
      "SIGNAL-X trading partner · " + ui.model +
      " · observation only, not financial advice";
  }

  function syncContext() {
    if (!el.context) return;
    const app = appState();
    const sym = app && app.sym;
    const tf = (app && app.tf) || "5m";
    const market = (app && app.market) || "auto";
    el.context.textContent = sym ? sym + " · " + tf + " · " + market : "load a chart to begin";
    // Keep the conversation when only the timeframe/market changes. The next
    // request already carries the fresh chart packet; a symbol change is a
    // different conversation and still starts clean.
    const key = sym || null;
    if (key !== ui.ctxKey) {
      ui.ctxKey = key;
      resetConversation();
      updateSuggestions();
    }
  }

  function open() {
    if (!el.panel) return;
    syncContext();
    el.panel.hidden = false;
    el.scrim.hidden = false;
    ui.open = true;
    el.toggle.classList.add("on");
    el.toggle.setAttribute("aria-expanded", "true");
    setTimeout(() => { if (el.input && ui.configured) el.input.focus(); }, 30);
    scrollBottom();
  }

  function close() {
    if (!el.panel) return;
    el.panel.hidden = true;
    el.scrim.hidden = true;
    ui.open = false;
    el.toggle.classList.remove("on");
    el.toggle.setAttribute("aria-expanded", "false");
  }

  function toggle() {
    if (ui.open) close();
    else open();
  }

  async function send(text) {
    if (ui.busy) return;
    const message = String(text || "").trim();
    if (!message) return;
    if (!ui.configured) {
      addMessage("error", "Chat is offline. Set OLLAMA_API_KEY or ~/.config/signal-x/ollama.key, then reload the dashboard.");
      return;
    }
    const app = appState();
    const symbol = app && app.sym;
    if (!symbol) {
      addMessage("error", "Load a chart first, then ask about it.");
      return;
    }

    const market = app.market || "auto";
    const tf = app.tf || "5m";
    el.input.value = "";
    addMessage("user", message);
    const typing = addMessage("assistant", "Reading the chart and checking SIGNAL-X evidence — M3.1 Flash can take 1–3 minutes.", []);
    if (typing) typing.classList.add("chat-typing");
    setBusy(true);

    const controller = new AbortController();
    ui.abort = controller;
    const timer = setTimeout(() => controller.abort(), 310000);
    let clientTimezone = "";
    try {
      clientTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    } catch { /* fall back to the server timezone */ }
    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message,
          symbol,
          market,
          tf,
          client_now_ms: Date.now(),
          client_timezone: clientTimezone,
          history: ui.history.slice(-8).map((turn) => ({
            role: turn.role,
            content: String(turn.content || "").slice(0, 4000),
          })),
        }),
        signal: controller.signal,
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) throw new Error(errorDetail(data, response.status));
      if (typing) typing.remove();
      const reply = (data && data.reply) || "No reply returned.";
      addMessage("assistant", reply, data && data.skills, data && data.tool_calls);
      ui.history.push({ role: "user", content: message });
      ui.history.push({ role: "assistant", content: reply });
      ui.history = ui.history.slice(-8);
      if (data && data.context && data.context.symbol) {
        el.context.textContent =
          data.context.symbol + " · " + (data.context.tf || tf) + " · " + (data.context.market || market);
      }
    } catch (error) {
      if (typing) typing.remove();
      const msg = error && error.name === "AbortError"
        ? "Request cancelled after 310s. The model may still be processing; try a shorter question."
        : (error && error.message) || "Chat request failed.";
      addMessage("error", msg);
    } finally {
      clearTimeout(timer);
      ui.abort = null;
      setBusy(false);
      scrollBottom();
    }
  }

  function onKey(event) {
    if (event.key === "Escape" && ui.open) {
      event.preventDefault();
      event.stopImmediatePropagation();
      close();
      return;
    }
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    const target = event.target;
    const typing = target && (
      target.tagName === "INPUT" || target.tagName === "TEXTAREA" ||
      target.tagName === "SELECT" || target.isContentEditable
    );
    if (!typing && (event.key === "c" || event.key === "C")) {
      event.preventDefault();
      event.stopImmediatePropagation();
      toggle();
    }
  }

  function init() {
    el.panel = $("chat-panel");
    if (!el.panel) return;
    Object.assign(el, {
      scrim: $("chat-scrim"),
      toggle: $("chat-toggle"),
      close: $("chat-close"),
      clear: $("chat-clear"),
      context: $("chat-context"),
      model: $("chat-model"),
      modelList: $("chat-model-list"),
      status: $("chat-status"),
      foot: $("chat-foot"),
      log: $("chat-log"),
      suggest: $("chat-suggest"),
      form: $("chat-form"),
      input: $("chat-input"),
      send: $("chat-send"),
    });

    el.toggle.addEventListener("click", toggle);
    el.close.addEventListener("click", close);
    el.scrim.addEventListener("click", close);
    el.clear.addEventListener("click", () => {
      resetConversation();
      if (ui.configured) el.input.focus();
    });
    el.form.addEventListener("submit", (event) => {
      event.preventDefault();
      send(el.input.value);
    });
    el.input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        el.form.requestSubmit();
      }
    });
    el.model.addEventListener("change", switchModel);
    el.model.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        el.model.blur();
      }
    });
    el.input.addEventListener("input", () => {
      el.input.style.height = "auto";
      el.input.style.height = Math.min(120, Math.max(40, el.input.scrollHeight)) + "px";
    });
    document.addEventListener("keydown", onKey, true);

    resetConversation();
    updateSuggestions();
    loadStatus();
  }

  return { init, syncContext, toggle, open, close };
})();

window.Chat = Chat;
Chat.init();
