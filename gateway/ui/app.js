/* Built-in test UI. Plain JavaScript, no libraries, no outside requests (the page's content
   security policy forbids them). Everything from the server is shown as text, never as HTML. */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var state = { token: "", session: null, lastQuestion: null, lastOpts: null, context: [], busy: false };

  var ERRORS = {
    unauthorized: "That token was not accepted.",
    local_model_unavailable: "The local model is not reachable. There is no fallback to the frontier, by design.",
    retrieval_unavailable: "Document search is unavailable (is the embedding model pulled?).",
    taint_blocked: "Blocked: this session holds private context, so it can never go to the frontier. Start a new session to clear it.",
    consent_required: "The frontier needs your consent first.",
    attachments_blocked: "Attachments are never sent to the frontier.",
    frontier_not_configured: "The frontier lane is off because no API key is set.",
    frontier_unavailable: "The frontier call failed. The attempt is in the audit log.",
    documents_private_lane_only: "Documents are answered by the local model only.",
    tools_private_lane_only: "Tool turns are local only.",
    rag_disabled: "Document search is switched off in the config.",
    reserved_source: "That source name is reserved.",
    unknown_model: "Unknown model."
  };

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function show(node, on) { node.hidden = !on; }
  function time(iso) { try { return new Date(iso).toLocaleTimeString(); } catch (e) { return iso; } }
  function store(fn) { try { return fn(); } catch (e) { return null; } }

  /* ---- API ---- */
  function api(method, path, body) {
    var opts = { method: method, headers: { "Authorization": "Bearer " + state.token } };
    if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return null; }).then(function (data) { return { status: r.status, data: data }; });
    });
  }

  /* ---- login ---- */
  function connect(token, remember) {
    state.token = token;
    return api("GET", "/session").then(function (r) {
      if (r.status !== 200) { state.token = ""; throw new Error(ERRORS.unauthorized); }
      if (remember) store(function () { sessionStorage.setItem("pai_token", token); });
      state.session = r.data;
      $("who").textContent = "user: " + r.data.user;
      show($("who"), true); show($("disconnect"), true);
      show($("login"), false); show($("app"), true);
      renderSession();
    });
  }
  function disconnect() {
    store(function () { sessionStorage.removeItem("pai_token"); });
    state.token = ""; state.session = null;
    clear($("messages")); hideOffer();
    document.querySelector("[data-tab=chat]").click();
    show($("app"), false); show($("login"), true); show($("who"), false); show($("disconnect"), false);
    $("token").value = "";
  }

  /* ---- session sidebar ---- */
  function refreshSession() {
    return api("GET", "/session").then(function (r) { if (r.status === 200) { state.session = r.data; renderSession(); } });
  }
  function renderSession() {
    var s = state.session; if (!s) return;
    var priv = s.taint === "PRIVATE";
    var b = $("taint-badge"); b.textContent = s.taint; b.className = "taint " + (priv ? "private" : "clean");
    $("taint-note").textContent = priv
      ? "Locked to the local model. Nothing in this session can reach the frontier until you start a new session."
      : (s.auto_route_clean ? "Clean. Messages route to the frontier automatically." : "Clean. Answers stay local; the frontier is offered and needs your consent.");
    $("s-frags").textContent = String(s.fragments);
    $("s-auto").textContent = s.auto_route_clean ? "true" : "false";
    $("s-scope").textContent = s.consent_scope;
    var sessionScope = s.consent_scope === "session";
    $("s-consent").textContent = sessionScope
      ? (s.consent ? (priv ? "given (no effect: PRIVATE)" : "given") : "not given") : "per message";
    show($("consent-wrap"), sessionScope);
    $("consent").checked = !!s.consent;
    show($("req-consent-wrap"), !sessionScope);
  }

  /* ---- chat ---- */
  function addMsg(kind, text) {
    var m = el("div", "msg " + kind, text);
    $("messages").appendChild(m);
    $("messages").scrollTop = $("messages").scrollHeight;
    return m;
  }
  function badge(cls, text) { return el("span", "badge " + cls, text); }

  function addAssistant(d) {
    var choice = (d.choices && d.choices[0]) || {};
    var text = (choice.message && choice.message.content) || "";
    var hasTools = choice.message && choice.message.tool_calls && choice.message.tool_calls.length;
    var empty = text ? "" : (hasTools ? "" :
      choice.finish_reason === "length"
        ? "The model ran out of tokens before it finished its answer. Small reasoning models can use them all while thinking. Try again, start a new session, or turn reasoning off."
        : "The model returned no text.");
    var m = addMsg("assistant", text || empty);
    if (empty) m.classList.add("empty");
    var meta = el("div", "meta");
    meta.appendChild(badge("lane-" + d.lane, "lane: " + (d.lane === "private" ? "local" : d.lane)));
    meta.appendChild(badge("t-" + d.taint, "taint: " + d.taint));
    if (d.frontier_status) meta.appendChild(badge("cite", "frontier: " + d.frontier_status.replace(/_/g, " ")));
    var tc = d.choices && d.choices[0] && d.choices[0].message && d.choices[0].message.tool_calls;
    if (tc && tc.length) meta.appendChild(badge("cite", "tool call: " + tc.map(function (c) { return c.function.name; }).join(", ")));
    (d.citations || []).forEach(function (c) { meta.appendChild(badge("cite", c.doc + " #" + c.chunk + " (" + c.score + ")")); });
    m.appendChild(meta);
  }
  function addError(r) {
    var code = r.data && r.data.error;
    var text = ERRORS[code] || (code ? "Error: " + code : "Request failed (" + r.status + ").");
    var m = addMsg("error", text);
    if (r.data && r.data.lane) m.appendChild(el("div", "meta")).appendChild(badge("lane-" + r.data.lane, "lane: " + (r.data.lane === "private" ? "local" : r.data.lane)));
  }
  function hideOffer() { show($("offer"), false); }

  function send(text, overrides) {
    if (state.busy || !text.trim()) return Promise.resolve();
    overrides = overrides || {};
    var body = {
      messages: [{ role: "user", content: text }],
      lane: overrides.lane || $("lane").value
    };
    if ($("docs").checked) body.documents = true;
    if (state.context.length) body.context = state.context.map(function (c) { return { text: c.text, source: c.source }; });
    var scope = state.session && state.session.consent_scope;
    if (scope === "request" && (overrides.consent || $("req-consent").checked)) body.consent = true;
    state.busy = true; $("send").disabled = true; hideOffer();
    addMsg("user", text);
    if (state.context.length) addMsg("note", "attached context: " + state.context.map(function (c) { return c.source; }).join(", "));
    var wait = addMsg("note", "thinking…");
    return api("POST", "/v1/chat/completions", body).then(function (r) {
      wait.remove();
      state.context = []; renderChips();
      if (r.status === 200 && r.data && r.data.choices) {
        addAssistant(r.data);
        state.lastQuestion = text;
        if (text.trim() === "/new") { clear($("messages")); addMsg("note", "New session started. Taint and consent cleared."); }
        else if (r.data.frontier_offer && r.data.lane === "private") show($("offer"), true);
      } else { addError(r); }
    }).catch(function () { wait.remove(); addMsg("error", "Could not reach the gateway."); })
      .then(function () { state.busy = false; $("send").disabled = false; return refreshSession(); });
  }

  function sendToFrontier() {
    var q = state.lastQuestion; if (!q) return;
    var msg = "Send this question to the frontier API?\n\nOnly the text of your message is sent. No history and no context.\nThis records your consent" +
      (state.session && state.session.consent_scope === "session" ? " for the rest of this session." : " for this one message.");
    if (!window.confirm(msg)) return;
    hideOffer();
    var go = function (o) { return send(q, o); };
    if (state.session.consent_scope === "session") {
      api("POST", "/session/consent", { consent: true }).then(function () { return go({ lane: "frontier" }); });
    } else { go({ lane: "frontier", consent: true }); }
  }

  /* ---- context chips ---- */
  function renderChips() {
    var ul = $("ctx-list"); clear(ul);
    state.context.forEach(function (c, i) {
      var li = el("li", null, (c.file ? "📎 " : "") + c.source + " (" + c.text.length + " chars)");
      var x = el("button", null, "×"); x.type = "button"; x.setAttribute("aria-label", "Remove " + c.source);
      x.addEventListener("click", function () { state.context.splice(i, 1); renderChips(); });
      li.appendChild(x); ul.appendChild(li);
    });
  }

  /* ---- attach files as context (drag-and-drop, or the + button) ----
     Runs entirely in the browser: a file's text becomes a context item exactly like the paste box
     below, with the filename as its source. No upload endpoint exists, so this adds no attack
     surface; the taint rules are unchanged (the source still has to be registered CLEAN in
     infra/config.yaml or it is PRIVATE). Binary formats such as PDF cannot be parsed here, because
     the page loads no libraries. */
  var ATTACH_EXTENSIONS = [".txt", ".md", ".markdown", ".csv", ".json", ".log"];
  var MAX_ATTACH_BYTES = 300000;

  function sanitizeSourceName(name) {
    var base = String(name).split(/[\\/]/).pop().replace(/[^A-Za-z0-9_.-]/g, "_").slice(0, 64);
    return base || "attached_file";
  }
  function uniqueSourceName(name) {
    var used = {}; state.context.forEach(function (c) { used[c.source] = true; });
    if (!used[name]) return name;
    for (var i = 2; used[name + "_" + i]; i++) {}
    return name + "_" + i;
  }
  function attachError(msg) {
    var box = $("attach-error"); box.textContent = msg; show(box, true);
    clearTimeout(attachError._t); attachError._t = setTimeout(function () { show(box, false); }, 6000);
  }
  function readFileAsText(file) {
    return new Promise(function (resolve, reject) {
      var r = new FileReader();
      r.onload = function () { resolve(String(r.result || "")); };
      r.onerror = function () { reject(new Error("could not read the file")); };
      r.readAsText(file);
    });
  }
  function addFilesAsContext(fileList) {
    var files = Array.prototype.slice.call(fileList || []);
    files.forEach(function (file) {
      var ext = "." + (file.name.split(".").pop() || "").toLowerCase();
      if (ATTACH_EXTENSIONS.indexOf(ext) === -1) {
        attachError(file.name + ": only text files are supported (" + ATTACH_EXTENSIONS.join(", ") + ").");
        return;
      }
      if (file.size > MAX_ATTACH_BYTES) {
        attachError(file.name + ": too large (over " + Math.round(MAX_ATTACH_BYTES / 1000) + " KB).");
        return;
      }
      readFileAsText(file).then(function (text) {
        if (!text.trim()) { attachError(file.name + ": the file is empty."); return; }
        var source = uniqueSourceName(sanitizeSourceName(file.name));
        state.context.push({ text: text, source: source, file: true });
        renderChips();
      }).catch(function () { attachError(file.name + ": could not read the file."); });
    });
  }

  /* ---- tools ---- */
  function runTool(name) {
    var args = {};
    if (name === "read_file") args.path = $("t-read").value;
    if (name === "search_files") args.query = $("t-search").value;
    if (name === "context_query") args.query = $("t-ctxq").value;
    $("tool-out").textContent = "running…";
    api("POST", "/v1/tools/" + name, { arguments: args }).then(function (r) {
      $("tool-out").textContent = r.status === 200 ? r.data.content : "error: " + ((r.data && r.data.error) || r.status);
    });
  }

  /* ---- logs ---- */
  function fillTable(id, rows, cols) {
    var tb = $(id).querySelector("tbody"); clear(tb);
    if (!rows.length) { var tr = el("tr"); var td = el("td", "empty", "nothing yet"); td.colSpan = cols.length; tr.appendChild(td); tb.appendChild(tr); return; }
    rows.forEach(function (row) {
      var tr = el("tr");
      cols.forEach(function (c) { tr.appendChild(el("td", null, String(c(row)))); });
      tb.appendChild(tr);
    });
  }
  function loadLogs() {
    api("GET", "/v1/audit").then(function (r) {
      fillTable("audit-table", (r.data && r.data.records) || [], [
        function (x) { return time(x.ts); }, function (x) { return x.lane; }, function (x) { return x.consent_mode; },
        function (x) { return (x.tokens_in == null ? "–" : x.tokens_in) + " / " + (x.tokens_out == null ? "–" : x.tokens_out); },
        function (x) { return x.status; }, function (x) { return x.destination; }, function (x) { return x.prompt_sha256.slice(0, 12) + "…"; }]);
    });
    api("GET", "/v1/security").then(function (r) {
      fillTable("sec-table", (r.data && r.data.events) || [], [
        function (x) { return time(x.ts); }, function (x) { return x.event; }, function (x) { return x.tool || "–"; }, function (x) { return x.reason || "–"; }]);
    });
  }

  /* ---- health ---- */
  function pollHealth() {
    fetch("/health").then(function (r) { return r.json().catch(function () { return {}; }); }).then(function (d) {
      var h = $("health"), up = d.model_server === "up";
      h.textContent = "model server: " + (up ? "up" : "down"); h.className = "pill " + (up ? "up" : "down");
    }).catch(function () { $("health").textContent = "gateway: unreachable"; $("health").className = "pill down"; });
  }

  /* ---- wiring ---- */
  document.addEventListener("DOMContentLoaded", function () {
    $("login-form").addEventListener("submit", function (e) {
      e.preventDefault(); show($("login-error"), false);
      connect($("token").value.trim(), $("remember").checked).catch(function (err) {
        $("login-error").textContent = err.message || "Could not connect."; show($("login-error"), true);
      });
    });
    $("disconnect").addEventListener("click", disconnect);

    document.querySelectorAll(".tab").forEach(function (t) {
      t.addEventListener("click", function () {
        document.querySelectorAll(".tab").forEach(function (x) { x.classList.toggle("active", x === t); });
        ["chat", "tools", "logs"].forEach(function (n) { show($("tab-" + n), n === t.dataset.tab); });
        show($("sidebar"), t.dataset.tab === "chat");
        if (t.dataset.tab === "logs") loadLogs();
      });
    });

    $("composer").addEventListener("submit", function (e) {
      e.preventDefault(); var t = $("input").value; $("input").value = ""; send(t);
    });
    $("input").addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("composer").requestSubmit(); }
    });
    $("send-frontier").addEventListener("click", sendToFrontier);
    $("dismiss-offer").addEventListener("click", hideOffer);

    $("consent").addEventListener("change", function () {
      api("POST", "/session/consent", { consent: $("consent").checked }).then(refreshSession);
    });
    $("new-session").addEventListener("click", function () {
      api("POST", "/session/new").then(function () {
        clear($("messages")); hideOffer(); state.context = []; renderChips();
        addMsg("note", "New session started. Taint and consent cleared."); return refreshSession();
      });
    });

    $("ctx-add").addEventListener("click", function () {
      var text = $("ctx-text").value, source = $("ctx-source").value.trim();
      if (!text.trim() || !source) return;
      state.context.push({ text: text, source: uniqueSourceName(sanitizeSourceName(source)) });
      $("ctx-text").value = ""; renderChips();
    });

    $("attach-btn").addEventListener("click", function () { $("attach-input").click(); });
    $("attach-input").addEventListener("change", function () {
      addFilesAsContext(this.files); this.value = "";
    });
    var dz = $("dropzone"), dragDepth = 0;
    ["dragenter", "dragover"].forEach(function (evt) {
      dz.addEventListener(evt, function (e) {
        if (!Array.prototype.includes.call(e.dataTransfer.types || [], "Files")) return;
        e.preventDefault(); e.dataTransfer.dropEffect = "copy";
        if (evt === "dragenter") dragDepth++;
        dz.classList.add("drag-over");
      });
    });
    dz.addEventListener("dragleave", function () {
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) dz.classList.remove("drag-over");
    });
    dz.addEventListener("drop", function (e) {
      e.preventDefault(); dragDepth = 0; dz.classList.remove("drag-over");
      if (e.dataTransfer.files && e.dataTransfer.files.length) addFilesAsContext(e.dataTransfer.files);
    });

    document.querySelectorAll("[data-tool]").forEach(function (b) {
      b.addEventListener("click", function () { runTool(b.dataset.tool); });
    });
    $("refresh-logs").addEventListener("click", loadLogs);

    // A file dropped outside the dropzone would otherwise navigate the tab away to open it.
    ["dragover", "drop"].forEach(function (evt) {
      document.addEventListener(evt, function (e) {
        if (Array.prototype.includes.call(e.dataTransfer.types || [], "Files")) e.preventDefault();
      });
    });

    pollHealth(); setInterval(pollHealth, 15000);
    var saved = store(function () { return sessionStorage.getItem("pai_token"); });
    if (saved) connect(saved, true).catch(function () { store(function () { sessionStorage.removeItem("pai_token"); }); });
  });
})();
