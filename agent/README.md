# caravan-agent

The self-organizing piece of Caravan described in
[`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md). One script, meant to
run identically on every node; each instance decides for itself whether
this machine is head (serves the target model) or a tail (contributes
compute), based on whether the model is present in this node's own Ollama
store.

## Status: scaffold (2026-09-06)

Real and tested today:

- Role detection against the actual local Ollama manifest/blob layout
  (`ollama_store.py`).
- Persistent per-node identity (`identity.py`).
- Tailscale peer discovery (`discovery/tailscale.py`).
- LAN mDNS/Bonjour discovery via `dns-sd` (`discovery/mdns.py`).
- A liveness-check gate (`health_server.py` + `is_agent_alive()` in
  `caravan_agent.py`) so a merely-*reachable* machine on the same LAN or
  tailnet isn't mistaken for a Caravan peer. This matters: an early dry
  run without this gate discovered an unrelated tailnet machine and would
  have wired it into `llama-server --rpc` as if it were a tail.
- Command construction for both roles (`supervisor.py`), matching the
  exact invocations proven in tonight's manual setup and the launchd
  plists in `../launchd/`.

**Not real yet:**

- Not wired into launchd. Running this doesn't replace or touch the
  existing manually-managed processes/plists in `../launchd/` at all.
- mDNS-discovered peers aren't reconciled with Tailscale-discovered ones
  by node identity yet (no TXT record carrying `node_id` published over
  mDNS) -- see `discover_peers()`'s docstring.
- No shard cache (that's `ARCHITECTURE.md`'s Phase 2, separate work).
- No handling for a node that's neither head nor a live tail candidate
  yet knowing what to do with itself beyond idling as a tail.

## Running it

```
python3 caravan_agent.py                 # dry run: prints what it would do
python3 caravan_agent.py --apply         # actually spawns+supervises (blocks)
python3 caravan_agent.py --model llama3  # target a different model
```

Dry run is the safe default on purpose -- it's meant to be exercisable
right now, on the live 2-node setup, without any risk to the
currently-working manually-managed processes. `--apply` will actually
launch `llama-server` or `rpc-server` and supervise it in a restart loop
(blocks forever); don't run that on node-a while the manual/launchd
`llama-server` is already bound to port 8080.

State (persistent node identity) lives at `~/.caravan/agent/`, outside
this repo -- runtime state, not source.
