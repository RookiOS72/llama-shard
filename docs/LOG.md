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

## Wiring it into a real assistant (openclaw)

Went straight to a real-world test: pointed `openclaw` (an existing
agent framework already running on `node-a`) at this `llama-server`
instance via its OpenAI-compatible endpoint, replacing a broken exo
config (a different model it was pointed at turned out to use an
architecture exo doesn't support — unrelated to this project, but it's
what created the opening to try this).

- Config: added a new model provider with `"api": "openai-completions"`
  (not `"openai"` — that name isn't accepted) pointed at
  `http://127.0.0.1:8080/v1`, no real API key needed since it's local.
- **Confirmed tool-calling works** through llama-server's OpenAI-compatible
  endpoint for this model — sent a request with a `tools` array, got back
  a correctly-formatted `tool_calls` response. This mattered because the
  model's Ollama-specific `RENDERER glimmer`/`PARSER glimmer` template
  hints aren't something llama.cpp understands, so this wasn't guaranteed
  to work — it does, likely because llama.cpp handles tool-calling via
  the model's own embedded GGUF chat template rather than Ollama's
  separate renderer/parser convention.
- **Real system prompts are much bigger than expected.** openclaw's full
  agent system prompt (with its default tool/skill set loaded) came in
  around 24-30K tokens — nowhere near the small prompts used for the
  earlier benchmarks in this log. First attempt at `-c 8192` failed
  outright (`request (30525 tokens) exceeds the available context size`).
  Bumped to `-c 65536` — this model's KV cache is lean (only 2 KV heads),
  so the memory cost of a much bigger context is modest.
- **Found and cleared a genuinely stale, unrelated problem**: an old
  long-running session on the openclaw side had accumulated ~66K tokens
  of stale history over many hours (heartbeats, earlier unrelated tests)
  and was stuck in an `aborted` state. Every new message inherited that
  entire backlog before even starting, which is why token counts kept
  climbing between attempts (30K → 80K) independent of anything in this
  project. Cleared with `openclaw sessions compact <key> --max-lines 10`
  (hard truncation, no LLM summarization needed — deleting the session
  outright wasn't allowed since it's a protected "main" session).
- **First real message timed out, then succeeded on retry.** With a
  clean ~24.5K-token prompt, prompt processing alone took ~280s at
  ~80 tok/s, longer than openclaw's default 300s provider timeout —
  got killed at 79% through, before generating anything. openclaw
  auto-retried, and because llama-server keeps a prefix cache, the
  retry reused 85.8% of the already-processed prompt (only ~4K new
  tokens to process) and completed in ~133s. Total end-to-end for this
  first message: roughly 7-8 minutes.

**Practical takeaway**: it works, but the "cold start" cost of a large
tool-equipped system prompt is real on this hardware — the first message
of a new/idle session will likely time out once and need the automatic
retry to actually get an answer, adding several minutes. Ongoing
messages in an already-warm session should be much faster since they
only reprocess the incremental new tokens, not the whole system prompt
— but that assumes the prefix cache slot doesn't get evicted by other
concurrent sessions/cron jobs competing for the same small pool of
cache slots. Worth deciding whether that tradeoff is acceptable, or
whether the fix is a smaller/faster model, a trimmed-down tool set, or
both.

**Tested the "trim the tool set" option — it didn't help much.** Cut
openclaw's loaded skills from 5 down to 1 (kept only email monitoring,
dropped apple-notes/apple-reminders/1password/browser-automation since
they weren't used day-to-day). Measured the resulting system prompt on a
clean scratch session afterward: still **~28,240 tokens**, essentially
unchanged from the ~24,576-30,722 baseline before the trim. Conclusion:
the loaded skills list was not the dominant contributor to prompt size —
the base `"coding"` tools profile itself (tool definitions, workspace
`AGENTS.md`, etc.) is the bigger factor, and wasn't touched by this
change. Next person picking this up should measure that directly (e.g.
via `openclaw proxy` to inspect the actual assembled request) before
assuming further skill trimming will help — it likely won't get you far
on its own.

## Diagnosing repeat slowness: it's slot assignment, not missing caching

Continued the investigation into why *ongoing* messages (not just the
first one) were still occasionally slow. llama-server's `/slots`
endpoint plus its own stdout log (`llama-server3.log`) gave a direct,
measured answer.

- llama-server's automatic prompt caching (`--cache-prompt`, on by
  default) genuinely works well *when a request lands back on the same
  slot it used last time*: observed 70-100% prefix reuse across several
  consecutive same-slot requests in the live log (e.g. one request reused
  ~25K of its ~30.7K tokens from the prior request on that slot; another
  reused nearly its entire prompt and finished in ~15s).
- The problem: there are only 4 slots (`-np` was left at its `-1`/auto
  default), and openclaw only has **one** real conversation identity
  today (the "main" agent — heartbeats run inside that same session, per
  `agents.defaults.heartbeat` in openclaw.json, not a separate one).
  Despite that, requests were landing on three different slots over time
  (slot 2, then slot 1, then slot 0) as the prompt's size and shape
  shifted from openclaw's own context trimming between turns. Each time
  the automatic prompt-similarity slot picker (`--slot-prompt-similarity`,
  default threshold 0.10) missed the slot actually holding the relevant
  cached prefix, that request paid a mostly- or fully-cold reprocess —
  directly reproduced live: task 1395 was killed mid-prefill at 20,475
  tokens by openclaw's own timeout, and the retry (task 1408) landed back
  on the same slot and reused that leftover state (72% reuse, 153.6s for
  the remaining ~7.7K tokens) — the exact "times out once, retry succeeds
  via cache" pattern described in the wiring section above, still
  happening live months (well, hours) later.
- **Fix: `proxy/slot_pin_proxy.py`**, a small dependency-free Python
  reverse proxy that sits between openclaw and llama-server. It forwards
  every request unchanged except for injecting a fixed `"id_slot"` into
  the JSON body (confirmed via llama-server's own source,
  `server-context.cpp`, that the OAI-compatible chat-completions path
  reads `id_slot` from the raw request body exactly like the native
  `/completion` endpoint does — same code path, just not documented on
  the OpenAI-compat side). Streams responses back chunk-by-chunk so SSE
  (`stream: true`) still works. Since there's only one real identity
  today, v1 pins everything to one fixed slot rather than trying to route
  by identity — see the module docstring for how to extend it if a second
  identity shows up later.
- Verified end-to-end: started the proxy on `127.0.0.1:8090`, confirmed a
  direct completion request landed on the pinned slot via `/slots`
  (task/prompt counters updated on exactly that slot, others untouched),
  then pointed openclaw's `models.providers.llama-shard.baseUrl` at
  `http://127.0.0.1:8090/v1` (was `:8080` directly) and restarted the
  `ai.openclaw.gateway` LaunchAgent to pick it up. Ran a real end-to-end
  test via `openclaw agent -m "..." --json` (no `--deliver`, so it
  doesn't hit a real channel) to confirm a full real agent turn works
  through the new path.
- **Bonus finding from `openclaw doctor` while checking the restart**:
  the workspace `AGENTS.md` file alone is 26,749 raw chars / 19,182
  injected chars (28% truncated at the default per-file limit), and total
  bootstrap injection is 24,194 of a 60,000-char budget (40%). This is
  concrete, measured evidence for the still-open question from the
  section above about what's actually filling the ~28K-token system
  prompt — worth picking up directly next time that's being investigated,
  rather than re-deriving it.
- Not yet done: the proxy isn't supervised (same "plain background
  process" caveat as `llama-server`/`rpc-server` — see README/PLAN); it
  and the pinned slot number are the natural next candidate for the
  launchd-service work already on the roadmap.

## launchd (issue #1): 2 of 3 services done, llama-server blocked

Added `launchd/` with plists for all three processes
(`com.llama-shard.llama-server`, `com.llama-shard.slot-pin-proxy` on
node-a, `com.llama-shard.rpc-server` on node-b) plus a small robustness
fix to the proxy (return 502 instead of a raw traceback when llama-server
isn't reachable yet, since under launchd it may start before llama-server
is ready).

- **`slot-pin-proxy` and `rpc-server`: working cleanly under launchd.**
  Installed as user LaunchAgents (`gui/<uid>` domain), `RunAtLoad` +
  `KeepAlive`, logs under `~/Library/Logs/llama-shard/`.
- **`llama-server`: blocked, reverted to a plain manual process for now.**
  Every launchd-launched attempt aborts near-instantly with
  `ggml-rpc.cpp:547: Failed to connect to 192.168.1.39:50052`, even
  though node-b's `rpc-server` is confirmed up and reachable (`nc -z`
  succeeds from node-a both before and during the failure). Isolated the
  cause with two tests before giving up on it:
  - Killed and replaced node-b's `rpc-server` with a completely fresh
    instance (in case the long-running original was wedged from the
    earlier abrupt `kill` of its old client) -- same failure, ruling that
    out.
  - Ran the *exact* same binary, same args, even with a stripped `env -i`
    environment (to rule out a missing env var launchd doesn't set) --
    directly in an interactive shell, and it worked fine, loading the
    model normally. So it's specifically "spawned by launchd" that
    fails, not the command itself or the environment.
  - Checked `~/Library/Application Support/com.apple.TCC/TCC.db` for a
    Local Network denial (the usual suspect for "works interactively,
    silently fails headless" on modern macOS) -- no record at all, for
    either outcome. Doesn't rule out a TCC-adjacent cause, but there's no
    direct evidence for the classic explanation either.
  - No third-party firewall found (no Little Snitch/LuLu processes or
    apps). Didn't get further into `pfctl`/Screen Time content-filtering
    since that needs `sudo`/GUI access this session doesn't have.
  - Best remaining guess: some per-process network policy that treats
    launchd-spawned processes differently from a shell's children (e.g.
    Screen Time Content & Privacy network restrictions, or an MDM
    profile) -- keyed by "responsible process" the way several macOS
    privacy/filtering features are. Untested alternative worth trying
    next: a **LaunchDaemon** (`/Library/LaunchDaemons`, `system` domain,
    root, needs `sudo`) instead of a LaunchAgent -- daemon-domain
    processes sit outside the per-user session policies that would
    explain this.
  - Net effect: llama-server is back to being a manual `nohup` process,
    same as before this issue was opened -- no regression, but the
    "survives a reboot" goal isn't met for this one process yet. See
    issue #1 for follow-up.
