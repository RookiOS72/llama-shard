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

2026-09-06 update #3: "whichever session last used them" turned out to
be the weak point -- there was only ever one shared PINNED_SLOT for
*every* session, so any two distinct sessions (e.g. the heartbeat's
`agent:main:main` and an interactive `scratch-test`) still evicted each
other's cache whenever they happened to both want slot 0, exactly the
thrashing the pinning design was meant to prevent. Confirmed live: a
`main` turn came in right after `scratch-test` retries had evicted its
cache, forcing a full ~40K-token cold reprocess that then blew through
the 900s timeout waiting in the FIFO queue behind other traffic. Fixed
by making the preferred slot per-session instead of global --
`SessionSlotAssigner` derives a stable fingerprint per session (hash of
its first non-system message, fixed for that session's life, no
cooperation needed from openclaw) and assigns it its own slot,
round-robin, on first sight. Distinct sessions now get distinct slots and
can't evict each other at all, up to N_SLOTS concurrently-active sessions
-- beyond that hard ceiling something has to give, which is a real
resource limit (each slot reserves its own chunk of context memory), not
a bug to chase further. See docs/LOG.md.

2026-09-06 update #4: none of the above stopped a request from running
to completion server-side after its client had already given up
(openclaw's own timeout, or a person closing the TUI) -- confirmed
directly tonight, a request kept processing for minutes after openclaw
had already logged it as a terminal error, still holding both an
llama-server slot and the FIFO gate the whole time, delaying every real
request queued behind it. `ProxyHandler._client_gone()` now polls the
client connection (non-blocking peek, no data consumed) while waiting on
a slow upstream and while streaming; the moment the client's gone, the
proxy shuts down its own connection to llama-server, which triggers
llama-server's own existing cancel-on-disconnect handling (the same
"stop: cancel task" path already seen tonight whenever the proxy itself
restarted mid-request) -- so an abandoned request now actually frees the
slot and the queue instead of grinding on for nobody.
"""
import hashlib
import http.client
import json
import os
import re
import select
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

UPSTREAM_HOST = os.environ.get("CARAVAN_UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.environ.get("CARAVAN_UPSTREAM_PORT", "8080"))
LISTEN_PORT = int(os.environ.get("CARAVAN_PROXY_PORT", "8090"))
N_SLOTS = int(os.environ.get("CARAVAN_N_SLOTS", "4"))

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


class SessionSlotAssigner:
    """Maps a stable per-session fingerprint to a dedicated slot, assigned
    round-robin the first time each session is seen -- so distinct
    sessions keep their own cache instead of evicting each other via a
    single shared preferred slot. See module docstring, update #3.

    With only N_SLOTS physical slots, more concurrently-active sessions
    than that can't all stay warm -- the (N_SLOTS+1)th distinct session
    reuses an assignment round-robin and may evict whoever's there. Real
    usage here is nowhere near that ceiling; not solved further than this.

    2026-09-06 update: persisted to disk (STATE_FILE below). Restarting
    *this proxy process* used to lose the whole mapping even though
    llama-server itself, and every slot's actual cache, was untouched --
    confirmed live tonight, a proxy-only restart caused a needless full
    cold reprocess that a still-warm slot could have served. This is
    deliberately a tiny fingerprint->slot number mapping, not the KV
    cache itself -- persisting the actual cache across an llama-server
    restart is the separate, much bigger issue #4.
    """

    def __init__(self, n_slots: int, state_file: "Path | None" = None):
        self._n_slots = n_slots
        self._lock = threading.Lock()
        self._assignments: dict[str, int] = {}
        self._next_slot = 0
        self._state_file = state_file
        self._load()

    def _load(self) -> None:
        if self._state_file is None or not self._state_file.exists():
            return
        try:
            data = json.loads(self._state_file.read_text())
            assignments = data.get("assignments", {})
            self._assignments = {
                k: v for k, v in assignments.items()
                if isinstance(v, int) and 0 <= v < self._n_slots
            }
            self._next_slot = int(data.get("next_slot", 0)) % self._n_slots
            sys.stderr.write(
                f"[slot-pin-proxy] restored {len(self._assignments)} session->slot "
                f"assignment(s) from {self._state_file}\n"
            )
        except (OSError, ValueError, json.JSONDecodeError) as e:
            # Corrupt or unreadable -- fine, just start fresh rather than
            # crash the proxy over a cache of a cache.
            sys.stderr.write(f"[slot-pin-proxy] could not restore slot assignments: {e}\n")

    def _save(self) -> None:
        if self._state_file is None:
            return
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "assignments": self._assignments,
                "next_slot": self._next_slot,
            }))
            tmp.replace(self._state_file)  # atomic on the same filesystem
        except OSError as e:
            sys.stderr.write(f"[slot-pin-proxy] could not persist slot assignments: {e}\n")

    def slot_for(self, fingerprint: str) -> int:
        with self._lock:
            slot = self._assignments.get(fingerprint)
            if slot is not None:
                return slot
            slot = self._next_slot % self._n_slots
            self._next_slot += 1
            self._assignments[fingerprint] = slot
            self._save()
            return slot


STATE_FILE = Path(os.environ.get(
    "CARAVAN_PROXY_STATE_FILE",
    str(Path.home() / ".caravan" / "proxy" / "session_slots.json"),
))
session_slots = SessionSlotAssigner(N_SLOTS, STATE_FILE)


# openclaw injects a synthetic marker as the first non-system message on
# every single call, e.g. "[Sun 2026-09-06 10:34 CDT] (session bootstrap)"
# -- timestamped to the current minute, not the session's real history.
# Root-caused 2026-09-06: this is why the naive "hash the first
# non-system message" approach fingerprinted the same real session
# differently on every call (confirmed live -- three requests, three
# different fingerprints, three different slots). Skip it and keep
# looking for the first message that isn't this synthetic wrapper.
_BOOTSTRAP_MARKER_RE = re.compile(r"^\[.*?\]\s*\(session bootstrap\)\s*$")


def session_fingerprint(body: dict) -> str:
    """Stable per-session key derived from the first *real* non-system,
    non-bootstrap-marker message -- fixed for a session's lifetime (that
    message never changes once sent), distinct across sessions, with no
    cooperation needed from openclaw (no session id is present in the
    OpenAI-compatible wire format)."""
    messages = body.get("messages")
    if isinstance(messages, list):
        skipped = []
        for m in messages:
            if not isinstance(m, dict) or m.get("role") == "system":
                continue
            content = m.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, sort_keys=True)
            if _BOOTSTRAP_MARKER_RE.match(content.strip()):
                skipped.append(content[:60])
                continue
            fp = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
            sys.stderr.write(
                f"[slot-pin-proxy][fp-debug] n_messages={len(messages)} "
                f"skipped_bootstrap_markers={len(skipped)} "
                f"used_role={m.get('role')!r} content_len={len(content)} "
                f"content_preview={content[:120]!r} fingerprint={fp}\n"
            )
            return fp
    # No usable message found (unusual) -- fall back to hashing the whole
    # body so it's at least stable within one retried request.
    fp = hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    sys.stderr.write(
        f"[slot-pin-proxy][fp-debug] no usable non-system message found, "
        f"hashed whole body, fingerprint={fp}\n"
    )
    return fp


def pick_slot(body: dict) -> int:
    """Prefer this session's assigned slot for cache reuse, but don't
    steal a busy slot from an in-flight request -- route to any other
    idle slot instead."""
    preferred = session_slots.slot_for(session_fingerprint(body))
    try:
        conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=2)
        conn.request("GET", "/slots")
        resp = conn.getresponse()
        slots = json.loads(resp.read())
        conn.close()
    except Exception:
        return preferred

    by_id = {s.get("id"): s for s in slots if isinstance(s, dict)}
    pinned = by_id.get(preferred)
    if pinned is not None and not pinned.get("is_processing"):
        return preferred
    for s in slots:
        if isinstance(s, dict) and not s.get("is_processing"):
            return s.get("id", preferred)
    return preferred


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # How often to check whether the client (openclaw) is still there while
    # we're blocked waiting on a slow upstream. See _client_gone().
    DISCONNECT_POLL_INTERVAL = 0.5

    def log_message(self, fmt, *args):
        sys.stderr.write("[slot-pin-proxy] " + (fmt % args) + "\n")

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            # Harmless: client closed a keep-alive connection between
            # requests instead of sending another one.
            self.close_connection = True

    def _client_gone(self) -> bool:
        """Non-blocking check for whether the client has closed its side
        of the connection -- used so a slow cold-start (minutes of prompt
        processing with no bytes sent yet) doesn't keep grinding away
        after openclaw has already given up and moved on (its own
        timeout, or the user killing the TUI). Peeks without consuming:
        a client that's just quietly waiting for our response shows up
        as "not readable" and is left alone; only an actual FIN/RST
        (readable, zero bytes, or a read error) counts as gone. See
        module docstring, "keep the LLM working only on requests someone
        is still waiting on" -- 2026-09-06."""
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
            if not ready:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

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
            # conn.getresponse() blocks with no timeout -- during a cold
            # prompt-processing run that can be many minutes with zero
            # bytes sent yet, so it happens on a background thread while
            # the main thread polls _client_gone(). If the client leaves,
            # shutting down conn's socket unblocks the thread immediately
            # and tells llama-server its client (us) disconnected, which
            # triggers its own existing cancel-on-disconnect handling --
            # same path already observed tonight when the proxy itself
            # was restarted mid-request. This is what actually frees the
            # slot and the FIFO gate instead of both sitting on a
            # request nobody is waiting for.
            response_box: dict = {}

            def _do_upstream_request():
                try:
                    conn.request(method, self.path, body=raw_body, headers=headers)
                    response_box["resp"] = conn.getresponse()
                except Exception as e:
                    response_box["exc"] = e

            req_thread = threading.Thread(target=_do_upstream_request, daemon=True)
            req_thread.start()
            while req_thread.is_alive():
                if self._client_gone():
                    self.log_message(
                        "%s %s -- client gone, cancelling upstream (was waiting on response)",
                        method, self.path,
                    )
                    if conn.sock is not None:
                        try:
                            conn.sock.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                    req_thread.join(timeout=2)
                    return
                req_thread.join(timeout=self.DISCONNECT_POLL_INTERVAL)

            if "exc" in response_box:
                exc = response_box["exc"]
                if not isinstance(exc, OSError):
                    raise exc
                # llama-server isn't up yet/crashed -- common right after
                # a launchd (re)start race, not worth a traceback.
                self.log_message("%s %s -> upstream unreachable (%s)", method, self.path, exc)
                body = b"llama-server upstream unreachable\n"
                self.send_response(502)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            upstream_resp = response_box["resp"]
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
            # (stream: true) so tokens still show up incrementally. Also
            # watched for disconnect here: generation alone can run
            # minutes at this hardware's ~4 tok/s, same reasoning as above.
            while True:
                if self._client_gone():
                    self.log_message(
                        "%s %s -- client gone, cancelling upstream (was streaming)",
                        method, self.path,
                    )
                    return
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
        f"pinning completions per-session across {N_SLOTS} slot(s), FIFO-serialized"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
