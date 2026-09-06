"""The health-check surface every Caravan agent exposes, so other agents'
discovery passes can tell "this Tailscale/mDNS peer is actually running
Caravan" apart from "this is just some other machine on the same LAN or
tailnet" -- see docs/ARCHITECTURE.md's open question on this, and the
caravan_agent.py dry-run output on 2026-09-06 that (correctly, if
uselessly) discovered an unrelated tailnet peer and would have wired it
into `--rpc` had this gate not existed.

Deliberately tiny: one endpoint, no auth (matches the existing
slot-pin-proxy's loopback-only trust model, though this binds all
interfaces since peers need to reach it -- LAN/tailnet-only by design,
same posture as rpc-server itself, see launchd/README.md's security
note).
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config


class _HealthHandler(BaseHTTPRequestHandler):
    node_id = "unknown"

    def log_message(self, fmt, *args):
        pass  # quiet -- this gets polled frequently by peers

    def do_GET(self):
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps({"status": "ok", "node_id": self.node_id}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start(node_id: str) -> threading.Thread:
    """Starts the health endpoint on config.AGENT_PORT in a background
    daemon thread and returns it. Dies with the process -- nothing to
    explicitly stop."""
    handler = type("_BoundHealthHandler", (_HealthHandler,), {"node_id": node_id})
    server = ThreadingHTTPServer(("0.0.0.0", config.AGENT_PORT), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread
