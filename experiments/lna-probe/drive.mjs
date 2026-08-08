// CDP driver for the LNA probe. Attaches to an already-running headed Chrome, finds the
// extension's service worker target, drains its console, reads chrome.storage.local, and
// exercises the side-panel gesture path. Node 24's global WebSocket, no dependencies.

const PORT = process.env.CDP_PORT || "9401";
const base = `http://127.0.0.1:${PORT}`;

async function targets() {
  const r = await fetch(`${base}/json/list`);
  return r.json();
}

function attach(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    let id = 0;
    const pending = new Map();
    const events = [];
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id && pending.has(m.id)) {
        const { res } = pending.get(m.id);
        pending.delete(m.id);
        res(m);
      } else if (m.method) {
        events.push(m);
      }
    };
    ws.onerror = (e) => reject(new Error(`ws error ${e.message || ""}`));
    ws.onopen = () =>
      resolve({
        events,
        send(method, params = {}) {
          const mid = ++id;
          ws.send(JSON.stringify({ id: mid, method, params }));
          return new Promise((res) => pending.set(mid, { res }));
        },
        close: () => ws.close(),
      });
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const [, , cmd = "report"] = process.argv;

const list = await targets();
const sw = list.find(
  (t) => t.type === "service_worker" && t.url.includes("/sw.js")
);
if (!sw) {
  console.log("NO SERVICE WORKER TARGET. Targets seen:");
  for (const t of list) console.log(" -", t.type, t.url);
  process.exit(1);
}
const extId = new URL(sw.url).host;
console.log(`extension id: ${extId}`);
console.log(`sw target:    ${sw.url}`);

const c = await attach(sw.webSocketDebuggerUrl);
await c.send("Runtime.enable");
await c.send("Log.enable");
await sleep(500);

if (cmd === "gesture") {
  // Open the side panel page as a normal tab and click the buttons: an extension-page
  // context with a real user activation behind the fetch.
  const t = await fetch(
    `${base}/json/new?chrome-extension://${extId}/sidepanel.html`,
    { method: "PUT" }
  ).then((r) => r.json());
  const p = await attach(t.webSocketDebuggerUrl);
  await p.send("Runtime.enable");
  await sleep(800);
  for (const btn of ["direct", "relay", "ws"]) {
    const r = await p.send("Runtime.evaluate", {
      expression: `document.getElementById(${JSON.stringify(
        btn
      )}).dispatchEvent(new MouseEvent('click',{bubbles:true}))`,
      userGesture: true,
      awaitPromise: false,
    });
    console.log(`clicked #${btn}:`, JSON.stringify(r.result?.result ?? r.result));
    await sleep(2500);
  }
  const dump = await p.send("Runtime.evaluate", {
    expression: "document.getElementById('out').textContent",
    returnByValue: true,
  });
  console.log("--- side panel output ---");
  console.log(dump.result?.result?.value || "(empty)");
  p.close();
}

const stored = await c.send("Runtime.evaluate", {
  expression:
    "chrome.storage.local.get('results').then(r => JSON.stringify(r.results||[], null, 1))",
  awaitPromise: true,
  returnByValue: true,
});
console.log("--- chrome.storage.local results ---");
console.log(stored.result?.result?.value ?? JSON.stringify(stored));

console.log("--- service worker console events ---");
for (const e of c.events) {
  if (e.method === "Runtime.consoleAPICalled") {
    console.log(
      "console:",
      e.params.args.map((a) => a.value ?? a.description).join(" ")
    );
  } else if (e.method === "Log.entryAdded") {
    console.log(
      `log[${e.params.entry.level}/${e.params.entry.source}]:`,
      e.params.entry.text
    );
  } else if (e.method === "Runtime.exceptionThrown") {
    console.log("exception:", JSON.stringify(e.params.exceptionDetails).slice(0, 400));
  }
}
c.close();
process.exit(0);
