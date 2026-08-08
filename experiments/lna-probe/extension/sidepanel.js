// Side panel: the user-gesture case, our fallback architecture if ambient fetches are
// gated. Both variants are covered: a fetch issued from the panel's own extension page,
// and a fetch the panel asks the service worker to perform inside the gesture's turn.

const out = document.getElementById("out");
const say = (s) => {
  out.textContent = `${s}\n${out.textContent}`;
  console.log("[LNA-PROBE][sidepanel]", s);
};

async function record(entry) {
  const stamped = { ...entry, at: new Date().toISOString() };
  const { results = [] } = await chrome.storage.local.get("results");
  results.push(stamped);
  await chrome.storage.local.set({ results });
}

document.getElementById("direct").addEventListener("click", async () => {
  for (const url of [
    "http://localhost:8787/probe",
    "http://127.0.0.1:8787/probe",
  ]) {
    const t0 = Date.now();
    try {
      const res = await fetch(url, { cache: "no-store" });
      const body = await res.text();
      const e = {
        transport: "sidepanel-gesture-direct",
        url,
        outcome: "ok",
        status: res.status,
        ms: Date.now() - t0,
        body: body.slice(0, 200),
      };
      await record(e);
      say(JSON.stringify(e));
    } catch (err) {
      const e = {
        transport: "sidepanel-gesture-direct",
        url,
        outcome: "error",
        ms: Date.now() - t0,
        error: `${err.name}: ${err.message}`,
      };
      await record(e);
      say(JSON.stringify(e));
    }
  }
});

document.getElementById("relay").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "probe-gesture" }, (resp) => {
    say(
      chrome.runtime.lastError
        ? `relay lastError: ${chrome.runtime.lastError.message}`
        : `relay response: ${JSON.stringify(resp)}`
    );
  });
});

document.getElementById("ws").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "ws-status" }, (resp) => {
    say(
      chrome.runtime.lastError
        ? `ws lastError: ${chrome.runtime.lastError.message}`
        : `ws status: ${JSON.stringify(resp)}`
    );
  });
});

document.getElementById("dump").addEventListener("click", async () => {
  const { results = [] } = await chrome.storage.local.get("results");
  say(results.map((r) => JSON.stringify(r)).join("\n"));
});

document.getElementById("clear").addEventListener("click", async () => {
  await chrome.storage.local.set({ results: [] });
  out.textContent = "";
});
