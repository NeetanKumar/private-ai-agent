// Natural-language action call: typing plain English triggers a real tool call, a confirmation
// dialog (since reminder_create requires confirmation), and a final assistant reply - the actual
// capability this wiring pass was for, exercised through the real rendered chat box.
import { start } from "./cdp_lib.mjs";
const [appPort, debugPort, shots] = process.argv.slice(2);
const { send, ev, until, shot, check, problems, finish } = await start(debugPort, shots);
await send("Page.navigate", { url: `http://127.0.0.1:${appPort}/ui` });
await until("document.readyState === 'complete' && !!document.getElementById('login')", "load");
await ev("document.getElementById('token').value='owner-token-123'; document.getElementById('login-form').requestSubmit(); 1");
await until("!document.getElementById('app').hidden", "app");

// Approve the confirmation dialog the action tool stages for reminder_create.
await ev("window.confirm = () => true; 1");
await ev("document.getElementById('input').value='remind me to buy milk'; document.getElementById('composer').requestSubmit(); 1");
await until("document.querySelectorAll('.msg.assistant').length >= 1 && !document.getElementById('send').disabled", "final reply");

const notes = await ev("Array.from(document.querySelectorAll('.msg.note')).map(e => e.textContent).join(' | ')");
check("a tool-call note for reminder_create was rendered", notes.includes("reminder_create"), notes);
const lastAssistant = await ev("Array.from(document.querySelectorAll('.msg.assistant')).pop().textContent");
check("final assistant reply confirms the reminder was created", lastAssistant.includes("Done! I've created a reminder to buy milk"), lastAssistant);
check("no CSP violations, script exceptions or console errors", problems.length === 0, problems.join("; "));
finish("NATURAL-LANGUAGE ACTION CALL UI");
