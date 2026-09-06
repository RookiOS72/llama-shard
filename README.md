# Caravan

*(Renamed from "llama-shard" — see [docs/LOG.md](docs/LOG.md) for the naming
discussion.)*

Experiment: shard a llama.cpp/GGUF model (the engine underneath Ollama) across
multiple Macs, the way [exo](https://github.com/exo-explore/exo) does for
MLX — but for the much larger llama.cpp/GGUF ecosystem instead of
Apple-Silicon-only MLX.

## Why

Ollama itself has no multi-machine sharding — it's deliberately single-node.
But the engine it's built on, llama.cpp, has a real (if less polished) RPC
backend (`ggml`'s `GGML_RPC` option) that lets you split a model's layers
across machines over the network. This project is step one of finding out
whether that's actually usable, before considering whether it's worth turning
into a proper app/open-source project.

## Status

Stages 1-2 (prove the mechanism, make it usable day-to-day) are done —
muse-glimmer runs sharded across both Macs and is openclaw's primary
model. Now on **stage 3, phase 1**: a self-organizing agent
(`agent/`) that figures out for itself whether a node is head or a tail,
replacing the hand-written per-role launchd plists in `launchd/`. See
[docs/PLAN.md](docs/PLAN.md) for the staged approach and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the current design.

## Hardware

Two Mac minis (Apple M4, 24GB unified memory each) on the same LAN,
referred to here as `node-a` (runs the orchestrating `llama-server`/
`llama-cli` process) and `node-b` (runs `rpc-server`). Both already run
[exo](https://github.com/exo-explore/exo) for MLX-based sharding — this
project is exploring the llama.cpp/GGUF equivalent.

## Log

See [docs/LOG.md](docs/LOG.md) for a running, dated log of what was tried,
what worked, and what didn't.
