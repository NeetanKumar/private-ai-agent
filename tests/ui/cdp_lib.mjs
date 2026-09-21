// Minimal Chrome DevTools driver used by the UI browser tests. No dependencies (Node 22+).
import fs from "node:fs";
export async function start(debugPort, shots) {
const list = await (await fetch(`http://127.0.0.1:${debugPort}/json`)).json();
const page = list.find(t => t.type === "page");
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r));
let id = 0; const pending = new Map(); const events = []; const problems = [];
ws.addEventListener("message", m => {
  const d = JSON.parse(m.data);
  if (d.id && pending.has(d.id)) { pending.get(d.id)(d); pending.delete(d.id); }
  else if (d.method) {
    events.push(d);
    if (d.method === "Log.entryAdded") { const e = d.params.entry;
      if (e.source === "security" || /Content Security Policy|Refused to/i.test(e.text)) problems.push("CSP:" + e.text);
      else if (["error","warning"].includes(e.level) && !/Failed to load resource: the server responded with a status of (400|401|403)/.test(e.text)) problems.push("log:" + e.text); }
    if (d.method === "Runtime.exceptionThrown") problems.push("exception:" + JSON.stringify(d.params.exceptionDetails.text));
    if (d.method === "Runtime.consoleAPICalled" && d.params.type === "error") problems.push("console.error:" + JSON.stringify(d.params.args.map(a => a.value)));
  }
});
const send = (method, params = {}) => new Promise(r => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
const ev = async (expr) => { const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true }); if (r.result.exceptionDetails) throw new Error("eval failed: " + expr + " -> " + JSON.stringify(r.result.exceptionDetails)); return r.result.result.value; };
const until = async (expr, what, ms = 15000) => { const t = Date.now(); while (Date.now() - t < ms) { if (await ev(expr)) return; await new Promise(r => setTimeout(r, 100)); } throw new Error("timeout waiting for: " + what); };
const shot = async (name) => { const r = await send("Page.captureScreenshot", { format: "png" }); fs.writeFileSync(`${shots}/${name}.png`, Buffer.from(r.result.data, "base64")); };
const results = []; const check = (name, ok, extra = "") => { results.push([name, ok]); console.log((ok ? "PASS " : "FAIL ") + name + (extra ? "  " + extra : "")); };

  await send("Page.enable"); await send("Runtime.enable"); await send("Log.enable");
  await send("Emulation.setDeviceMetricsOverride", { width: 1200, height: 900, deviceScaleFactor: 1, mobile: false });
  const finish = (label) => { const bad = results.filter(r => !r[1]).length; console.log(bad === 0 ? `ALL ${results.length} ${label} CHECKS PASSED` : `${bad} ${label} CHECK(S) FAILED`); ws.close(); process.exit(bad ? 1 : 0); };
  return { send, ev, until, shot, check, results, problems, finish };
}
