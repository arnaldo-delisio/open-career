#!/usr/bin/env python3
"""Minimal loopback probe server for the Chrome Local Network Access experiment (OC-Q4).

Serves one JSON endpoint over HTTP and one WebSocket endpoint, both on a fixed
localhost port, using only the standard library. Every request is logged with its
Origin header so the extension's requests can be told apart from anything else.

Usage: python3 server.py [--port 8787] [--allow-origin chrome-extension://<id>]
The allow-origin default is "*", which is enough for the probe; pass the real
extension origin to check that a scoped ACAO also works.
"""

import argparse
import base64
import hashlib
import json
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

ALLOW_ORIGIN = "*"
LOG_LOCK = threading.Lock()


def log(*parts):
    with LOG_LOCK:
        print(f"[{time.strftime('%H:%M:%S')}]", *parts, flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter default logging
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", ALLOW_ORIGIN)
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        # Local Network Access preflight opt-in: Chrome sends
        # Access-Control-Request-Private-Network / -Local-Network on the preflight
        # and requires the matching response header before allowing the request.
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Allow-Local-Network-Access", "true")

    def do_OPTIONS(self):
        origin = self.headers.get("Origin", "-")
        pna = self.headers.get("Access-Control-Request-Private-Network", "-")
        lna = self.headers.get("Access-Control-Request-Local-Network-Access", "-")
        log(f"OPTIONS {self.path} origin={origin} req-private-network={pna} req-lna={lna}")
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self.handle_websocket()
            return
        origin = self.headers.get("Origin", "-")
        log(f"GET {self.path} origin={origin}")
        body = json.dumps(
            {"ok": True, "path": self.path, "origin": origin, "ts": time.time()}
        ).encode()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- minimal RFC 6455 server, enough to hold a connection open and echo -----

    def handle_websocket(self):
        key = self.headers.get("Sec-WebSocket-Key")
        origin = self.headers.get("Origin", "-")
        log(f"WS upgrade {self.path} origin={origin}")
        if not key:
            self.send_response(400)
            self.end_headers()
            return
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()
        ).decode()
        self.wfile.write(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode()
        )
        self.wfile.flush()
        log("WS connected")
        conn = self.connection
        conn.settimeout(1.0)
        deadline = time.time() + 300
        last_ping = 0.0
        try:
            while time.time() < deadline:
                # server-initiated traffic every 5s: this is what the MV3 keepalive relies on
                if time.time() - last_ping > 5:
                    self._ws_send(json.dumps({"tick": time.time()}))
                    last_ping = time.time()
                try:
                    frame = self._ws_recv()
                except socket.timeout:
                    continue
                if frame is None:
                    break
                log(f"WS recv: {frame!r}")
                self._ws_send(json.dumps({"echo": frame}))
        except (OSError, ConnectionError) as exc:
            log(f"WS closed: {exc}")
        log("WS disconnected")

    def _ws_send(self, text):
        payload = text.encode()
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(n)
        elif n < (1 << 16):
            header.append(126)
            header += struct.pack(">H", n)
        else:
            header.append(127)
            header += struct.pack(">Q", n)
        self.connection.sendall(bytes(header) + payload)

    def _read_exactly(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.connection.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _ws_recv(self):
        head = self._read_exactly(2)
        if head is None:
            return None
        opcode = head[0] & 0x0F
        masked = head[1] & 0x80
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exactly(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exactly(8))[0]
        mask = self._read_exactly(4) if masked else b"\x00\x00\x00\x00"
        data = self._read_exactly(length) if length else b""
        if data is None:
            return None
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if opcode == 0x8:  # close
            return None
        return data.decode("utf-8", "replace")


def main():
    global ALLOW_ORIGIN
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--allow-origin", default="*")
    args = ap.parse_args()
    ALLOW_ORIGIN = args.allow_origin
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    log(f"probe server on http://127.0.0.1:{args.port}/ acao={ALLOW_ORIGIN}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
