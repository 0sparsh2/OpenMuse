/* OpenMuse web client — views. Renders every client surface from the
 * API layer in openmuse-api.js. All durable state is server state;
 * this file holds only ephemeral UI state (drafts, selection, tabs). */
(function () {
  "use strict";
  const API = window.OpenMuseAPI;

  const state = {
    tab: "chat",
    chats: [],            // [{sessionId, chatId, title, messages: []}]
    activeChat: 0,
    approvals: [],        // ApprovalCard list (pending)
    drafts: {},
  };

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  function el(tag, attrs, children) {
    const n = document.createElement(tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) {
      if (k === "text") n.textContent = v;
      else if (k === "html") n.innerHTML = v;
      else n.setAttribute(k, v);
    }
    (children || []).forEach((c) => n.appendChild(
      typeof c === "string" ? document.createTextNode(c) : c));
    return n;
  }

  function toast(msg) {
    const t = el("div", { class: "toast", role: "status", text: msg });
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 4200);
  }

  function esc(s) { return String(s == null ? "" : s); }

  /* -- tab shell ---------------------------------------------------------- */
  const TABS = [
    ["chat", "Chat", "M4 5.5h16v10H9.5L5 19.5v-4H4z"],
    ["approvals", "Approvals", "M12 3l7 7-7 7-7-7z M12 8v4"],
    ["feed", "Feed", "M4 6h16M4 12h16M4 18h10"],
    ["goals", "Goals", "M5 13l4 4L19 7"],
    ["library", "Library", "M4 5h6v14H4zM10 5h6v14h-6zM16 5h4v14h-4z"],
    ["ideas", "Ideas", "M12 3a7 7 0 00-4 12.7V18h8v-2.3A7 7 0 0012 3zM9 21h6"],
    ["memory", "Memory", "M12 3v18M5 8l7-5 7 5M5 16l7 5 7-5"],
    ["schedules", "Schedules", "M12 7v5l3 3M12 21a9 9 0 100-18 9 9 0 000 18z"],
    ["connectors", "Apps", "M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM17 14v6M14 17h6"],
    ["activity", "Activity", "M3 12h4l3-8 4 16 3-8h4"],
    ["usage", "Usage", "M4 20V10M10 20V4M16 20v-8M22 20H2"],
    ["voice", "Voice", "M12 3a3 3 0 013 3v6a3 3 0 01-6 0V6a3 3 0 013-3zM6 11a6 6 0 0012 0M12 18v3"],
  ];

  function icon(path) {
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="' + path + '"/></svg>';
  }

  // Bottom tab bar: the five everyday destinations; everything else is in the drawer.
  const TABBAR = [["chat", "Chat"], ["library", "Library"], ["ideas", "Ideas"], ["goals", "Goals"], ["connectors", "Apps"]];

  function openDrawer(open) {
    const d = $("#drawer"), o = $("#drawerOverlay"), b = $("#menuBtn");
    if (!d) return;
    d.classList.toggle("open", open);
    o.hidden = !open;
    b.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) { const f = $("#drawerNew"); if (f) f.focus(); if (state.renderChatList) state.renderChatList(); }
    else b.focus();
  }

  function renderTabbar() {
    const bar = $("#tabbar"); if (!bar) return;
    bar.innerHTML = "";
    TABBAR.forEach(([id, label]) => {
      const t = TABS.find((x) => x[0] === id);
      const b = el("button", { type: "button", "data-tab": id, "aria-label": label, title: label,
                               "aria-current": state.tab === id ? "page" : "false" });
      b.innerHTML = icon(t[2]);
      b.addEventListener("click", () => showTab(id));
      bar.appendChild(b);
    });
  }

  function renderNav() {
    renderTabbar();
    const nav = $("#nav"); nav.innerHTML = "";
    nav.appendChild(el("div", { class: "nav-label", text: "Sections" }));
    TABS.forEach(([id, label, path], i) => {
      const b = el("button", {
        type: "button",
        "aria-current": state.tab === id ? "page" : "false",
        "aria-label": label + " tab",
        "data-tab": id,
        title: label + " (Alt+" + ((i + 1) % 10) + ")",
      });
      b.innerHTML = icon(path) + "<span>" + esc(label) +
        (id === "approvals" && state.approvals.length
          ? ' <span class="badge warn">' + state.approvals.length + "</span>" : "") + "</span>";
      b.addEventListener("click", () => showTab(id));
      nav.appendChild(b);
    });
  }

  function showTab(id) {
    state.tab = id;
    if ($("#drawer") && $("#drawer").classList.contains("open")) openDrawer(false);
    const mainEl = $("#main"); if (mainEl) mainEl.classList.toggle("chat-mode", id === "chat");
    renderNav();
    $$(".view").forEach((v) => { v.hidden = v.dataset.view !== id; });
    const view = $('.view[data-view="' + id + '"]');
    if (view && VIEWS[id] && VIEWS[id].onShow) VIEWS[id].onShow(view);
    const h = view ? view.querySelector("h1") : null;
    if (h) { h.setAttribute("tabindex", "-1"); }
  }

  /* -- chat view ------------------------------------------------------------ */
  // Conversation-first surface: avatar header with live status, bubbles,
  // live tool cards (the browser card streams real frames), inline approvals,
  // and a pill composer. Transcripts persist per browser (no secrets in them).
  const AVATAR_SVG =
    '<svg viewBox="0 0 64 64" aria-hidden="true">' +
    '<defs><radialGradient id="omAvG" cx="40%" cy="32%" r="75%">' +
    '<stop offset="0" stop-color="#ffe7d6"/><stop offset=".65" stop-color="#f5c3a5"/>' +
    '<stop offset="1" stop-color="#e2a07f"/></radialGradient></defs>' +
    '<circle cx="32" cy="32" r="32" fill="#eef1f6"/>' +
    '<path d="M16 60c1-10 7-15 16-15s15 5 16 15z" fill="#c9d6ea"/>' +
    '<ellipse cx="32" cy="29" rx="15" ry="17" fill="url(#omAvG)"/>' +
    '<ellipse cx="26.5" cy="30" rx="1.9" ry="2.4" fill="#3b2a24"/>' +
    '<ellipse cx="37.5" cy="30" rx="1.9" ry="2.4" fill="#3b2a24"/>' +
    '<ellipse cx="23" cy="35" rx="3" ry="1.8" fill="#f29a8a" opacity=".45"/>' +
    '<ellipse cx="41" cy="35" rx="3" ry="1.8" fill="#f29a8a" opacity=".45"/>' +
    '<path d="M28.5 37.5q3.5 2.6 7 0" stroke="#3b2a24" stroke-width="1.6" fill="none" stroke-linecap="round"/>' +
    '</svg>';
  const GLOBE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.8 3 2.8 15 0 18M12 3c-2.8 3-2.8 15 0 18"/></svg>';
  const GEAR = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/></svg>';
  const LOCK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 018 0v3"/></svg>';
  const CART = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M3 4h2l2.4 11h10.2L20 7H6.2"/><circle cx="9" cy="19" r="1.4"/><circle cx="17" cy="19" r="1.4"/></svg>';

  const TOOL_LABELS = {
    "system.clock": "Clock", "math.calc": "Calculator", "files.read": "Files", "files.write": "Files",
    "files.list": "Files", "web.fetch": "Web", "memory.note": "Memory", "shell.exec": "Terminal",
  };

  function hostOf(url) { try { return new URL(url).host.replace(/^www\./, ""); } catch (e) { return url || ""; } }

  // Minimal, escape-first markdown: **bold**, `code`, links, bullets, line breaks.
  function renderRich(text) {
    const escd = String(text || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const lines = escd.split("\n");
    let html = "", inList = false;
    const inline = (t) => t
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
    for (let i = 0; i < lines.length; i++) {
      const raw = lines[i];
      // GitHub-style table: header row, |---| separator, body rows
      if (/^\s*\|.*\|\s*$/.test(raw) && /^\s*\|?\s*:?-{3,}/.test((lines[i + 1] || "").replace(/^\s*$/, "x"))) {
        const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
        if (inList) { html += "</ul>"; inList = false; }
        html += '<div class="mc-table"><table><thead><tr>' + cells(raw).map((c) => "<th>" + c + "</th>").join("") + "</tr></thead><tbody>";
        i += 2;
        for (; i < lines.length; i++) {
          if (!lines[i].trim()) { if (/^\s*\|/.test(lines[i + 1] || "")) continue; break; }
          if (!/^\s*\|/.test(lines[i])) { i--; break; }
          html += "<tr>" + cells(lines[i]).map((c) => "<td>" + c + "</td>").join("") + "</tr>";
        }
        html += "</tbody></table></div>";
        continue;
      }
      let line = raw
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
        .replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s.,;:!?)]|$)/g, "$1<em>$2</em>")
        .replace(/`([^`]+)`/g, "<code>$1</code>")
        .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
        .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
      const li = /^\s*(?:[-*•]|\d+\.)\s+(.*)$/.exec(line);
      if (li) { if (!inList) { html += "<ul>"; inList = true; } html += "<li>" + li[1] + "</li>"; continue; }
      if (inList) { html += "</ul>"; inList = false; }
      const h = /^#{1,4}\s+(.*)$/.exec(line);
      html += h ? "<p><strong>" + h[1] + "</strong></p>" : (line.trim() ? "<p>" + line + "</p>" : "");
    }
    if (inList) html += "</ul>";
    return html;
  }

  function fmtTime(ts) {
    return new Date(ts).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }

  /* Persist transcripts (UI state only; never keys or credentials). */
  const chatStoreKey = () => "om.chats.v2." + ((state.user && state.user.user_id) || "anon");
  function saveChats() {
    try {
      const slim = state.chats.slice(-30).map((c) => ({
        sessionId: c.sessionId, chatId: c.chatId, title: c.title, offline: c.offline,
        messages: c.messages.map((m) => ({ ...m, running: false })),
      }));
      localStorage.setItem(chatStoreKey(), JSON.stringify(slim));
    } catch (e) { /* storage full or disabled */ }
  }
  function loadChats() {
    try { return JSON.parse(localStorage.getItem(chatStoreKey()) || "[]"); } catch (e) { return []; }
  }

  /* Live browser frames: one poller per session, shared by cards + viewer. */
  const Live = {
    sessions: {},  // sid -> {info, frameUrl, seq, subs:Set, timer, fast}
    watch(sid, fn) {
      const s = this.sessions[sid] || (this.sessions[sid] = { info: null, frameUrl: "", seq: -1, subs: new Set(), timer: 0, fast: 0 });
      s.subs.add(fn);
      if (s.info) fn(s);
      this._schedule(sid, 0);
      return () => { s.subs.delete(fn); };
    },
    boost(sid, ms) { const s = this.sessions[sid]; if (s) { s.fast = Date.now() + (ms || 60000); this._schedule(sid, 0); } },
    _schedule(sid, delay) {
      const s = this.sessions[sid];
      if (!s || s.timer) return;
      s.timer = setTimeout(() => { s.timer = 0; this._tick(sid); }, delay);
    },
    async _tick(sid) {
      const s = this.sessions[sid];
      if (!s || !s.subs.size) return;
      try {
        const info = await API.browserSession(sid);
        s.info = info;
        if (info.frame_seq !== s.seq) {
          const url = await API.browserFrameURL(sid);
          if (s.frameUrl) setTimeout(((u) => () => URL.revokeObjectURL(u))(s.frameUrl), 2000);
          s.frameUrl = url; s.seq = info.frame_seq;
        }
        s.subs.forEach((fn) => fn(s));
        if (info.state === "closed") return;
      } catch (e) {
        if (e.status === 404) { s.info = { state: "closed" }; s.subs.forEach((fn) => fn(s)); return; }
      }
      const fast = Date.now() < s.fast || document.querySelector(".bv-modal[data-sid='" + sid + "']");
      this._schedule(sid, fast ? 600 : 4000);
    },
  };

  function chatView(root) {
    root.innerHTML = "";
    root.classList.add("mchat");

    // header: avatar, name, one-line live status, and the "Computer" pill
    const header = el("header", { class: "mc-header" });
    const persona = el("div", { class: "mc-persona" });
    persona.innerHTML = '<div class="mc-avatar">' + AVATAR_SVG + "</div>";
    const pname = el("div", { class: "mc-name" });
    pname.innerHTML = '<div class="mc-name-t">OpenMuse</div><div class="mc-status" aria-live="polite"></div>';
    persona.appendChild(pname);
    const pill = el("button", { type: "button", class: "mc-pill", hidden: true, "aria-label": "Open the live computer" });
    persona.appendChild(pill);
    const ctrl = el("button", { type: "button", class: "mc-ctrl", hidden: true });
    persona.appendChild(ctrl);
    function updateCtrl() {
      if (!activeRun) { ctrl.hidden = true; return; }
      const paused = activeRun.amsg.paused;
      ctrl.hidden = false;
      ctrl.textContent = paused ? "▶ Resume" : (activeRun.amsg.pauseRequested ? "Pausing…" : "❚❚ Pause");
      ctrl.disabled = !paused && !!activeRun.amsg.pauseRequested;
      ctrl.onclick = async () => {
        const r = activeRun; if (!r) return;
        try {
          if (r.amsg.paused) { await API.runs.resume(r.runId); r.amsg.paused = false; r.amsg.pauseRequested = false; setStatus("Resuming"); }
          else { await API.runs.pause(r.runId); r.amsg.pauseRequested = true; }
        } catch (e) { toast("Couldn't change: " + e.message); }
        updateCtrl();
      };
    }
    header.appendChild(persona);
    root.appendChild(header);
    const statusEl = $(".mc-status", pname);

    const scroller = el("div", { class: "mc-scroll" });
    const thread = el("div", { class: "mc-thread", role: "log", "aria-label": "Conversation" });
    scroller.appendChild(thread);
    root.appendChild(scroller);
    const latest = el("button", { type: "button", class: "mc-latest", hidden: true });
    latest.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 5v14M6 13l6 6 6-6"/></svg><span>Latest messages</span>';
    latest.addEventListener("click", () => scroller.scrollTo({ top: scroller.scrollHeight, behavior: "smooth" }));
    root.appendChild(latest);
    scroller.addEventListener("scroll", () => {
      latest.hidden = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 200;
    });

    const form = el("form", { class: "mc-composer" });
    const plus = el("button", { type: "button", class: "mc-icon", "aria-label": "New chat", title: "New chat" });
    plus.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>';
    const input = el("textarea", { rows: "1", "aria-label": "Message", placeholder: "Message", autocomplete: "off" });
    const mic = el("button", { type: "button", class: "mc-icon", "aria-label": "Dictate", title: "Dictate" });
    mic.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0013 0M12 17.5V21"/></svg>';
    const send = el("button", { type: "submit", class: "mc-send", "aria-label": "Send" });
    form.appendChild(plus); form.appendChild(input); form.appendChild(mic); form.appendChild(send);
    const qnote = el("div", { class: "queued-note", hidden: true });
    const composerWrap = el("div", { class: "mc-bottom" }, [qnote, form]);
    root.appendChild(composerWrap);

    let activeRun = null;  // {runId, amsg, chat, abort}

    function setStatus(text, icon) {
      statusEl.innerHTML = text
        ? ((icon || '<span class="mc-pulse"></span>') + "<span>" + esc(text).replace(/</g, "&lt;") + "</span>")
        : '<span class="mc-idle">Here when you need me</span>';
      persona.classList.toggle("busy", !!text);
    }

    // "Computer · take control": visible while this chat has a live browser
    function updatePill() {
      const chat = state.chats[state.activeChat];
      let sid = "";
      (chat ? chat.messages : []).forEach((m) => (m.blocks || []).forEach((b) => {
        if (b.type === "browser" && b.sessionId && b.state !== "closed") sid = b.sessionId;
      }));
      pill.hidden = !sid;
      if (sid) {
        pill.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">' +
          '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg><span>Computer · take control</span><i class="mc-live"></i>';
        pill.onclick = () => openViewer(sid);
      }
    }

    function updateSend() {
      updateCtrl();
      const running = !!activeRun;
      send.classList.toggle("stop", running);
      send.setAttribute("aria-label", running ? "Stop" : "Send");
      send.innerHTML = running
        ? '<span class="mc-stopsq"></span>'
        : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true"><path d="M12 19V5M6 11l6-6 6 6"/></svg>';
      send.disabled = !running && !input.value.trim();
    }

    function autosize() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 160) + "px"; }
    input.value = state.drafts[state.activeChat] || "";
    input.addEventListener("input", () => { state.drafts[state.activeChat] = input.value; autosize(); updateSend(); });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
    });
    plus.addEventListener("click", () => newChat());

    // dictation via Web Speech where available
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) mic.hidden = true;
    let rec = null;
    mic.addEventListener("click", () => {
      if (rec) { rec.stop(); return; }
      rec = new SR(); rec.lang = "en-US"; rec.interimResults = true;
      mic.classList.add("on");
      rec.onresult = (e) => { input.value = Array.from(e.results).map((r) => r[0].transcript).join(""); autosize(); updateSend(); };
      rec.onend = () => { rec = null; mic.classList.remove("on"); input.focus(); };
      rec.start();
    });

    /* -- chat list (drawer), backed by the server's thread API -------------- */
    let serverChats = [];
    async function syncChats() {
      try {
        const res = await API.chats();
        serverChats = res.chats || [];
        const active = state.chats[state.activeChat];
        const byId = new Map(state.chats.map((c) => [c.chatId, c]));
        const merged = serverChats.map((sc) => {
          const local = byId.get(sc.chat_id);
          if (local) { local.title = sc.title || local.title; local.turns = sc.turns; local.updatedAt = sc.updated_at; return local; }
          return { sessionId: sc.chat_id, chatId: sc.chat_id, title: sc.title, messages: [],
                   turns: sc.turns, remote: true, updatedAt: sc.updated_at };
        });
        // the server is the source of truth; only offline drafts survive locally
        state.chats.filter((c) => c.offline).forEach((c) => merged.push(c));
        merged.sort((x, y) => (x.updatedAt || 0) - (y.updatedAt || 0));   // oldest first; newest = last
        state.chats = merged;
        const idx = active ? state.chats.findIndex((c) => c.chatId === active.chatId) : -1;
        state.activeChat = idx >= 0 ? idx : state.chats.length - 1;
        renderChatList();
      } catch (e) { /* offline: keep the local cache */ }
    }

    async function loadTranscript(chat) {
      if (chat.messages.length || !chat.turns) return;
      try {
        const res = await API.chatMessages(chat.chatId);
        chat.messages = [];
        res.turns.forEach((t) => {
          chat.messages.push({ role: "user", text: t.user_text, ts: t.created_at * 1000 });
          const blocks = t.browser_session ? [{ type: "browser", sessionId: t.browser_session, status: "Session ended", state: "closed" }] : [];
          chat.messages.push({ role: "assistant", text: t.final_text, blocks, ts: t.created_at * 1000,
                               error: t.state === "FAILED" ? (t.failure_message || "This task failed.") : "" });
        });
        renderThread(); saveChats();
      } catch (e) { toast("Couldn't load this chat: " + e.message); }
    }

    function selectChat(i) {
      state.activeChat = i;
      input.value = state.drafts[i] || "";
      renderAll();
      loadTranscript(state.chats[i]);
      openDrawer(false);
      showTab("chat");
    }

    function renderChatList() {
      const list = $("#chatList"); if (!list) return;
      const q = ($("#chatSearch").value || "").toLowerCase();
      list.innerHTML = "";
      const order = state.chats.map((c, i) => [c, i]).reverse().filter(([c]) => !q || (c.title || "").toLowerCase().includes(q));
      if (!order.length) list.appendChild(el("div", { class: "drawer-empty", text: q ? "No matching chats." : "No chats yet." }));
      order.forEach(([c, i]) => {
        const row = el("div", { class: "drawer-chat" + (i === state.activeChat ? " active" : "") });
        const open = el("button", { type: "button", class: "drawer-chat-open", text: c.title || "New chat" });
        open.addEventListener("click", () => selectChat(i));
        const more = el("button", { type: "button", class: "drawer-chat-more", "aria-label": "Chat options for " + (c.title || "New chat"), text: "⋯" });
        more.addEventListener("click", (e) => {
          e.stopPropagation();
          const existing = row.querySelector(".drawer-chat-menu");
          if (existing) { existing.remove(); return; }
          const m = el("div", { class: "drawer-chat-menu" });
          const rn = el("button", { type: "button", text: "Rename" });
          rn.addEventListener("click", () => {
            m.innerHTML = "";
            const inp = el("input", { type: "text", "aria-label": "New chat title" });
            inp.value = c.title || "";
            const ok = el("button", { type: "button", text: "Save" });
            const save = async () => {
              const title = inp.value.trim(); if (!title) return;
              try { await API.updateChat(c.chatId, { title }); c.title = title; saveChats(); renderChatList(); renderAll(); }
              catch (err) { toast("Rename failed: " + err.message); }
            };
            ok.addEventListener("click", save);
            inp.addEventListener("keydown", (ev) => { if (ev.key === "Enter") save(); });
            m.appendChild(inp); m.appendChild(ok); inp.focus();
          });
          const ar = el("button", { type: "button", text: "Archive" });
          ar.addEventListener("click", async () => {
            try {
              await API.updateChat(c.chatId, { archived: true });
              state.chats.splice(i, 1);
              if (state.activeChat >= state.chats.length) state.activeChat = state.chats.length - 1;
              saveChats(); renderChatList(); renderAll();
              toast("Chat archived.");
            } catch (err) { toast("Archive failed: " + err.message); }
          });
          m.appendChild(rn); m.appendChild(ar);
          row.appendChild(m);
        });
        row.appendChild(open); row.appendChild(more);
        list.appendChild(row);
      });
    }
    state.renderChatList = renderChatList;
    $("#chatSearch").addEventListener("input", renderChatList);
    $("#drawerNew").addEventListener("click", () => { openDrawer(false); showTab("chat"); newChat(); });

    /* -- thread rendering --------------------------------------------------- */
    function renderThread() {
      const nearBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 120;
      thread.innerHTML = "";
      const chat = state.chats[state.activeChat];
      if (!chat || !chat.messages.length) {
        const empty = el("div", { class: "mc-empty" });
        empty.innerHTML = '<div class="mc-avatar big">' + AVATAR_SVG + "</div><h2>What can I do for you?</h2>" +
          "<p>I can browse the web live — you can watch every step and take over anytime.</p>";
        const sugg = el("div", { class: "mc-suggest" });
        ["Find the cheapest nonstop SFO → JFK flight on Dec 14, back on the 20th. Use a browser.",
         "Open Hacker News in the browser and summarize the top 5 stories.",
         "What time is it in Tokyo right now?"].forEach((t) => {
          const b = el("button", { type: "button", text: t });
          b.addEventListener("click", () => { input.value = t; autosize(); updateSend(); form.requestSubmit(); });
          sugg.appendChild(b);
        });
        empty.appendChild(sugg);
        thread.appendChild(empty);
        return;
      }
      let lastTs = 0;
      chat.messages.forEach((m) => {
        if (m.ts && m.ts - lastTs > 5 * 60 * 1000) thread.appendChild(el("div", { class: "mc-time", text: fmtTime(m.ts) }));
        if (m.ts) lastTs = m.ts;
        if (m.role === "user") {
          const sched = /^\[Scheduled task “([^”]*)”[^\]]*\]\s*/.exec(m.text || "");
          if (sched) {  // system-originated turn: show what ran, not the runtime header
            const card = el("div", { class: "mc-sched" });
            card.appendChild(el("span", { class: "mc-sched-tag", text: "⏰ Scheduled · " + sched[1] }));
            card.appendChild(el("span", { text: m.text.slice(sched[0].length) }));
            thread.appendChild(el("div", { class: "mc-row user" }, [card]));
            return;
          }
          const b = el("div", { class: "mc-row user" }, [el("div", { class: "mc-bubble user" })]);
          b.firstChild.textContent = m.text;
          thread.appendChild(b);
          return;
        }
        (m.blocks || []).forEach((blk) => thread.appendChild(renderBlock(blk, m)));
        if (m.text) {
          const bub = el("div", { class: "mc-bubble bot" });
          bub.innerHTML = renderRich(m.text);
          thread.appendChild(el("div", { class: "mc-row bot" }, [bub]));
        }
        if (m.error) {
          const bub = el("div", { class: "mc-bubble bot err", text: m.error });
          if (m.runId && !m.retried && !m.running && !/queued/.test(m.error)) {
            const rb = el("button", { type: "button", class: "mc-retry", text: "Retry" });
            rb.addEventListener("click", () => retryTask(chat, m));
            bub.appendChild(rb);
          }
          thread.appendChild(el("div", { class: "mc-row bot" }, [bub]));
        }
        if (m.runId && !m.running && (m.text || m.error)) {
          const det = el("details", { class: "mc-receipt" });
          det.appendChild(el("summary", { text: "Task details" }));
          det.addEventListener("toggle", async () => {
            if (!det.open || det.dataset.loaded) return;
            det.dataset.loaded = "1";
            try {
              const r = await API.runs.receipt(m.runId);
              const lines = [
                "Outcome: " + r.outcome + " · " + r.duration_s + "s · " + r.model_calls + " model calls",
                "Tools: " + (Object.entries(r.tools || {}).map(([k, v]) => k + " ×" + v).join(", ") || "none"),
              ];
              if ((r.approvals || []).length) lines.push("Approvals: " + r.approvals.map((a) => a.verdict).join(", "));
              if (r.plan && r.plan.steps) lines.push("Plan: " + r.plan.steps.map((s) => (s.status === "done" ? "✓ " : "· ") + s.title).join("  "));
              lines.forEach((l) => det.appendChild(el("div", { class: "mc-receipt-line", text: l })));
            } catch (e) { det.appendChild(el("div", { class: "mc-receipt-line", text: "No receipt available." })); }
          });
          thread.appendChild(el("div", { class: "mc-row bot" }, [det]));
        }
        if (m.running && !m.text && !m.waiting) {
          const dots = el("div", { class: "mc-typing", "aria-label": "OpenMuse is working" });
          dots.innerHTML = "<i></i><i></i><i></i>";
          thread.appendChild(el("div", { class: "mc-row bot" }, [dots]));
        }
      });
      if (nearBottom || activeRun) scroller.scrollTop = scroller.scrollHeight;
      updatePill();
    }

    // Live cards keep their DOM across re-renders (no flicker, buttons stay put).
    const nodeCache = new WeakMap();
    function renderBlock(blk, m) {
      if (blk.type === "plan") return planCard(blk, m);
      if (blk.type === "question") return questionCard(blk, m);
      if (blk.type === "browser" || blk.type === "approval") {
        const sig = blk.type === "browser"
          ? [blk.sessionId, blk.state].join("|")
          : [blk.decided || ""].join("|");
        const hit = nodeCache.get(blk);
        if (hit && hit.sig === sig) {
          if (hit.update) hit.update(blk, m);
          return hit.node;
        }
        const node = blk.type === "browser" ? browserCard(blk, m) : approvalBlock(blk, m);
        nodeCache.set(blk, { sig, node, update: node._update });
        return node;
      }
      // Tools render as one quiet line ("🔍 Searched your memory"); only
      // results that carry content (display data) get a card underneath.
      const wrap = el("div", { class: "mc-stepwrap" });
      const step = el("div", { class: "mc-step" + (blk.ok === false ? " failed" : blk.ok ? " done" : " running") });
      step.innerHTML = '<span class="mc-step-ic">' + stepIcon(blk.tool) + "</span>";
      step.appendChild(el("span", { text: stepText(blk) }));
      wrap.appendChild(step);
      if (blk.display) {
        const card = resultCard(blk.display);
        if (card) wrap.appendChild(card);
      }
      return wrap;
    }

    function planCard(blk, m) {
      const card = el("div", { class: "mc-plan" });
      const done = blk.steps.filter((s) => s.status === "done").length;
      const head = el("div", { class: "mc-plan-head" });
      head.appendChild(el("strong", { text: blk.goal || "Plan" }));
      head.appendChild(el("span", { text: done + "/" + blk.steps.length }));
      card.appendChild(head);
      const bar = el("div", { class: "mc-plan-bar" });
      bar.appendChild(el("i", { style: "width:" + Math.round(100 * done / Math.max(1, blk.steps.length)) + "%" }));
      card.appendChild(bar);
      const ol = el("ol", {});
      blk.steps.forEach((s) => {
        const st = s.status === "active" && !m.running ? "pending" : s.status;
        const li = el("li", { class: "st-" + st });
        li.appendChild(el("span", { class: "mc-plan-ic", "aria-hidden": "true" }));
        li.appendChild(el("span", { text: s.title }));
        if (s.note) li.appendChild(el("em", { text: s.note }));
        ol.appendChild(li);
      });
      card.appendChild(ol);
      return el("div", { class: "mc-row bot" }, [card]);
    }

    function questionCard(blk, m) {
      const card = el("div", { class: "mc-question" + (blk.answered ? " answered" : "") });
      card.appendChild(el("div", { class: "mc-question-q", text: blk.question }));
      if (blk.options && blk.options.length && !blk.answered) {
        const row = el("div", { class: "mc-question-opts" });
        blk.options.forEach((o) => {
          const b = el("button", { type: "button", text: o });
          b.addEventListener("click", () => {
            blk.answered = o;
            input.value = o; autosize(); updateSend();
            form.requestSubmit();
          });
          row.appendChild(b);
        });
        card.appendChild(row);
      } else if (blk.answered) {
        card.appendChild(el("div", { class: "mc-question-a", text: "You answered: " + blk.answered }));
      } else {
        card.appendChild(el("div", { class: "mc-question-a", text: "Reply below to continue." }));
      }
      return el("div", { class: "mc-row bot" }, [card]);
    }

    const STEP_ICONS = {
      search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="6.5"/><path d="M16 16l4 4"/></svg>',
      clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>',
      file: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4"/></svg>',
      brain: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 4a3 3 0 00-3 3v.5A3 3 0 004 10.5 3 3 0 006 16a3 3 0 003 3V4zM15 4a3 3 0 013 3v.5a3 3 0 012 3 3 3 0 01-2 5.5 3 3 0 01-3 3V4z"/></svg>',
      web: GLOBE, gear: GEAR,
    };
    function stepIcon(tool) {
      tool = tool || "";
      if (tool.startsWith("memory.")) return STEP_ICONS.brain;
      if (tool.startsWith("files.")) return STEP_ICONS.file;
      if (tool === "system.clock") return STEP_ICONS.clock;
      if (tool === "web.fetch") return STEP_ICONS.web;
      if (/search|recall|list|find/.test(tool)) return STEP_ICONS.search;
      return STEP_ICONS.gear;
    }
    function stepText(blk) {
      const done = blk.ok === true, failed = blk.ok === false;
      const a = blk.args || {};
      const T = {
        "memory.recall": ["Searching your memory", "Searched your memory"],
        "memory.note": ["Saving to memory", "Saved to memory"],
        "memory.forget": ["Forgetting", "Forgot that"],
        "system.clock": ["Checking the time", "Checked the time"],
        "math.calc": ["Calculating", "Calculated"],
        "files.read": ["Reading " + (a.path || "a file"), "Read " + (a.path || "a file")],
        "files.write": ["Saving " + (a.path || "a file"), "Saved " + (a.path || "a file")],
        "files.list": ["Looking at your files", "Looked at your files"],
        "web.fetch": ["Reading " + hostOf(a.url || ""), "Read " + hostOf(a.url || "")],
        "shell.exec": ["Running a command", "Ran a command"],
      }[blk.tool];
      if (failed) return blk.status === "Blocked by policy" ? "Blocked by policy: " + (blk.title || blk.tool) : "Couldn't finish: " + (blk.title || blk.tool);
      if (T) return done ? T[1] : T[0] + "…";
      return (done ? "Used " : "Using ") + (blk.title || blk.tool) + (done ? "" : "…");
    }

    // Result cards, keyed by display.type. Connectors (#8 Gmail, #9 Calendar,
    // #14 documents) add types here; unknown types fall back to a link card.
    function resultCard(d) {
      const card = el("div", { class: "mc-card" });
      const head = el("div", { class: "mc-card-head" });
      const ic = el("div", { class: "mc-tool-ic" });
      const meta = el("div", { class: "mc-card-meta" });
      head.appendChild(ic); head.appendChild(meta); card.appendChild(head);
      const action = (label, href) => {
        const a = el("a", { class: "mc-open", href: href, target: "_blank", rel: "noopener noreferrer", text: label });
        card.appendChild(a);
      };
      if (d.type === "email") {
        ic.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3.5 6.5l8.5 6 8.5-6"/></svg>';
        meta.appendChild(el("div", { class: "mc-tool-t", text: d.from || "Email" }));
        meta.appendChild(el("div", { class: "mc-tool-s", text: "Email" + (d.count ? " · " + d.count + " message" + (d.count > 1 ? "s" : "") : "") }));
        card.appendChild(el("div", { class: "mc-card-title", text: d.subject || "(no subject)" }));
        if (d.snippet) card.appendChild(el("div", { class: "mc-card-body", text: d.snippet }));
        if (d.url) action("Open email", d.url);
      } else if (d.type === "event") {
        ic.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3.5" y="5" width="17" height="15" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4"/></svg>';
        meta.appendChild(el("div", { class: "mc-tool-t", text: d.title || "Event" }));
        meta.appendChild(el("div", { class: "mc-tool-s", text: [d.when, d.location].filter(Boolean).join(" · ") }));
        if (d.url) action("Open in calendar", d.url);
      } else if (d.type === "document") {
        ic.innerHTML = STEP_ICONS.file;
        meta.appendChild(el("div", { class: "mc-tool-t", text: d.title || "Document" }));
        meta.appendChild(el("div", { class: "mc-tool-s", text: d.subtitle || "Document" }));
        if (d.url) action("Open", d.url);
      } else {
        if (!d.url) return null;
        ic.innerHTML = GLOBE;
        meta.appendChild(el("div", { class: "mc-tool-t", text: d.title || hostOf(d.url) }));
        meta.appendChild(el("div", { class: "mc-tool-s", text: hostOf(d.url) }));
        action("Open page", d.url);
      }
      return card;
    }

    function browserCard(blk, m) {
      const card = el("div", { class: "mc-tool browser" + (blk.state === "closed" ? " closed" : "") });
      const top = el("div", { class: "mc-tool-head" });
      top.innerHTML = '<div class="mc-tool-ic globe">' + GLOBE + "</div>";
      const txt = el("div", { class: "mc-tool-txt" });
      const t = el("div", { class: "mc-tool-t", text: "Browser" });
      t.appendChild(el("span", { class: "mc-beta", text: "Live" }));
      const s = el("div", { class: "mc-tool-s" + (m.running && blk.state !== "closed" ? " shimmer" : ""), text: blk.status || "Working" });
      txt.appendChild(t); txt.appendChild(s);
      top.appendChild(txt);
      card.appendChild(top);
      if (blk.sessionId) {
        const shot = el("div", { class: "mc-shot" });
        const img = el("img", { alt: "Live view of the agent's browser" });
        const cur = el("div", { class: "mc-cursor", hidden: true });
        shot.appendChild(img); shot.appendChild(cur);
        const urlbar = el("div", { class: "mc-shot-url" });
        card.appendChild(shot); card.appendChild(urlbar);
        const open = el("button", { type: "button", class: "mc-open", text: "Open browser" });
        open.addEventListener("click", () => openViewer(blk.sessionId));
        card.appendChild(open);
        shot.addEventListener("click", () => openViewer(blk.sessionId));
        let unwatch = null, seen = false;
        unwatch = Live.watch(blk.sessionId, (live) => {
          if (card.isConnected) seen = true;
          else if (seen) { if (unwatch) unwatch(); return; }  // card was re-rendered away
          if (live.frameUrl && img.src !== live.frameUrl) img.src = live.frameUrl;
          const info = live.info || {};
          urlbar.innerHTML = LOCK + "<span></span>";
          urlbar.querySelector("span").textContent = hostOf(info.url || "");
          placeCursor(cur, info, shot);
          if (info.state === "challenged") s.textContent = "Needs you — human check";
          else if (info.controller === "user") s.textContent = "Paused — you're in control";
        });
      }
      const row = el("div", { class: "mc-row bot" }, [card]);
      row._update = (b, msg) => {
        s.textContent = b.status || "Working";
        s.classList.toggle("shimmer", !!msg.running && b.state !== "closed");
      };
      return row;
    }

    function placeCursor(cur, info, box) {
      const c = info.cursor;
      if (!c || !info.viewport || Date.now() / 1000 - c.at > 6) { cur.hidden = true; return; }
      cur.hidden = false;
      cur.style.left = (100 * c.x / info.viewport.width) + "%";
      cur.style.top = (100 * c.y / info.viewport.height) + "%";
      cur.classList.toggle("user", c.actor === "user");
    }

    function describeApproval(card) {
      const b = card.bindFields || {};
      const a = b.action || {};
      if (card.tool === "browser.start_session") return { title: "OpenMuse wants to open a browser", detail: "A private browser session you can watch live." };
      if (card.tool === "browser.act") {
        if (a.kind === "confirm_commit") return {
          title: "Checkout · OpenMuse wants to place an order" + (b.destination ? " at " + b.destination : ""),
          detail: "Verify the details on the merchant site before approving.", commit: true,
          amount: b.amount_minor ? "$" + (b.amount_minor / 100).toFixed(2) : "" };
        const verbs = { navigate: "open " + hostOf(a.url || ""), click: "click on the page", type: "type “" + (a.text || "") + "”",
                        select: "choose “" + (a.option || "") + "”", scroll: "scroll the page", back: "go back", wait: "wait for the page" };
        return { title: "OpenMuse wants to " + (verbs[a.kind] || a.kind), detail: a.url || "" };
      }
      if (card.tool.startsWith("gmail.")) {
        const verb = { "gmail.send_email": "send an email", "gmail.reply_to_thread": "reply to an email",
                       "gmail.send_draft": "send a draft", "gmail.forward_message": "forward an email" }[card.tool] || "use Gmail";
        const to = b.recipient_email || b.to || "";
        const body = b.body || b.message_body || "";
        return { title: "OpenMuse wants to " + verb + (to ? " to " + to : ""),
                 detail: (b.subject ? "Subject: " + b.subject + "\n" : "") + (body ? body.slice(0, 400) : "") };
      }
      if (card.tool.startsWith("calendar.")) {
        const verb = { "calendar.create_event": "add an event", "calendar.quick_add": "add an event",
                       "calendar.update_event": "change an event", "calendar.delete_event": "delete an event" }[card.tool] || "use your calendar";
        const who = Array.isArray(b.attendees) ? b.attendees.map((x) => typeof x === "string" ? x : x.email).filter(Boolean) : [];
        return { title: "OpenMuse wants to " + verb + (b.summary ? ": " + b.summary : ""),
                 detail: [b.start_datetime && ("When: " + b.start_datetime + (b.timezone ? " (" + b.timezone + ")" : "")),
                          b.text && ("“" + b.text + "”"),
                          who.length && ("Invites: " + who.join(", ")), b.location && ("Where: " + b.location)].filter(Boolean).join("\n") };
      }
      if (card.tool === "browser.close_session") return { title: "OpenMuse wants to close the browser", detail: "" };
      if (card.tool === "browser.checkpoint") return { title: "OpenMuse wants to save a checkpoint", detail: "" };
      if (card.tool === "files.write") return { title: "OpenMuse wants to save a file", detail: (card.bindFields || {}).path || "" };
      return { title: "OpenMuse wants to use " + (TOOL_LABELS[card.tool] || card.tool), detail: card.destination !== "?" ? card.destination : "" };
    }

    function approvalBlock(blk, m) {
      const card = blk.card;
      const d = describeApproval(card);
      const box = el("div", { class: "mc-approval" + (d.commit ? " commit" : "") + (blk.decided ? " decided" : "") });
      const head = el("div", { class: "mc-ap-head" });
      head.innerHTML = '<div class="mc-tool-ic">' + (d.commit ? CART : card.tool.startsWith("browser.") ? GLOBE : GEAR) + "</div>";
      const ht = el("div", { class: "mc-ap-title", text: d.title });
      head.appendChild(ht);
      box.appendChild(head);
      if (d.detail) box.appendChild(el("div", { class: "mc-ap-detail", text: d.detail }));
      if (d.amount) {
        const row = el("div", { class: "mc-ap-total" });
        row.appendChild(el("span", { text: "Estimated total" }));
        row.appendChild(el("strong", { text: d.amount }));
        box.appendChild(row);
      }
      if (blk.decided) {
        box.appendChild(el("div", { class: "mc-ap-result " + blk.decided, text: blk.decided === "approve" ? "Allowed" : "Denied" }));
      } else {
        const actions = el("div", { class: "mc-ap-actions" });
        const deny = el("button", { type: "button", class: "mc-deny", text: "Deny" });
        const allow = el("button", { type: "button", class: "mc-allow", text: "Allow" });
        const decide = async (verdict) => {
          allow.disabled = deny.disabled = true;
          try {
            await API.decideApproval(card, verdict);
            blk.decided = verdict;
            m.waiting = false;
            state.approvals = state.approvals.filter((c) => c.approvalId !== card.approvalId);
            renderNav(); renderThread(); saveChats();
          } catch (e) {
            allow.disabled = deny.disabled = false;
            toast("Could not " + verdict + ": " + (e.message || e));
          }
        };
        deny.addEventListener("click", () => decide("deny"));
        allow.addEventListener("click", () => decide("approve"));
        actions.appendChild(deny); actions.appendChild(allow);
        box.appendChild(actions);
        const more = el("details", { class: "mc-ap-more" });
        more.appendChild(el("summary", { text: "Details" }));
        more.appendChild(el("pre", { class: "mono", text: card.tool + " · " + card.risk + "\n" +
          JSON.stringify(card.bindFields, null, 2) + "\nhash " + card.argumentHash }));
        box.appendChild(more);
      }
      return el("div", { class: "mc-row bot" }, [box]);
    }

    /* -- live browser viewer (full screen) ---------------------------------- */
    function openViewer(sid) {
      const old = document.querySelector(".bv-modal");
      if (old) old.remove();
      const modal = el("div", { class: "bv-modal", "data-sid": sid, role: "dialog", "aria-modal": "true", "aria-label": "Live browser" });
      const panel = el("div", { class: "bv-panel" });
      const bar = el("div", { class: "bv-bar" });
      const close = el("button", { type: "button", class: "bv-x", "aria-label": "Close live browser", text: "✕" });
      const url = el("div", { class: "bv-url" });
      const mode = el("div", { class: "bv-mode" });
      const take = el("button", { type: "button", class: "bv-take", text: "Take control" });
      bar.appendChild(close); bar.appendChild(url); bar.appendChild(mode); bar.appendChild(take);
      const body = el("div", { class: "bv-body" });
      const stage = el("div", { class: "bv-stage" });
      const img = el("img", { alt: "Live browser" });
      const cur = el("div", { class: "mc-cursor big", hidden: true });
      const banner = el("div", { class: "bv-banner", hidden: true });
      const frameBox = el("div", { class: "bv-frame" }, [img, cur]);
      stage.appendChild(frameBox); stage.appendChild(banner);
      const side = el("aside", { class: "bv-side" });
      side.appendChild(el("div", { class: "bv-side-h", text: "Activity" }));
      const log = el("ol", { class: "bv-log" });
      side.appendChild(log);
      const typeRow = el("form", { class: "bv-type", hidden: true });
      const typeIn = el("input", { type: "text", placeholder: "Type into the page…", "aria-label": "Type into the page" });
      const typeBtn = el("button", { type: "submit", class: "btn small", text: "Send" });
      typeRow.appendChild(typeIn); typeRow.appendChild(typeBtn);
      side.appendChild(typeRow);
      body.appendChild(stage); body.appendChild(side);
      panel.appendChild(bar); panel.appendChild(body);
      modal.appendChild(panel);
      document.body.appendChild(modal);
      Live.boost(sid, 10 * 60 * 1000);

      let control = false;
      const setControl = (on) => {
        control = on;
        modal.classList.toggle("control", on);
        take.textContent = on ? "Hand back" : "Take control";
        typeRow.hidden = !on;
        mode.textContent = on ? "You're in control — OpenMuse is paused" : "OpenMuse is browsing";
        if (on) typeIn.focus();
      };
      setControl(false);
      const handoff = (on) => API.browserInput(sid, { kind: on ? "take_control" : "release_control" })
        .catch((err) => toast("Control change failed: " + err.message));
      take.addEventListener("click", () => { setControl(!control); handoff(control); });
      const shut = () => {
        if (control) handoff(false);  // never leave the agent paused behind a closed viewer
        modal.remove(); unwatch(); document.removeEventListener("keydown", onKey);
      };
      close.addEventListener("click", shut);
      modal.addEventListener("click", (e) => { if (e.target === modal) shut(); });
      const onKey = (e) => {
        if (e.key === "Escape") { shut(); return; }
        if (!control || document.activeElement === typeIn) return;
        const keys = ["Enter", "Tab", "Backspace", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Delete"];
        if (keys.includes(e.key)) { e.preventDefault(); API.browserInput(sid, { kind: "key", key: e.key }).catch(() => {}); }
      };
      document.addEventListener("keydown", onKey);
      let lastInfo = {};
      stage.addEventListener("click", (e) => {
        if (!control || !lastInfo.viewport) return;
        const r = img.getBoundingClientRect();
        const x = (e.clientX - r.left) / r.width * lastInfo.viewport.width;
        const y = (e.clientY - r.top) / r.height * lastInfo.viewport.height;
        API.browserInput(sid, { kind: "click", x, y }).catch((err) => toast("Click failed: " + err.message));
      });
      stage.addEventListener("wheel", (e) => {
        if (!control) return;
        e.preventDefault();
        API.browserInput(sid, { kind: "scroll", dy: Math.round(e.deltaY) }).catch(() => {});
      }, { passive: false });
      typeRow.addEventListener("submit", (e) => {
        e.preventDefault();
        const t = typeIn.value; typeIn.value = "";
        API.browserInput(sid, { kind: "type", text: t }).then(() => API.browserInput(sid, { kind: "key", key: "Enter" })).catch(() => {});
      });
      const unwatch = Live.watch(sid, (live) => {
        const info = live.info || {};
        lastInfo = info;
        if (live.frameUrl && img.src !== live.frameUrl) img.src = live.frameUrl;
        url.innerHTML = LOCK + "<span class='h'></span><span class='t'></span>";
        url.querySelector(".h").textContent = hostOf(info.url || "");
        url.querySelector(".t").textContent = info.title || "";
        placeCursor(cur, info, stage);
        banner.hidden = info.state !== "challenged";
        banner.textContent = "Human verification needed. Take control and complete it — OpenMuse never solves these.";
        if (info.controller === "user" && !control) setControl(true);
        if (info.state === "closed") { mode.textContent = "Session ended"; take.disabled = true; }
        log.innerHTML = "";
        (info.log || []).slice().reverse().forEach((entry) => {
          const li = el("li", { class: entry.actor === "user" ? "user" : "" });
          li.appendChild(el("span", { class: "l", text: entry.label }));
          li.appendChild(el("span", { class: "tm", text: fmtTime(entry.at * 1000) }));
          log.appendChild(li);
        });
      });
    }

    /* -- sending + event handling ------------------------------------------- */
    async function newChat() {
      if (activeRun) { toast("Wait for the current task to finish or stop it first."); return; }
      try {
        const s = await API.createSession("");
        state.chats.push({ sessionId: s.session_id, chatId: s.chat_id, title: "", messages: [],
                           updatedAt: Date.now() / 1000 });
      } catch (e) {
        if (e.status === 401) { toast("API key rejected — paste a valid key in the sidebar."); return; }
        state.chats.push({ sessionId: "local-" + state.chats.length, chatId: "local-" + state.chats.length,
                           title: "Offline chat", messages: [], offline: true });
        toast("Backend unreachable — messages will queue.");
      }
      state.activeChat = state.chats.length - 1;
      input.value = "";
      renderAll(); saveChats();
      input.focus();
    }

    function browserBlockFor(amsg, sid) {
      let blk = amsg.blocks.find((b) => b.type === "browser" && (b.sessionId === sid || (!b.sessionId && !sid)));
      if (!blk && sid) blk = amsg.blocks.find((b) => b.type === "browser" && !b.sessionId);
      if (!blk) {
        blk = { type: "browser", sessionId: sid || "", status: "Starting browser", state: "open" };
        amsg.blocks.push(blk);
      }
      if (sid && !blk.sessionId) blk.sessionId = sid;
      return blk;
    }

    function browserVerb(a) {
      a = a || {};
      return ({ navigate: "Opening " + hostOf(a.url || ""), click: "Clicking", type: "Typing “" + (a.text || "").slice(0, 30) + "”",
                select: "Selecting “" + (a.option || "") + "”", scroll: "Scrolling", wait: "Waiting for the page",
                back: "Going back", confirm_commit: "Placing order" })[a.kind] || "Working";
    }

    function handleEvent(run, ev) {
      const amsg = run.amsg, d = ev.data || {};
      switch (ev.name) {
        case "run.status":
          if (d.status === "AWAITING_MODEL" && !amsg.waiting) setStatus(amsg.blocks.length ? "Thinking" : "Preparing");
          if (d.status === "WAITING_FOR_APPROVAL") { amsg.waiting = true; setStatus("Waiting for you"); }
          if (d.status === "EXECUTING_TOOLS") amsg.waiting = false;
          if (d.status === "ASSEMBLING_CONTEXT" && amsg.paused) { amsg.paused = false; updateCtrl(); }
          break;
        case "tool.call": {
          const tool = d.tool || "";
          if (tool === "tools.load_namespace" || tool.startsWith("task.")) break;  // shown as plan/question cards
          if (tool.startsWith("browser.")) {
            const sid = (d.args || {}).session_id || "";
            const blk = browserBlockFor(amsg, sid);
            if (tool === "browser.act") blk.status = browserVerb((d.args || {}).action);
            else if (tool === "browser.observe") blk.status = "Reading the page";
            else if (tool === "browser.close_session") blk.status = "Closing";
            if (d.decision === "DENY") blk.status = "Blocked by policy";
            setStatus(blk.status, '<span class="mc-st-ic">' + GLOBE + "</span>");
            if (blk.sessionId) Live.boost(blk.sessionId);
          } else {
            let blk = amsg.blocks.find((b) => b.callId === d.call_id);
            if (!blk) {
              const args = d.args || {};
              const title = (TOOL_LABELS[tool] || tool) + (tool === "web.fetch" && args.url ? " · " + hostOf(args.url) : "");
              blk = { type: "tool", callId: d.call_id, title, status: "Working", tool, args };
              amsg.blocks.push(blk);
            }
            if (d.decision === "DENY") { blk.status = "Blocked by policy"; blk.ok = false; }
            setStatus("Using " + blk.title);
          }
          break;
        }
        case "tool.result": {
          const blk = amsg.blocks.find((b) => b.callId === d.call_id);
          if (blk) { blk.status = d.ok ? "Done" : "Failed"; blk.ok = d.ok; if (d.display) blk.display = d.display; }
          break;
        }
        case "browser.session": {
          const blk = browserBlockFor(amsg, d.session_id);
          blk.state = d.state;
          if (d.state === "open") { blk.status = "Browser ready"; Live.boost(d.session_id); }
          if (d.state === "closed") blk.status = "Closed";
          break;
        }
        case "browser.action": {
          const blk = browserBlockFor(amsg, d.session_id);
          if (d.label) blk.status = d.label;
          if (d.status === "challenge_paused") blk.status = "Needs you — human check";
          if (d.status === "commit_proposed") blk.status = "Waiting for your approval";
          if (d.status === "refused" && d.code) blk.status = "Couldn't do that (" + d.code.toLowerCase().replace(/_/g, " ") + ")";
          setStatus(blk.status, '<span class="mc-st-ic">' + GLOBE + "</span>");
          Live.boost(d.session_id);
          break;
        }
        case "approval.required":
          amsg.waiting = true;
          setStatus("Waiting for you");
          API.getApproval(d.approval_id).then((card) => {
            if (!state.approvals.some((c) => c.approvalId === card.approvalId)) state.approvals.push(card);
            amsg.blocks.push({ type: "approval", card });
            renderNav(); renderThread(); saveChats();
          }).catch(() => {});
          break;
        case "approval.decided": {
          const blk = amsg.blocks.find((b) => b.type === "approval" && b.card.approvalId === d.approval_id);
          if (blk) blk.decided = d.verdict;
          amsg.waiting = false;
          break;
        }
        case "task.plan":
          amsg.blocks = amsg.blocks.filter((b) => b.type !== "plan");
          amsg.blocks.unshift({ type: "plan", goal: d.goal, steps: (d.steps || []).map((s) => ({ ...s })) });
          break;
        case "task.step": {
          const plan = amsg.blocks.find((b) => b.type === "plan");
          const st = plan && plan.steps.find((s) => s.id === d.id);
          if (st) { st.status = d.status; st.note = d.note || ""; }
          if (st && d.status === "active") setStatus(st.title);
          break;
        }
        case "task.input_required":
          amsg.blocks.push({ type: "question", question: d.question, options: d.options || [] });
          break;
        case "run.paused":
          amsg.paused = true; amsg.pauseRequested = false;
          setStatus("Paused", '<span class="mc-paused-ic">❚❚</span>');
          updateCtrl();
          break;
        case "assistant.delta":
          amsg.text += d.text || d.delta || "";
          break;
        case "run.completed":
          if (d.final_text) amsg.text = d.final_text;
          break;
        case "run.failed":
          amsg.error = "Something went wrong: " + (d.message || d.code || "run failed");
          break;
        case "run.cancelled":
          amsg.error = "Stopped.";
          break;
      }
    }

    function finishRun(run) {
      run.amsg.running = false; run.amsg.waiting = false;
      (run.amsg.blocks || []).forEach((b) => {   // settle live-card labels once the task ends
        if (b.type === "browser") b.status = b.state === "closed" ? "Session ended" : "Done · browser still open";
        if (b.type === "tool" && b.ok === undefined) b.ok = true;
      });
      if (activeRun === run) activeRun = null;
      setStatus("");
      updateSend(); renderThread(); saveChats();
    }

    function startStream(chat, amsg, runId) {
      const run = { runId, amsg, chat };
      activeRun = run; amsg.runId = runId;
      setStatus("Preparing"); updateSend();
      let pending = false;
      API.streamRun(runId, {
        onEvent: (ev) => {
          handleEvent(run, ev);
          if (!pending) { pending = true; requestAnimationFrame(() => { pending = false; if (chat === state.chats[state.activeChat]) renderThread(); }); }
        },
      }).then(() => finishRun(run))
        .catch((err) => { amsg.error = "Connection lost: " + err.message; finishRun(run); });
    }

    async function retryTask(chat, oldMsg) {
      if (activeRun) { toast("Wait for the current task first."); return; }
      try {
        const res = await API.runs.retry(oldMsg.runId);
        oldMsg.retried = true;
        const amsg = { role: "assistant", text: "", blocks: [], running: true, ts: Date.now() };
        chat.messages.push(amsg);
        renderThread();
        startStream(chat, amsg, res.run_id);
      } catch (e) { toast("Retry failed: " + ((e.body && e.body.error && e.body.error.message) || e.message)); }
    }

    // open a chat from elsewhere (notifications, activity)
    state.openChat = async (chatId) => {
      showTab("chat");
      let i = state.chats.findIndex((c) => c.chatId === chatId);
      if (i < 0) { await syncChats(); i = state.chats.findIndex((c) => c.chatId === chatId); }
      if (i >= 0) selectChat(i);
    };

    async function onSend(e) {
      e.preventDefault();
      if (activeRun) {  // stop button
        try { await API.cancelRun(activeRun.runId); } catch (err) { toast("Stop failed: " + err.message); }
        return;
      }
      const text = input.value.trim();
      if (!text) return;
      let chat = state.chats[state.activeChat];
      if (!chat) { await newChat(); chat = state.chats[state.activeChat]; if (!chat) return; }
      input.value = ""; state.drafts[state.activeChat] = ""; autosize();
      if (!chat.title) chat.title = text.length > 42 ? text.slice(0, 40) + "…" : text;
      chat.messages.push({ role: "user", text, ts: Date.now() });
      const amsg = { role: "assistant", text: "", blocks: [], running: true, ts: Date.now() };
      chat.messages.push(amsg);
      renderAll();
      try {
        let res;
        try { res = await API.sendMessage(chat.chatId, text); }
        catch (err) {
          if (err.status !== 404) throw err;
          const s2 = await API.createSession(chat.title || "");  // backend restarted: new session
          chat.sessionId = s2.session_id; chat.chatId = s2.chat_id;
          res = await API.sendMessage(chat.chatId, text);
        }
        startStream(chat, amsg, res.runId);
      } catch (err) {
        amsg.running = false;
        if (err.status) {  // backend answered with an error; don't pretend we're offline
          amsg.error = "Error " + err.status + ": " + ((err.body && err.body.error && err.body.error.message) || "request failed") +
            (err.status === 401 ? " — check the API key in the sidebar" : "");
          renderThread(); saveChats(); return;
        }
        API.queue.push({ chatId: chat.chatId, text, idempotencyKey: "idem_" + API.uuid().replace(/-/g, "") });
        qnote.hidden = false;
        qnote.textContent = "Offline — message queued.";
        amsg.text = "(queued — will send when the backend is reachable)";
        renderThread(); saveChats();
      }
    }
    form.addEventListener("submit", onSend);

    function renderAll() { renderChatList(); renderThread(); updateSend(); autosize(); updatePill(); if (!activeRun) setStatus(""); }
    root._renderAll = renderAll;

    const saved = loadChats();
    if (saved.length && !state.chats.length) { state.chats = saved; state.activeChat = saved.length - 1; }
    renderAll();
    syncChats().then(() => { const c = state.chats[state.activeChat]; if (c) loadTranscript(c); renderAll(); });
  }

  /* -- approvals view ------------------------------------------------------- */
  function approvalsView(root) {
    root.innerHTML = "";
    root.appendChild(el("h1", { text: "Approvals" }));
    const list = el("div", { "aria-live": "polite" });
    root.appendChild(list);

    async function refresh() {
      list.innerHTML = "";
      if (!state.approvals.length) {
        list.appendChild(el("div", { class: "empty", text: "No pending approvals." }));
        return;
      }
      for (const card of state.approvals) list.appendChild(renderCard(card));
    }

    function renderCard(card) {
      const high = API.HIGH_RISK.has(card.risk);
      const box = el("div", { class: "approval" + (high ? " high-risk" : ""), role: "group",
                              "aria-label": "Approval request for " + card.tool });
      const badge = el("span", { class: "risk-badge", text: card.risk + (high ? " · device auth required" : "") });
      box.appendChild(badge);
      const dl = el("dl", {});
      const rows = [
        ["Tool", card.tool + "@" + (card.toolVersion || "?")],
        ["Destination", card.destination],
        ["Effect", card.effect],
        ["Run", card.runId],
      ];
      rows.forEach(([k, v]) => {
        dl.appendChild(el("dt", { text: k }));
        const dd = el("dd", {}); dd.textContent = v; dl.appendChild(dd);
      });
      box.appendChild(dl);
      const hashLabel = el("div", { class: "muted", text: "Bound argument hash" });
      box.appendChild(hashLabel);
      box.appendChild(el("div", { class: "hash", text: card.argumentHash }));
      const fields = el("details", { class: "timeline" });
      fields.appendChild(el("summary", { text: "Bound fields (" + Object.keys(card.bindFields).length + ")" }));
      const pre = el("pre", { class: "mono", text: JSON.stringify(card.bindFields, null, 2) });
      fields.appendChild(pre);
      box.appendChild(fields);

      if (high && !card.deviceAuthed) {
        box.appendChild(el("div", {
          class: "device-auth-note",
          text: "High-risk approval (" + card.risk + "). Approving requires device " +
                "authentication; the bound fields above will be re-displayed after you authenticate.",
        }));
      }
      const actions = el("div", { class: "actions" });
      const ok = el("button", { class: "btn" + (high ? " danger" : ""), type: "button",
                                text: high && !card.deviceAuthed ? "Authenticate & approve" : "Approve" });
      const no = el("button", { class: "btn secondary", type: "button", text: "Reject" });
      ok.addEventListener("click", async () => {
        try {
          // re-display bound fields after device auth happens inside decideApproval
          await API.decideApproval(card, "approve");
          toast("Approved " + card.tool + " (hash " + card.argumentHash.slice(0, 18) + "…)");
          state.approvals = state.approvals.filter((c) => c !== card);
          renderNav(); refresh();
        } catch (e) { toast("Approval failed: " + e.message); }
      });
      no.addEventListener("click", async () => {
        try {
          await API.decideApproval(card, "deny");
          toast("Rejected " + card.tool);
          state.approvals = state.approvals.filter((c) => c !== card);
          renderNav(); refresh();
        } catch (e) { toast("Reject failed: " + e.message); }
      });
      actions.appendChild(ok); actions.appendChild(no);
      box.appendChild(actions);
      return box;
    }

    root._refresh = refresh;
    refresh();
  }

  /* -- feed view -------------------------------------------------------------- */
  function feedView(root) {
    root.innerHTML = "";
    root.appendChild(el("h1", { text: "Feed" }));
    const list = el("div", {});
    root.appendChild(list);
    async function refresh() {
      list.innerHTML = "";
      let items = [];
      try { items = await API.local("GET", "/feed"); } catch (e) { /* degraded */ }
      if (!items.length) { list.appendChild(el("div", { class: "empty", text: "No feed items yet." })); return; }
      items.forEach((it) => {
        const c = el("div", { class: "card" });
        c.appendChild(el("h3", { text: it.title }));
        if (it.body) { const p = el("p", { class: "muted" }); p.textContent = it.body; c.appendChild(p); }
        const meta = el("div", { class: "muted", text: "from " + (it.source_type || "?") + " · " +
          new Date(it.created_at * 1000).toLocaleString() });
        c.appendChild(meta);
        const row = el("div", { class: "form-row" });
        const mk = (label, fn, cls) => {
          const b = el("button", { class: "btn small " + (cls || "secondary"), type: "button", text: label });
          b.addEventListener("click", async () => { await fn(); refresh(); });
          row.appendChild(b);
        };
        if (it.run_id) mk("View run", async () => { showTab("chat"); toast("Run " + it.run_id); });
        mk("Mute source", async () => API.local("POST", "/feed/mute", { source_id: it.source_id }));
        mk("Dismiss", async () => API.local("POST", "/feed/" + it.id + "/dismiss", {}));
        c.appendChild(row);
        list.appendChild(c);
      });
    }
    root._refresh = refresh; refresh();
  }

  /* -- goals view --------------------------------------------------------------- */
  function goalsView(root) {
    root.innerHTML = "";
    root.appendChild(el("h1", { text: "Goals" }));
    const form = el("form", { class: "card" });
    form.innerHTML = '<h3>New goal</h3>';
    const title = el("input", { type: "text", "aria-label": "Goal title", placeholder: "Goal title" });
    const desc = el("textarea", { "aria-label": "Goal description", placeholder: "Description", rows: "2" });
    const add = el("button", { class: "btn", type: "submit", text: "Add goal" });
    form.appendChild(title); form.appendChild(desc);
    form.appendChild(el("div", {}, [add]));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!title.value.trim()) return;
      await API.local("POST", "/goals", { title: title.value.trim(), description: desc.value });
      title.value = ""; desc.value = ""; refresh();
    });
    root.appendChild(form);
    const list = el("div", {});
    root.appendChild(list);
    async function refresh() {
      list.innerHTML = "";
      let goals = [];
      try { goals = await API.local("GET", "/goals"); } catch (e) {}
      if (!goals.length) { list.appendChild(el("div", { class: "empty", text: "No goals yet." })); return; }
      goals.forEach((g) => {
        const c = el("div", { class: "card" });
        const head = el("div", { class: "list-item" });
        const t = el("div", {});
        t.appendChild(el("strong", { text: g.title }));
        t.appendChild(el("div", { class: "muted", text: (g.activity || []).length + " activities · " +
          (g.attachments || []).length + " attachments" }));
        const badge = el("span", { class: "badge" + (g.status === "active" ? " ok" : ""), text: g.status });
        head.appendChild(t); head.appendChild(badge);
        c.appendChild(head);
        if (g.description) { const p = el("p", {}); p.textContent = g.description; c.appendChild(p); }
        const row = el("div", { class: "form-row" });
        const act = el("input", { type: "text", "aria-label": "Log activity for " + g.title, placeholder: "Log activity…" });
        const logBtn = el("button", { class: "btn small secondary", type: "button", text: "Log" });
        logBtn.addEventListener("click", async () => {
          if (!act.value.trim()) return;
          await API.local("POST", "/goals/" + g.id + "/activity", { text: act.value.trim() });
          refresh();
        });
        const done = el("button", { class: "btn small secondary", type: "button",
                                   text: g.status === "completed" ? "Reopen" : "Complete" });
        done.addEventListener("click", async () => {
          await API.local("POST", "/goals/" + g.id + "/status",
                          { status: g.status === "completed" ? "active" : "completed" });
          refresh();
        });
        const del = el("button", { class: "btn small danger", type: "button", text: "Delete" });
        del.addEventListener("click", async () => {
          if (confirm("Delete goal \"" + g.title + "\"?")) {
            await API.local("DELETE", "/goals/" + g.id); refresh();
          }
        });
        row.appendChild(act); row.appendChild(logBtn); row.appendChild(done); row.appendChild(del);
        c.appendChild(row);
        list.appendChild(c);
      });
    }
    root._refresh = refresh; refresh();
  }

  /* -- library view --------------------------------------------------------------- */
  function libraryView(root) {
    root.innerHTML = "";
    root.appendChild(el("h1", { text: "Library" }));
    const form = el("form", { class: "card" });
    form.innerHTML = "<h3>Upload artifact</h3>";
    const file = el("input", { type: "file", "aria-label": "Choose file to upload" });
    const up = el("button", { class: "btn", type: "submit", text: "Upload" });
    form.appendChild(file); form.appendChild(up);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!file.files.length) return;
      const f = file.files[0];
      const meta = await API.uploadArtifact(f.name, f);
      toast("Uploaded " + meta.name + " (" + meta.size + " bytes)");
      refresh(meta.artifact_id);
    });
    root.appendChild(form);
    const list = el("div", {});
    root.appendChild(list);
    async function refresh(highlight) {
      list.innerHTML = "";
      let items = [];
      try { items = await API.local("GET", "/artifacts"); } catch (e) {}
      if (!items.length) { list.appendChild(el("div", { class: "empty", text: "No artifacts yet." })); return; }
      items.forEach((a) => {
        const c = el("div", { class: "card" });
        c.appendChild(el("h3", { text: a.name }));
        c.appendChild(el("div", { class: "muted mono", text: a.artifact_id + " · " + a.size + " bytes · " + (a.content_type || "?") }));
        const row = el("div", { class: "form-row" });
        const dl = el("button", { class: "btn small secondary", type: "button", text: "Download" });
        dl.addEventListener("click", async () => {
          const { meta, bytes } = await API.downloadArtifact(a.artifact_id);
          const blob = new Blob([bytes], { type: meta.content_type || "application/octet-stream" });
          const link = document.createElement("a");
          link.href = URL.createObjectURL(blob);
          link.download = meta.name;
          link.click();
          setTimeout(() => URL.revokeObjectURL(link.href), 5000);
        });
        const prev = el("button", { class: "btn small secondary", type: "button", text: "Preview" });
        prev.addEventListener("click", async () => {
          const { meta, bytes } = await API.downloadArtifact(a.artifact_id);
          let text = "";
          try { text = new TextDecoder().decode(bytes).slice(0, 4000); }
          catch (e) { text = "(binary content — preview unavailable)"; }
          const pre = el("pre", { class: "mono", text });
          c.appendChild(pre);
        });
        row.appendChild(dl); row.appendChild(prev);
        c.appendChild(row);
        list.appendChild(c);
      });
    }
    root._refresh = refresh; refresh();
  }

  /* -- ideas view ------------------------------------------------------------------- */
  function ideasView(root) {
    root.innerHTML = "";
    root.appendChild(el("h1", { text: "Ideas" }));
    const form = el("form", { class: "card" });
    const input = el("input", { type: "text", "aria-label": "Capture an idea", placeholder: "Capture an idea…" });
    const add = el("button", { class: "btn", type: "submit", text: "Capture" });
    const row = el("div", { class: "form-row" });
    row.appendChild(input); row.appendChild(add);
    form.appendChild(row);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!input.value.trim()) return;
      await API.local("POST", "/ideas", { text: input.value.trim() });
      input.value = ""; refresh();
    });
    root.appendChild(form);
    const list = el("div", {});
    root.appendChild(list);
    async function refresh() {
      list.innerHTML = "";
      let ideas = [];
      try { ideas = await API.local("GET", "/ideas"); } catch (e) {}
      if (!ideas.length) { list.appendChild(el("div", { class: "empty", text: "No ideas yet." })); return; }
      ideas.forEach((idea) => {
        const c = el("div", { class: "card" });
        const p = el("p", {}); p.textContent = idea.text; c.appendChild(p);
        c.appendChild(el("span", { class: "badge", text: idea.status }));
        const row2 = el("div", { class: "form-row" });
        const prom = el("button", { class: "btn small secondary", type: "button", text: "Promote to goal" });
        prom.addEventListener("click", async () => {
          await API.local("POST", "/ideas/" + idea.id + "/promote", {});
          toast("Promoted to a goal."); refresh();
        });
        const arch = el("button", { class: "btn small secondary", type: "button", text: "Archive" });
        arch.addEventListener("click", async () => {
          await API.local("POST", "/ideas/" + idea.id + "/archive", {}); refresh();
        });
        row2.appendChild(prom); row2.appendChild(arch);
        c.appendChild(row2);
        list.appendChild(c);
      });
    }
    root._refresh = refresh; refresh();
  }

  /* -- memory view ------------------------------------------------------------------ */
  /* -- accounts: sign in / sign up gate ----------------------------------- */
  // Every chat, run, approval, browser session and memory belongs to the
  // signed-in user; the API enforces it, the UI just carries the token.
  async function authGate() {
    if (API.store.apiKey) {
      try {
        const res = await API.me();
        state.user = res.user; state.userKind = res.kind;
        return;
      } catch (e) {
        if (e.status !== 401 && e.status !== 403) {  // offline: keep going with cached views
          state.user = { user_id: "offline", name: "Offline", email: "" };
          return;
        }
        API.store.clear();
      }
    }
    await new Promise((resolve) => showAuth(resolve));
  }

  function showAuth(done) {
    const box = $("#auth");
    box.hidden = false;
    document.querySelector(".app").setAttribute("aria-hidden", "true");
    let mode = "signin";
    box.innerHTML = "";
    const card = el("div", { class: "auth-card", role: "dialog", "aria-modal": "true", "aria-labelledby": "authTitle" });
    card.innerHTML = '<div class="mc-avatar big">' + AVATAR_SVG + '</div><h1 id="authTitle"></h1><p class="auth-sub"></p>';
    const tabs = el("div", { class: "auth-tabs", role: "tablist" });
    const tIn = el("button", { type: "button", role: "tab", text: "Sign in" });
    const tUp = el("button", { type: "button", role: "tab", text: "Create account" });
    tabs.appendChild(tIn); tabs.appendChild(tUp);
    const form = el("form", { class: "auth-form" });
    const name = el("input", { type: "text", placeholder: "Your name", autocomplete: "name", "aria-label": "Name" });
    const email = el("input", { type: "email", placeholder: "Email", autocomplete: "email", required: "", "aria-label": "Email" });
    const pw = el("input", { type: "password", placeholder: "Password", required: "", minlength: "8", "aria-label": "Password" });
    const keepL = el("label", { class: "auth-keep" });
    const keep = el("input", { type: "checkbox" }); keep.checked = true;
    keepL.appendChild(keep); keepL.appendChild(document.createTextNode(" Keep me signed in"));
    const err = el("div", { class: "auth-err", role: "alert" });
    const submit = el("button", { type: "submit", class: "auth-submit" });
    [name, email, pw, keepL, err, submit].forEach((n) => form.appendChild(n));
    card.appendChild(tabs); card.appendChild(form);
    card.appendChild(el("p", { class: "auth-foot", text: "Your chats and memory are private to your account." }));
    box.appendChild(card);

    function render() {
      $("#authTitle", card).textContent = mode === "signin" ? "Welcome back" : "Create your account";
      $(".auth-sub", card).textContent = mode === "signin"
        ? "Sign in to pick up where you left off." : "OpenMuse remembers what matters — just for you.";
      tIn.setAttribute("aria-selected", mode === "signin"); tUp.setAttribute("aria-selected", mode === "signup");
      name.hidden = mode === "signin";
      pw.setAttribute("autocomplete", mode === "signin" ? "current-password" : "new-password");
      submit.textContent = mode === "signin" ? "Sign in" : "Create account";
      err.textContent = "";
      (mode === "signup" ? name : email).focus();
    }
    tIn.addEventListener("click", () => { mode = "signin"; render(); });
    tUp.addEventListener("click", () => { mode = "signup"; render(); });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      submit.disabled = true; err.textContent = "";
      try {
        const res = mode === "signin"
          ? await API.login(email.value, pw.value)
          : await API.signup(email.value, pw.value, name.value);
        API.store.setToken(res.token, keep.checked);
        state.user = res.user; state.userKind = "user";
        box.hidden = true; box.innerHTML = "";
        document.querySelector(".app").removeAttribute("aria-hidden");
        done();
      } catch (ex) {
        err.textContent = (ex.body && ex.body.error && ex.body.error.message) ||
          (ex.status ? "Something went wrong (" + ex.status + ")." : "Can't reach the server.");
        submit.disabled = false;
      }
    });
    render();
  }

  function renderUserBox() {
    const u = state.user || {};
    $("#userBox").hidden = false;
    $("#userInitial").textContent = (u.name || u.email || "?").trim().charAt(0).toUpperCase();
    $("#userName").textContent = u.name || "You";
    $("#userEmail").textContent = state.userKind === "developer_key" ? "developer key" : (u.email || "");
    $("#signOut").onclick = async () => {
      await API.logout();
      API.store.clear();
      location.reload();
    };
  }

  /* -- memory view: everything OpenMuse knows about the signed-in user ---- */
  function memoryView(root) {
    root.innerHTML = "";
    root.classList.add("memv");
    const head = el("div", { class: "memv-head" });
    head.innerHTML = "<h1>Memory</h1><p class='muted'>What OpenMuse knows about you — private to your account. " +
      "It learns from your chats in the background; you can search, correct, or forget anything.</p>";
    root.appendChild(head);
    const statsRow = el("div", { class: "memv-stats" });
    root.appendChild(statsRow);

    // search + forget
    const search = el("form", { class: "memv-search" });
    const q = el("input", { type: "search", placeholder: "Search your memory… e.g. “where do I fly from?”", "aria-label": "Search memory" });
    search.appendChild(q);
    search.appendChild(el("button", { type: "submit", class: "btn", text: "Search" }));
    const hits = el("div", { class: "memv-hits" });
    root.appendChild(search); root.appendChild(hits);

    const tabs = el("div", { class: "memv-tabs", role: "tablist" });
    const panel = el("div", { class: "memv-panel" });
    root.appendChild(tabs); root.appendChild(panel);
    const SECTIONS = [["curated", "Curated"], ["people", "People"], ["journal", "Journal"],
                      ["documents", "Documents"], ["summaries", "Chat summaries"], ["profile", "Profile"]];
    let current = "curated";
    SECTIONS.forEach(([id, label]) => {
      const b = el("button", { type: "button", role: "tab", "data-sec": id, text: label });
      b.addEventListener("click", () => { current = id; renderTabs(); loadPanel(); });
      tabs.appendChild(b);
    });
    function renderTabs() { $$("button", tabs).forEach((b) => b.setAttribute("aria-selected", b.dataset.sec === current)); }

    const badge = (t, cls) => el("span", { class: "memv-badge " + (cls || ""), text: t });
    const when = (iso) => { try { return new Date(iso).toLocaleDateString([], { month: "short", day: "numeric" }); } catch (e) { return ""; } };

    function forgetFlow(container, query) {
      const box = el("div", { class: "memv-forget" });
      box.textContent = "Checking what would be removed…";
      container.appendChild(box);
      API.memory.forget(query, false).then((plan) => {
        box.innerHTML = "";
        if (plan.status !== "ready") {
          box.textContent = plan.status === "ambiguous"
            ? "More than one memory matches — refine the text and try again." : "Nothing matched to forget.";
          return;
        }
        box.appendChild(el("div", { text: "Will remove " + plan.targets.length + " item(s) and everything derived from them (index entries, MEMORY.md line, people pages)." }));
        const yes = el("button", { type: "button", class: "btn small danger", text: "Forget permanently" });
        const no = el("button", { type: "button", class: "btn small secondary", text: "Cancel" });
        yes.addEventListener("click", async () => {
          yes.disabled = true;
          const res = await API.memory.forget(query, true);
          toast(res.verified ? "Forgotten — and verified it no longer comes back." : "Forget finished: " + res.status);
          refresh();
        });
        no.addEventListener("click", () => box.remove());
        const row = el("div", { class: "form-row" }, [yes, no]);
        box.appendChild(row);
      }).catch((e) => { box.textContent = "Forget failed: " + e.message; });
    }

    search.addEventListener("submit", async (e) => {
      e.preventDefault();
      hits.innerHTML = "";
      if (!q.value.trim()) return;
      hits.appendChild(el("div", { class: "muted", text: "Searching…" }));
      try {
        const res = await API.memory.recall(q.value, 8);
        hits.innerHTML = "";
        if (!res.results.length) hits.appendChild(el("div", { class: "memv-empty", text: "Nothing found." }));
        res.results.forEach((h) => {
          const c = el("div", { class: "memv-item" });
          const top = el("div", { class: "memv-item-top" }, [badge(h.source), badge("match " + Math.round(h.score * 100) + "%", "soft")]);
          c.appendChild(top);
          c.appendChild(el("div", { class: "memv-text", text: h.text }));
          if (h.source !== "documents" && h.source !== "summaries") {
            const fg = el("button", { type: "button", class: "memv-link", text: "Forget" });
            fg.addEventListener("click", () => forgetFlow(c, h.text.replace(/^[^:]+:\s*/, "").slice(0, 120)));
            c.appendChild(fg);
          }
          hits.appendChild(c);
        });
      } catch (err) { hits.innerHTML = ""; hits.appendChild(el("div", { class: "memv-empty", text: "Search failed: " + err.message })); }
    });

    async function loadPanel() {
      panel.innerHTML = "<div class='muted'>Loading…</div>";
      try {
        if (current === "curated") return renderCurated(await API.memory.records());
        if (current === "people") return renderPeople(await API.memory.people());
        if (current === "journal") return renderJournal(await API.memory.journal());
        if (current === "documents") return renderDocuments(await API.memory.documents());
        if (current === "summaries") return renderSummaries(await API.memory.summaries());
        if (current === "profile") return renderProfile(await API.memory.profile());
      } catch (e) { panel.innerHTML = ""; panel.appendChild(el("div", { class: "memv-empty", text: "Couldn't load: " + e.message })); }
    }

    const KIND_LABEL = { stable_fact: "Fact", preference: "Preference", commitment: "Commitment",
                         relationship_update: "Relationship", operating_lesson: "Lesson" };
    function renderCurated(data) {
      panel.innerHTML = "";
      const recs = data.records || [];
      const showHist = el("label", { class: "memv-toggle" });
      const cb = el("input", { type: "checkbox" });
      showHist.appendChild(cb); showHist.appendChild(document.createTextNode(" Show replaced (history)"));
      panel.appendChild(showHist);
      const list = el("div", {});
      panel.appendChild(list);
      const draw = () => {
        list.innerHTML = "";
        const shown = recs.filter((r) => cb.checked || r.status === "active");
        if (!shown.length) list.appendChild(el("div", { class: "memv-empty", text: "No curated memories yet. Tell OpenMuse about yourself in a chat — it picks up facts, preferences and commitments automatically." }));
        shown.forEach((r) => {
          const c = el("div", { class: "memv-item" + (r.status !== "active" ? " old" : "") });
          const top = el("div", { class: "memv-item-top" }, [badge(KIND_LABEL[r.kind] || r.kind),
            badge(Math.round(r.confidence * 100) + "% sure", "soft"), badge(when(r.created_at), "soft")]);
          if (r.status !== "active") top.appendChild(badge("replaced", "warn"));
          if (r.supersedes) top.appendChild(badge("updated", "ok"));
          c.appendChild(top);
          c.appendChild(el("div", { class: "memv-text", text: r.claim }));
          const meta = el("div", { class: "memv-meta", text: (r.predicate ? r.predicate + " · " : "") +
            "from " + (r.source_refs || []).length + " source(s)" + (r.history && r.history.length ? " · refined " + r.history.length + "×" : "") });
          c.appendChild(meta);
          if (r.status === "active") {
            const fg = el("button", { type: "button", class: "memv-link", text: "Forget" });
            fg.addEventListener("click", () => forgetFlow(c, r.claim));
            c.appendChild(fg);
          }
          list.appendChild(c);
        });
      };
      cb.addEventListener("change", draw);
      draw();
    }

    function renderPeople(data) {
      panel.innerHTML = "";
      const people = data.people || [];
      if (!people.length) panel.appendChild(el("div", { class: "memv-empty", text: "No people yet. Mention someone (“my sister Priya…”) and a page is created for them." }));
      people.forEach((p) => {
        const c = el("div", { class: "memv-item person" });
        const initial = el("div", { class: "memv-pi", text: (p.name || "?").charAt(0).toUpperCase() });
        const body = el("div", { class: "memv-pbody" });
        body.appendChild(el("div", { class: "memv-pname", text: p.name }));
        body.appendChild(el("div", { class: "memv-meta", text: (p.relationship || "relationship unknown") + " · " + p.fact_count + " fact(s)" }));
        const facts = el("ul", { class: "memv-facts", hidden: true });
        body.appendChild(facts);
        c.appendChild(initial); c.appendChild(body);
        c.addEventListener("click", async () => {
          if (!facts.hidden) { facts.hidden = true; return; }
          const res = await API.memory.person(p.person_id);
          facts.innerHTML = "";
          (res.person.facts || []).forEach((f) => facts.appendChild(el("li", { text: f.fact })));
          (res.person.interactions || []).forEach((i) => facts.appendChild(el("li", { text: i.date + ": " + i.summary })));
          facts.hidden = false;
        });
        panel.appendChild(c);
      });
    }

    function renderJournal(data) {
      panel.innerHTML = "";
      const entries = data.entries || [];
      if (!entries.length) panel.appendChild(el("div", { class: "memv-empty", text: "The journal records what happened in your chats — nothing yet." }));
      let lastDay = "";
      entries.forEach((e) => {
        const day = (e.timestamp || "").slice(0, 10);
        if (day !== lastDay) { panel.appendChild(el("div", { class: "memv-day", text: new Date(day + "T12:00:00").toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" }) })); lastDay = day; }
        const c = el("div", { class: "memv-item" });
        c.appendChild(el("div", { class: "memv-item-top" }, [badge(e.title || "Entry"), badge(fmtTime(e.timestamp), "soft")]));
        c.appendChild(el("div", { class: "memv-text", text: e.text }));
        panel.appendChild(c);
      });
    }

    function renderDocuments(data) {
      panel.innerHTML = "";
      const up = el("div", { class: "memv-upload" });
      up.innerHTML = "<strong>Add to your knowledge bank</strong><p class='muted'>Upload notes, PDFs or text files, or paste text. OpenMuse searches them when they're relevant.</p>";
      const file = el("input", { type: "file", accept: ".txt,.md,.markdown,.csv,.json,.pdf,.html,.log", "aria-label": "Upload a document" });
      const title = el("input", { type: "text", placeholder: "Title (optional)", "aria-label": "Document title" });
      const text = el("textarea", { rows: "4", placeholder: "…or paste text here", "aria-label": "Document text" });
      const add = el("button", { type: "button", class: "btn", text: "Add document" });
      [file, title, text, add].forEach((n) => up.appendChild(n));
      panel.appendChild(up);
      add.addEventListener("click", async () => {
        add.disabled = true; add.textContent = "Indexing…";
        try {
          const f = file.files && file.files[0];
          let payload;
          if (f) {
            const buf = new Uint8Array(await f.arrayBuffer());
            let bin = ""; for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
            payload = { title: title.value || f.name, filename: f.name, content_base64: btoa(bin) };
          } else if (text.value.trim()) {
            payload = { title: title.value || text.value.trim().split("\n")[0].slice(0, 60), text: text.value };
          } else { toast("Choose a file or paste some text."); return; }
          const res = await API.memory.addDocument(payload);
          toast("Added “" + res.document.title + "” (" + res.document.chunks + " chunk(s)).");
          refresh();
        } catch (e) {
          toast("Couldn't add: " + ((e.body && e.body.error && e.body.error.message) || e.message));
        } finally { add.disabled = false; add.textContent = "Add document"; }
      });
      const docs = data.documents || [];
      if (!docs.length) panel.appendChild(el("div", { class: "memv-empty", text: "No documents yet." }));
      docs.forEach((d) => {
        const c = el("div", { class: "memv-item" });
        c.appendChild(el("div", { class: "memv-item-top" }, [badge("document"), badge(when(d.created_at), "soft"),
          badge(Math.round(d.chars / 100) / 10 + "k chars · " + d.chunks + " chunk(s)", "soft")]));
        c.appendChild(el("div", { class: "memv-text", text: d.title }));
        const del = el("button", { type: "button", class: "memv-link", text: "Delete" });
        del.addEventListener("click", async () => { await API.memory.deleteDocument(d.document_id); toast("Deleted."); refresh(); });
        c.appendChild(del);
        panel.appendChild(c);
      });
    }

    function renderSummaries(data) {
      panel.innerHTML = "";
      const list = data.summaries || [];
      if (!list.length) panel.appendChild(el("div", { class: "memv-empty", text: "Long chats get a rolling summary so older turns stay in context. None yet." }));
      list.forEach((s) => {
        const c = el("div", { class: "memv-item" });
        c.appendChild(el("div", { class: "memv-item-top" }, [badge("chat summary"), badge("turns 1–" + s.to_sequence, "soft"), badge(when(s.created_at), "soft")]));
        c.appendChild(el("div", { class: "memv-text", text: s.text || "" }));
        [["decisions", "Decisions"], ["commitments", "Commitments"], ["open_threads", "Open threads"]].forEach(([k, label]) => {
          if ((s[k] || []).length) c.appendChild(el("div", { class: "memv-meta", text: label + ": " + s[k].join(" · ") }));
        });
        panel.appendChild(c);
      });
    }

    function renderProfile(data) {
      panel.innerHTML = "";
      const files = data.files || {};
      const help = { "USER.md": "Stable facts about you that OpenMuse should always apply (name, timezone, communication style).",
                     "IDENTITY.md": "Your agent's name and presentation.",
                     "SOUL.md": "Tone and posture — never permissions.",
                     "AGENTS.md": "Operating lessons, e.g. a site quirk. Cannot override platform policy." };
      ["USER.md", "IDENTITY.md", "SOUL.md", "AGENTS.md"].forEach((fname) => {
        const c = el("div", { class: "memv-item" });
        c.appendChild(el("div", { class: "memv-item-top" }, [badge(fname)]));
        c.appendChild(el("div", { class: "memv-meta", text: help[fname] }));
        const ta = el("textarea", { rows: "6", class: "memv-md", "aria-label": fname });
        ta.value = files[fname] || "";
        const save = el("button", { type: "button", class: "btn small", text: "Save" });
        save.addEventListener("click", async () => {
          try { await API.memory.saveProfile(fname, ta.value); toast("Saved " + fname + "."); }
          catch (e) { toast("Couldn't save: " + ((e.body && e.body.error && e.body.error.message) || e.message)); }
        });
        c.appendChild(ta); c.appendChild(save);
        panel.appendChild(c);
      });
    }

    async function refresh() {
      try {
        const ov = await API.memory.overview();
        const s = ov.stats;
        statsRow.innerHTML = "";
        [["Curated", s.curated_active], ["People", s.people], ["Journal", s.journal_entries],
         ["Documents", s.documents], ["Summaries", s.summaries]].forEach(([k, v]) => {
          const chip = el("div", { class: "memv-stat" });
          chip.appendChild(el("strong", { text: String(v) }));
          chip.appendChild(el("span", { text: k }));
          statsRow.appendChild(chip);
        });
        statsRow.appendChild(el("div", { class: "memv-embed", text: "search: " + (s.embedder || "").replace(/^embed-/, "") }));
      } catch (e) {
        statsRow.innerHTML = "";
        statsRow.appendChild(el("div", { class: "memv-empty", text: "Memory unavailable: " + e.message }));
      }
      renderTabs(); loadPanel();
    }
    root._refresh = refresh; refresh();
  }


  /* -- schedules view ----------------------------------------------------------------- */
  /* -- schedules view (issue #4): the signed-in user's own schedules -------- */
  function schedulesView(root) {
    root.innerHTML = "";
    root.classList.add("memv");
    root.appendChild(el("div", { class: "memv-head", html: "<h1>Schedules</h1><p class='muted'>Recurring and one-time tasks OpenMuse runs for you. Results land in your “Scheduled” chat and your notifications.</p>" }));
    const form = el("form", { class: "memv-upload sched-form" });
    form.innerHTML = "<strong>New schedule</strong>";
    const name = el("input", { type: "text", placeholder: "Name, e.g. Morning briefing", "aria-label": "Schedule name" });
    const instr = el("textarea", { rows: "3", placeholder: "What should OpenMuse do? e.g. “Summarize my day and anything due this week”", "aria-label": "Instructions" });
    const row = el("div", { class: "sched-when" });
    const preset = el("select", { "aria-label": "Repeat" });
    [["daily", "Every day"], ["weekdays", "Every weekday"], ["weekly", "Every week on…"], ["hourly", "Every hour"], ["once", "Once"], ["cron", "Custom (cron)"]]
      .forEach(([v, t]) => { const o = el("option", { value: v, text: t }); preset.appendChild(o); });
    const day = el("select", { "aria-label": "Day of week", hidden: true });
    ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"].forEach((d, i) => day.appendChild(el("option", { value: String(i), text: d })));
    day.value = "1";
    const time = el("input", { type: "time", value: "08:00", "aria-label": "Time" });
    const when = el("input", { type: "datetime-local", "aria-label": "Run at", hidden: true });
    const cron = el("input", { type: "text", placeholder: "m h dom mon dow", "aria-label": "Cron expression", hidden: true });
    [preset, day, time, when, cron].forEach((n) => row.appendChild(n));
    const tzNote = el("div", { class: "memv-meta" });
    const add = el("button", { type: "submit", class: "btn", text: "Create schedule" });
    [name, instr, row, tzNote, add].forEach((n) => form.appendChild(n));
    root.appendChild(form);
    const list = el("div", {});
    root.appendChild(list);
    const sync = () => {
      const v = preset.value;
      day.hidden = v !== "weekly"; time.hidden = !["daily", "weekdays", "weekly"].includes(v);
      when.hidden = v !== "once"; cron.hidden = v !== "cron";
    };
    preset.addEventListener("change", sync); sync();

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!instr.value.trim()) { toast("Tell OpenMuse what to do."); return; }
      const [h, m] = (time.value || "08:00").split(":").map((x) => parseInt(x, 10));
      const body = { name: name.value.trim(), instructions: instr.value.trim(), kind: "cron" };
      if (preset.value === "daily") body.schedule = `${m} ${h} * * *`;
      else if (preset.value === "weekdays") body.schedule = `${m} ${h} * * 1-5`;
      else if (preset.value === "weekly") body.schedule = `${m} ${h} * * ${day.value}`;
      else if (preset.value === "hourly") body.schedule = "0 * * * *";
      else if (preset.value === "cron") body.schedule = cron.value.trim();
      else { body.kind = "once"; body.run_at = when.value ? new Date(when.value).toISOString() : ""; }
      add.disabled = true;
      try { await API.schedules.create(body); toast("Schedule created."); form.reset(); time.value = "08:00"; sync(); refresh(); }
      catch (err) { toast("Couldn't create: " + ((err.body && err.body.error && err.body.error.message) || err.message)); }
      finally { add.disabled = false; }
    });

    const fmtWhen = (iso) => { try { return new Date(iso).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }); } catch (e) { return iso; } };
    async function refresh() {
      list.innerHTML = "";
      let res;
      try { res = await API.schedules.list(); }
      catch (e) { list.appendChild(el("div", { class: "memv-empty", text: "Schedules unavailable: " + e.message })); return; }
      tzNote.textContent = "Times are in " + res.timezone + " (from your profile).";
      if (!res.schedules.length) list.appendChild(el("div", { class: "memv-empty", text: "No schedules yet. Create one above, or ask in chat: “every weekday at 8am, summarize my calendar”." }));
      res.schedules.forEach((s) => {
        const c = el("div", { class: "memv-item" + (s.enabled ? "" : " old") });
        c.appendChild(el("div", { class: "memv-item-top" }, [
          el("span", { class: "memv-badge", text: s.cadence }),
          el("span", { class: "memv-badge soft", text: s.enabled ? "on" : "paused" }),
          el("span", { class: "memv-badge soft", text: "v" + s.version })]));
        c.appendChild(el("div", { class: "memv-pname", text: s.name }));
        c.appendChild(el("div", { class: "memv-text", text: s.instruction }));
        if (s.next_runs && s.next_runs.length) c.appendChild(el("div", { class: "memv-meta", text: "Next: " + s.next_runs.slice(0, 3).map(fmtWhen).join(" · ") }));
        const acts = el("div", { class: "sched-acts" });
        const mk = (label, fn, cls) => { const b = el("button", { type: "button", class: "btn small " + (cls || "secondary"), text: label }); b.addEventListener("click", fn); acts.appendChild(b); };
        mk("Run now", async () => { const r = await API.schedules.runNow(s.schedule_id); toast("Started — opening the Scheduled chat."); if (state.openChat) state.openChat(r.chat_id); });
        mk(s.enabled ? "Pause" : "Resume", async () => { await API.schedules.update(s.schedule_id, { enabled: !s.enabled }); refresh(); });
        mk("Delete", async () => { await API.schedules.remove(s.schedule_id); toast("Deleted."); refresh(); }, "danger");
        c.appendChild(acts);
        list.appendChild(c);
      });
    }
    root._refresh = refresh; refresh();
  }

  /* -- activity view (issue #6): every task across chats -------------------- */
  function activityView(root) {
    root.innerHTML = "";
    root.classList.add("memv");
    root.appendChild(el("div", { class: "memv-head", html: "<h1>Activity</h1><p class='muted'>Everything OpenMuse has worked on, across all chats.</p>" }));
    const tabs = el("div", { class: "memv-tabs", role: "tablist" });
    const list = el("div", {});
    root.appendChild(tabs); root.appendChild(list);
    let filter = "all", data = [];
    const LABELS = { all: "All", running: "Running", waiting: "Waiting on you", done: "Done", failed: "Failed" };
    Object.entries(LABELS).forEach(([k, v]) => {
      const b = el("button", { type: "button", role: "tab", "data-sec": k, text: v });
      b.addEventListener("click", () => { filter = k; draw(); });
      tabs.appendChild(b);
    });
    const CHIP = { running: ["Running", ""], waiting: ["Waiting on you", "warn"], done: ["Done", "ok"], failed: ["Failed", "warn"] };
    function draw() {
      $$("button", tabs).forEach((b) => b.setAttribute("aria-selected", b.dataset.sec === filter));
      list.innerHTML = "";
      const rows = data.filter((r) => filter === "all" || r.bucket === filter);
      if (!rows.length) list.appendChild(el("div", { class: "memv-empty", text: "Nothing here." }));
      rows.forEach((r) => {
        const c = el("button", { type: "button", class: "memv-item act-row" });
        const [label, cls] = CHIP[r.bucket];
        const top = el("div", { class: "memv-item-top" }, [el("span", { class: "memv-badge " + cls, text: r.state === "PAUSED" ? "Paused" : label }),
          el("span", { class: "memv-badge soft", text: r.chat_title || "Chat" }),
          el("span", { class: "memv-badge soft", text: new Date(r.created_at * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) })]);
        if (r.steps_total) top.appendChild(el("span", { class: "memv-badge soft", text: r.steps_done + "/" + r.steps_total + " steps" }));
        c.appendChild(top);
        c.appendChild(el("div", { class: "memv-text", text: r.text.replace(/^\[Scheduled task[^\]]*\]\s*/, "") }));
        if (r.failure) c.appendChild(el("div", { class: "memv-meta", text: r.failure }));
        c.addEventListener("click", () => state.openChat && state.openChat(r.chat_id));
        list.appendChild(c);
      });
    }
    async function refresh() {
      try { data = (await API.activity()).runs; } catch (e) { data = []; }
      draw();
    }
    root._refresh = refresh; refresh();
  }

  /* -- notifications (issue #5): bell, panel, live stream -------------------- */
  const Notes = {
    items: [],
    async load() {
      try { const r = await API.notifications.list(); this.items = r.notifications || []; } catch (e) { /* offline */ }
      this.renderDot();
    },
    unread() { return this.items.filter((n) => !n.read_at).length; },
    renderDot() { const d = $("#bellDot"); if (d) d.hidden = this.unread() === 0; },
    start() {
      this.load();
      API.notifications.stream((note) => {
        this.items.unshift(note);
        this.renderDot();
        toast(note.title);
        try {
          if (document.hidden && window.Notification && Notification.permission === "granted") {
            const n = new Notification(note.title, { body: note.body || "", tag: note.id });
            n.onclick = () => { window.focus(); this.open(note); };
          }
        } catch (e) { /* not supported */ }
        const panel = $(".notif-panel"); if (panel) this.renderPanel(panel);
      });
      $("#bellBtn").addEventListener("click", (e) => { e.stopPropagation(); this.toggle(); });
      document.addEventListener("click", (e) => { const p = $(".notif-panel"); if (p && !p.contains(e.target)) p.remove(); });
    },
    toggle() {
      const existing = $(".notif-panel");
      if (existing) { existing.remove(); return; }
      const panel = el("div", { class: "notif-panel", role: "dialog", "aria-label": "Notifications" });
      document.body.appendChild(panel);
      this.renderPanel(panel);
    },
    async open(note) {
      $(".notif-panel") && $(".notif-panel").remove();
      if (!note.read_at) { note.read_at = Date.now() / 1000; this.renderDot(); API.notifications.read(note.id).catch(() => {}); }
      if (note.link && note.link.chat_id && state.openChat) state.openChat(note.link.chat_id);
    },
    renderPanel(panel) {
      panel.innerHTML = "";
      const head = el("div", { class: "notif-head" });
      head.appendChild(el("strong", { text: "Notifications" }));
      const all = el("button", { type: "button", class: "memv-link notif-readall", text: "Mark all read" });
      all.addEventListener("click", async (e) => { e.stopPropagation(); await API.notifications.readAll(); this.items.forEach((n) => { n.read_at = n.read_at || Date.now() / 1000; }); this.renderDot(); this.renderPanel(panel); });
      head.appendChild(all);
      panel.appendChild(head);
      if (window.Notification && Notification.permission === "default") {
        const en = el("button", { type: "button", class: "notif-enable", text: "Enable desktop alerts when this tab is in the background" });
        en.addEventListener("click", async (e) => { e.stopPropagation(); await Notification.requestPermission(); this.renderPanel(panel); });
        panel.appendChild(en);
      }
      if (!this.items.length) { panel.appendChild(el("div", { class: "drawer-empty", text: "You're all caught up." })); return; }
      let group = "";
      this.items.slice(0, 40).forEach((n) => {
        const d = new Date(n.created_at * 1000);
        const g = d.toDateString() === new Date().toDateString() ? "Today" : "Earlier";
        if (g !== group) { panel.appendChild(el("div", { class: "notif-group", text: g })); group = g; }
        const row = el("button", { type: "button", class: "notif-row" + (n.read_at ? "" : " unread") });
        row.appendChild(el("div", { class: "notif-title", text: n.title }));
        if (n.body) row.appendChild(el("div", { class: "notif-body", text: n.body }));
        row.appendChild(el("div", { class: "notif-time", text: fmtTime(d) }));
        row.addEventListener("click", (e) => { e.stopPropagation(); this.open(n); });
        panel.appendChild(row);
      });
    },
  };



  /* -- connectors view ------------------------------------------------------------------ */
  /* -- apps (issue #7): per-user app connections via Composio ---------------- */
  const APP_ICONS = {
    gmail: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3.5 6.5l8.5 6 8.5-6"/></svg>',
    googlecalendar: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3.5" y="5" width="17" height="15" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4"/></svg>',
  };
  function connectorsView(root) {
    root.innerHTML = "";
    root.classList.add("memv");
    root.appendChild(el("div", { class: "memv-head", html: "<h1>Apps</h1><p class='muted'>Connect your accounts so OpenMuse can help with them. Reading is automatic; sending email or changing your calendar always asks you first. Only you can use your connections.</p>" }));
    const grid = el("div", { class: "apps-grid" });
    root.appendChild(grid);
    let polling = null;

    async function refresh() {
      let res;
      try { res = await API.req("GET", "/v1/apps"); }
      catch (e) { grid.innerHTML = ""; grid.appendChild(el("div", { class: "memv-empty", text: "Apps unavailable: " + e.message })); return; }
      grid.innerHTML = "";
      if (!res.configured) { grid.appendChild(el("div", { class: "memv-empty", text: "Connectors aren't configured on this server (set COMPOSIO_API_KEY)." })); return; }
      res.apps.forEach((a) => {
        const card = el("div", { class: "app-card" + (a.available ? "" : " soon") });
        const head = el("div", { class: "app-head" });
        const ic = el("div", { class: "mc-tool-ic app-ic" });
        ic.innerHTML = APP_ICONS[a.toolkit] || GEAR;
        head.appendChild(ic);
        const meta = el("div", {});
        meta.appendChild(el("div", { class: "memv-pname", text: a.name }));
        meta.appendChild(el("div", { class: "memv-meta", text: a.description }));
        head.appendChild(meta);
        card.appendChild(head);
        if (a.can && a.can.length) {
          const ul = el("ul", { class: "app-can" });
          a.can.forEach((c) => ul.appendChild(el("li", { text: c })));
          card.appendChild(ul);
        }
        const act = el("div", { class: "app-act" });
        if (!a.available) act.appendChild(el("span", { class: "memv-badge soft", text: "Coming soon" }));
        else if (a.connected) {
          act.appendChild(el("span", { class: "memv-badge ok", text: "Connected ✓" }));
          const d = el("button", { type: "button", class: "btn small secondary", text: "Disconnect" });
          d.addEventListener("click", async () => {
            d.disabled = true;
            try { await API.req("DELETE", "/v1/apps/" + a.toolkit); toast(a.name + " disconnected."); refresh(); }
            catch (e) { toast("Couldn't disconnect: " + e.message); d.disabled = false; }
          });
          act.appendChild(d);
        } else {
          const c = el("button", { type: "button", class: "btn small", text: "Connect" });
          c.addEventListener("click", async () => {
            c.disabled = true; c.textContent = "Opening…";
            const win = window.open("about:blank", "_blank");
            try {
              const r = await API.req("POST", "/v1/apps/" + a.toolkit + "/connect",
                                      { body: { return_to: location.origin + "/?apps=connected" } });
              if (win) win.location = r.redirect_url; else location.href = r.redirect_url;
              c.textContent = "Waiting for Google…";
              const t0 = Date.now();
              clearInterval(polling);
              polling = setInterval(async () => {
                const s = await API.req("GET", "/v1/apps").catch(() => null);
                const now = s && s.apps.find((x) => x.toolkit === a.toolkit);
                if (now && now.connected) { clearInterval(polling); toast(a.name + " connected."); refresh(); }
                else if (Date.now() - t0 > 5 * 60 * 1000) { clearInterval(polling); refresh(); }
              }, 2500);
            } catch (e) {
              if (win) win.close();
              toast("Couldn't start connecting: " + ((e.body && e.body.error && e.body.error.message) || e.message));
              c.disabled = false; c.textContent = "Connect";
            }
          });
          act.appendChild(c);
        }
        card.appendChild(act);
        grid.appendChild(card);
      });
    }
    root._refresh = refresh; refresh();
  }



  /* -- usage view ------------------------------------------------------------------------- */
  function usageView(root) {
    root.innerHTML = "";
    root.appendChild(el("h1", { text: "Usage & Privacy" }));
    const card = el("div", { class: "card" });
    root.appendChild(card);
    const priv = el("div", { class: "card" });
    priv.innerHTML = "<h3>Privacy</h3>";
    const exp = el("button", { class: "btn secondary", type: "button", text: "Export my data" });
    exp.addEventListener("click", async () => {
      const r = await API.local("POST", "/usage/export", {});
      toast("Export ready: snapshot " + (r.snapshot_id || "?"));
    });
    const theme = el("button", { class: "btn secondary", type: "button", text: "Toggle dark mode" });
    theme.addEventListener("click", () => {
      const cur = document.documentElement.dataset.theme === "dark" ? "" : "dark";
      document.documentElement.dataset.theme = cur;
      localStorage.setItem("om.theme", cur);
    });
    priv.appendChild(exp); priv.appendChild(theme);
    root.appendChild(priv);
    async function refresh() {
      try {
        const s = await API.local("GET", "/usage");
        card.innerHTML = "<h3>Usage</h3><pre class='mono'>" +
          esc(JSON.stringify(s, null, 2)) + "</pre>";
      } catch (e) { card.innerHTML = "<div class='empty'>Usage service unavailable.</div>"; }
    }
    root._refresh = refresh; refresh();
  }

  /* -- voice view --------------------------------------------------------------------------- */
  function voiceView(root) {
    root.innerHTML = "";
    root.appendChild(el("h1", { text: "Voice" }));
    const card = el("div", { class: "card" });
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    const status = el("p", { class: "muted", text: SR ? "Ready — press and talk." : "Speech recognition is not available in this browser." });
    card.appendChild(status);
    const transcript = el("div", { class: "thread", "aria-live": "polite" });
    card.appendChild(transcript);
    const row = el("div", { class: "form-row" });
    const talk = el("button", { class: "btn", type: "button", text: "Push to talk", disabled: !SR });
    const speak = el("button", { class: "btn secondary", type: "button", text: "Read last reply aloud" });
    row.appendChild(talk); row.appendChild(speak);
    card.appendChild(row);
    root.appendChild(card);

    let rec = null;
    talk.addEventListener("click", () => {
      if (!SR) return;
      if (rec) { rec.stop(); rec = null; talk.textContent = "Push to talk"; return; }
      rec = new SR();
      rec.lang = "en-US"; rec.interimResults = false;
      rec.onresult = (e) => {
        const text = e.results[0][0].transcript;
        const m = el("div", { class: "msg user" });
        m.appendChild(el("span", { class: "role", text: "You (voice)" }));
        const d = el("div", {}); d.textContent = text; m.appendChild(d);
        transcript.appendChild(m);
        // route the transcript as an ordinary chat turn
        const chat = state.chats[state.activeChat];
        if (chat && !chat.offline) {
          API.sendMessage(chat.chatId, text).then((res) => {
            const am = el("div", { class: "msg assistant" });
            am.appendChild(el("span", { class: "role", text: "OpenMuse" }));
            const ad = el("div", { text: "…" }); am.appendChild(ad);
            transcript.appendChild(am);
            window._lastReply = ad;
            API.streamRun(res.runId, {
              onEvent: (ev) => {
                if (ev.name === "assistant.delta" && ev.data.delta) ad.textContent += ev.data.delta;
                if (ev.name === "run.completed") ad.textContent = ev.data.final_text || ad.textContent;
              },
            }).catch(() => {});
          }).catch(() => toast("Voice send failed."));
        }
      };
      rec.onend = () => { rec = null; talk.textContent = "Push to talk"; };
      rec.start();
      talk.textContent = "Stop";
    });
    speak.addEventListener("click", () => {
      const last = window._lastReply;
      if (!last || !last.textContent.trim() || !("speechSynthesis" in window)) {
        toast("Nothing to read aloud."); return;
      }
      speechSynthesis.cancel();
      speechSynthesis.speak(new SpeechSynthesisUtterance(last.textContent));
    });
  }

  const VIEWS = {
    chat: { render: chatView },
    approvals: { render: approvalsView, onShow: (r) => r._refresh && r._refresh() },
    feed: { render: feedView, onShow: (r) => r._refresh && r._refresh() },
    goals: { render: goalsView, onShow: (r) => r._refresh && r._refresh() },
    library: { render: libraryView, onShow: (r) => r._refresh && r._refresh() },
    ideas: { render: ideasView, onShow: (r) => r._refresh && r._refresh() },
    memory: { render: memoryView, onShow: (r) => r._refresh && r._refresh() },
    schedules: { render: schedulesView, onShow: (r) => r._refresh && r._refresh() },
    connectors: { render: connectorsView, onShow: (r) => r._refresh && r._refresh() },
    usage: { render: usageView, onShow: (r) => r._refresh && r._refresh() },
    voice: { render: voiceView },
    activity: { render: activityView, onShow: (r) => r._refresh && r._refresh() },
  };

  function boot() {
    const saved = localStorage.getItem("om.theme");
    if (saved) document.documentElement.dataset.theme = saved;
    renderNav();
    $("#menuBtn").addEventListener("click", () => openDrawer(!$("#drawer").classList.contains("open")));
    $("#drawerOverlay").addEventListener("click", () => openDrawer(false));
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && $("#drawer").classList.contains("open")) openDrawer(false); });
    const themeToggle = $("#themeToggle");
    themeToggle.checked = document.documentElement.dataset.theme === "dark";
    themeToggle.addEventListener("change", () => {
      document.documentElement.dataset.theme = themeToggle.checked ? "dark" : "light";
      try { localStorage.setItem("om.theme", document.documentElement.dataset.theme); } catch (e) { /* ignore */ }
    });
    const main = $("#main");
    main.classList.toggle("chat-mode", state.tab === "chat");
    TABS.forEach(([id]) => {
      const v = el("section", { class: "view", "data-view": id, "aria-label": id });
      v.hidden = id !== state.tab;
      main.appendChild(v);
      VIEWS[id].render(v);
    });
    // keyboard: Alt+1..9,0,= jump between tabs
    document.addEventListener("keydown", (e) => {
      if (e.altKey && !e.ctrlKey && !e.metaKey) {
        const n = parseInt(e.key, 10);
        if (!isNaN(n)) {
          const idx = (n + 9) % 10; // Alt+1 -> 0 ... Alt+0 -> 9, Alt+= -> 10
          const key = e.key === "=" ? 10 : idx;
          if (TABS[key]) { e.preventDefault(); showTab(TABS[key][0]); }
        }
      }
    });
    // connectivity indicator
    const dot = $("#connDot"), label = $("#connLabel");
    async function checkConn() {
      try {
        await API.req("GET", "/v1");
        dot.classList.remove("offline"); label.textContent = "Connected";
      } catch (e) {
        dot.classList.add("offline"); label.textContent = "Offline — read-only cached views";
      }
    }
    checkConn();
    setInterval(checkConn, 15000);
    // flush queued sends when back online
    setInterval(async () => {
      const q = API.queue.all();
      if (!q.length) return;
      try {
        await API.req("GET", "/v1");
        for (const item of q) {
          await API.sendMessage(item.chatId, item.text, { idempotencyKey: item.idempotencyKey });
        }
        API.queue.clear();
        toast("Queued messages sent.");
      } catch (e) { /* still offline */ }
    }, 20000);
  }

  function oauthLanding() {
    if (!/[?&]apps=connected/.test(location.search)) return false;
    document.body.innerHTML = '<div class="auth"><div class="auth-card"><h1>Connected ✓</h1>' +
      '<p class="auth-sub">You can close this tab and go back to OpenMuse.</p></div></div>';
    setTimeout(() => { try { window.close(); } catch (e) { /* ignore */ } }, 1200);
    return true;
  }
  document.addEventListener("DOMContentLoaded", () => {
    if (oauthLanding()) return;
    authGate().then(() => { renderUserBox(); boot(); Notes.start(); });
  });
})();
