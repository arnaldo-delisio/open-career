// Service worker: the ambient (no user gesture) case, which is the one at risk under
// Chrome's Local Network Access gate. Results are appended to chrome.storage.local so
// they survive service-worker termination and can be read from any surface.

const ENDPOINTS = [
  ["localhost", "http://localhost:8787/probe"],
  ["127.0.0.1", "http://127.0.0.1:8787/probe"],
];

async function record(entry) {
  const stamped = { ...entry, at: new Date().toISOString() };
  console.log("[LNA-PROBE]", JSON.stringify(stamped));
  const { results = [] } = await chrome.storage.local.get("results");
  results.push(stamped);
  await chrome.storage.local.set({ results });
}

async function probeFetch(transport, label, url) {
  const started = Date.now();
  try {
    const res = await fetch(url, { cache: "no-store" });
    const body = await res.text();
    await record({
      transport,
      host: label,
      url,
      outcome: "ok",
      status: res.status,
      ms: Date.now() - started,
      body: body.slice(0, 200),
    });
    return true;
  } catch (err) {
    await record({
      transport,
      host: label,
      url,
      outcome: "error",
      ms: Date.now() - started,
      error: `${err && err.name}: ${err && err.message}`,
    });
    return false;
  }
}

async function runFetchSuite(transport) {
  for (const [label, url] of ENDPOINTS) {
    await probeFetch(transport, label, url);
  }
}

// --- ambient case: fires at install and at every service-worker startup, with no
// user gesture anywhere in the call stack.
chrome.runtime.onInstalled.addListener(() => {
  record({ transport: "sw-ambient", outcome: "marker", note: "onInstalled fired" });
  runFetchSuite("sw-ambient-oninstalled");
  startWebSocket();
});

chrome.runtime.onStartup.addListener(() => {
  runFetchSuite("sw-ambient-onstartup");
});

// Also fire immediately at top level of the worker: the purest ambient case.
runFetchSuite("sw-ambient-toplevel");

// An alarm-driven ambient fetch: the realistic background-poll shape.
chrome.alarms?.create("probe", { periodInMinutes: 1 });
chrome.alarms?.onAlarm.addListener(() => runFetchSuite("sw-ambient-alarm"));

// --- content-script relay -------------------------------------------------------
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.type === "probe-relay") {
    (async () => {
      const results = [];
      for (const [label, url] of ENDPOINTS) {
        results.push(await probeFetch("content-relay", label, url));
      }
      sendResponse({ done: true, results });
    })();
    return true;
  }
  if (msg?.type === "probe-gesture") {
    (async () => {
      const results = [];
      for (const [label, url] of ENDPOINTS) {
        results.push(await probeFetch("sidepanel-gesture-relay", label, url));
      }
      sendResponse({ done: true, results });
    })();
    return true;
  }
  if (msg?.type === "ws-status") {
    sendResponse(wsStatus);
    return false;
  }
  return false;
});

// --- WebSocket from the service worker -------------------------------------------
// The source note claims active WS traffic resets the MV3 30s idle timer (Chrome 116+).
// We open it ambiently and log lifetime, so a >30s survival is direct evidence.
let wsStatus = { state: "not-started" };
let ws = null;

function startWebSocket() {
  if (ws) return;
  const opened = Date.now();
  wsStatus = { state: "connecting", opened };
  try {
    ws = new WebSocket("ws://127.0.0.1:8787/ws");
  } catch (err) {
    wsStatus = { state: "construct-threw", error: `${err.name}: ${err.message}` };
    record({ transport: "websocket", outcome: "error", error: wsStatus.error });
    return;
  }
  ws.onopen = () => {
    wsStatus = { state: "open", opened };
    record({ transport: "websocket", outcome: "ok", note: "onopen" });
    ws.send(JSON.stringify({ hello: "probe" }));
  };
  ws.onmessage = (ev) => {
    const alive = Math.round((Date.now() - opened) / 1000);
    wsStatus = { state: "open", opened, aliveSeconds: alive };
    // Log only at milestones so storage does not fill with ticks.
    if ([5, 10, 20, 35, 50, 65, 95].includes(alive)) {
      record({
        transport: "websocket",
        outcome: "ok",
        note: `alive ${alive}s, sw still running`,
        sample: String(ev.data).slice(0, 120),
      });
    }
  };
  ws.onerror = () => {
    wsStatus = { state: "error", opened };
    record({ transport: "websocket", outcome: "error", error: "onerror fired" });
  };
  ws.onclose = (ev) => {
    const alive = Math.round((Date.now() - opened) / 1000);
    wsStatus = { state: "closed", opened, aliveSeconds: alive };
    record({
      transport: "websocket",
      outcome: "closed",
      note: `closed after ${alive}s code=${ev.code} clean=${ev.wasClean}`,
    });
    ws = null;
  };
}

startWebSocket();

chrome.action.onClicked?.addListener?.((tab) => {
  chrome.sidePanel.open({ windowId: tab.windowId });
});
chrome.sidePanel
  ?.setPanelBehavior?.({ openPanelOnActionClick: true })
  .catch(() => {});
