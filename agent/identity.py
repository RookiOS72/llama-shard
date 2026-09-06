"""Persistent per-node identity.

Both discovery paths (LAN mDNS and Tailscale) can find the same physical
machine two different ways -- a node's mDNS advertisement and its
Tailscale peer entry aren't inherently linked to each other. This UUID,
generated once and reused across every restart, is what discovery.py
reconciles those two sightings against so "found twice" doesn't turn
into "treated as two different nodes." See docs/ARCHITECTURE.md,
"Discovery -- hybrid: LAN mDNS + Tailscale when available".
"""
import json
import socket
import uuid
from pathlib import Path

from config import STATE_DIR

IDENTITY_FILE = STATE_DIR / "identity.json"


def load_or_create() -> dict:
    """Returns {"node_id": <uuid str>, "hostname": <str>}, creating and
    persisting a new node_id on first run."""
    if IDENTITY_FILE.exists():
        data = json.loads(IDENTITY_FILE.read_text())
        # Hostname can legitimately change (DHCP rename, etc.); node_id
        # must not, so always refresh hostname but keep the stored id.
        data["hostname"] = socket.gethostname()
        _save(data)
        return data

    data = {"node_id": str(uuid.uuid4()), "hostname": socket.gethostname()}
    _save(data)
    return data


def _save(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    IDENTITY_FILE.write_text(json.dumps(data, indent=2))
