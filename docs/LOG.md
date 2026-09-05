# Log

## 2026-09-05

- Confirmed Ollama has no native multi-machine sharding, but llama.cpp
  (the engine underneath it) has an RPC backend (`GGML_RPC`) that does
  cross-machine layer splitting. Decided to try it as "step 1" before
  considering anything bigger.
- Set up SSH key-based access from `exo01` to `exo02` (`brenden@192.168.1.39`)
  to make working across both machines practical.
- Checked Homebrew's `llama.cpp` formula: it does **not** set `GGML_RPC` or
  `GGML_METAL` explicitly, relying on upstream defaults. Confirmed upstream
  defaults are `GGML_METAL_DEFAULT=ON` on Apple platforms (good) but
  `GGML_RPC` defaults to `OFF` — so the Homebrew bottle would **not** have
  RPC support. Building from source instead.
- Installed `cmake` via Homebrew on both machines (neither had it).
- Cloned `llama.cpp` (shallow, `--depth 1`) into `~/dev/llama.cpp` on both
  machines.
- Configured the build with `-DGGML_RPC=ON -DGGML_METAL=ON
  -DCMAKE_BUILD_TYPE=Release` on both. Notable output on both machines:
  ```
  -- Metal framework found
  -- Including METAL backend
  -- Using RPC backend
  --   RDMA transport enabled (Apple RDMA-over-Thunderbolt, UC)
  -- Including RPC backend
  ```
  The RDMA-over-Thunderbolt line is a good sign — if these two Macs are
  ever connected via Thunderbolt, the RPC backend can use that instead of
  Wi-Fi/Ethernet for node-to-node traffic, which should help throughput a
  lot given how much a plain network hop hurt exo's MLX pipeline earlier
  this session.
- Build started on both machines (background). Next: once built, start
  `rpc-server` on exo02, point `llama-server`/`llama-cli` at it from
  exo01 with `--rpc`, load a real GGUF model split across both, and
  benchmark actual tok/s against what we saw with exo/MLX.
- Created the public GitHub repo:
  [github.com/RookiOS72/llama-shard](https://github.com/RookiOS72/llama-shard).
  `gh` was already installed and authenticated, so no extra setup needed.
- First build attempt on exo02 failed instantly (`nohup: cmake: No such
  file or directory`) — the background SSH command didn't have Homebrew's
  `/opt/homebrew/bin` on `PATH` before invoking `cmake`. Non-interactive
  SSH sessions on macOS don't source the same shell profile as an
  interactive one, so anything installed via Homebrew needs its `PATH`
  exported explicitly in one-off SSH commands. Fixed by exporting
  `PATH="/opt/homebrew/bin:$PATH"` before the build command; restarted.
- Both builds finished successfully. `ggml-rpc-server`, `llama-cli`, and
  `llama-server` all present in `build/bin/` on both machines.
- Started `ggml-rpc-server` on exo02. First attempt used the default bind
  host (`127.0.0.1`) and was unreachable from exo01 (`connection refused`)
  — the RPC server only listens on localhost unless told otherwise.
  Restarted with `-H 0.0.0.0 -p 50052`, which printed this warning on
  startup (important, keep this in mind for anyone else using this repo):
  ```
  WARNING: Host ('0.0.0.0') is != '127.0.0.1'
           Never expose the RPC server to an open network!
           This is an experimental feature and is not secure!
  ```
  This is fine on a private LAN but should **not** be exposed beyond that
  — the RPC protocol has no auth/encryption. Confirmed reachable from
  exo01 after the restart. Metal initialized correctly on exo02's GPU
  (`Apple M4`, ~19GB usable GPU working set).
- Next: pick a GGUF model to actually test with, point `llama-server` on
  exo01 at exo02's RPC endpoint (`--rpc 192.168.1.39:50052`), confirm it
  splits layers across both machines, and benchmark real tok/s.
