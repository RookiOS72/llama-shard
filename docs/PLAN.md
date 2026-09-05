# Plan

A staged approach, each stage only starting once the previous one proves
worthwhile:

1. **Prove the mechanism.** Build llama.cpp with `GGML_RPC=ON` and
   `GGML_METAL=ON` on both machines, run `rpc-server` on one, point
   `llama-server`/`llama-cli` at it from the other with `--rpc`, load a
   real model split across both, and benchmark actual tokens/sec. If this
   isn't reasonably fast or reasonably stable, there's no point going
   further.
2. **Make it usable day-to-day.** If step 1 looks good, wrap it in
   something more convenient than two manually-started terminal processes —
   a small launcher/config, maybe a systemd/launchd-style service, pointed
   at from openclaw (or whatever's consuming it) via an OpenAI- or
   Ollama-compatible endpoint.
3. **Only then, consider the bigger picture.** Dashboard, auto peer
   discovery, packaging, calling it an actual open-source project for
   other people to use. This is a much bigger scope than 1-2 and isn't
   worth starting until 1-2 have proven the idea is sound.

We are currently on **stage 1**.

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
