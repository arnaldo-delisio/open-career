# LNA probe — does Chrome's Local Network Access gate apply to extension origins?

Settles open question OC-Q4. Chrome's Local Network Access (LNA) gate prompts a page before it
may reach a loopback address. If the same gate applied to `chrome-extension://` origins, the
extension could not call the local backend in the background, and every backend call would have
to originate from a user gesture in the side panel — a materially different architecture.

## What it contains

- `server.py` — stdlib-only loopback server on port 8787. Serves JSON on any path, upgrades
  `/ws` to a WebSocket that pushes a tick every 5s, logs the `Origin` of every request, and
  answers LNA/PNA preflights with the opt-in headers. No dependencies.
- `extension/` — Manifest V3 extension. `host_permissions` is scoped to
  `http://localhost:8787/*` and `http://127.0.0.1:8787/*` only, never `<all_urls>` (OC-1).
  It exercises four transports and records every outcome to `chrome.storage.local`, which
  survives service-worker termination:
  - `sw-ambient-*` — fetch from the service worker at worker top level, at `onInstalled`, at
    `onStartup`, and from an alarm. No user gesture anywhere in the call stack. The case at risk.
  - `content-relay` — content script → `runtime.sendMessage` → service worker → fetch.
  - `sidepanel-gesture-*` — side panel button click, both fetching directly from the panel page
    and asking the service worker to fetch inside the gesture's turn. The fallback path.
  - `websocket` — service-worker WebSocket to the local server, logging how long it stays open;
    a lifetime past 30s is direct evidence that WS traffic resets the MV3 idle timer.
  - The content script also issues a **direct** fetch from the *page's* origin. That is not a
    transport we would ship; it is the control that proves the LNA gate is actually active in
    the browser under test.
- `drive.mjs` — CDP driver (Node 24 global WebSocket, no dependencies). Finds the extension's
  service-worker target, drains its console, reads `chrome.storage.local`, and drives the side
  panel with `userGesture: true`.

## Running it

```bash
python3 server.py --port 8787 &

google-chrome --user-data-dir=/tmp/lna-profile --remote-debugging-port=9401 \
  --no-first-run --enable-unsafe-extension-debugging about:blank &

# Chrome 137+ ignores --load-extension; load over CDP instead.
# Note: do NOT pass --disable-extensions-except, which suppresses the load entirely.
node -e '...Extensions.loadUnpacked with path=./extension...'

CDP_PORT=9401 node drive.mjs report    # ambient + relay results
CDP_PORT=9401 node drive.mjs gesture   # side panel results
```

Run headed on a real display (per `decisions/browser-agent-capabilities.md`); the LNA prompt is
browser UI and is invisible to `Page.captureScreenshot`, so capture the X display to see it.

## Result, Chrome 150.0.7871.186 (Linux, headed)

All four extension transports reach loopback ungated, in both the default configuration and
with the gate forced to its strict setting
(`--enable-features=LocalNetworkAccessChecks:LocalNetworkAccessChecksWarn/false,LocalNetworkAccessChecksWebSockets,LocalNetworkAccessChecksWebTransport`).
No preflight was ever sent, and the server saw no `Origin` header, so the requests are being
treated as extension-privileged under `host_permissions` rather than as cross-origin web
requests. The service-worker WebSocket stayed open 187 seconds with no debugger attached and no
reconnect, so it does hold the worker alive past the 30s idle timer.

The control proves the gate is live in the same browser: a fetch to the same URL from a *page*
origin raises the "wants to access other apps and services on this device" prompt
(`evidence-lna-prompt-page-origin.png`) and never reaches the server.

## Confound to respect

Attaching a CDP debugger to a service worker keeps that worker alive. Any claim about the MV3
idle timer must be measured with **no debugger attached to the service-worker target** — read the
evidence from the server's connection log instead.
