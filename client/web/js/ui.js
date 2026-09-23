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
    ["chat", "Chat", "M3 12l9-8 9 8M5 10v10h5v-6h4v6h5V10"],
    ["approvals", "Approvals", "M12 3l7 7-7 7-7-7z M12 8v4"],
    ["feed", "Feed", "M4 6h16M4 12h16M4 18h10"],
    ["goals", "Goals", "M5 13l4 4L19 7"],
    ["library", "Library", "M4 5h6v14H4zM10 5h6v14h-6zM16 5h4v14h-4z"],
    ["ideas", "Ideas", "M12 3a7 7 0 00-4 12.7V18h8v-2.3A7 7 0 0012 3zM9 21h6"],
    ["memory", "Memory", "M12 3v18M5 8l7-5 7 5M5 16l7 5 7-5"],
    ["schedules", "Schedules", "M12 7v5l3 3M12 21a9 9 0 100-18 9 9 0 000 18z"],
    ["connectors", "Connectors", "M9 3v6M15 3v6M7 9h10v4a5 5 0 01-10 0zM12 18v3"],
    ["usage", "Usage", "M4 20V10M10 20V4M16 20v-8M22 20H2"],
    ["voice", "Voice", "M12 3a3 3 0 013 3v6a3 3 0 01-6 0V6a3 3 0 013-3zM6 11a6 6 0 0012 0M12 18v3"],
  ];

  function icon(path) {
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="' + path + '"/></svg>';
  }

  function renderNav() {
    const nav = $("#nav"); nav.innerHTML = "";
    nav.appendChild(el("div", { class: "nav-label", text: "Workspace" }));
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
  const CHAT_STORE = "om.chats.v2";
  function saveChats() {
    try {
      const slim = state.chats.slice(-30).map((c) => ({
        sessionId: c.sessionId, chatId: c.chatId, title: c.title, offline: c.offline,
        messages: c.messages.map((m) => ({ ...m, running: false })),
      }));
      localStorage.setItem(CHAT_STORE, JSON.stringify(slim));
    } catch (e) { /* storage full or disabled */ }
  }
  function loadChats() {
    try { return JSON.parse(localStorage.getItem(CHAT_STORE) || "[]"); } catch (e) { return []; }
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

    // header: chat switcher chip (left) + avatar with live status (center)
    const header = el("header", { class: "mc-header" });
    const chip = el("button", { class: "mc-chip", type: "button", "aria-haspopup": "menu", "aria-label": "Chats" });
    const menu = el("div", { class: "mc-menu", role: "menu", hidden: true });
    const persona = el("div", { class: "mc-persona" });
    persona.innerHTML = '<div class="mc-avatar">' + AVATAR_SVG + "</div>";
    const pname = el("div", { class: "mc-name" });
    pname.innerHTML = '<div class="mc-name-t">OpenMuse</div><div class="mc-status" aria-live="polite"></div>';
    persona.appendChild(pname);
    const chipWrap = el("div", { class: "mc-chipwrap" }, [chip, menu]);
    header.appendChild(chipWrap); header.appendChild(persona);
    root.appendChild(header);
    const statusEl = $(".mc-status", pname);

    const scroller = el("div", { class: "mc-scroll" });
    const thread = el("div", { class: "mc-thread", role: "log", "aria-label": "Conversation" });
    scroller.appendChild(thread);
    root.appendChild(scroller);

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
      statusEl.innerHTML = text ? ((icon || '<span class="mc-pulse"></span>') + "<span>" + esc(text).replace(/</g, "&lt;") + "</span>") : "";
      persona.classList.toggle("busy", !!text);
    }

    function updateSend() {
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

    /* -- chat switcher ------------------------------------------------------ */
    function renderChip() {
      const chat = state.chats[state.activeChat];
      chip.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16"/></svg>' +
        "<span></span>";
      chip.querySelector("span").textContent = chat ? (chat.title || "New chat") : "New chat";
    }
    function renderMenu() {
      menu.innerHTML = "";
      const nb = el("button", { type: "button", role: "menuitem", class: "mc-menu-new", text: "+ New chat" });
      nb.addEventListener("click", () => { menu.hidden = true; newChat(); });
      menu.appendChild(nb);
      state.chats.slice().reverse().forEach((c) => {
        const i = state.chats.indexOf(c);
        const b = el("button", { type: "button", role: "menuitem", "aria-current": i === state.activeChat ? "true" : "false" });
        b.textContent = c.title || "New chat";
        b.addEventListener("click", () => { menu.hidden = true; state.activeChat = i; input.value = state.drafts[i] || ""; renderAll(); });
        menu.appendChild(b);
      });
    }
    chip.addEventListener("click", (e) => { e.stopPropagation(); renderMenu(); menu.hidden = !menu.hidden; });
    document.addEventListener("click", (e) => { if (!chipWrap.contains(e.target)) menu.hidden = true; });

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
        if (m.error) thread.appendChild(el("div", { class: "mc-row bot" }, [el("div", { class: "mc-bubble bot err", text: m.error })]));
        if (m.running && !m.text && !m.waiting) {
          const dots = el("div", { class: "mc-typing", "aria-label": "OpenMuse is working" });
          dots.innerHTML = "<i></i><i></i><i></i>";
          thread.appendChild(el("div", { class: "mc-row bot" }, [dots]));
        }
      });
      if (nearBottom || activeRun) scroller.scrollTop = scroller.scrollHeight;
    }

    // Live cards keep their DOM across re-renders (no flicker, buttons stay put).
    const nodeCache = new WeakMap();
    function renderBlock(blk, m) {
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
      const card = el("div", { class: "mc-tool" + (blk.ok === false ? " failed" : "") });
      card.innerHTML = '<div class="mc-tool-ic">' + GEAR + "</div>";
      const txt = el("div", { class: "mc-tool-txt" });
      txt.appendChild(el("div", { class: "mc-tool-t", text: blk.title }));
      txt.appendChild(el("div", { class: "mc-tool-s", text: blk.status || "" }));
      card.appendChild(txt);
      return el("div", { class: "mc-row bot" }, [card]);
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
        state.chats.push({ sessionId: s.session_id, chatId: s.chat_id, title: "", messages: [] });
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
          break;
        case "tool.call": {
          const tool = d.tool || "";
          if (tool === "tools.load_namespace") break;
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
              blk = { type: "tool", callId: d.call_id, title, status: "Working" };
              amsg.blocks.push(blk);
            }
            if (d.decision === "DENY") { blk.status = "Blocked by policy"; blk.ok = false; }
            setStatus("Using " + blk.title);
          }
          break;
        }
        case "tool.result": {
          const blk = amsg.blocks.find((b) => b.callId === d.call_id);
          if (blk) { blk.status = d.ok ? "Done" : "Failed"; blk.ok = d.ok; }
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
      if (activeRun === run) activeRun = null;
      setStatus("");
      updateSend(); renderThread(); saveChats();
    }

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
        const run = { runId: res.runId, amsg, chat };
        activeRun = run; amsg.runId = res.runId;
        setStatus("Preparing"); updateSend();
        let pending = false;
        API.streamRun(res.runId, {
          onEvent: (ev) => {
            handleEvent(run, ev);
            if (!pending) { pending = true; requestAnimationFrame(() => { pending = false; if (chat === state.chats[state.activeChat]) renderThread(); }); }
          },
        }).then(() => finishRun(run))
          .catch((err) => { amsg.error = "Connection lost: " + err.message; finishRun(run); });
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

    function renderAll() { renderChip(); renderThread(); updateSend(); autosize(); }
    root._renderAll = renderAll;

    const saved = loadChats();
    if (saved.length && !state.chats.length) { state.chats = saved; state.activeChat = saved.length - 1; }
    renderAll();
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
  function memoryView(root) {
    root.innerHTML = "";
    root.appendChild(el("h1", { text: "Memory" }));
    const stats = el("div", { class: "card" });
    root.appendChild(stats);
    const form = el("form", { class: "card" });
    form.innerHTML = "<h3>Recall</h3>";
    const q = el("input", { type: "search", "aria-label": "Search memory", placeholder: "Search memory…" });
    const go = el("button", { class: "btn", type: "submit", text: "Search" });
    const row = el("div", { class: "form-row" });
    row.appendChild(q); row.appendChild(go); form.appendChild(row);
    const hits = el("div", {});
    form.appendChild(hits);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      hits.innerHTML = "";
      const res = await API.local("POST", "/memory/recall", { query: q.value, top_k: 8 });
      (res.hits || []).forEach((h) => {
        const c = el("div", { class: "card" });
        const p = el("p", {}); p.textContent = h.text; c.appendChild(p);
        c.appendChild(el("div", { class: "muted", text: h.source + " · score " + h.score +
          (h.source_refs && h.source_refs.length ? " · sources: " + h.source_refs.join(", ") : "") }));
        const fg = el("button", { class: "btn small danger", type: "button", text: "Forget this" });
        fg.addEventListener("click", async () => {
          if (!confirm("Forget this memory? A plan is shown before anything is removed.")) return;
          const plan = await API.local("POST", "/memory/forget", { query: h.text.slice(0, 80), confirmed: false });
          const names = (plan.plan.targets || []).map((t) => t.memory_id || t.entry_id).join(", ");
          if (confirm("Forget plan targets: " + (names || "none") + ". Execute?")) {
            await API.local("POST", "/memory/forget", { query: h.text.slice(0, 80), confirmed: true });
            toast("Forgotten."); refresh();
          }
        });
        c.appendChild(fg);
        hits.appendChild(c);
      });
    });
    root.appendChild(form);
    const people = el("div", { class: "card" });
    people.innerHTML = "<h3>People</h3>";
    root.appendChild(people);
    async function refresh() {
      try {
        const s = await API.local("GET", "/memory/stats");
        stats.innerHTML = "<h3>Overview</h3><div class='muted'>" +
          esc(s.curated_active) + " curated records · " + esc(s.journal_entries) +
          " journal entries · " + esc(s.people) + " people</div>";
        const pp = await API.local("GET", "/memory/people");
        people.innerHTML = "<h3>People</h3>";
        (pp.people || []).forEach((p) => {
          const d = el("div", { class: "list-item" });
          d.appendChild(el("span", { text: p.name + " (" + p.fact_count + " facts)" }));
          const v = el("button", { class: "btn small secondary", type: "button", text: "View page" });
          v.addEventListener("click", async () => {
            const pg = await API.local("GET", "/memory/people/" + encodeURIComponent(p.name));
            const pre = el("pre", { class: "mono", text: pg.page || "(empty)" });
            people.appendChild(pre);
          });
          d.appendChild(v);
          people.appendChild(d);
        });
      } catch (e) { stats.innerHTML = "<div class='empty'>Memory service unavailable.</div>"; }
    }
    root._refresh = refresh; refresh();
  }

  /* -- schedules view ----------------------------------------------------------------- */
  function schedulesView(root) {
    root.innerHTML = "";
    root.appendChild(el("h1", { text: "Schedules & Hooks" }));
    const form = el("form", { class: "card" });
    form.innerHTML = "<h3>New schedule</h3>";
    const name = el("input", { type: "text", "aria-label": "Schedule name", placeholder: "Name" });
    const cron = el("input", { type: "text", "aria-label": "Cron expression", placeholder: "Cron, e.g. 0 9 * * *" });
    const tz = el("input", { type: "text", "aria-label": "Timezone", placeholder: "Timezone", value: "America/New_York" });
    const instr = el("textarea", { "aria-label": "Instructions", placeholder: "Instructions", rows: "2" });
    const add = el("button", { class: "btn", type: "submit", text: "Create schedule" });
    [name, cron, tz, instr].forEach((n) => {
      const lab = el("label", { class: "field" }); lab.appendChild(n); form.appendChild(lab);
    });
    form.appendChild(add);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      await API.local("POST", "/schedules", {
        name: name.value, schedule: cron.value, timezone: tz.value || "UTC",
        instructions: instr.value, kind: "cron",
      });
      name.value = cron.value = instr.value = "";
      refresh();
    });
    root.appendChild(form);
    const list = el("div", {});
    root.appendChild(list);
    const hooksH = el("h2", { text: "Hooks" });
    root.appendChild(hooksH);
    const hooks = el("div", {});
    root.appendChild(hooks);

    async function refresh() {
      list.innerHTML = ""; hooks.innerHTML = "";
      let ss = [], hh = [];
      try {
        ss = await API.local("GET", "/schedules");
        hh = await API.local("GET", "/hooks");
      } catch (e) {}
      if (!ss.length) list.appendChild(el("div", { class: "empty", text: "No schedules." }));
      ss.forEach((s) => {
        const c = el("div", { class: "card" });
        const head = el("div", { class: "list-item" });
        head.appendChild(el("div", {}, [
          el("strong", { text: s.name }),
          el("div", { class: "muted", text: (s.cron_expression || s.run_at || "") + " · next " + (s.next_fire_at || "?") }),
        ]));
        head.appendChild(el("span", { class: "badge" + (s.enabled ? " ok" : ""), text: s.enabled ? "enabled" : "disabled" }));
        c.appendChild(head);
        const row = el("div", { class: "form-row" });
        const pv = el("button", { class: "btn small secondary", type: "button", text: "Next runs" });
        pv.addEventListener("click", async () => {
          const p = await API.local("GET", "/schedules/" + s.schedule_id + "/preview");
          toast("Next: " + (p.preview || []).slice(0, 3).join(", "));
        });
        const tg = el("button", { class: "btn small secondary", type: "button",
                                 text: s.enabled ? "Disable" : "Enable" });
        tg.addEventListener("click", async () => {
          await API.local("POST", "/schedules/" + s.schedule_id + "/enabled", { enabled: !s.enabled });
          refresh();
        });
        const rn = el("button", { class: "btn small secondary", type: "button", text: "Run now" });
        rn.addEventListener("click", async () => {
          await API.local("POST", "/schedules/" + s.schedule_id + "/run", {});
          toast("Run enqueued.");
        });
        const del = el("button", { class: "btn small danger", type: "button", text: "Delete" });
        del.addEventListener("click", async () => {
          if (confirm("Delete schedule \"" + s.name + "\"?")) {
            await API.local("DELETE", "/schedules/" + s.schedule_id); refresh();
          }
        });
        [pv, tg, rn, del].forEach((b) => row.appendChild(b));
        c.appendChild(row);
        list.appendChild(c);
      });
      if (!hh.length) hooks.appendChild(el("div", { class: "empty", text: "No hooks." }));
      hh.forEach((h) => {
        const c = el("div", { class: "card" });
        c.appendChild(el("strong", { text: h.name }));
        c.appendChild(el("div", { class: "muted", text: h.provider + "." + h.event_type }));
        const del = el("button", { class: "btn small danger", type: "button", text: "Remove" });
        del.addEventListener("click", async () => {
          await API.local("DELETE", "/hooks/" + h.hook_id); refresh();
        });
        c.appendChild(del);
        hooks.appendChild(c);
      });
    }
    root._refresh = refresh; refresh();
  }

  /* -- connectors view ------------------------------------------------------------------ */
  function connectorsView(root) {
    root.innerHTML = "";
    root.appendChild(el("h1", { text: "Connectors" }));
    const note = el("p", { class: "muted", text:
      "Credentials are captured on the Secure Vault capture page. This client never sees or stores secret values." });
    root.appendChild(note);
    const list = el("div", {});
    root.appendChild(list);
    async function refresh() {
      list.innerHTML = "";
      let cat = [];
      try { cat = await API.local("GET", "/connectors"); } catch (e) {}
      if (!cat.length) { list.appendChild(el("div", { class: "empty", text: "No connectors registered." })); return; }
      cat.forEach((c) => {
        const card = el("div", { class: "card" });
        card.appendChild(el("h3", { text: c.display_name || c.provider }));
        card.appendChild(el("div", { class: "muted", text: "auth: " + (c.auth_kinds || []).join(", ") +
          " · scopes: " + (c.scopes || []).join(", ") }));
        const row = el("div", { class: "form-row" });
        if ((c.auth_kinds || []).includes("oauth_pkce")) {
          const b = el("button", { class: "btn small", type: "button", text: "Connect with OAuth" });
          b.addEventListener("click", async () => {
            const r = await API.local("POST", "/connectors/connect", {
              provider: c.provider, auth_kind: "oauth_pkce", scopes: c.scopes || [] });
            if (r.authorization_url) window.open(r.authorization_url, "_blank", "noopener");
            toast("Complete authorization in the opened window.");
          });
          row.appendChild(b);
        }
        if ((c.auth_kinds || []).includes("api_key")) {
          const b = el("button", { class: "btn small secondary", type: "button", text: "Connect with API key" });
          b.addEventListener("click", async () => {
            const cap = await API.local("POST", "/connectors/capture", {
              provider: c.provider, purpose: "connector connect" });
            // The capture page is served by the vault; the secret is entered
            // there, never here.
            window.open(cap.capture_url, "_blank", "noopener");
            toast("Enter the key on the Secure Vault capture page, then finish below.");
            const fin = el("button", { class: "btn small", type: "button", text: "Finish connect" });
            fin.addEventListener("click", async () => {
              const r = await API.local("POST", "/connectors/connect-capture", {
                provider: c.provider, capture_id: cap.capture_id, scopes: c.scopes || [] });
              toast("Connected: " + r.status);
            });
            row.appendChild(fin);
          });
          row.appendChild(b);
        }
        card.appendChild(row);
        list.appendChild(card);
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
  };

  function boot() {
    const saved = localStorage.getItem("om.theme");
    if (saved) document.documentElement.dataset.theme = saved;
    renderNav();
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

  document.addEventListener("DOMContentLoaded", boot);
})();
