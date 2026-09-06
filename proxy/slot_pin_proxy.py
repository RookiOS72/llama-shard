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
so a single preferred slot for everything is the correct-sized fix. If
a second identity shows up later (e.g. a separate agent), extend
`pick_slot()` to map identities to distinct slot numbers instead of
rewriting this.

2026-09-06 update: pinning was originally unconditional (always
id_slot=PINNED_SLOT), which is fine for one request at a time but
actively harmful when two calls for the same session overlap (e.g. a
heartbeat firing while a manual/CLI turn is still mid-flight on slow
hardware): the second request grabbing the same busy slot forces
llama-server to cancel whatever the first one was doing. Now `pick_slot`
checks `/slots` first and only pins to PINNED_SLOT if it's actually
idle; otherwise it hands the request to any other idle slot so two
overlapping calls don't fight over one. Falls back to PINNED_SLOT
(old behavior) only if every slot is busy or the `/slots` check itself
fails -- no worse than before in that case, just no better.

2026-09-06 update #2: that fix stopped collisions, but surfaced a
different cost -- two *different* sessions (e.g. the heartbeat's session
and an interactive one) landing on two different idle slots and actually
running concurrently. llama-server's RPC-sharded backend doesn't give
each concurrent job its own throughput; they visibly split it (watched
one job's speed drop from ~94 tok/s to ~62 tok/s as a second and third
started). A FIFO_LOCK below serializes completion requests through the
proxy -- only one is ever in flight to llama-server at a time, others
wait in strict arrival order -- so nothing loses throughput to a
concurrent neighbor. This does NOT reintroduce the cache-thrashing
problem `pick_slot` above was built to avoid: cache lives on the slot,
not on request timing, and slots stay assigned to whichever session last
used them regardless of serialization -- see docs/LOG.md.
"""
import http.client
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_HOST = os.environ.get("CARAVAN_UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.environ.get("CARAVAN_UPSTREAM_PORT", "8080"))
LISTEN_PORT = int(os.environ.get("CARAVAN_PROXY_PORT", "8090"))
PINNED_SLOT = int(os.environ.get("CARAVAN_PINNED_SLOT", "0"))

# Paths that take a JSON completion-shaped body and accept `id_slot`.
COMPLETION_PATHS = {"/v1/chat/completions", "/v1/completions", "/completion"}


class FifoGate:
    """Strict first-in-first-out mutual exclusion.

    A plain threading.Lock doesn't guarantee waiters are woken in arrival
    order -- this does, via an explicit ticket queue: each waiter takes a
    ticket, and only the ticket at the front of the line is ever allowed
    through. See module docstring, "2026-09-06 update #2".
    """

    def __init__(self):
        self._cv = threading.Condition()
        self._queue = []
        self._next_ticket = 0

    def acquire(self) -> tuple[int, int, float]:
        """Blocks until it's this caller's turn. Returns
        (ticket, queue_depth_ahead, waited_seconds) for logging."""
        start = time.monotonic()
        with self._cv:
            ticket = self._next_ticket
            self._next_ticket += 1
            self._queue.append(ticket)
            ahead = len(self._queue) - 1
            while self._queue[0] != ticket:
                self._cv.wait()
        return ticket, ahead, time.monotonic() - start

    def release(self, ticket: int) -> None:
        with self._cv:
            self._queue.remove(ticket)
            self._cv.notify_all()


completion_fifo = FifoGate()


def pick_slot(_body: dict) -> int:
    """Prefer PINNED_SLOT for cache reuse, but don't steal it from an
    in-flight request -- route to any idle slot instead when it's busy."""
    try:
        conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=2)
        conn.request("GET", "/slots")
        resp = conn.getresponse()
        slots = json.loads(resp.read())
        conn.close()
    except Exception:
        return PINNED_SLOT

    by_id = {s.get("id"): s for s in slots if isinstance(s, dict)}
    pinned = by_id.get(PINNED_SLOT)
    if pinned is not None and not pinned.get("is_processing"):
        return PINNED_SLOT
    for s in slots:
        if isinstance(s, dict) and not s.get("is_processing"):
            return s.get("id", PINNED_SLOT)
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

        is_completion = self.path in COMPLETION_PATHS and bool(raw_body)
        ticket = None
        if is_completion:
            ticket, ahead, waited = completion_fifo.acquire()
            if ahead > 0 or waited > 0.05:
                self.log_message(
                    "%s %s -- waited %.1fs behind %d queued request(s)",
                    method, self.path, waited, ahead,
                )
        try:
            self._forward_locked(method, raw_body, is_completion)
        finally:
            if ticket is not None:
                completion_fifo.release(ticket)

    def _forward_locked(self, method: str, raw_body: bytes, is_completion: bool):
        slot_note = ""
        if is_completion:
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
