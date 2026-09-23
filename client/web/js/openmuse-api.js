/* OpenMuse web client — API layer over the External API contracts.
 *
 * - POST /v1/chats/{chat_id}/messages with Idempotency-Key
 * - SSE consumption (run.status, assistant.delta, approval.required,
 *   run.completed) with Last-Event-ID reconnect
 * - approval decisions bound to the exact argument hash
 * - artifact upload/download through client paths
 * - degraded mode: queued sends, stream resume, read-only cached views
 *
 * The client never handles raw secrets: connector credential capture is
 * done on the Secure Vault capture page; this layer only passes the
 * opaque capture id and reads the capture status (complete/incomplete).
 */
(function () {
  "use strict";

  // Bearer credential: a per-user session token from sign-in (kept in
  // localStorage when "keep me signed in", else sessionStorage), or a
  // developer API key pasted in the sidebar (session only).
  const safe = (fn) => { try { return fn(); } catch (e) { return ""; } };
  const store = {
    get apiKey() {
      return safe(() => sessionStorage.getItem("om.apiKey")) || safe(() => localStorage.getItem("om.token")) || "";
    },
    set apiKey(v) { safe(() => sessionStorage.setItem("om.apiKey", v)); },
    setToken(token, remember) {
      safe(() => sessionStorage.setItem("om.apiKey", token));
      if (remember) safe(() => localStorage.setItem("om.token", token));
    },
    clear() {
      safe(() => sessionStorage.removeItem("om.apiKey"));
      safe(() => localStorage.removeItem("om.token"));
    },
    get base() { return ""; }, // same-origin; serve_ui.py proxies /v1/*
  };

  function uuid() {
    return (crypto.randomUUID ? crypto.randomUUID() : "xxxxxxxx-xxxx-4xxx".replace(/x/g, () =>
      Math.floor(Math.random() * 16).toString(16)));
  }

  async function req(method, path, { body, headers, idempotencyKey } = {}) {
    const h = Object.assign({ "Accept": "application/json" }, headers || {});
    if (store.apiKey) h["Authorization"] = "Bearer " + store.apiKey;
    if (idempotencyKey) h["Idempotency-Key"] = idempotencyKey;
    let payload;
    if (body !== undefined) { payload = JSON.stringify(body); h["Content-Type"] = "application/json"; }
    const resp = await fetch(store.base + path, { method, headers: h, body: payload });
    const text = await resp.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch (e) { data = { _raw: text }; }
    if (!resp.ok) {
      const err = new Error("HTTP " + resp.status);
      err.status = resp.status; err.body = data; throw err;
    }
    return data;
  }

  /* -- sessions / messages ------------------------------------------- */
  async function createSession(title) {
    return req("POST", "/v1/sessions", { body: { title: title || "" } });
  }

  async function sendMessage(chatId, text, { idempotencyKey } = {}) {
    const key = idempotencyKey || ("idem_" + uuid().replace(/-/g, ""));
    const body = await req("POST", "/v1/chats/" + encodeURIComponent(chatId) + "/messages", {
      body: { content: [{ type: "text", text }] },
      idempotencyKey: key,
    });
    return { idempotencyKey: key, messageId: body.message_id, runId: body.run_id,
             state: body.status, streamUrl: body.stream_url };
  }

  /* -- chat threads (server-side list, rename, archive, transcript) --- */
  async function chats(archived) { return req("GET", "/v1/chats" + (archived ? "?archived=1" : "")); }
  async function updateChat(chatId, patch) { return req("PATCH", "/v1/chats/" + encodeURIComponent(chatId), { body: patch }); }
  async function chatMessages(chatId) { return req("GET", "/v1/chats/" + encodeURIComponent(chatId) + "/messages"); }

  /* -- schedules, notifications, task control, activity -------------- */
  const schedules = {
    list: () => req("GET", "/v1/schedules"),
    create: (s) => req("POST", "/v1/schedules", { body: s }),
    update: (id, patch) => req("PATCH", "/v1/schedules/" + encodeURIComponent(id), { body: patch }),
    remove: (id) => req("DELETE", "/v1/schedules/" + encodeURIComponent(id)),
    runNow: (id) => req("POST", "/v1/schedules/" + encodeURIComponent(id) + "/run", { body: {} }),
  };
  const notifications = {
    list: () => req("GET", "/v1/notifications"),
    read: (id) => req("POST", "/v1/notifications/" + encodeURIComponent(id) + "/read", { body: {} }),
    readAll: () => req("POST", "/v1/notifications/read-all", { body: {} }),
    // Live stream: reconnects forever (server closes each connection after ~25s).
    async stream(onNote, signal) {
      for (;;) {
        if (signal && signal.aborted) return;
        try {
          const h = { "Accept": "text/event-stream" };
          if (store.apiKey) h["Authorization"] = "Bearer " + store.apiKey;
          const resp = await fetch("/v1/notifications/stream", { headers: h, signal });
          if (resp.status === 401) return;
          const reader = resp.body.getReader(); const dec = new TextDecoder(); let buf = "";
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            buf += dec.decode(value, { stream: true });
            let i;
            while ((i = buf.indexOf("\n\n")) >= 0) {
              const block = buf.slice(0, i); buf = buf.slice(i + 2);
              const data = block.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5)).join("");
              if (data) { try { onNote(JSON.parse(data)); } catch (e) { /* skip */ } }
            }
          }
        } catch (e) { if (signal && signal.aborted) return; }
        await new Promise((r) => setTimeout(r, 1500));
      }
    },
  };
  const runs = {
    pause: (id) => req("POST", "/v1/runs/" + encodeURIComponent(id) + "/pause", { body: {} }),
    resume: (id) => req("POST", "/v1/runs/" + encodeURIComponent(id) + "/resume", { body: {} }),
    retry: (id) => req("POST", "/v1/runs/" + encodeURIComponent(id) + "/retry", { body: {} }),
    receipt: (id) => req("GET", "/v1/runs/" + encodeURIComponent(id) + "/receipt"),
  };
  const activity = () => req("GET", "/v1/activity");

  /* -- accounts ------------------------------------------------------ */
  async function signup(email, password, name) {
    let timezone = "";
    try { timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) { /* ignore */ }
    return req("POST", "/v1/auth/signup", { body: { email, password, name, timezone } });
  }
  async function login(email, password) {
    return req("POST", "/v1/auth/login", { body: { email, password } });
  }
  async function logout() { try { await req("POST", "/v1/auth/logout", { body: {} }); } catch (e) { /* already gone */ } }
  async function me() { return req("GET", "/v1/auth/me"); }

  /* -- memory (the signed-in user's own store) ------------------------ */
  const memory = {
    overview: () => req("GET", "/v1/memory"),
    recall: (query, top_k) => req("POST", "/v1/memory/recall", { body: { query, top_k: top_k || 8 } }),
    records: () => req("GET", "/v1/memory/records"),
    journal: () => req("GET", "/v1/memory/journal"),
    people: () => req("GET", "/v1/memory/people"),
    person: (id) => req("GET", "/v1/memory/people/" + encodeURIComponent(id)),
    forget: (query, confirm) => req("POST", "/v1/memory/forget", { body: { query, confirm: !!confirm } }),
    profile: () => req("GET", "/v1/memory/profile"),
    saveProfile: (fname, text) => req("PUT", "/v1/memory/profile/" + fname, { body: { text } }),
    documents: () => req("GET", "/v1/memory/documents"),
    addDocument: (doc) => req("POST", "/v1/memory/documents", { body: doc }),
    deleteDocument: (id) => req("DELETE", "/v1/memory/documents/" + encodeURIComponent(id)),
    summaries: () => req("GET", "/v1/memory/summaries"),
  };

  /* -- live browser view --------------------------------------------- */
  async function browserSession(sessionId) {
    return req("GET", "/v1/browser/sessions/" + encodeURIComponent(sessionId));
  }
  async function browserFrameURL(sessionId) {
    const h = {};
    if (store.apiKey) h["Authorization"] = "Bearer " + store.apiKey;
    const resp = await fetch("/v1/browser/sessions/" + encodeURIComponent(sessionId) + "/frame",
                             { headers: h, cache: "no-store" });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    return URL.createObjectURL(await resp.blob());
  }
  async function browserInput(sessionId, event) {
    return req("POST", "/v1/browser/sessions/" + encodeURIComponent(sessionId) + "/input", { body: event });
  }

  async function getRun(runId) { return req("GET", "/v1/runs/" + encodeURIComponent(runId)); }
  async function cancelRun(runId) { return req("POST", "/v1/runs/" + encodeURIComponent(runId) + "/cancel"); }

  /* -- SSE with Last-Event-ID resume ---------------------------------- */
  function streamRun(runId, { onEvent, lastEventId, signal } = {}) {
    const cursors = streamRun.cursors;
    const startFrom = lastEventId !== undefined ? lastEventId : (cursors[runId] || null);
    const url = "/v1/runs/" + encodeURIComponent(runId) + "/events" +
      (startFrom ? "?last_event_id=" + encodeURIComponent(startFrom) : "");
    const es = new EventSource(url);
    if (store.apiKey) {
      // EventSource cannot set Authorization headers; serve_ui.py accepts the
      // key as a query param for streams only when the session cookie/key
      // was already presented on this origin. Fallback: fetch-reader below.
      es.close();
      return streamRunFetch(runId, { onEvent, lastEventId: startFrom, signal });
    }
    es.onmessage = null;
    es.addEventListener("run.status", (e) => handle("run.status", e));
    es.addEventListener("assistant.delta", (e) => handle("assistant.delta", e));
    es.addEventListener("approval.required", (e) => handle("approval.required", e));
    es.addEventListener("run.completed", (e) => handle("run.completed", e));
    es.addEventListener("tool.call", (e) => handle("tool.call", e));
    es.addEventListener("tool.result", (e) => handle("tool.result", e));
    function handle(name, e) {
      if (e.lastEventId) cursors[runId] = e.lastEventId;
      let data = {};
      try { data = JSON.parse(e.data); } catch (err) { /* keep {} */ }
      if (onEvent) onEvent({ id: e.lastEventId, name, data });
      if (name === "run.completed") es.close();
    }
    es.onerror = () => { /* auto-reconnect handled by EventSource */ };
    return { close() { es.close(); } };
  }
  streamRun.cursors = {};

  /* Fetch-based SSE reader (used when an Authorization header is needed).
   * The server closes each stream after ~20s; reconnect from the last event
   * id until the run reaches a terminal event. */
  async function streamRunFetch(runId, opts = {}) {
    let finished = false;
    const onEvent = (ev) => {
      if (ev.name === "run.completed" || ev.name === "run.failed" || ev.name === "run.cancelled") finished = true;
      if (opts.onEvent) opts.onEvent(ev);
    };
    let lastEventId = opts.lastEventId;
    for (let attempt = 0; !finished && attempt < 500; attempt++) {
      if (opts.signal && opts.signal.aborted) return;
      try {
        await streamRunFetchOnce(runId, { onEvent, lastEventId, signal: opts.signal });
      } catch (e) {
        if (e.message && /HTTP 4\d\d/.test(e.message)) throw e;
        await new Promise((r) => setTimeout(r, 1000));
      }
      lastEventId = streamRun.cursors[runId];
    }
  }

  async function streamRunFetchOnce(runId, { onEvent, lastEventId, signal } = {}) {
    const cursors = streamRun.cursors;
    const startFrom = lastEventId !== undefined ? lastEventId : (cursors[runId] || null);
    let url = "/v1/runs/" + encodeURIComponent(runId) + "/events";
    if (startFrom) url += "?last_event_id=" + encodeURIComponent(startFrom);
    const h = { "Accept": "text/event-stream" };
    if (store.apiKey) h["Authorization"] = "Bearer " + store.apiKey;
    const resp = await fetch(url, { headers: h, signal });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    let cur = {};
    function flush() {
      if (!cur.event) { cur = {}; return; }
      let data = {};
      try { data = cur.data ? JSON.parse(cur.data) : {}; } catch (e) { /* keep {} */ }
      if (cur.id) cursors[runId] = cur.id;
      if (onEvent) onEvent({ id: cur.id, name: cur.event, data });
      const done = cur.event === "run.completed" || cur.event === "run.failed" ||
        cur.event === "run.cancelled";
      cur = {};
      return done;
    }
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, idx); buf = buf.slice(idx + 2);
        cur = {};
        for (const line of block.split("\n")) {
          if (line.startsWith("id:")) cur.id = line.slice(3).trim();
          else if (line.startsWith("event:")) cur.event = line.slice(6).trim();
          else if (line.startsWith("data:")) cur.data = (cur.data || "") + line.slice(5).trim();
        }
        if (flush()) { reader.cancel(); return; }
      }
    }
  }

  /* -- approvals -------------------------------------------------------- */
  const HIGH_RISK = new Set(["R4", "R5"]);

  async function getApproval(approvalId) {
    const body = await req("GET", "/v1/approvals/" + encodeURIComponent(approvalId));
    return approvalCard(body);
  }

  function approvalCard(payload) {
    const bind = payload.bind_fields || payload.presentation || {};
    return {
      approvalId: payload.approval_id,
      runId: payload.run_id || "",
      tool: payload.tool_name || payload.tool || "",
      toolVersion: payload.tool_version || "",
      risk: payload.risk || "",
      argumentHash: payload.argument_hash,
      bindFields: bind,
      destination: deriveDestination(payload.tool_name || payload.tool || "", bind),
      effect: deriveEffect(payload.tool_name || payload.tool || "", bind),
      expiresInSeconds: payload.expires_in_seconds || 900,
      deviceAuthed: false,
    };
  }

  function deriveDestination(tool, b) {
    if (tool === "files.write" || tool === "files.read") return b.path || "?";
    if (tool === "browser.act") return b.url || b.origin || "?";
    if (tool === "email.send" || tool === "message.send") return b.to || b.channel || "?";
    return b.destination || b.path || b.url || b.to || b.target || "?";
  }
  function deriveEffect(tool, b) {
    if (tool === "files.write") return "write file " + (b.path || "?");
    if (tool === "browser.act") return (b.action || "act") + " on " + (b.url || b.origin || "?");
    return b.effect || (tool + " with " + Object.keys(b).length + " bound argument(s)");
  }

  /* Device authentication: WebAuthn platform authenticator on real devices.
   * Falls back to an explicit user confirmation only when WebAuthn is
   * unavailable (never silently auto-approves). */
  async function deviceAuthenticate(reason) {
    if (window.PublicKeyCredential &&
        window.isSecureContext !== false) {
      try {
        const challenge = crypto.getRandomValues(new Uint8Array(32));
        await navigator.credentials.get({
          publicKey: {
            challenge,
            userVerification: "required",
            timeout: 60000,
          },
        });
        return { ok: true, method: "webauthn", at: Date.now() };
      } catch (e) {
        return { ok: false, error: String(e && e.name || e) };
      }
    }
    return { ok: false, error: "webauthn-unavailable" };
  }

  async function decideApproval(card, decision, { deviceAuth } = {}) {
    if (decision !== "approve" && decision !== "deny") throw new Error("bad decision");
    if (HIGH_RISK.has(card.risk)) {
      const auth = deviceAuth || await deviceAuthenticate(
        "Approve " + card.tool + " (" + card.risk + ") → " + card.destination);
      if (!auth || !auth.ok) throw new Error("device authentication required for " + card.risk);
      card.deviceAuthed = true; // re-display bound fields after auth (caller renders card)
    }
    return req("POST", "/v1/approvals/" + encodeURIComponent(card.approvalId) + "/decision", {
      body: { decision, argument_hash: card.argumentHash }, // binds to the exact hash shown
    });
  }

  /* -- artifacts ---------------------------------------------------------- */
  function b64encodeBytes(bytes) {
    let s = "";
    for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s);
  }
  async function uploadArtifact(name, file, contentType) {
    const buf = new Uint8Array(await file.arrayBuffer());
    return req("POST", "/v1/artifacts", {
      body: { name, content_base64: b64encodeBytes(buf),
              content_type: contentType || file.type || "application/octet-stream" },
      idempotencyKey: "idem_" + uuid().replace(/-/g, ""),
    });
  }
  async function downloadArtifact(artifactId) {
    const body = await req("GET", "/v1/artifacts/" + encodeURIComponent(artifactId));
    const bin = atob(body.content_base64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return { meta: body, bytes };
  }

  /* -- local domain endpoints (goals/feed/ideas/memory/schedules/...) ---- */
  async function local(method, path, body) {
    return req(method, "/v1/local" + path, body === undefined ? {} : { body });
  }

  /* -- degraded mode: queued sends --------------------------------------- */
  const queue = {
    key: "om.queuedSends",
    all() { try { return JSON.parse(localStorage.getItem(this.key) || "[]"); } catch (e) { return []; } },
    push(item) { const q = this.all(); q.push(item); localStorage.setItem(this.key, JSON.stringify(q)); },
    clear() { localStorage.removeItem(this.key); },
  };

  window.OpenMuseAPI = {
    store, req, uuid,
    createSession, sendMessage, getRun, cancelRun,
    streamRun, streamRunFetch,
    browserSession, browserFrameURL, browserInput,
    signup, login, logout, me, memory, chats, updateChat, chatMessages,
    schedules, notifications, runs, activity,
    getApproval, decideApproval, approvalCard, deviceAuthenticate, HIGH_RISK,
    uploadArtifact, downloadArtifact,
    local, queue,
  };
})();
