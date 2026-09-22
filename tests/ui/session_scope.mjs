// Session-scope scenario: login, chat, consent, taint lock, new session, tools, logs, mobile layout.
import { start } from "./cdp_lib.mjs";
const [appPort, debugPort, shots] = process.argv.slice(2);
const { send, ev, until, shot, check, problems, finish } = await start(debugPort, shots);
const BASE = `http://127.0.0.1:${appPort}`;
await send("Page.navigate", { url: BASE + "/" });
await until("document.readyState === 'complete' && !!document.getElementById('login')", "page load");
check("root redirects to /ui", (await ev("location.pathname")) === "/ui");
await until("document.getElementById('health').textContent.includes('up')", "health pill");
await shot("1-login");

// wrong token
await ev("document.getElementById('token').value='nope'; document.getElementById('login-form').requestSubmit(); 1");
await until("!document.getElementById('login-error').hidden", "login error");
check("wrong token is rejected", (await ev("document.getElementById('login-error').textContent")).includes("not accepted"));

// right token
await ev("document.getElementById('token').value='owner-token-123'; document.getElementById('login-form').requestSubmit(); 1");
await until("!document.getElementById('app').hidden", "app visible");
check("connects with the right token", await ev("document.getElementById('who').textContent") === "user: owner");
check("per-message consent checkbox is NOT visible in session scope", (await ev("document.getElementById('req-consent-wrap').offsetParent")) === null);
check("session consent checkbox is visible in session scope", (await ev("document.getElementById('consent-wrap').offsetParent")) !== null);
check("no model picker is shown", (await ev("document.getElementById('model')")) === null);
check("session starts CLEAN", (await ev("document.getElementById('taint-badge').textContent")) === "CLEAN");

// clean question, local answer, frontier offered
const say = async (text) => { await ev(`document.getElementById('input').value=${JSON.stringify(text)}; document.getElementById('composer').requestSubmit(); 1`); await until("!document.getElementById('send').disabled", "reply"); };
await say("What is 2+2?");
check("answer shows lane local", (await ev("document.querySelector('.msg.assistant .badge.lane-private')?.textContent")) === "lane: local");
check("answer shows taint CLEAN", (await ev("document.querySelector('.msg.assistant .badge.t-CLEAN')?.textContent")) === "taint: CLEAN");
check("frontier offer is shown", await ev("!document.getElementById('offer').hidden"));
const shown = await ev("document.querySelector('.msg.assistant').firstChild.textContent");
check("reply rendered as text, image markup stripped by the gateway", shown.includes("[image removed]") && !shown.includes("evil.example"), JSON.stringify(shown.slice(0, 90)));
check("no <img> element exists in the page", (await ev("document.querySelectorAll('img').length")) === 0);
await shot("2-local-answer-with-offer");

// consent and send to the frontier
await ev("window.confirm = () => true; document.getElementById('send-frontier').click(); 1");
await until("document.querySelectorAll('.msg.assistant').length >= 2 && !document.getElementById('send').disabled", "frontier reply");
const last = await ev("Array.from(document.querySelectorAll('.msg.assistant')).pop().firstChild.textContent");
check("frontier answer arrives after consent", last === "frontier answer", last);
check("frontier answer is labelled FRONTIER", (await ev("Array.from(document.querySelectorAll('.msg.assistant')).pop().querySelector('.badge.lane-frontier')?.textContent")) === "lane: frontier");
check("sidebar shows consent given", (await ev("document.getElementById('s-consent').textContent")) === "given");
await shot("3-frontier-answer");

// private context locks the session
await ev(`
  var dt = new DataTransfer();
  dt.items.add(new File(["salary bands: L5 = 250k"], "salary.txt", {type: "text/plain"}));
  document.getElementById('attach-input').files = dt.files;
  document.getElementById('attach-input').dispatchEvent(new Event('change', {bubbles:true}));
`);
await until("document.querySelectorAll('#ctx-list li').length === 1", "chip added");
check("context chip added", (await ev("document.querySelectorAll('#ctx-list li').length")) === 1);
await say("Summarise the note");
check("private context gives taint PRIVATE", (await ev("document.getElementById('taint-badge').textContent")) === "PRIVATE");
check("no frontier offer when PRIVATE", await ev("document.getElementById('offer').hidden"));
check("sidebar says consent has no effect while PRIVATE", (await ev("document.getElementById('s-consent').textContent")).includes("no effect"));
check("assistant badge says PRIVATE", (await ev("Array.from(document.querySelectorAll('.msg.assistant')).pop().querySelector('.badge.t-PRIVATE')?.textContent")) === "taint: PRIVATE");
await say("innocent follow up");
check("follow-up stays local (history taint)", (await ev("Array.from(document.querySelectorAll('.msg.assistant')).pop().querySelector('.badge.lane-private')?.textContent")) === "lane: local");
await shot("4-private-locked");

// new session
await ev("document.getElementById('new-session').click(); 1");
await until("document.getElementById('taint-badge').textContent === 'CLEAN' && document.querySelectorAll('.msg.assistant').length === 0", "new session");
check("New session clears taint and the transcript", true);
check("consent cleared by new session", (await ev("document.getElementById('s-consent').textContent")) === "not given");

// attach a file via the + button (real File objects, real FileReader, no mocking)
await ev(`
  var dt = new DataTransfer();
  dt.items.add(new File(["salary bands: L5 = 250k"], "notes.txt", {type: "text/plain"}));
  document.getElementById('attach-input').files = dt.files;
  document.getElementById('attach-input').dispatchEvent(new Event('change', {bubbles:true}));
`);
await until("document.querySelectorAll('#ctx-list li').length === 1", "file chip added via the + button");
check("a file chosen with the + button becomes a context chip named after the file",
  (await ev("document.getElementById('ctx-list').textContent")).includes("notes.txt"));

// drag a second file onto the message box itself: real dragenter/dragover/drop events
await ev(`
  window.__dt2 = new DataTransfer();
  window.__dt2.items.add(new File(["board minutes: acquisition of ExampleCorp"], "board.md", {type: "text/markdown"}));
  var dz = document.getElementById('dropzone');
  dz.dispatchEvent(new DragEvent('dragenter', {bubbles:true, cancelable:true, dataTransfer: window.__dt2}));
  dz.dispatchEvent(new DragEvent('dragover', {bubbles:true, cancelable:true, dataTransfer: window.__dt2}));
`);
check("dragging a file over the message box highlights it as a drop target",
  await ev("document.getElementById('dropzone').classList.contains('drag-over')"));
await ev("document.getElementById('dropzone').dispatchEvent(new DragEvent('drop', {bubbles:true, cancelable:true, dataTransfer: window.__dt2}));");
await until("document.querySelectorAll('#ctx-list li').length === 2", "second file chip added via drop");
check("dropping a file onto the message box attaches it",
  (await ev("document.getElementById('ctx-list').textContent")).includes("board.md"));
check("the drag-over highlight clears after the drop",
  !(await ev("document.getElementById('dropzone').classList.contains('drag-over')")));
await shot("4b-file-attached");

// a real PDF is sent to /v1/extract-text and its extracted text becomes a context chip
await ev(`
  function minimalPdf(lines) {
    var esc = s => s.replace(/\\\\/g, "\\\\\\\\").replace(/\\(/g, "\\\\(").replace(/\\)/g, "\\\\)");
    var ops = ["BT", "/F1 12 Tf", "72 720 Td", "16 TL"].concat(lines.map(l => "(" + esc(l) + ") Tj T*")).concat(["ET"]).join("\\n");
    var objs = ["<</Type/Catalog/Pages 2 0 R>>", "<</Type/Pages/Kids[3 0 R]/Count 1>>",
      "<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
      "<</Length " + ops.length + ">>\\nstream\\n" + ops + "\\nendstream", "<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"];
    var out = "%PDF-1.4\\n", offsets = [];
    objs.forEach((body, i) => { offsets.push(out.length); out += (i + 1) + " 0 obj\\n" + body + "\\nendobj\\n"; });
    var xref = out.length;
    out += "xref\\n0 " + (objs.length + 1) + "\\n0000000000 65535 f \\n";
    offsets.forEach(o => { out += String(o).padStart(10, "0") + " 00000 n \\n"; });
    out += "trailer\\n<</Size " + (objs.length + 1) + "/Root 1 0 R>>\\nstartxref\\n" + xref + "\\n%%EOF\\n";
    return out;
  }
  var bytes = new TextEncoder().encode(minimalPdf(["PDF ATTACH TEST 42", "generated in-browser for a real end-to-end check"]));
  window.__pdfDt = new DataTransfer();
  window.__pdfDt.items.add(new File([bytes], "sample.pdf", {type: "application/pdf"}));
  document.getElementById('attach-input').files = window.__pdfDt.files;
  document.getElementById('attach-input').dispatchEvent(new Event('change', {bubbles:true}));
`);
await until("document.querySelectorAll('#ctx-list li').length === 3", "PDF extracted and chip added", 20000);
check("a real PDF is extracted server-side and attached as context",
  (await ev("document.getElementById('ctx-list').textContent")).includes("sample.pdf"));

// an unsupported extension is refused, not silently attached
await ev(`
  var dt3 = new DataTransfer();
  dt3.items.add(new File(["binary-ish"], "program.exe", {type: "application/octet-stream"}));
  document.getElementById('attach-input').files = dt3.files;
  document.getElementById('attach-input').dispatchEvent(new Event('change', {bubbles:true}));
`);
await until("!document.getElementById('attach-error').hidden", "attach error shown for a bad extension");
check("an unsupported file type is rejected with a visible message and no new chip",
  (await ev("document.getElementById('attach-error').textContent")).includes("unsupported type") &&
  (await ev("document.querySelectorAll('#ctx-list li').length")) === 3);

// an oversized file is refused too
await ev(`
  var dt4 = new DataTransfer();
  dt4.items.add(new File(["x".repeat(400000)], "huge.txt", {type: "text/plain"}));
  document.getElementById('attach-input').files = dt4.files;
  document.getElementById('attach-input').dispatchEvent(new Event('change', {bubbles:true}));
`);
await until("document.getElementById('attach-error').textContent.includes('too large')", "attach error shown for an oversized file");
check("an oversized file is rejected, not attached", (await ev("document.querySelectorAll('#ctx-list li').length")) === 3);

// attached files taint the session exactly like pasted context, and both are sent
await say("what do the attached files say?");
check("attached files taint the session PRIVATE, same as pasted context",
  (await ev("document.getElementById('taint-badge').textContent")) === "PRIVATE");
const attachedNote = await ev("Array.from(document.querySelectorAll('.msg.note')).find(n => n.textContent.includes('attached context'))?.textContent || ''");
check("all three attached files were sent as context, named by their filenames",
  attachedNote.includes("notes.txt") && attachedNote.includes("board.md") && attachedNote.includes("sample.pdf"), attachedNote);
check("chips are cleared from the composer after sending", (await ev("document.querySelectorAll('#ctx-list li').length")) === 0);

await ev("document.getElementById('new-session').click(); 1");
await until("document.getElementById('taint-badge').textContent === 'CLEAN'", "session reset after the attach test");

check("no lane picker or documents checkbox remain in the composer",
  (await ev("document.getElementById('lane')")) === null && (await ev("document.getElementById('docs')")) === null);

check("tools tab is removed from the UI", (await ev("document.querySelector('[data-tab=tools]')")) === null);

// logs tab
await ev("document.querySelector('[data-tab=logs]').click(); 1");
await until("document.querySelectorAll('#audit-table tbody tr').length > 0 && !document.querySelector('#audit-table td.empty')", "audit rows");
const row = await ev("Array.from(document.querySelectorAll('#audit-table tbody tr td')).map(t=>t.textContent).join(' | ')");
check("audit tab shows the one frontier call", (await ev("document.querySelectorAll('#audit-table tbody tr').length")) === 1 && row.includes("frontier") && row.includes("session"), row);
check("audit tab shows hash, not text", row.includes("…") && !row.includes("2+2"));
await shot("6-logs");

// disconnect
await ev("document.getElementById('disconnect').click(); 1");
check("disconnect returns to login", await ev("!document.getElementById('login').hidden && document.getElementById('app').hidden"));

// mobile layout
await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 800, deviceScaleFactor: 2, mobile: true });
await ev("document.getElementById('token').value='owner-token-123'; document.getElementById('login-form').requestSubmit(); 1");
await until("!document.getElementById('app').hidden", "app visible (mobile)");
await shot("7-mobile");

// mobile layout: nothing may overflow the page, and the status pill must stay a pill
await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 800, deviceScaleFactor: 2, mobile: true });
await ev("document.getElementById('token').value='owner-token-123'; document.getElementById('login-form').requestSubmit(); 1");
await until("!document.getElementById('app').hidden", "app visible (mobile)");
check("disconnect resets to the Chat tab", await ev("document.querySelector('.tab.active').dataset.tab === 'chat'"));
await ev("document.querySelector('[data-tab=logs]').click(); 1");
await until("document.querySelectorAll('#audit-table tbody tr').length > 0", "logs (mobile)");
check("mobile: page does not scroll sideways", await ev("document.documentElement.scrollWidth <= window.innerWidth + 1"));
check("mobile: status pill is one line", (await ev("document.getElementById('health').offsetHeight")) < 40);
await shot("7-mobile");
check("no CSP violations, script exceptions or console errors", problems.length === 0, problems.join("; "));
finish("SESSION-SCOPE UI");
