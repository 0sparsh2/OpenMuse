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
    ["monitors", "Monitors", "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12zM12 15a3 3 0 100-6 3 3 0 000 6z"],
    ["logins", "Saved logins", "M7 11V8a5 5 0 0110 0v3M5 11h14v10H5zM12 15v2"],
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
    "web.search": "Web search", "web.read": "Web", "web.weather": "Weather",
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

  /* -- web citations (search) ------------------------------------------------------------ */
  function favicon(site) {
    const i = el("img", { class: "mc-fav", alt: "", width: "16", height: "16", loading: "lazy",
                          src: "https://icons.duckduckgo.com/ip3/" + encodeURIComponent(site || "") + ".ico" });
    i.addEventListener("error", () => { i.replaceWith(el("span", { class: "mc-fav mc-fav-l", text: (site || "?")[0].toUpperCase() })); });
    return i;
  }
  function webSources(m) {
    const map = new Map();
    (m.blocks || []).forEach((b) => {
      if (b.display && b.display.type === "web_sources") (b.display.sources || []).forEach((src) => map.set(Number(src.n), src));
      if (b.display && b.display.source && b.display.source.n) map.set(Number(b.display.source.n), b.display.source);
    });
    // the list saved with the finished answer wins (it knows which were cited; survives reloads)
    (m.sources || []).forEach((src) => map.set(Number(src.n), { ...(map.get(Number(src.n)) || {}), ...src }));
    return map;
  }
  // after removing markers: no ",,,", no " .", no empty "Sources:" line
  function tidyCites(t) {
    return String(t || "").replace(/(?:\s*,)+\s*(?=[.;:!?]|$)/gm, "").replace(/[ \t]+([.,;:!?])/g, "$1")
      .replace(/^[ \t>*_-]*(?:\*\*)?(?:sources?|references?|citations?)(?:\*\*)?\s*[:：-]?\s*(?:\*\*)?[\s,.;]*$/gim, "")
      .replace(/\n{3,}/g, "\n\n").trim();
  }
  const CITE_RE = /\[(\d{1,3}(?:\s*,\s*\d{1,3})*)\](?!\()|【(\d{1,3}(?:\s*[,，]\s*\d{1,3})*)】/g;
  function citedNumbers(text) {
    const out = new Set();
    String(text || "").replace(CITE_RE, (_, a, b) => { (a || b).split(/[,，\s]+/).forEach((x) => x && out.add(Number(x))); return ""; });
    return out;
  }
  // [1, 3] -> one chip naming the first site (+ how many more), linking to it
  function renderCited(text, srcs) {
    const marked = String(text || "").replace(CITE_RE, (_, a, b) => {
      const ns = (a || b).split(/[,，\s]+/).filter(Boolean).map(Number).filter((n) => srcs.has(n));
      return ns.length ? "\u0001" + ns.join(",") + "\u0001" : "";
    }).replace(/\u0001\s*\u0001/g, ",");
    return renderRich(marked).replace(/\u0001([\d,]+)\u0001/g, (_, list) => {
      const ns = [...new Set(list.split(",").map(Number))];
      const first = srcs.get(ns[0]);
      const title = ns.map((n) => { const s = srcs.get(n); return "[" + n + "] " + (s.title || s.site); }).join("\n")
        .replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
      return '<a class="cite" href="' + encodeURI(first.url).replace(/"/g, "%22") + '" target="_blank" rel="noopener noreferrer" title="' + title + '">' +
        String(first.site).replace(/&/g, "&amp;").replace(/</g, "&lt;") + (ns.length > 1 ? " +" + (ns.length - 1) : "") + "</a>";
    });
  }
  // Under a searched answer: the websites it used, cited ones first, always visible
  function sourcesRow(text, srcs) {
    const cited = citedNumbers(text);
    const all = [...srcs.values()].sort((a, b) => a.n - b.n);
    const used = all.filter((s) => s.cited || cited.has(Number(s.n)));
    const shown = (used.length ? used : all).slice(0, 6);
    const row = el("div", { class: "mc-srcrow", role: "group", "aria-label": "Sources" });
    row.appendChild(el("span", { class: "mc-srcrow-l", text: used.length ? "Sources" : "Read" }));
    shown.forEach((src) => {
      const a = el("a", { class: "mc-srcchip", href: src.url, target: "_blank", rel: "noopener noreferrer", title: src.title || src.site });
      a.appendChild(el("span", { class: "mc-srcchip-n", text: String(src.n) }));
      a.appendChild(favicon(src.site));
      a.appendChild(el("span", { text: src.site }));
      row.appendChild(a);
    });
    const more = all.length - shown.length;
    const btn = el("button", { type: "button", class: "mc-srcchip mc-srcchip-all", text: more > 0 ? "All " + all.length + " sources" : "Details" });
    btn.addEventListener("click", () => sourcesSheet(text, srcs));
    row.appendChild(btn);
    return row;
  }

  function sourceRow(src) {
    const li = el("li", {});
    const a = el("a", { href: src.url, target: "_blank", rel: "noopener noreferrer" });
    a.appendChild(favicon(src.site));
    const txt = el("span", {});
    txt.appendChild(el("span", { class: "mc-src-t", text: src.title || src.site }));
    txt.appendChild(el("span", { class: "mc-src-s", text: src.site + (src.date ? " · " + src.date : "") }));
    a.appendChild(txt);
    li.appendChild(el("span", { class: "mc-src-n", text: String(src.n) }));
    li.appendChild(a);
    return li;
  }
  function sourcesSheet(text, srcs) {
    const cited = citedNumbers(text);
    const ov = el("div", { class: "src-sheet-ov" });
    const sheet = el("div", { class: "src-sheet", role: "dialog", "aria-label": "Sources" });
    const head = el("div", { class: "src-sheet-h" });
    head.appendChild(el("strong", { text: "Sources" }));
    const x = el("button", { type: "button", class: "bv-x", "aria-label": "Close", text: "✕" });
    head.appendChild(x);
    sheet.appendChild(head);
    const isCited = (s) => s.cited || cited.has(Number(s.n));
    const groups = [["Cited", [...srcs.values()].filter(isCited)], ["Also read", [...srcs.values()].filter((s) => !isCited(s))]];
    groups.forEach(([label, list]) => {
      if (!list.length) return;
      sheet.appendChild(el("div", { class: "src-sheet-g", text: label }));
      const ol = el("ol", { class: "mc-srcs" });
      list.forEach((src) => ol.appendChild(sourceRow(src)));
      sheet.appendChild(ol);
    });
    const close = () => ov.remove();
    x.addEventListener("click", close);
    ov.addEventListener("click", (e) => { if (e.target === ov) close(); });
    document.addEventListener("keydown", function esc(e) { if (e.key === "Escape") { close(); document.removeEventListener("keydown", esc); } });
    ov.appendChild(sheet);
    document.body.appendChild(ov);
    x.focus();
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
    const voiceBtn = el("button", { type: "button", class: "mc-icon", "aria-label": "Voice mode", title: "Voice mode" });
    voiceBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M4 10v4M8 7v10M12 4v16M16 7v10M20 10v4"/></svg>';
    voiceBtn.addEventListener("click", () => VoiceMode.open());
    form.appendChild(plus); form.appendChild(input); form.appendChild(mic); form.appendChild(voiceBtn); form.appendChild(send);
    const qnote = el("div", { class: "queued-note", hidden: true });
    const composerWrap = el("div", { class: "mc-bottom" }, [qnote, form]);
    root.appendChild(composerWrap);
    state.prefillChat = (t) => { input.value = t; input.dispatchEvent(new Event("input")); input.focus(); };

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
          chat.messages.push({ role: "assistant", text: t.final_text, blocks, sources: t.sources || [], ts: t.created_at * 1000,
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
      const chat = state.chats[state.activeChat];
      // redrawing the same chat mid-run (streaming text, status): don't replay every fade-in
      thread.classList.toggle("redraw", !!(activeRun && chat && thread.dataset.chat === chat.chatId && thread.childElementCount));
      thread.dataset.chat = chat ? chat.chatId : "";
      thread.innerHTML = "";
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
        if (!m.text && m.running && m.streamText && m.streamText.trim()) {
          // still being written: show it now, with chips for sources already found;
          // hide a marker that's only half-typed ("[1", "【")
          const live = el("div", { class: "mc-bubble bot streaming" });
          const partial = m.streamText.replace(/[\[【][^\]】]{0,40}$/, "");
          const srcsNow = webSources(m);
          live.innerHTML = srcsNow.size ? renderCited(partial, srcsNow) : renderRich(String(partial).replace(CITE_RE, ""));
          thread.appendChild(el("div", { class: "mc-row bot" }, [live]));
        }
        if (m.text) {
          const bub = el("div", { class: "mc-bubble bot" });
          const srcs = webSources(m);
          // no known sources: never show bare [2, 3] markers
          bub.innerHTML = srcs.size ? renderCited(tidyCites(m.text), srcs) : renderRich(tidyCites(String(m.text).replace(CITE_RE, "")));
          thread.appendChild(el("div", { class: "mc-row bot" }, [bub]));
          if (srcs.size && !m.running) thread.appendChild(el("div", { class: "mc-row bot" }, [sourcesRow(m.text, srcs)]));
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
        if (m.running && !m.text && !m.waiting && !(m.streamText && m.streamText.trim())) {
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
      if (blk.type === "helpers") return helpersCard(blk, m);
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
      if (blk.display && blk.display.type === "web_sources") return searchStep(blk);
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

    // "Searched the web · 3 searches · read 4 sites" with site icons; expands to queries + sources
    function searchStep(blk) {
      const d = blk.display;
      const det = el("details", { class: "mc-search" });
      const sum = el("summary", { class: "mc-step done" });
      sum.innerHTML = '<span class="mc-step-ic">' + STEP_ICONS.web + "</span>";
      const nq = (d.queries || []).length;
      sum.appendChild(el("span", { text: (blk.tool === "web.read" ? "Read " + ((d.sources[0] || {}).site || "a page")
        : "Searched the web" + (nq > 1 ? " · " + nq + " searches" : "") + (d.read ? " · read " + d.read + " site" + (d.read > 1 ? "s" : "") : "")) }));
      const icons = el("span", { class: "mc-favs", "aria-hidden": "true" });
      (d.sources || []).slice(0, 5).forEach((src) => icons.appendChild(favicon(src.site)));
      sum.appendChild(icons);
      det.appendChild(sum);
      const body = el("div", { class: "mc-search-body" });
      if (nq) {
        const qs = el("div", { class: "mc-queries" });
        d.queries.forEach((q) => qs.appendChild(el("span", { class: "mc-query", text: q })));
        body.appendChild(qs);
      }
      const ol = el("ol", { class: "mc-srcs" });
      (d.sources || []).forEach((src) => ol.appendChild(sourceRow(src)));
      body.appendChild(ol);
      det.appendChild(body);
      return det;
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

    function helpersCard(blk, m) {
      const card = el("div", { class: "mc-plan mc-helpers" });
      const done = blk.items.filter((x) => x.status !== "running").length;
      const head = el("div", { class: "mc-plan-head" });
      head.appendChild(el("strong", { text: "Helpers working in parallel" }));
      head.appendChild(el("span", { text: done + "/" + blk.items.length }));
      card.appendChild(head);
      const ol = el("ol", {});
      blk.items.forEach((x) => {
        const st = x.status === "running" ? (m.running ? "active" : "pending") : x.status === "completed" ? "done" : "failed";
        const li = el("li", { class: "st-" + st });
        li.appendChild(el("span", { class: "mc-plan-ic", "aria-hidden": "true" }));
        li.appendChild(el("span", { text: x.objective }));
        if (x.summary) li.appendChild(el("em", { text: x.summary }));
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
      if (tool === "web.fetch" || tool === "web.read" || tool === "web.search" || tool === "web.weather") return STEP_ICONS.web;
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
        "web.read": ["Reading " + hostOf(a.url || ""), "Read " + hostOf(a.url || "")],
        "web.search": ["Searching the web", "Searched the web"],
        "web.weather": ["Checking the weather" + (a.location ? " in " + a.location : ""), "Checked the weather" + (a.location ? " in " + a.location : "")],
        "monitor.create": ["Setting up a watch on " + hostOf(a.url || ""), "Watching " + hostOf(a.url || "")],
        "goals.create": ["Planning your goal", "Planned your goal"],
        "goals.update": ["Updating your goal", "Updated your goal"],
        "goals.list": ["Checking your goals", "Checked your goals"],
        "goals.propose_idea": ["Saving an idea", "Saved an idea for later"],
        "goals.post_update": ["Posting to your feed", "Posted to your feed"],
        "monitor.list": ["Checking your monitors", "Checked your monitors"],
        "browser.logins": ["Checking your saved logins", "Checked your saved logins"],
        "monitor.remove": ["Removing a monitor", "Removed a monitor"],
        "shell.exec": ["Running a command", "Ran a command"],
        "docs.list": ["Looking in your Library", "Looked in your Library"],
        "docs.read": ["Reading a document", "Read a document"],
        "docs.pdf_fields": ["Reading the form", "Read the form"],
        "docs.pdf_fill": ["Filling in the form", "Filled in the form"],
        "docs.create": ["Creating " + (a.title || "a document"), "Created " + (a.title || "a document")],
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
      } else if (d.type === "agenda") {
        ic.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3.5" y="5" width="17" height="15" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4"/></svg>';
        meta.appendChild(el("div", { class: "mc-tool-t", text: "Your calendar" }));
        meta.appendChild(el("div", { class: "mc-tool-s", text: d.title }));
        const ul = el("ul", { class: "mc-agenda" });
        (d.items || []).forEach((it) => {
          const li = el("li", {});
          li.appendChild(el("span", { class: "mc-agenda-when", text: it.when }));
          const t = it.url ? el("a", { href: it.url, target: "_blank", rel: "noopener noreferrer", text: it.title })
                           : el("span", { text: it.title });
          li.appendChild(t);
          if (it.location) li.appendChild(el("span", { class: "mc-agenda-loc", text: it.location }));
          ul.appendChild(li);
        });
        if (d.more) ul.appendChild(el("li", { class: "mc-agenda-more", text: "+" + d.more + " more" }));
        card.appendChild(ul);
      } else if (d.type === "weather") {
        ic.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="9" cy="9" r="3.5"/><path d="M9 2.5v1.5M2.5 9H4M4.4 4.4l1 1M13.6 4.4l-1 1M8 20h9a3.5 3.5 0 000-7 5 5 0 00-9.6 1.4A2.9 2.9 0 008 20z"/></svg>';
        meta.appendChild(el("div", { class: "mc-tool-t", text: d.place }));
        meta.appendChild(el("div", { class: "mc-tool-s", text: "Weather · Open-Meteo" }));
        const now = el("div", { class: "mc-wx-now" });
        now.appendChild(el("strong", { text: Math.round(d.now.temp) + d.unit }));
        now.appendChild(el("span", { text: d.now.summary + (d.now.feels_like != null ? " · feels like " + Math.round(d.now.feels_like) + d.unit : "") }));
        card.appendChild(now);
        const days = el("div", { class: "mc-wx-days" });
        (d.days || []).forEach((x) => {
          const day = el("div", { class: "mc-wx-day" });
          day.appendChild(el("span", { class: "mc-wx-dn", text: new Date(x.date + "T12:00:00").toLocaleDateString(undefined, { weekday: "short" }) }));
          day.appendChild(el("span", { class: "mc-wx-sum", text: x.summary }));
          day.appendChild(el("span", { class: "mc-wx-t", text: Math.round(x.high) + "° / " + Math.round(x.low) + "°" }));
          if (x.rain_chance != null) day.appendChild(el("span", { class: "mc-wx-rain", text: x.rain_chance + "% rain" }));
          days.appendChild(day);
        });
        card.appendChild(days);
      } else if (d.type === "free_slots") {
        ic.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3.5" y="5" width="17" height="15" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4M9 15l2 2 4-4"/></svg>';
        meta.appendChild(el("div", { class: "mc-tool-t", text: d.title || "Free times" }));
        meta.appendChild(el("div", { class: "mc-tool-s", text: d.slots && d.slots.length ? "Tap a time to plan something" : "Try a wider range" }));
        const wrap = el("div", { class: "mc-slots" });
        (d.slots || []).forEach((sl) => {
          const b = el("button", { type: "button", class: "mc-slot", text: sl.label });
          b.addEventListener("click", () => { if (state.prefillChat) state.prefillChat("Book " + sl.label + " for "); });
          wrap.appendChild(b);
        });
        card.appendChild(wrap);
      } else if (d.type === "goal") {
        ic.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r="1"/></svg>';
        meta.appendChild(el("div", { class: "mc-tool-t", text: d.title || "Goal" }));
        meta.appendChild(el("div", { class: "mc-tool-s", text: (d.milestones || []).length + " milestones" }));
        if ((d.milestones || []).length) {
          const ol = el("ol", { class: "mc-goal-ms" });
          d.milestones.forEach((t) => ol.appendChild(el("li", { text: t })));
          card.appendChild(ol);
        }
        const b = el("button", { type: "button", class: "mc-open", text: "See goals" });
        b.addEventListener("click", () => showTab("goals"));
        card.appendChild(b);
      } else if (d.type === "monitor") {
        ic.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>';
        meta.appendChild(el("div", { class: "mc-tool-t", text: d.title || "Monitor" }));
        meta.appendChild(el("div", { class: "mc-tool-s", text: d.subtitle || "Watching" }));
        const b = el("button", { type: "button", class: "mc-open", text: "See monitors" });
        b.addEventListener("click", () => showTab("monitors"));
        card.appendChild(b);
      } else if (d.type === "document") {
        ic.innerHTML = STEP_ICONS.file;
        meta.appendChild(el("div", { class: "mc-tool-t", text: d.title || "Document" }));
        meta.appendChild(el("div", { class: "mc-tool-s", text: d.subtitle || "Document" }));
        if (d.fields && d.fields.length) {   // filled form: show values for review
          const dl = el("dl", { class: "mc-fields" });
          d.fields.forEach((f) => { dl.appendChild(el("dt", { text: f.label })); dl.appendChild(el("dd", { text: f.value })); });
          card.appendChild(dl);
        }
        if (d.artifact_id) {
          const b = el("button", { type: "button", class: "mc-open", text: "Open" });
          const mime = d.mime || (/\.pdf$/i.test(d.title || "") ? "application/pdf" : "");
          b.addEventListener("click", () => previewDoc(d.artifact_id, d.title, mime));
          card.appendChild(b);
        } else if (d.url) action("Open", d.url);
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
        if (a.kind === "type" && a.text_ref) return {
          title: "OpenMuse wants to sign in to " + hostOf(b.credential_site || "") + (b.credential_user ? " as " + b.credential_user : ""),
          detail: "It will enter your saved " + (b.credential_field === "password" ? "password" : "username") +
                  " on " + (b.credential_site || "the site") + ". The password is never shown to the AI." };
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
      return ({ navigate: "Opening " + hostOf(a.url || ""), click: "Clicking", type: a.text_ref ? "Signing in" : "Typing “" + (a.text || "").slice(0, 30) + "”",
                select: "Selecting “" + (a.option || "") + "”", scroll: "Scrolling", wait: "Waiting for the page",
                back: "Going back", confirm_commit: "Placing order" })[a.kind] || "Working";
    }

    function handleEvent(run, ev) {
      const amsg = run.amsg, d = ev.data || {};
      if (state.voiceTap) { try { state.voiceTap(ev, run); } catch (e) { /* voice UI must never break the chat */ } }
      switch (ev.name) {
        case "run.status":
          if (d.status === "AWAITING_MODEL" && !amsg.waiting) setStatus(amsg.blocks.length ? "Thinking" : "Preparing");
          if (d.status === "WAITING_FOR_APPROVAL") { amsg.waiting = true; setStatus("Waiting for you"); }
          if (d.status === "EXECUTING_TOOLS") amsg.waiting = false;
          if (d.status === "ASSEMBLING_CONTEXT" && amsg.paused) { amsg.paused = false; updateCtrl(); }
          break;
        case "tool.call": {
          const tool = d.tool || "";
          if (tool === "tools.load_namespace" || tool.startsWith("task.") || tool === "subagent.parallel") break;  // shown as cards
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
        case "subagent": {
          let hb = amsg.blocks.find((b) => b.type === "helpers");
          if (!hb) { hb = { type: "helpers", items: [] }; amsg.blocks.push(hb); }
          let it = hb.items.find((x) => x.id === d.delegation_id);
          if (!it) { it = { id: d.delegation_id, objective: d.objective || "Helper", status: "running", summary: "" }; hb.items.push(it); }
          if (d.status && d.status !== "running") { it.status = d.status; it.summary = d.summary || ""; }
          const busy = hb.items.filter((x) => x.status === "running").length;
          setStatus(busy ? busy + " helper" + (busy > 1 ? "s" : "") + " working" : "Combining results");
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
        case "assistant.partial":          // the answer as it's being written
          if (d.reset || d.step !== amsg.streamStep) { amsg.streamStep = d.step; amsg.streamText = ""; }
          if (d.text) {
            amsg.streamText = (amsg.streamText || "") + d.text;
            if (!amsg.waiting) setStatus("Writing");
          }
          break;
        case "assistant.delta":
          amsg.text += d.text || d.delta || "";
          amsg.streamText = "";
          break;
        case "run.completed":
          if (d.final_text) amsg.text = d.final_text;
          if (d.sources) amsg.sources = d.sources;
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
      if (state.voiceTap) { try { state.voiceTap({ name: "run.finished", data: {} }, run); } catch (e) { /* ignore */ } }
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
      input.value = ""; state.drafts[state.activeChat] = ""; autosize();
      await sendText(text);
    }

    // voice mode sends through the same path so the thread shows every turn, card and approval
    state.describeApproval = describeApproval;
    state.sendChat = async (text, opts) => {
      if (activeRun) {  // a new spoken request replaces whatever was still running
        try { await API.cancelRun(activeRun.runId); } catch (e) { /* already ending */ }
        for (let i = 0; i < 40 && activeRun; i++) await new Promise((r) => setTimeout(r, 100));
      }
      return sendText(text, opts);
    };

    async function sendText(text, opts) {
      opts = opts || {};
      let chat = state.chats[state.activeChat];
      if (!chat) { await newChat(); chat = state.chats[state.activeChat]; if (!chat) return; }
      if (!chat.title) chat.title = text.length > 42 ? text.slice(0, 40) + "…" : text;
      chat.messages.push({ role: "user", text, ts: Date.now() });
      const amsg = { role: "assistant", text: "", blocks: [], running: true, ts: Date.now() };
      chat.messages.push(amsg);
      renderAll();
      try {
        let res;
        try { res = await API.sendMessage(chat.chatId, text, { mode: opts.mode }); }
        catch (err) {
          if (err.status !== 404) throw err;
          const s2 = await API.createSession(chat.title || "");  // backend restarted: new session
          chat.sessionId = s2.session_id; chat.chatId = s2.chat_id;
          res = await API.sendMessage(chat.chatId, text, { mode: opts.mode });
        }
        startStream(chat, amsg, res.runId);
        return res.runId;
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


  /* -- goals view --------------------------------------------------------------- */


  /* -- library view --------------------------------------------------------------- */
  /* -- library (issue #14): the signed-in user's documents -------------------- */
  const DOC_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4"/></svg>';
  async function fetchDoc(id) {
    const h = {};
    if (API.store.apiKey) h["Authorization"] = "Bearer " + API.store.apiKey;
    const r = await fetch("/v1/library/" + encodeURIComponent(id), { headers: h });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.blob();
  }
  async function downloadDoc(id, name) {
    const url = URL.createObjectURL(await fetchDoc(id));
    const a = el("a", { href: url, download: name || "document" });
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  }
  async function previewDoc(id, name, mime) {
    const modal = el("div", { class: "bv-modal doc-modal", role: "dialog", "aria-modal": "true", "aria-label": "Preview " + (name || "") });
    const panel = el("div", { class: "bv-panel" });
    const bar = el("div", { class: "bv-bar" });
    const close = el("button", { type: "button", class: "bv-x", "aria-label": "Close preview", text: "✕" });
    const title = el("div", { class: "bv-url" }); title.innerHTML = DOC_ICON; title.appendChild(el("span", { class: "h", text: name || "Document" }));
    const dl = el("button", { type: "button", class: "bv-take", text: "Download" });
    bar.appendChild(close); bar.appendChild(title); bar.appendChild(dl);
    const body = el("div", { class: "doc-body" });
    body.appendChild(el("div", { class: "memv-empty", text: "Loading…" }));
    panel.appendChild(bar); panel.appendChild(body); modal.appendChild(panel);
    document.body.appendChild(modal);
    let url = "";
    const shut = () => { modal.remove(); if (url) URL.revokeObjectURL(url); document.removeEventListener("keydown", onKey); };
    const onKey = (e) => { if (e.key === "Escape") shut(); };
    document.addEventListener("keydown", onKey);
    close.addEventListener("click", shut);
    modal.addEventListener("click", (e) => { if (e.target === modal) shut(); });
    dl.addEventListener("click", () => downloadDoc(id, name));
    try {
      body.innerHTML = "";
      if ((mime || "").startsWith("image/") || mime === "application/pdf") {
        url = URL.createObjectURL(await fetchDoc(id));
        if (mime === "application/pdf") {
          const wrap = el("div", { class: "doc-pdf" });
          wrap.appendChild(el("iframe", { src: url, class: "doc-frame", title: name || "PDF" }));  // browser's own viewer
          const alt = el("div", { class: "doc-alt" });
          alt.appendChild(el("a", { href: url, target: "_blank", rel: "noopener", text: "Open in a new tab" }));
          const tx = el("button", { type: "button", text: "Show text" });
          tx.addEventListener("click", async () => {
            const r = await API.req("GET", "/v1/library/" + encodeURIComponent(id) + "/text");
            wrap.innerHTML = ""; wrap.appendChild(el("pre", { class: "doc-text", text: r.text || "(this PDF has no text layer)" }));
          });
          alt.appendChild(tx);
          wrap.appendChild(alt);
          body.appendChild(wrap);
        } else {
          body.appendChild(el("img", { src: url, alt: name || "image", class: "doc-img" }));
        }
      } else {
        const r = await API.req("GET", "/v1/library/" + encodeURIComponent(id) + "/text");
        body.appendChild(el("pre", { class: "doc-text", text: r.text || "(empty)" }));
      }
    } catch (e) { body.innerHTML = ""; body.appendChild(el("div", { class: "memv-empty", text: "Couldn't open: " + e.message })); }
  }

  function libraryView(root) {
    root.innerHTML = "";
    root.classList.add("memv");
    root.appendChild(el("div", { class: "memv-head", html: "<h1>Library</h1><p class='muted'>Your documents — uploads, files OpenMuse creates, and filled forms. Private to your account.</p>" }));
    const up = el("div", { class: "memv-upload lib-up" });
    const file = el("input", { type: "file", multiple: "", "aria-label": "Upload documents" });
    const go = el("button", { type: "button", class: "btn", text: "Upload" });
    up.appendChild(el("strong", { text: "Add files" }));
    up.appendChild(el("p", { class: "muted", text: "PDFs, text, Markdown, CSV, spreadsheets or images — up to 15 MB each." }));
    up.appendChild(file); up.appendChild(go);
    root.appendChild(up);
    const filters = el("div", { class: "memv-tabs", role: "tablist" });
    const list = el("div", {});
    root.appendChild(filters); root.appendChild(list);
    let docs = [], filter = "all";
    const KIND = (d) => d.mime === "application/pdf" ? "pdf" : d.mime.startsWith("image/") ? "image"
      : /sheet|csv/.test(d.mime) ? "sheet" : "text";
    [["all", "All"], ["pdf", "PDFs"], ["sheet", "Spreadsheets"], ["text", "Notes & text"], ["image", "Images"]].forEach(([k, v]) => {
      const b = el("button", { type: "button", role: "tab", "data-sec": k, text: v });
      b.addEventListener("click", () => { filter = k; draw(); });
      filters.appendChild(b);
    });
    go.addEventListener("click", async () => {
      if (!file.files.length) { toast("Choose a file first."); return; }
      go.disabled = true;
      for (const f of Array.from(file.files)) {
        try {
          const buf = new Uint8Array(await f.arrayBuffer());
          let bin = ""; for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
          await API.req("POST", "/v1/library", { body: { name: f.name, content_base64: btoa(bin) } });
          toast("Added " + f.name);
        } catch (e) { toast(f.name + ": " + ((e.body && e.body.error && e.body.error.message) || e.message)); }
      }
      go.disabled = false; file.value = ""; refresh();
    });
    const size = (n) => n > 1e6 ? (n / 1e6).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1e3)) + " KB";
    function draw() {
      $$("button", filters).forEach((b) => b.setAttribute("aria-selected", b.dataset.sec === filter));
      list.innerHTML = "";
      const rows = docs.filter((d) => filter === "all" || KIND(d) === filter);
      if (!rows.length) list.appendChild(el("div", { class: "memv-empty", text: docs.length ? "Nothing in this filter." : "No documents yet. Upload one, or ask OpenMuse to make a spreadsheet or PDF." }));
      rows.forEach((d) => {
        const c = el("div", { class: "memv-item lib-item" });
        const ic = el("div", { class: "mc-tool-ic app-ic" }); ic.innerHTML = DOC_ICON;
        const mid = el("div", { class: "lib-mid" });
        const name = el("button", { type: "button", class: "lib-name", text: d.name });
        name.addEventListener("click", () => previewDoc(d.artifact_id, d.name, d.mime));
        mid.appendChild(name);
        mid.appendChild(el("div", { class: "memv-meta", text: [d.source, size(d.size), new Date(d.created_at * 1000).toLocaleDateString([], { month: "short", day: "numeric" })].join(" · ") }));
        const acts = el("div", { class: "sched-acts lib-acts" });
        const mk = (label, fn, cls) => { const b = el("button", { type: "button", class: "btn small " + (cls || "secondary"), text: label }); b.addEventListener("click", fn); acts.appendChild(b); };
        mk("Open", () => previewDoc(d.artifact_id, d.name, d.mime));
        mk("Download", () => downloadDoc(d.artifact_id, d.name));
        mk("Add to memory", async () => {
          try { await API.req("POST", "/v1/library/" + d.artifact_id + "/memory", { body: {} }); toast("Added to memory — OpenMuse can search it now."); }
          catch (e) { toast("Couldn't add: " + ((e.body && e.body.error && e.body.error.message) || e.message)); }
        });
        mk("Delete", async () => { await API.req("DELETE", "/v1/library/" + d.artifact_id); refresh(); }, "danger");
        c.appendChild(ic); c.appendChild(mid); c.appendChild(acts);
        list.appendChild(c);
      });
    }
    async function refresh() {
      try { docs = (await API.req("GET", "/v1/library")).documents; } catch (e) { docs = []; }
      draw();
    }
    root._refresh = refresh; refresh();
  }



  /* -- ideas view ------------------------------------------------------------------- */


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

  /* -- ideas / goals / feed (issue #12): the signed-in user's own ---------- */
  const errMsg = (e) => (e.body && e.body.error && e.body.error.message) || e.message;

  function ideasView(root) {
    root.innerHTML = "";
    root.classList.add("memv");
    root.appendChild(el("div", { class: "memv-head", html: "<h1>Ideas</h1><p class='muted'>Suggestions OpenMuse noticed from what it knows about you — each shows why. Nothing happens until you say “Do it”.</p>" }));
    const bar = el("div", { class: "idea-bar" });
    const gen = el("button", { type: "button", class: "btn", text: "Suggest ideas now" });
    const add = el("input", { type: "text", placeholder: "…or jot down your own idea", "aria-label": "Add an idea" });
    const addBtn = el("button", { type: "button", class: "btn secondary", text: "Add" });
    bar.appendChild(gen); bar.appendChild(add); bar.appendChild(addBtn);
    root.appendChild(bar);
    const list = el("div", {});
    root.appendChild(list);
    gen.addEventListener("click", async () => {
      gen.disabled = true; gen.textContent = "Thinking…";
      try {
        const r = await API.req("POST", "/v1/ideas/generate", { body: {} });
        toast(r.ideas.length ? r.ideas.length + " new idea(s)." : "Nothing new to suggest right now.");
      } catch (e) { toast("Couldn't suggest: " + errMsg(e)); }
      gen.disabled = false; gen.textContent = "Suggest ideas now"; refresh();
    });
    addBtn.addEventListener("click", async () => {
      if (!add.value.trim()) return;
      try { await API.req("POST", "/v1/ideas", { body: { title: add.value.trim() } }); add.value = ""; refresh(); }
      catch (e) { toast(errMsg(e)); }
    });
    async function refresh() {
      list.innerHTML = "";
      let ideas = [];
      try { ideas = (await API.req("GET", "/v1/ideas")).ideas; } catch (e) { list.appendChild(el("div", { class: "memv-empty", text: "Ideas unavailable: " + errMsg(e) })); return; }
      if (!ideas.length) list.appendChild(el("div", { class: "memv-empty", text: "No ideas yet. The more OpenMuse knows (chats, goals, connected apps), the better its suggestions." }));
      ideas.forEach((i) => {
        const c = el("div", { class: "memv-item idea-card" });
        const top = el("div", { class: "memv-item-top" }, [el("span", { class: "memv-badge", text: i.source === "user" ? "Your idea" : "Suggested" })]);
        if (i.from_email) top.appendChild(el("span", { class: "memv-badge warn", text: "from email" }));
        c.appendChild(top);
        c.appendChild(el("div", { class: "memv-pname", text: i.title }));
        if (i.rationale) c.appendChild(el("div", { class: "memv-text", text: i.rationale }));
        if ((i.evidence || []).length) {
          const ev = el("div", { class: "idea-ev" });
          ev.appendChild(el("span", { class: "memv-meta", text: "Because:" }));
          i.evidence.forEach((e) => ev.appendChild(el("span", { class: "memv-badge soft", title: e.kind, text: e.label })));
          c.appendChild(ev);
        }
        if (i.from_email || (i.action_prompt && i.action_prompt !== i.title)) {
          c.appendChild(el("div", { class: "memv-meta idea-plan", text: "If you say yes, OpenMuse will: " + i.action_prompt }));
        }
        const acts = el("div", { class: "sched-acts" });
        const doit = el("button", { type: "button", class: "btn small", text: "Do it" });
        doit.addEventListener("click", async () => {
          doit.disabled = true;
          try { const r = await API.req("POST", "/v1/ideas/" + i.idea_id + "/accept", { body: {} }); toast("On it."); if (state.openChat) state.openChat(r.chat_id); }
          catch (e) { toast(errMsg(e)); doit.disabled = false; }
        });
        acts.appendChild(doit);
        [["Tomorrow", 1], ["Next week", 7]].forEach(([label, days]) => {
          const b = el("button", { type: "button", class: "btn small secondary", text: "Snooze · " + label });
          b.addEventListener("click", async () => { await API.req("POST", "/v1/ideas/" + i.idea_id + "/snooze", { body: { days } }); refresh(); });
          acts.appendChild(b);
        });
        const dis = el("button", { type: "button", class: "btn small secondary", text: "Not for me" });
        dis.addEventListener("click", () => {
          acts.innerHTML = "";
          const why = el("input", { type: "text", placeholder: "Why not? (optional — helps future ideas)", "aria-label": "Reason" });
          const ok = el("button", { type: "button", class: "btn small danger", text: "Dismiss" });
          ok.addEventListener("click", async () => { await API.req("POST", "/v1/ideas/" + i.idea_id + "/dismiss", { body: { reason: why.value } }); toast("Got it — I won't suggest that again."); refresh(); });
          acts.appendChild(why); acts.appendChild(ok); why.focus();
        });
        acts.appendChild(dis);
        c.appendChild(acts);
        list.appendChild(c);
      });
    }
    root._refresh = refresh; refresh();
  }

  function ring(done, total) {
    const pct = total ? done / total : 0, r = 18, c = 2 * Math.PI * r;
    return '<svg class="ring" viewBox="0 0 44 44" aria-hidden="true"><circle cx="22" cy="22" r="' + r + '" fill="none" stroke="currentColor" stroke-opacity=".15" stroke-width="5"/>' +
      '<circle cx="22" cy="22" r="' + r + '" fill="none" stroke="currentColor" stroke-width="5" stroke-linecap="round" stroke-dasharray="' + (c * pct).toFixed(1) + " " + c.toFixed(1) + '" transform="rotate(-90 22 22)"/></svg>';
  }

  function goalsView(root) {
    root.innerHTML = "";
    root.classList.add("memv");
    root.appendChild(el("div", { class: "memv-head", html: "<h1>Goals</h1><p class='muted'>What you're working toward. Tell OpenMuse a goal in chat and it drafts a plan with milestones; it updates progress as you go.</p>" }));
    const form = el("form", { class: "memv-upload" });
    form.innerHTML = "<strong>New goal</strong>";
    const title = el("input", { type: "text", placeholder: "e.g. Run a half marathon", "aria-label": "Goal", required: "" });
    const date = el("input", { type: "date", "aria-label": "Target date" });
    const ms = el("textarea", { rows: "3", placeholder: "Milestones, one per line (optional)", "aria-label": "Milestones" });
    const go = el("button", { type: "submit", class: "btn", text: "Add goal" });
    [title, date, ms, go].forEach((n) => form.appendChild(n));
    root.appendChild(form);
    const list = el("div", {});
    root.appendChild(list);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await API.req("POST", "/v1/goals", { body: { title: title.value, target_date: date.value,
          milestones: ms.value.split("\n").map((s) => s.trim()).filter(Boolean) } });
        form.reset(); refresh();
      } catch (err) { toast(errMsg(err)); }
    });
    const patchGoal = (id, body) => API.req("PATCH", "/v1/goals/" + id, { body }).then(refresh).catch((e) => toast(errMsg(e)));
    async function refresh() {
      list.innerHTML = "";
      let goals = [];
      try { goals = (await API.req("GET", "/v1/goals")).goals; } catch (e) { list.appendChild(el("div", { class: "memv-empty", text: "Goals unavailable: " + errMsg(e) })); return; }
      const shown = goals.filter((g) => g.status !== "archived");
      if (!shown.length) list.appendChild(el("div", { class: "memv-empty", text: "No goals yet." }));
      shown.forEach((g) => {
        const done = g.milestones.filter((m) => m.done).length;
        const c = el("div", { class: "memv-item goal-card" + (g.status === "done" ? " old" : "") });
        const head = el("div", { class: "goal-head" });
        const rg = el("div", { class: "goal-ring" }); rg.innerHTML = ring(done, g.milestones.length);
        rg.appendChild(el("span", { text: g.milestones.length ? done + "/" + g.milestones.length : "—" }));
        const meta = el("div", {});
        meta.appendChild(el("div", { class: "memv-pname", text: g.title }));
        meta.appendChild(el("div", { class: "memv-meta", text: [g.status !== "active" ? g.status : "", g.target_date ? "by " + g.target_date : "",
          g.activity.length ? "updated " + new Date(g.updated_at * 1000).toLocaleDateString([], { month: "short", day: "numeric" }) : ""].filter(Boolean).join(" · ") }));
        head.appendChild(rg); head.appendChild(meta);
        c.appendChild(head);
        if (g.milestones.length) {
          const ul = el("ul", { class: "goal-ms" });
          g.milestones.forEach((m) => {
            const li = el("li", { class: m.done ? "done" : "" });
            const cb = el("input", { type: "checkbox", "aria-label": m.title });
            cb.checked = m.done; cb.disabled = m.done;
            cb.addEventListener("change", () => patchGoal(g.goal_id, { complete_milestone: m.id }));
            li.appendChild(cb); li.appendChild(el("span", { text: m.title + (m.due ? " · " + m.due : "") }));
            ul.appendChild(li);
          });
          c.appendChild(ul);
        }
        const last = g.activity[g.activity.length - 1];
        if (last && last.text !== "Goal created") c.appendChild(el("div", { class: "memv-meta", text: "Latest: " + last.text }));
        const acts = el("div", { class: "sched-acts" });
        const mk = (label, fn, cls) => { const b = el("button", { type: "button", class: "btn small " + (cls || "secondary"), text: label }); b.addEventListener("click", fn); acts.appendChild(b); };
        mk("Add note", () => {
          const inp = el("input", { type: "text", placeholder: "Progress note", "aria-label": "Progress note" });
          const ok = el("button", { type: "button", class: "btn small", text: "Save" });
          ok.addEventListener("click", () => inp.value.trim() && patchGoal(g.goal_id, { note: inp.value.trim() }));
          acts.innerHTML = ""; acts.appendChild(inp); acts.appendChild(ok); inp.focus();
        });
        if (g.status === "active") mk("Mark done", () => patchGoal(g.goal_id, { status: "done" }));
        mk(g.status === "paused" ? "Resume" : "Pause", () => patchGoal(g.goal_id, { status: g.status === "paused" ? "active" : "paused" }));
        mk("Archive", () => patchGoal(g.goal_id, { status: "archived" }), "danger");
        c.appendChild(acts);
        list.appendChild(c);
      });
    }
    root._refresh = refresh; refresh();
  }

  function feedView(root) {
    root.innerHTML = "";
    root.classList.add("memv");
    root.appendChild(el("div", { class: "memv-head", html: "<h1>Feed</h1><p class='muted'>Updates OpenMuse posted for you — results worth keeping from background work.</p>" }));
    const list = el("div", {});
    root.appendChild(list);
    async function refresh() {
      list.innerHTML = "";
      let items = [];
      try { items = (await API.req("GET", "/v1/feed")).items; } catch (e) { list.appendChild(el("div", { class: "memv-empty", text: "Feed unavailable: " + errMsg(e) })); return; }
      if (!items.length) list.appendChild(el("div", { class: "memv-empty", text: "Nothing in your feed yet." }));
      items.forEach((f) => {
        const c = el("div", { class: "memv-item" });
        c.appendChild(el("div", { class: "memv-item-top" }, [el("span", { class: "memv-badge soft", text: f.source }),
          el("span", { class: "memv-badge soft", text: new Date(f.created_at * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) })]));
        c.appendChild(el("div", { class: "memv-pname", text: f.title }));
        if (f.body) c.appendChild(el("div", { class: "memv-text", text: f.body }));
        const acts = el("div", { class: "sched-acts" });
        if (f.url) acts.appendChild(el("a", { class: "btn small secondary", href: f.url, target: "_blank", rel: "noopener noreferrer", text: "Open" }));
        const d = el("button", { type: "button", class: "btn small secondary", text: "Dismiss" });
        d.addEventListener("click", async () => { await API.req("POST", "/v1/feed/" + f.item_id + "/dismiss", { body: {} }); refresh(); });
        acts.appendChild(d);
        c.appendChild(acts);
        list.appendChild(c);
      });
    }
    root._refresh = refresh; refresh();
  }

  /* -- saved logins view (issue #16) -------------------------------------------- */
  function loginsView(root) {
    root.innerHTML = "";
    root.classList.add("memv");
    root.appendChild(el("div", { class: "memv-head", html: "<h1>Saved logins</h1><p class='muted'>Sign-ins OpenMuse can use in its browser. It asks you before each sign-in, only fills them on the matching site, and never sees the password — it's encrypted and entered straight into the page.</p>" }));
    const form = el("form", { class: "memv-upload", autocomplete: "off" });
    form.innerHTML = "<strong>Add a login</strong>";
    const site = el("input", { type: "text", placeholder: "Site, e.g. https://www.united.com", "aria-label": "Site", required: "" });
    const user = el("input", { type: "text", placeholder: "Username or email", "aria-label": "Username", required: "", autocomplete: "off" });
    const pw = el("input", { type: "password", placeholder: "Password", "aria-label": "Password", required: "", autocomplete: "new-password" });
    const add = el("button", { type: "submit", class: "btn", text: "Save login" });
    [site, user, pw, add].forEach((n) => form.appendChild(n));
    root.appendChild(form);
    const list = el("div", {});
    root.appendChild(list);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      add.disabled = true;
      try {
        const r = await API.req("POST", "/v1/logins", { body: { site: site.value, username: user.value, password: pw.value } });
        toast("Saved your " + hostOf(r.login.site) + " login.");
        form.reset(); refresh();
      } catch (err) { toast("Couldn't save: " + ((err.body && err.body.error && err.body.error.message) || err.message)); }
      finally { pw.value = ""; add.disabled = false; }
    });
    async function refresh() {
      list.innerHTML = "";
      let res;
      try { res = await API.req("GET", "/v1/logins"); }
      catch (e) { list.appendChild(el("div", { class: "memv-empty", text: "Saved logins unavailable: " + e.message })); return; }
      if (!res.logins.length) list.appendChild(el("div", { class: "memv-empty", text: "No saved logins. Without one, OpenMuse pauses and lets you sign in yourself from the live browser." }));
      res.logins.forEach((lg) => {
        const c = el("div", { class: "memv-item" });
        c.appendChild(el("div", { class: "memv-pname", text: hostOf(lg.site) }));
        c.appendChild(el("div", { class: "memv-meta", text: lg.username + " · ●●●●●●●● · " +
          (lg.last_used ? "last used " + fmtTime(lg.last_used * 1000) : "never used") }));
        const acts = el("div", { class: "sched-acts" });
        const del = el("button", { type: "button", class: "btn small danger", text: "Delete" });
        del.addEventListener("click", async () => { await API.req("DELETE", "/v1/logins/" + lg.login_id); refresh(); });
        acts.appendChild(del);
        c.appendChild(acts);
        list.appendChild(c);
      });
    }
    root._refresh = refresh; refresh();
  }

  /* -- monitors view (issue #11) --------------------------------------------- */
  function sparkline(points) {
    const vals = (points || []).map((p) => p[1]).filter((v) => typeof v === "number");
    if (vals.length < 2) return "";
    const w = 120, h = 32, min = Math.min(...vals), max = Math.max(...vals), span = max - min || 1;
    const pts = vals.map((v, i) => (i * w / (vals.length - 1)).toFixed(1) + "," + (h - 3 - (v - min) * (h - 6) / span).toFixed(1)).join(" ");
    return '<svg class="spark" viewBox="0 0 ' + w + " " + h + '" aria-hidden="true"><polyline points="' + pts + '" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>';
  }
  function monitorsView(root) {
    root.innerHTML = "";
    root.classList.add("memv");
    root.appendChild(el("div", { class: "memv-head", html: "<h1>Monitors</h1><p class='muted'>Pages OpenMuse watches for you — price drops, restocks, or any change. You'll get a notification when something happens.</p>" }));
    const form = el("form", { class: "memv-upload" });
    form.innerHTML = "<strong>New monitor</strong>";
    const url = el("input", { type: "url", placeholder: "https://… page to watch", "aria-label": "Page URL", required: "" });
    const row = el("div", { class: "sched-when" });
    const kind = el("select", { "aria-label": "Condition" });
    [["price_below", "Price drops below"], ["price_above", "Price rises above"], ["text_appears", "Text appears"],
     ["text_disappears", "Text disappears"], ["changed", "Anything changes"]].forEach(([v, t]) => kind.appendChild(el("option", { value: v, text: t })));
    const target = el("input", { type: "text", placeholder: "e.g. 199.99", "aria-label": "Target" });
    const every = el("select", { "aria-label": "Check every" });
    [[15, "every 15 min"], [60, "every hour"], [360, "every 6 hours"], [1440, "every day"]].forEach(([v, t]) => every.appendChild(el("option", { value: String(v), text: t })));
    every.value = "60";
    [kind, target, every].forEach((n) => row.appendChild(n));
    const add = el("button", { type: "submit", class: "btn", text: "Start watching" });
    [url, row, add].forEach((n) => form.appendChild(n));
    kind.addEventListener("change", () => {
      target.hidden = kind.value === "changed";
      target.placeholder = kind.value.startsWith("price") ? "e.g. 199.99" : "e.g. In stock";
    });
    root.appendChild(form);
    const list = el("div", {});
    root.appendChild(list);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      add.disabled = true;
      try {
        const r = await API.req("POST", "/v1/monitors", { body: { url: url.value, kind: kind.value, target: target.value, every_minutes: parseInt(every.value, 10) } });
        toast("Watching " + r.monitor.name + ".");
        form.reset(); every.value = "60"; refresh();
      } catch (err) { toast("Couldn't start: " + ((err.body && err.body.error && err.body.error.message) || err.message)); }
      finally { add.disabled = false; }
    });
    const LABEL = { price_below: "under ", price_above: "over ", text_appears: "shows “", text_disappears: "loses “", changed: "any change" };
    async function refresh() {
      list.innerHTML = "";
      let res;
      try { res = await API.req("GET", "/v1/monitors"); }
      catch (e) { list.appendChild(el("div", { class: "memv-empty", text: "Monitors unavailable: " + e.message })); return; }
      if (!res.monitors.length) list.appendChild(el("div", { class: "memv-empty", text: "Nothing watched yet. Add a page above, or ask in chat: “tell me when these headphones drop under $150”." }));
      res.monitors.forEach((m) => {
        const c = el("div", { class: "memv-item mon-item" + (m.active ? "" : " old") });
        const cond = LABEL[m.kind] + (m.kind === "changed" ? "" :
          (m.kind.startsWith("price") ? (m.currency || "$") : "") + m.target + (m.kind.startsWith("text") ? "”" : ""));
        const top = el("div", { class: "memv-item-top" }, [el("span", { class: "memv-badge" + (m.condition_met ? " ok" : ""), text: m.condition_met ? "Met" : "Watching" }),
          el("span", { class: "memv-badge soft", text: cond }), el("span", { class: "memv-badge soft", text: "every " + (m.every_minutes >= 60 ? (m.every_minutes / 60) + "h" : m.every_minutes + "m") })]);
        if (!m.active) top.appendChild(el("span", { class: "memv-badge warn", text: "paused" }));
        if (m.failures) top.appendChild(el("span", { class: "memv-badge warn", text: m.failures + " failed check(s)" }));
        c.appendChild(top);
        const line = el("div", { class: "mon-line" });
        const left = el("div", {});
        left.appendChild(el("div", { class: "memv-pname", text: m.name }));
        const a = el("a", { href: m.url, target: "_blank", rel: "noopener noreferrer", class: "memv-meta", text: hostOf(m.url) });
        left.appendChild(a);
        line.appendChild(left);
        const right = el("div", { class: "mon-val" });
        right.innerHTML = sparkline(m.history);
        right.appendChild(el("strong", { text: typeof m.last_value === "number" ? (m.currency || "$") + m.last_value.toFixed(2) : (m.last_value || "—") }));
        line.appendChild(right);
        c.appendChild(line);
        if (m.last_checked) c.appendChild(el("div", { class: "memv-meta", text: "Checked " + fmtTime(m.last_checked * 1000) + (m.last_error ? " · " + m.last_error : "") }));
        const acts = el("div", { class: "sched-acts" });
        const mk = (label, fn, cls) => { const b = el("button", { type: "button", class: "btn small " + (cls || "secondary"), text: label }); b.addEventListener("click", fn); acts.appendChild(b); };
        mk("Check now", async () => { await API.req("POST", "/v1/monitors/" + m.monitor_id + "/check", { body: {} }); refresh(); });
        mk(m.active ? "Pause" : "Resume", async () => { await API.req("PATCH", "/v1/monitors/" + m.monitor_id, { body: { active: !m.active } }); refresh(); });
        mk("Delete", async () => { await API.req("DELETE", "/v1/monitors/" + m.monitor_id); refresh(); }, "danger");
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

  /* -- voice mode (issue #19): talk to OpenMuse, it talks back ------------------
     Mic -> energy VAD (works with echo cancellation, so it can hear you over its
     own voice) -> 16 kHz WAV -> server STT (NVIDIA Riva) or the browser's Web
     Speech -> the normal chat send (mode "voice": short spoken answers) ->
     sentence-chunked TTS, played in order. Speaking while it talks stops the
     audio at once (barge-in) and captures what you say next. */
  const VoiceMode = {
    ui: null, cfg: null, state: "off", ctx: null, stream: null, proc: null,
    frames: [], preroll: [], speechMs: 0, silenceMs: 0, heardAt: 0, noise: 0.004, loudRun: 0,
    queue: [], playing: null, ttsAbort: null, pendingText: "", spoken: "", runId: null,
    confirm: null, sr: null, stats: { turns: 0, latencies: [], bargeIns: [] },
    YES: /^(yes|yeah|yep|yup|sure|ok(ay)?|go ahead|do it|please do|confirm|approve|sounds good)\b/i,
    NO: /^(no|nope|don'?t|stop|cancel|deny|never ?mind|wait)\b/i,

    async open() {
      if (this.ui) return;
      if (!this.cfg) { try { this.cfg = await API.req("GET", "/v1/voice/config"); } catch (e) { this.cfg = { stt: null, tts: null }; } }
      const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!this.cfg.stt && !SR) { toast("Voice needs a microphone-capable browser (or server speech set up)."); return; }
      showTab("chat");
      const ui = el("div", { class: "vm", role: "dialog", "aria-label": "Voice mode" });
      ui.innerHTML = '<button type="button" class="vm-x" aria-label="End voice mode">✕</button>' +
        '<button type="button" class="vm-orb" aria-label="Tap to talk or interrupt"><span class="vm-ring r1"></span><span class="vm-ring r2"></span><span class="vm-core"></span></button>' +
        '<div class="vm-state" aria-live="polite"></div><div class="vm-you"></div><div class="vm-reply"></div>' +
        '<div class="vm-acts"></div><div class="vm-lat"></div>';
      document.body.appendChild(ui);
      this.ui = ui;
      ui.querySelector(".vm-x").addEventListener("click", () => this.close());
      ui.querySelector(".vm-orb").addEventListener("click", () => this.tap());
      document.addEventListener("keydown", this._esc = (e) => { if (e.key === "Escape") this.close(); });
      state.voiceTap = (ev, run) => this.onEvent(ev, run);
      try {
        this.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
      } catch (e) { this.say("I can't hear you — allow the microphone for this site."); this.set("off", "Microphone blocked"); return; }
      const AC = window.AudioContext || window.webkitAudioContext;
      this.ctx = new AC();
      const src = this.ctx.createMediaStreamSource(this.stream);
      this.proc = this.ctx.createScriptProcessor(2048, 1, 1);
      this.proc.onaudioprocess = (e) => this.onAudio(e.inputBuffer.getChannelData(0));
      const mute = this.ctx.createGain(); mute.gain.value = 0;
      src.connect(this.proc); this.proc.connect(mute); mute.connect(this.ctx.destination);
      this.listen();
    },

    close() {
      if (!this.ui) return;
      this.stopAudio();
      if (this.sr) { try { this.sr.abort(); } catch (e) { /* ignore */ } this.sr = null; }
      if (this.proc) this.proc.disconnect();
      if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
      if (this.ctx) this.ctx.close().catch(() => {});
      document.removeEventListener("keydown", this._esc);
      this.ui.remove();
      Object.assign(this, { ui: null, ctx: null, stream: null, proc: null, state: "off", confirm: null, runId: null });
      state.voiceTap = null;
    },

    set(st, label) {
      this.state = st;
      if (!this.ui) return;
      this.ui.dataset.state = st;
      this.ui.querySelector(".vm-state").textContent = label != null ? label :
        ({ listening: "Listening…", hearing: "Listening…", thinking: "Thinking…", speaking: "Speaking — talk to interrupt",
           idle: "Tap to talk", off: "" })[st] || "";
    },

    listen() {
      this.frames = []; this.speechMs = 0; this.silenceMs = 0; this.loudRun = 0; this.heardAt = 0;
      if (this.spec) { this.spec.abort.abort(); this.spec = null; }
      this.listenStarted = performance.now();
      this.set("listening");
      if (!this.cfg.stt) this.startWebSpeech();
    },

    tap() {
      if (this.state === "speaking" || this.state === "thinking") { this.interrupt("tap"); return; }
      if (this.state === "hearing") { this.endUtterance(); return; }
      if (this.state === "idle" || this.state === "off") this.listen();
    },

    // -- capture + VAD --------------------------------------------------------------
    onAudio(buf) {
      let sum = 0;
      for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
      const rms = Math.sqrt(sum / buf.length);
      const frameMs = buf.length / this.ctx.sampleRate * 1000;
      const speaking = this.state === "speaking";
      const thr = Math.max(0.012, this.noise * 3) * (speaking ? 2.2 : 1);   // its own (echo-cancelled) voice needs a higher bar
      const loud = rms > thr;
      if (!loud && this.state !== "hearing") this.noise = this.noise * 0.95 + rms * 0.05;
      if (this.ui) this.ui.style.setProperty("--lvl", Math.min(1, rms * 12).toFixed(3));
      const copy = new Float32Array(buf);
      if (this.state === "listening" || this.state === "speaking" || this.state === "thinking" || this.state === "idle") {
        this.preroll.push(copy); if (this.preroll.length > 8) this.preroll.shift();       // ~350 ms before speech
        this.loudRun = loud ? this.loudRun + frameMs : 0;
        if (this.loudRun >= 120) {                                                       // ~3 frames of voice
          if (speaking || this.state === "thinking") this.interrupt("voice");
          if (this.state !== "listening" && this.state !== "idle") return;
          this.frames = this.preroll.slice(); this.preroll = [];
          this.speechMs = this.loudRun; this.silenceMs = 0; this.heardAt = this.lastLoudAt = performance.now();
          this.set("hearing");
        } else if (this.state === "listening" && performance.now() - this.listenStarted > 12000) {
          this.set("idle");                                                             // nobody spoke: stop waiting
        }
        return;
      }
      if (this.state === "hearing") {
        this.frames.push(copy);
        if (loud) {
          this.speechMs += frameMs; this.silenceMs = 0; this.lastLoudAt = performance.now();
          if (this.spec) { this.spec.abort.abort(); this.spec = null; }   // still talking: drop the early guess
        } else this.silenceMs += frameMs;
        // speculative transcription: start ASR on a short pause; if they keep
        // talking it's discarded, if not the text is (nearly) ready at end of turn
        if (!this.spec && this.cfg.stt && this.silenceMs >= 350 && this.speechMs >= 250) this.spec = this.transcribe();
        if (this.silenceMs >= 600 || this.speechMs > 30000) this.endUtterance();
      }
    },

    wav16k() {
      const rate = this.ctx.sampleRate, ratio = rate / 16000;
      const total = this.frames.reduce((n, f) => n + f.length, 0);
      const flat = new Float32Array(total); let o = 0;
      this.frames.forEach((f) => { flat.set(f, o); o += f.length; });
      const n = Math.floor(total / ratio), pcm = new Int16Array(n);
      for (let i = 0; i < n; i++) {
        const a = Math.floor(i * ratio), b = Math.min(total, Math.floor((i + 1) * ratio));
        let s = 0; for (let j = a; j < b; j++) s += flat[j];
        const v = Math.max(-1, Math.min(1, s / Math.max(1, b - a)));
        pcm[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
      }
      const out = new DataView(new ArrayBuffer(44 + pcm.length * 2));
      const w = (off, s) => { for (let i = 0; i < s.length; i++) out.setUint8(off + i, s.charCodeAt(i)); };
      w(0, "RIFF"); out.setUint32(4, 36 + pcm.length * 2, true); w(8, "WAVE"); w(12, "fmt ");
      out.setUint32(16, 16, true); out.setUint16(20, 1, true); out.setUint16(22, 1, true);
      out.setUint32(24, 16000, true); out.setUint32(28, 32000, true); out.setUint16(32, 2, true); out.setUint16(34, 16, true);
      w(36, "data"); out.setUint32(40, pcm.length * 2, true);
      for (let i = 0; i < pcm.length; i++) out.setInt16(44 + i * 2, pcm[i], true);
      return new Uint8Array(out.buffer);
    },

    async endUtterance() {
      if (this.state !== "hearing") return;
      this.endedAt = this.lastLoudAt || performance.now();   // latency counts from the end of speech, not the VAD's hangover
      if (!this.cfg.stt) { if (this.sr) this.sr.stop(); this.set("thinking"); return; }   // Web Speech delivers the text
      if (this.speechMs < 250) { this.spec = null; this.listen(); return; }            // a cough, not a request
      this.set("thinking", "Got it…");
      const job = this.spec || this.transcribe();
      this.spec = null; this.frames = [];
      try { this.heard((await job.result).text || ""); }
      catch (e) { if (this.ui) this.say("Sorry, I didn't catch that."); }
    },

    transcribe() {
      const bytes = this.wav16k();
      let bin = ""; for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
      const abort = new AbortController();
      const h = { "Content-Type": "application/json" };
      if (API.store.apiKey) h["Authorization"] = "Bearer " + API.store.apiKey;
      const result = fetch("/v1/voice/transcribe", { method: "POST", headers: h, body: JSON.stringify({ audio_base64: btoa(bin) }), signal: abort.signal })
        .then((r) => { if (!r.ok) throw new Error("stt " + r.status); return r.json(); });
      result.catch(() => {});                                  // an aborted guess isn't an error
      return { abort, result };
    },

    startWebSpeech() {
      const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SR || this.sr) return;
      const sr = new SR(); sr.lang = navigator.language || "en-US"; sr.interimResults = true; sr.continuous = false;
      let finalText = "";
      sr.onresult = (e) => {
        const t = Array.from(e.results).map((r) => r[0].transcript).join("");
        if (this.ui) this.ui.querySelector(".vm-you").textContent = t;
        if (e.results[e.results.length - 1].isFinal) finalText = t;
      };
      sr.onend = () => { this.sr = null; if (!this.endedAt) this.endedAt = performance.now(); if (finalText) this.heard(finalText); else if (this.state !== "off" && this.state !== "speaking") this.set("idle"); };
      this.sr = sr; this.endedAt = 0;
      try { sr.start(); } catch (e) { this.sr = null; }
    },

    // -- a finished utterance -----------------------------------------------------------
    async heard(text) {
      text = (text || "").trim();
      if (!this.ui) return;
      if (!text) { this.listen(); return; }
      this.ui.querySelector(".vm-you").textContent = text;
      this.ui.querySelector(".vm-reply").textContent = "";
      this.ui.querySelector(".vm-acts").innerHTML = "";
      if (this.confirm) {                                   // answering "should I go ahead?"
        const card = this.confirm; this.confirm = null;
        if (this.YES.test(text) || this.NO.test(text)) {
          const yes = this.YES.test(text);
          try {
            await API.decideApproval(card, yes ? "approve" : "deny", { channel: "voice" });
            this.say(yes ? "Okay, doing it." : "Okay, I won't.", true);
          } catch (e) { this.say("I need you to confirm that on screen."); this.showReview(); }
          return;
        }
      }
      this.set("thinking");
      this.pendingText = ""; this.spoken = ""; this.firstAudioLogged = false; this.stepNo = null; this.stepText = "";
      try { this.runId = await state.sendChat(text, { mode: "voice" }); }
      catch (e) { this.say("Something went wrong sending that."); }
    },

    onEvent(ev, run) {
      if (!this.ui || !run || run.runId !== this.runId) return;
      const d = ev.data || {};
      if (ev.name === "tool.call" && /^web\.(search|read|weather)$/.test(d.tool || "") && !this.spoken && !this.playing) {
        this.enqueue(d.tool === "web.weather" ? "Checking the weather." : "Let me look that up.");
        this.spoken = "";      // don't treat the filler as part of the answer
        this.stepText = "";
        return;
      }
      if (ev.name === "approval.required") {
        API.getApproval(d.approval_id).then((card) => {
          const desc = state.describeApproval ? state.describeApproval(card) : { title: "OpenMuse needs your OK" };
          const what = desc.title.replace(/^OpenMuse wants to /, "");
          if (["R0", "R1", "R2"].includes(card.risk)) {
            this.confirm = card;
            this.say("Should I " + what + "?", true);
          } else {
            this.say("I need your OK on screen to " + what + ".");
            this.showReview();
          }
        }).catch(() => {});
      } else if (ev.name === "assistant.partial") {           // live tokens (voice runs stream)
        if (d.reset) { if (d.step === this.stepNo) this.stepText = ""; return; }
        if (d.step !== this.stepNo) { this.stepNo = d.step; this.stepText = ""; }
        this.stepText += d.text || "";
        this.feed(d.text || "", false);
      } else if (ev.name === "assistant.delta") {             // the final answer, all at once
        const full = d.text || d.delta || "";
        if (this.stepText && full.startsWith(this.stepText)) this.feed(full.slice(this.stepText.length), false);
        else if (!this.stepText) this.feed(full, false);
      } else if (ev.name === "run.completed") {
        this.feed("", true);
      } else if (ev.name === "run.failed") {
        this.say("Sorry — that didn't work.");
      } else if (ev.name === "run.finished") {
        if (this.state === "thinking" && !this.queue.length && !this.playing && !this.confirm) this.listen();
      }
    },

    // -- speaking ------------------------------------------------------------------------
    feed(delta, final) {
      this.pendingText += delta;
      const parts = [];
      let m;
      // mid-stream a sentence ends only once whitespace follows ("3." might become "3.5")
      const re = final ? /[^.!?]*[.!?]+(?:["')\]]*)(?=\s|$)/g : /[^.!?]*[.!?]+(?:["')\]]*)(?=\s)/g;
      let last = 0;
      while ((m = re.exec(this.pendingText))) { parts.push(m[0]); last = re.lastIndex; }
      let rest = this.pendingText.slice(last);
      if (final && rest.trim()) { parts.push(rest); rest = ""; }
      // the first thing said should be short (speech time grows with length): break a long
      // opening sentence at a clause ("Buddy is a classic name, | friendly and easy to call")
      if (!this.spoken) {
        const head = parts.length ? parts[0] : rest;
        if (head.length > 70) {
          const m = /^(.{12,80}?[,;:—–])\s/.exec(head);
          if (m) {
            if (parts.length) parts.splice(0, 1, m[1], head.slice(m[0].length));
            else { parts.unshift(m[1]); rest = rest.slice(m[0].length); }
          }
        }
      }
      this.pendingText = rest;
      // merge tiny fragments so each TTS call carries a natural phrase
      const chunks = [];
      parts.forEach((p, idx) => {
        const opener = !this.spoken && idx === 1 && chunks.length === 1;   // keep the quick first clause on its own
        if (chunks.length && chunks[chunks.length - 1].length < 40 && !opener) chunks[chunks.length - 1] += p; else chunks.push(p);
      });
      chunks.forEach((c) => { if (c.trim()) this.enqueue(c.trim()); });
      if (this.ui) this.ui.querySelector(".vm-reply").textContent = (this.spoken + this.pendingText).trim();
    },

    say(text, thenListen) {
      this.stopAudio();
      this.pendingText = ""; this.spoken = "";
      this.enqueue(text, thenListen !== false);
      if (this.ui) this.ui.querySelector(".vm-reply").textContent = text;
    },

    enqueue(text) {
      this.spoken += (this.spoken ? " " : "") + text;
      if (!this.ttsAbort) this.ttsAbort = new AbortController();
      const signal = this.ttsAbort.signal;
      const item = { text };
      if (this.cfg.tts) {
        const h = { "Content-Type": "application/json" };
        if (API.store.apiKey) h["Authorization"] = "Bearer " + API.store.apiKey;
        item.audio = fetch("/v1/voice/speak", { method: "POST", headers: h, body: JSON.stringify({ text }), signal })
          .then((r) => r.ok ? r.blob() : null).catch(() => null);
      }
      this.queue.push(item);
      if (!this.playing) this.playNext();
    },

    async playNext() {
      const item = this.queue.shift();
      if (item) (this.spokenOrder = this.spokenOrder || []).push(item.text);   // what was played, in order
      if (!item) {
        this.playing = null;
        if (this.ui && this.state === "speaking") this.listen();
        return;
      }
      this.playing = item;
      const started = () => {
        if (this.state !== "speaking") this.set("speaking");
        if (!this.firstAudioLogged && this.endedAt) {
          this.firstAudioLogged = true;
          const ms = Math.round(performance.now() - this.endedAt);
          this.stats.latencies.push(ms); this.stats.turns++;
          if (this.ui) this.ui.querySelector(".vm-lat").textContent = (ms / 1000).toFixed(1) + "s";
        }
      };
      const blob = item.audio ? await item.audio : null;
      if (this.playing !== item) return;                                   // interrupted while fetching
      if (blob) {
        const a = new Audio(URL.createObjectURL(blob));
        item.el = a;
        a.addEventListener("playing", started, { once: true });
        a.addEventListener("ended", () => { URL.revokeObjectURL(a.src); if (this.playing === item) this.playNext(); });
        a.addEventListener("error", () => { if (this.playing === item) this.playNext(); });
        a.play().catch(() => { if (this.playing === item) this.playNext(); });
      } else if (window.speechSynthesis) {
        const u = new SpeechSynthesisUtterance(item.text);
        u.onstart = started;
        u.onend = u.onerror = () => { if (this.playing === item) this.playNext(); };
        item.utter = u;
        speechSynthesis.speak(u);
      } else this.playNext();
    },

    stopAudio() {
      if (this.ttsAbort) { this.ttsAbort.abort(); this.ttsAbort = null; }
      const p = this.playing; this.playing = null; this.queue = [];
      if (p && p.el) { p.el.pause(); p.el.src = ""; }
      if (window.speechSynthesis) speechSynthesis.cancel();
    },

    interrupt(how) {
      const t0 = performance.now();
      const wasSpeaking = this.state === "speaking";
      this.stopAudio();
      this.pendingText = ""; this.spoken = "";
      if (wasSpeaking) this.stats.bargeIns.push({ how, stopMs: Math.round(performance.now() - t0), at: Date.now() });
      this.runId = null;                        // ignore the rest of that answer; the next utterance takes over
      this.listen();
    },

    showReview() {
      if (!this.ui) return;
      const acts = this.ui.querySelector(".vm-acts");
      acts.innerHTML = "";
      const b = el("button", { type: "button", class: "btn", text: "Review on screen" });
      b.addEventListener("click", () => this.close());
      acts.appendChild(b);
    },
  };
  window.__omVoice = VoiceMode;

  /* -- installed app (issue #17): service worker, web push, share target ------ */
  const Push = {
    reg: null, cfg: null, sub: null,
    supported() { return "serviceWorker" in navigator && "PushManager" in window && window.Notification && window.isSecureContext; },
    async init() {
      if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
      try { this.reg = await navigator.serviceWorker.register("/sw.js", { scope: "/" }); } catch (e) { return; }
      navigator.serviceWorker.addEventListener("message", (e) => {
        if (e.data && e.data.type === "open-note" && e.data.note_id) Notes.openById(e.data.note_id);
      });
      if (!this.supported()) return;
      try { this.cfg = await API.req("GET", "/v1/push/config"); } catch (e) { this.cfg = null; }
      try { const ready = await navigator.serviceWorker.ready; this.sub = await ready.pushManager.getSubscription(); } catch (e) { this.sub = null; }
      // re-register an existing device subscription (e.g. after a server reset or a new sign-in)
      if (this.sub && this.cfg && this.cfg.enabled && Notification.permission === "granted") this.save(this.sub).catch(() => {});
    },
    b64ToBytes(b64) {
      const pad = "=".repeat((4 - (b64.length % 4)) % 4);
      const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
      return Uint8Array.from(raw, (c) => c.charCodeAt(0));
    },
    device() { const ua = navigator.userAgent; return (/iPhone|iPad/.test(ua) ? "iPhone/iPad" : /Android/.test(ua) ? "Android" : /Mac/.test(ua) ? "Mac" : /Windows/.test(ua) ? "Windows" : "Browser"); },
    save(sub) { return API.req("POST", "/v1/push/subscribe", { body: { subscription: sub.toJSON(), device: this.device() } }); },
    async enable() {
      if (await Notification.requestPermission() !== "granted") { toast("Notifications are blocked for this site — allow them in your browser settings."); return; }
      const ready = await navigator.serviceWorker.ready;
      this.sub = await ready.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: this.b64ToBytes(this.cfg.public_key) });
      await this.save(this.sub);
      toast("Notifications are on for this device.");
    },
    async disable() {
      if (!this.sub) return;
      const endpoint = this.sub.endpoint;
      try { await this.sub.unsubscribe(); } catch (e) { /* already gone */ }
      await API.req("POST", "/v1/push/unsubscribe", { body: { endpoint } }).catch(() => {});
      this.sub = null;
      toast("Notifications are off for this device.");
    },
    renderToggle(panel, rerender) {
      const mk = (text, fn, cls) => {
        const b = el("button", { type: "button", class: "notif-enable " + (cls || ""), text });
        b.addEventListener("click", async (e) => { e.stopPropagation(); b.disabled = true; try { await fn(); } catch (err) { toast(err.message); } rerender(); });
        panel.appendChild(b);
      };
      if (this.supported() && this.cfg && this.cfg.enabled) {
        if (!this.sub) {
          const ios = /iPhone|iPad/.test(navigator.userAgent) && !window.matchMedia("(display-mode: standalone)").matches;
          if (ios) panel.appendChild(el("div", { class: "drawer-empty", text: "On iPhone: tap Share → Add to Home Screen, then open OpenMuse from there to turn on notifications." }));
          else mk("Get notifications on this device — even when OpenMuse is closed", () => this.enable(), "push");
        } else {
          mk("Notifications on for this device · Send a test", () => API.req("POST", "/v1/push/test", { body: {} }), "push");
          mk("Turn off on this device", () => this.disable(), "push");
        }
      } else if (window.Notification && Notification.permission === "default") {
        mk("Enable desktop alerts when this tab is in the background", () => Notification.requestPermission());
      }
    },
  };

  async function shareLanding() {
    const q = new URLSearchParams(location.search);
    const note = q.get("note");
    if (note) Notes.openById(note);
    if (q.has("note") || q.has("share") || q.has("source")) history.replaceState(null, "", "/");
    if (q.get("share") === "unsupported") { toast("Open OpenMuse once more, then share again."); return; }
    if (q.get("share") !== "1" || !("caches" in window)) return;
    let meta = null, files = [];
    try {
      const c = await caches.open("om-share");
      const m = await c.match("/__share/meta");
      if (m) {
        meta = await m.json();
        for (const f of meta.files) { const r = await c.match("/__share/file/" + f.i); if (r) files.push({ name: f.name, type: f.type, blob: await r.blob() }); }
      }
      await Promise.all((await c.keys()).map((k) => c.delete(k)));  // one-shot: don't keep shared content around
    } catch (e) { meta = null; }
    if (!meta) return;
    const link = meta.url || ((meta.text || "").match(/https?:\/\/\S+/) || [""])[0];
    const text = [meta.title, meta.text && meta.text !== link ? meta.text : "", link].filter(Boolean).join("\n");
    const sheet = el("div", { class: "share-sheet", role: "dialog", "aria-label": "Shared with OpenMuse" });
    sheet.appendChild(el("h2", { text: "Shared with OpenMuse" }));
    sheet.appendChild(el("div", { class: "share-what", text: [text, ...files.map((f) => "📎 " + f.name)].filter(Boolean).join("\n") }));
    const acts = el("div", { class: "share-acts" });
    sheet.appendChild(acts);
    const close = () => sheet.remove();
    async function upload(f) {
      const buf = new Uint8Array(await f.blob.arrayBuffer());
      let bin = ""; for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
      return (await API.req("POST", "/v1/library", { body: { name: f.name, content_base64: btoa(bin) } })).document || {};
    }
    const mk = (label, fn, cls) => {
      const b = el("button", { type: "button", class: "btn " + (cls || ""), text: label });
      b.addEventListener("click", async () => { $$("button", acts).forEach((x) => { x.disabled = true; }); try { await fn(); close(); } catch (e) { toast((e.body && e.body.error && e.body.error.message) || e.message); $$("button", acts).forEach((x) => { x.disabled = false; }); } });
      acts.appendChild(b);
    };
    mk("Ask OpenMuse about this", async () => {
      const names = [];
      for (const f of files) { await upload(f); names.push(f.name); }
      showTab("chat");
      if (state.prefillChat) state.prefillChat((text ? text + "\n\n" : "") + (names.length ? "(In my Library: " + names.join(", ") + ") " : ""));
    });
    mk("Remember this", async () => {
      for (const f of files) { const d = await upload(f); if (d.artifact_id) await API.req("POST", "/v1/library/" + d.artifact_id + "/memory", { body: {} }); }
      if (text) await API.memory.addDocument({ title: (meta.title || link || text).slice(0, 120), text });
      toast("Saved to memory — OpenMuse can use it from now on.");
    }, "secondary");
    if (files.length) mk("Just save to Library", async () => { for (const f of files) await upload(f); toast("Saved to your Library."); }, "secondary");
    mk("Cancel", async () => {}, "secondary");
    document.body.appendChild(sheet);
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
    async openById(id) {
      let note = this.items.find((n) => n.id === id);
      if (!note) { await this.load(); note = this.items.find((n) => n.id === id); }
      if (note) this.open(note);
    },
    async open(note) {
      $(".notif-panel") && $(".notif-panel").remove();
      if (!note.read_at) { note.read_at = Date.now() / 1000; this.renderDot(); API.notifications.read(note.id).catch(() => {}); }
      if (note.link && note.link.chat_id && state.openChat) state.openChat(note.link.chat_id);
      else if (note.link && note.link.monitor_id) showTab("monitors");
      else if (note.link && note.link.tab) showTab(note.link.tab);
    },
    renderPanel(panel) {
      panel.innerHTML = "";
      const head = el("div", { class: "notif-head" });
      head.appendChild(el("strong", { text: "Notifications" }));
      const all = el("button", { type: "button", class: "memv-link notif-readall", text: "Mark all read" });
      all.addEventListener("click", async (e) => { e.stopPropagation(); await API.notifications.readAll(); this.items.forEach((n) => { n.read_at = n.read_at || Date.now() / 1000; }); this.renderDot(); this.renderPanel(panel); });
      head.appendChild(all);
      panel.appendChild(head);
      Push.renderToggle(panel, () => this.renderPanel(panel));
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
    // built in: web search (issue: ChatGPT-style search)
    const ws = el("div", { class: "app-builtin" });
    ws.innerHTML = '<div><strong>Web search</strong><span class="memv-meta">Built in · DuckDuckGo · answers cite their sources</span></div>';
    const wlab = el("label", { class: "app-alerts" });
    const wcb = el("input", { type: "checkbox", id: "searchAuto", "aria-label": "Search the web automatically" });
    wlab.appendChild(wcb); wlab.appendChild(document.createTextNode(" Search automatically when a question needs it"));
    ws.appendChild(wlab);
    root.appendChild(ws);
    API.req("GET", "/v1/settings/search").then((r) => { wcb.checked = !!r.auto; }).catch(() => { ws.hidden = true; });
    wcb.addEventListener("change", async () => {
      try { await API.req("PUT", "/v1/settings/search", { body: { auto: wcb.checked } });
            toast(wcb.checked ? "OpenMuse will search the web when a question needs it." : "OpenMuse will only search when you ask."); }
      catch (e) { wcb.checked = !wcb.checked; toast("Couldn't change that: " + e.message); }
    });
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
          if (a.toolkit === "gmail") {   // opt-in new-mail alerts (issue #8)
            const lab = el("label", { class: "app-alerts" });
            const cb = el("input", { type: "checkbox", "aria-label": "Tell me about new email" });
            lab.appendChild(cb); lab.appendChild(document.createTextNode(" Tell me about new email"));
            API.req("GET", "/v1/apps/gmail/alerts").then((r) => { cb.checked = !!r.enabled; }).catch(() => { lab.hidden = true; });
            cb.addEventListener("change", async () => {
              try { await API.req("PUT", "/v1/apps/gmail/alerts", { body: { enabled: cb.checked } });
                    toast(cb.checked ? "You'll get a notification when new email arrives." : "New-email alerts off."); }
              catch (e) { cb.checked = !cb.checked; toast("Couldn't change that: " + e.message); }
            });
            card.appendChild(lab);
          }
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
    root.classList.add("memv");
    root.appendChild(el("div", { class: "memv-head", html: "<h1>Voice</h1><p class='muted'>Talk to OpenMuse and hear it answer. Interrupt any time by speaking. Anything that sends, buys or signs in still needs your OK on screen.</p>" }));
    const go = el("button", { type: "button", class: "btn", text: "Start voice mode" });
    go.addEventListener("click", () => VoiceMode.open());
    root.appendChild(go);
    const info = el("p", { class: "memv-meta" });
    root.appendChild(info);
    API.req("GET", "/v1/voice/config").then((c) => {
      VoiceMode.cfg = c;
      info.textContent = "Listening: " + (c.stt ? c.stt.replace(/-/g, " ") : "your browser") + " · Speaking: " + (c.tts ? c.tts.replace(/-/g, " ") : "your browser");
    }).catch(() => { info.textContent = "Using your browser's speech."; });
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
    monitors: { render: monitorsView, onShow: (r) => r._refresh && r._refresh() },
    logins: { render: loginsView, onShow: (r) => r._refresh && r._refresh() },
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
    authGate().then(() => { renderUserBox(); boot(); Notes.start(); Push.init(); shareLanding(); });
  });
})();
