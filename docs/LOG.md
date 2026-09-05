# Log

## 2026-09-05

- Confirmed Ollama has no native multi-machine sharding, but llama.cpp
  (the engine underneath it) has an RPC backend (`GGML_RPC`) that does
  cross-machine layer splitting. Decided to try it as "step 1" before
  considering anything bigger.
- Set up SSH key-based access from `node-a` to `node-b` (its LAN IP) to
  make working across both machines practical.
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
  `rpc-server` on node-b, point `llama-server`/`llama-cli` at it from
  node-a with `--rpc`, load a real GGUF model split across both, and
  benchmark actual tok/s against what we saw with exo/MLX.
- Created the public GitHub repo:
  [github.com/RookiOS72/llama-shard](https://github.com/RookiOS72/llama-shard).
  `gh` was already installed and authenticated, so no extra setup needed.
- First build attempt on node-b failed instantly (`nohup: cmake: No such
  file or directory`) — the background SSH command didn't have Homebrew's
  `/opt/homebrew/bin` on `PATH` before invoking `cmake`. Non-interactive
  SSH sessions on macOS don't source the same shell profile as an
  interactive one, so anything installed via Homebrew needs its `PATH`
  exported explicitly in one-off SSH commands. Fixed by exporting
  `PATH="/opt/homebrew/bin:$PATH"` before the build command; restarted.
- Both builds finished successfully. `ggml-rpc-server`, `llama-cli`, and
  `llama-server` all present in `build/bin/` on both machines.
- Started `ggml-rpc-server` on node-b. First attempt used the default bind
  host (`127.0.0.1`) and was unreachable from node-a (`connection refused`)
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
  node-a after the restart. Metal initialized correctly on node-b's GPU
  (`Apple M4`, ~19GB usable GPU working set).
- Next: pick a GGUF model to actually test with, point `llama-server` on
  `node-a` at `node-b`'s RPC endpoint (`--rpc <node-b-ip>:50052`), confirm
  it splits layers across both machines, and benchmark real tok/s.
- Pulled `muse-glimmer` via `ollama pull muse-glimmer` (18GB total: a
  16GB main weights blob + 1.4GB vision/adapter blob). Deliberately chose
  this model since it's the one that started this whole detour — exo
  couldn't load it (unsupported architecture), so getting it running here
  would be a satisfying full-circle test. Located the raw GGUF blob
  directly in Ollama's own content-addressed blob store
  (`~/.ollama/models/blobs/sha256-...`) — no need to re-download from
  elsewhere, and no need to translate the format; Ollama blobs are
  themselves valid GGUF files (confirmed via magic bytes).
- **First load attempt did not actually shard.** Started `llama-server`
  with just `--rpc <node-b-ip>:50052` and no explicit device/split flags.
  It loaded fine and generated correctly (6.2 tok/s), but `node-b` showed
  ~0% CPU throughout — the whole model loaded onto `node-a` alone. Since
  this 16GB model comfortably fits in `node-a`'s own ~19GB Metal budget,
  llama.cpp's automatic placement logic didn't bother using the remote
  RPC device at all.
- Ran `--list-devices` to see how devices enumerate with `--rpc` set:
  ```
  MTL0: Apple M4 (18186 MiB, 18185 MiB free)
  BLAS: Accelerate (0 MiB, 0 MiB free)
  RPC0: 192.168.1.39:50052 (18186 MiB, 18185 MiB free)
  RPC1: 192.168.1.39:50052 (0 MiB, 0 MiB free)
  ```
  Note the phantom `RPC1` entry reporting 0 MiB for the same endpoint —
  this is likely what caused the original run's
  `device RPC1 did not report memory; --fit will not use it` warning and
  probably confused automatic placement.
- **Fix: force it explicitly.** Restarted with
  `--device MTL0,RPC0 --tensor-split 1,1` to name the two real devices
  directly and split evenly, sidestepping the phantom device entirely.
  This time `node-b`'s `rpc-server` process memory climbed steadily
  during load (0 → ~2GB → ~4.5GB → ~6.7GB → ~8.27GB over about 7 minutes
  total) — real tensor data streaming to it over the LAN, no local copy
  of the model file needed on `node-b` at all (unlike exo's MLX approach,
  which required each node to hold its own full copy on disk).
- **First genuinely distributed generation.** With both machines
  confirmed active (CPU ticked up on both during generation, not just
  `node-a`), a test prompt returned successfully:
  - **4.23 tok/s** generation, **15.5 tok/s** prompt processing
  - For comparison: exo's own 2-node MLX split earlier this session
    measured ~4-4.5 tok/s on a similarly-sized model. Roughly the same
    ballpark — this isn't a magic speed win over exo, but it **is** proof
    the mechanism works end-to-end on real hardware with a real model,
    which was the actual goal of step 1.

**Step 1 verdict: proven.** The core trick works, is stable enough to
finish a real request, and performs comparably to exo's own approach.
Worth deciding next whether/how to move to step 2 (see
[PLAN.md](PLAN.md)).
