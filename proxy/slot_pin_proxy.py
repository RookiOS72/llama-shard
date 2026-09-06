#!/usr/bin/env python3
"""
slot-pin-proxy: sits between an OpenAI-compatible client (openclaw) and
llama-server, and forces every completion request onto one fixed
llama-server slot via the native `id_slot` param.

Why: llama-server's own prompt cache (`--cache-prompt`, on by default)
reuses the common prefix between a request and whatever a slot already
holds, but only if the request lands on the *same* slot as last time.
The OpenAI-compatible endpoint has no client-facing way to request a
specific slot, and the automatic prompt-similarity picker can bounce a
single ongoing conversation across different slots (observed directly:
task 273/287 on slot 2, 814/829 on slot 1, 1395/1408 on slot 0 -- same
logical openclaw session, three different slots), throwing away cache
that would otherwise apply. Pinning to one slot makes that reuse
guaranteed instead of lucky.

Today openclaw only has one real conversation identity (the "main"
agent; heartbeats run inside that same session -- see openclaw.json),
so a single fixed slot for everything is the correct-sized fix. If a
second identity shows up later (e.g. a separate agent), extend
`pick_slot()` to map identities to distinct slot numbers instead of
rewriting this.
"""
import http.client
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_HOST = os.environ.get("CARAVAN_UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.environ.get("CARAVAN_UPSTREAM_PORT", "8080"))
LISTEN_PORT = int(os.environ.get("CARAVAN_PROXY_PORT", "8090"))
PINNED_SLOT = int(os.environ.get("CARAVAN_PINNED_SLOT", "0"))

# Paths that take a JSON completion-shaped body and accept `id_slot`.
COMPLETION_PATHS = {"/v1/chat/completions", "/v1/completions", "/completion"}


def pick_slot(_body: dict) -> int:
    """Single fixed slot for now -- see module docstring."""
    return PINNED_SLOT


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[slot-pin-proxy] " + (fmt % args) + "\n")

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            # Harmless: client closed a keep-alive connection between
            # requests instead of sending another one.
            self.close_connection = True

    def _forward(self, method: str):
        raw_len = int(self.headers.get("Content-Length", 0) or 0)
        raw_body = self.rfile.read(raw_len) if raw_len else b""

        slot_note = ""
        if self.path in COMPLETION_PATHS and raw_body:
            try:
                data = json.loads(raw_body)
            except ValueError:
                data = None
            if isinstance(data, dict):
                slot = pick_slot(data)
                data["id_slot"] = slot
                raw_body = json.dumps(data).encode("utf-8")
                slot_note = f" id_slot={slot}"

        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() not in ("host", "content-length")
        }
        headers["Content-Length"] = str(len(raw_body))
        headers["Host"] = f"{UPSTREAM_HOST}:{UPSTREAM_PORT}"

        conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT)
        try:
            try:
                conn.request(method, self.path, body=raw_body, headers=headers)
                upstream_resp = conn.getresponse()
            except OSError as e:
                # llama-server isn't up yet/crashed -- common right after a
                # launchd (re)start race, not worth a traceback.
                self.log_message("%s %s -> upstream unreachable (%s)", method, self.path, e)
                body = b"llama-server upstream unreachable\n"
                self.send_response(502)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self.log_message(
                "%s %s -> %s%s", method, self.path, upstream_resp.status, slot_note
            )

            self.send_response(upstream_resp.status)
            for k, v in upstream_resp.getheaders():
                if k.lower() in ("transfer-encoding", "connection"):
                    continue
                self.send_header(k, v)
            self.end_headers()

            # Stream the response back as it arrives -- required for SSE
            # (stream: true) so tokens still show up incrementally.
            while True:
                chunk = upstream_resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()

    def do_GET(self):
        self._forward("GET")

    def do_POST(self):
        self._forward("POST")

    def do_PUT(self):
        self._forward("PUT")

    def do_DELETE(self):
        self._forward("DELETE")


def main():
    server = ThreadingHTTPServer(("127.0.0.1", LISTEN_PORT), ProxyHandler)
    print(
        f"[slot-pin-proxy] listening on 127.0.0.1:{LISTEN_PORT}, "
        f"forwarding to {UPSTREAM_HOST}:{UPSTREAM_PORT}, "
        f"pinning all completions to id_slot={PINNED_SLOT}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
