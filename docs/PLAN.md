# Plan

A staged approach, each stage only starting once the previous one proves
worthwhile:

1. ~~**Prove the mechanism.**~~ **Done.** Built llama.cpp with
   `GGML_RPC=ON` and `GGML_METAL=ON` on both machines, ran `rpc-server` on
   one, pointed `llama-server` at it from the other with `--rpc`, loaded
   muse-glimmer split across both. It works and is stable enough for real
   use — see `docs/LOG.md` for throughput numbers and the rough edges
   found along the way.
2. ~~**Make it usable day-to-day.**~~ **Done, then some.** Wired into
   openclaw as its primary model via an OpenAI-compatible endpoint, added
   a slot-pinning reverse proxy to fix cache-losing slot hops, put two of
   the three processes under launchd supervision, and root-caused/fixed a
   real production incident (a multi-hour retry cascade from session
   bloat + a proxy bug + too-tight timeouts). See `docs/LOG.md` for all of
   it.
3. **Now: the self-organizing app.** A single agent, installed
   identically on every machine, that figures out for itself whether it's
   head or a tail — no more hand-edited per-role plists or manually-picked
   IPs. Split into two phases (orchestration/robustness, then a shard
   cache) — full design in
   [`docs/ARCHITECTURE.md`](ARCHITECTURE.md), worked out in the
   2026-09-06 design session logged in `docs/LOG.md`.

We are currently starting **stage 3, phase 1** (the orchestration/robustness
agent — see ARCHITECTURE.md).

## Known risks going in

- llama.cpp's RPC backend has a reputation for being less mature/optimized
  than the single-node path. Real-world throughput over RPC could be
  disappointing even if it technically works.
- RPC + Metal (Apple GPU) specifically is a less-exercised combination than
  RPC + CUDA or single-node Metal — more likely to hit rough edges.
- Both Macs have 24GB unified memory each; need to watch for the same kind
  of memory-pressure issues we hit running exo/MLX models on this hardware
  (see the broader session history — a single-node MLX placement crashed
  under memory pressure from GPU-level allocation failures).
