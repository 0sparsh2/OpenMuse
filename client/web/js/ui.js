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
    renderNav();
    $$(".view").forEach((v) => { v.hidden = v.dataset.view !== id; });
    const view = $('.view[data-view="' + id + '"]');
    if (view && VIEWS[id] && VIEWS[id].onShow) VIEWS[id].onShow(view);
    const h = view ? view.querySelector("h1") : null;
    if (h) { h.setAttribute("tabindex", "-1"); }
  }

  /* -- chat view ------------------------------------------------------------ */
  function chatView(root) {
    root.innerHTML = "";
    root.appendChild(el("h1", { text: "Chat" }));

    const chatsBar = el("div", { class: "tabs", role: "tablist", "aria-label": "Chats" });
    const newBtn = el("button", { type: "button", text: "+ New chat", "aria-label": "Start a new chat" });
    newBtn.addEventListener("click", newChat);
    root.appendChild(chatsBar);

    const thread = el("div", { class: "thread", role: "log", "aria-label": "Conversation", "aria-live": "polite" });
    root.appendChild(thread);

    const form = el("form", { class: "composer" });
    const input = el("input", {
      type: "text", "aria-label": "Message", placeholder: "Message OpenMuse…",
      autocomplete: "off",
    });
    const send = el("button", { class: "btn", type: "submit", text: "Send" });
    form.appendChild(input); form.appendChild(send);
    form.appendChild(newBtn);
    root.appendChild(form);
    const qnote = el("div", { class: "queued-note", hidden: true });
    root.appendChild(qnote);

    input.value = state.drafts[state.activeChat] || "";
    input.addEventListener("input", () => { state.drafts[state.activeChat] = input.value; });

    function renderChatsBar() {
      chatsBar.innerHTML = "";
      state.chats.forEach((c, i) => {
        const b = el("button", {
          type: "button", role: "tab",
          "aria-selected": i === state.activeChat ? "true" : "false",
          text: c.title || ("Chat " + (i + 1)),
        });
        b.addEventListener("click", () => { state.activeChat = i; renderAll(); });
        chatsBar.appendChild(b);
      });
    }

    function renderThread() {
      thread.innerHTML = "";
      const chat = state.chats[state.activeChat];
      if (!chat) { thread.appendChild(el("div", { class: "empty", text: "Start a new chat to begin." })); return; }
      chat.messages.forEach((m) => {
        const wrap = el("div", { class: "msg " + m.role });
        wrap.appendChild(el("span", { class: "role", text: m.role === "user" ? "You" : "OpenMuse" }));
        const body = el("div", {});
        body.textContent = m.text;
        wrap.appendChild(body);
        (m.tools || []).forEach((t) => {
          const tc = el("div", { class: "tool-card" });
          tc.innerHTML = "<strong>" + esc(t.name) + "</strong> <span class='muted'>" +
            esc(t.status || "") + "</span>";
          const det = el("details", { class: "timeline" });
          const sum = el("summary", { text: "Run timeline" });
          det.appendChild(sum);
          const pre = el("pre", { class: "mono", text: (t.trace || []).join("\n") });
          det.appendChild(pre);
          tc.appendChild(det);
          wrap.appendChild(tc);
        });
        thread.appendChild(wrap);
      });
      thread.scrollTop = thread.scrollHeight;
    }

    async function newChat() {
      try {
        const s = await API.createSession("Chat " + (state.chats.length + 1));
        state.chats.push({ sessionId: s.session_id, chatId: s.chat_id, title: s.title || "", messages: [] });
      } catch (e) {
        // degraded: local-only chat shell; messages queue until online
        state.chats.push({ sessionId: "local-" + state.chats.length, chatId: "local-" + state.chats.length,
                           title: "Chat " + (state.chats.length + 1) + " (offline)", messages: [], offline: true });
        toast("Backend unreachable — messages will queue.");
      }
      state.activeChat = state.chats.length - 1;
      renderAll();
    }

    async function onSend(e) {
      e.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      const chat = state.chats[state.activeChat];
      if (!chat) { toast("Create a chat first."); return; }
      input.value = ""; state.drafts[state.activeChat] = "";
      chat.messages.push({ role: "user", text });
      renderThread();
      const amsg = { role: "assistant", text: "", tools: [] };
      chat.messages.push(amsg); renderThread();
      try {
        const res = await API.sendMessage(chat.chatId, text);
        if (res.state === "QUEUED") {
          qnote.hidden = false;
          qnote.textContent = "Offline — message queued (" + API.queue.all().length + " pending).";
          amsg.text = "(queued — will send when the backend is reachable)";
          renderThread(); return;
        }
        API.streamRun(res.runId, {
          onEvent: (ev) => {
            if (ev.name === "assistant.delta" && ev.data.delta) amsg.text += ev.data.delta;
            if (ev.name === "approval.required") {
              API.getApproval(ev.data.approval_id).then((card) => {
                state.approvals.push(card); renderNav();
                toast("Approval required: " + card.tool + " → " + card.destination);
              }).catch(() => {});
            }
            if (ev.name === "run.completed") {
              amsg.text = ev.data.final_text || amsg.text || "(run completed)";
            }
            renderThread();
          },
        }).catch((err) => { amsg.text = "(stream error: " + err.message + ")"; renderThread(); });
      } catch (err) {
        // queue for degraded send
        API.queue.push({ chatId: chat.chatId, text, idempotencyKey: "idem_" + API.uuid().replace(/-/g, "") });
        qnote.hidden = false;
        qnote.textContent = "Offline — message queued.";
        amsg.text = "(queued — will send when the backend is reachable)";
        renderThread();
      }
    }
    form.addEventListener("submit", onSend);

    function renderAll() { renderChatsBar(); renderThread(); }
    root._renderAll = renderAll;
    renderAll();
    if (!state.chats.length) newChat();
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
