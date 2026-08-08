// Content script: relays a probe request to the service worker, which performs the
// actual loopback fetch under the extension's own host_permissions. Runs ambiently at
// document_idle, so there is no user gesture in this path either.
console.log("[LNA-PROBE] content script live on", location.href);
chrome.runtime.sendMessage({ type: "probe-relay" }, (resp) => {
  const err = chrome.runtime.lastError;
  console.log(
    "[LNA-PROBE] content relay response:",
    err ? `lastError: ${err.message}` : JSON.stringify(resp)
  );
});

// Also try a direct fetch from the content script's own context. This runs in the
// PAGE's origin, not the extension origin, so it is the case Local Network Access
// most plausibly gates. Useful as a contrast against the service-worker result.
fetch("http://127.0.0.1:8787/probe-from-content", { cache: "no-store" })
  .then((r) => r.text())
  .then((t) =>
    console.log("[LNA-PROBE] content DIRECT fetch ok:", t.slice(0, 150))
  )
  .catch((e) =>
    console.log("[LNA-PROBE] content DIRECT fetch FAILED:", e.name, e.message)
  );
