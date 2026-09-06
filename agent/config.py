"""Tunables for the Caravan agent -- paths, ports, and the current target
model. See docs/ARCHITECTURE.md for why these values are what they are.
"""
from pathlib import Path

# Where each agent instance persists its own identity (see identity.py).
# Deliberately outside the git repo -- this is runtime state, not source.
STATE_DIR = Path.home() / ".caravan" / "agent"

# The model this cluster currently serves. Hardcoded for now -- Caravan
# supports one real conversation identity today (see openclaw.json), so
# one target model is the correct-sized scaffold. Revisit if/when
# multi-model support becomes real usage, not just a nice side effect of
# the self-declaration design (see ARCHITECTURE.md).
TARGET_MODEL = "muse-glimmer"

# llama.cpp binaries -- same paths used by the hand-written launchd
# plists in ../launchd/. Not yet unified with those plists; this agent is
# meant to eventually replace them (see ARCHITECTURE.md, issue #1).
LLAMA_SERVER_BIN = Path.home() / "dev" / "llama.cpp" / "build" / "bin" / "llama-server"
RPC_SERVER_BIN = Path.home() / "dev" / "llama.cpp" / "build" / "bin" / "ggml-rpc-server"

# Ports. RPC_PORT and LLAMA_SERVER_PORT match tonight's proven manual
# setup. AGENT_PORT is new -- a small HTTP surface each agent exposes for
# peer liveness checks (mDNS/Tailscale discovery both need something to
# actually probe). Not yet implemented -- see caravan_agent.py.
RPC_PORT = 50052
LLAMA_SERVER_PORT = 8080
AGENT_PORT = 8091

LLAMA_SERVER_CTX_SIZE = 65536

# mDNS/Bonjour service type this agent advertises and browses for.
MDNS_SERVICE_TYPE = "_caravan._tcp"
MDNS_DOMAIN = "local"
