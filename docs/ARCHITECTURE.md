# Architecture: the self-organizing Caravan agent

Design target for Caravan's next phase, worked out in a 2026-09-06 design
session (see `docs/LOG.md` for the narrative version of how we got here).
This doc is the current prescriptive reference; update it in place as the
design evolves, rather than letting it drift out of sync with LOG.md's
diary entries.

## Goal

One app (the "Caravan agent"), installed identically on every machine in
the cluster. Each instance figures out on its own whether it's acting as
**head** (serves inference, holds the full model) or a **tail**
(contributes compute to whichever machine is head) — no manually-edited
plist per role, no hand-picked IPs, no human deciding up front which
machine is "the server."

## Why this isn't just "build what exo does"

llama.cpp's RPC backend has a structural asymmetry that MLX/exo doesn't:

- **Head** (`llama-server`) needs the *full model file* on local disk (to
  read metadata/tokenizer and do orchestration) and does the actual
  serving.
- **Tail** (`rpc-server`) needs *nothing stored* — a dumb, stateless
  compute backend that executes whatever tensors arrive over the wire and
  forgets them when the process exits.

exo's model requires every node to hold a full local copy, even nodes
only ever computing a slice — see the "Why model load takes 5-7 minutes"
and shard-cache entries in LOG.md. Caravan was deliberately built to avoid
that N-way disk duplication, so "any node can become head" can't be
bought the same way exo buys it (everyone has everything). Instead: head
role follows wherever the full model already lives.

## Role determination — self-declared, not elected

No leader-election protocol. Each agent instance checks its own local
Ollama blob store on startup/periodically:

- Has the model requested → **head** for that model. Launches
  `llama-server --rpc <discovered peers>`.
- Doesn't have it → **tail** candidate. Ensures `rpc-server` is running,
  waits to be used.

In practice there's one Ollama instance across the whole cluster (the
machine running openclaw's gateway, or whichever machine a human/agent
points requests at) — that machine pulls models and is therefore always
head. This is fine and expected, not a limitation to design around: the
value of "one app everywhere" is a uniform install and automatic
tail-side behavior, not that head literally moves around day to day.
Multi-model support falls out for free regardless (different nodes could
independently be head for different models, if that ever becomes real
usage) without any extra design work.

No safeguard is needed for "two nodes both have the same model and both
try to become head" — given the single-Ollama-instance reality this
isn't a real scenario. Revisit only if actual usage changes.

## Discovery — hybrid: LAN mDNS + Tailscale when available

- Every agent advertises + browses a `_caravan._tcp` Bonjour/mDNS service
  on the LAN — zero-config, no dependency on any other infrastructure.
- Separately, when Tailscale is present and running on a machine, the
  agent also queries `tailscale status --json` for peers.
- Each agent has a small persistent identity (a UUID written to disk on
  first run) so a peer discovered both ways doesn't get treated as two
  different nodes.
- When a peer is reachable both ways, **prefer its Tailscale IP**. That's
  the empirically reliable path from tonight's incident (LAN IPs threw
  unexplained `EHOSTUNREACH` from certain process contexts, worked around
  by switching to node-b's Tailscale IP — see LOG.md, "The retry cascade,
  root-caused" and the full-rebrand entry). LAN IPs are used only when a
  peer has no Tailscale address to offer.

## Head/tail interaction

Head, once it knows which peers are live tail candidates, decides a
tensor split and launches `llama-server --rpc host1:port,host2:port,...`
pointed at them — conceptually "I've got this model, I'm head, here's
your shard." Tails just need `rpc-server` already running and reachable;
llama.cpp's own RPC protocol handles the actual tensor transfer.

"Cache it for next time" (a tail persisting its assigned tensor range
across restarts) is **not** part of this phase — see Phase 2 below.

## Two build phases

**Phase 1 — orchestration/robustness layer (building now).** Discovery,
self role-detection, process supervision (spawn/monitor `llama-server` or
`rpc-server` locally based on role, replacing tonight's hand-written
launchd plists), always picking the reliable IP per peer. This directly
absorbs GitHub issue #1 (manual launchd supervision) — once the agent
exists, hand-maintained per-role plists go away entirely rather than
getting a standalone fix first.

**Phase 2 — shard cache.** A real patch to `ggml-rpc.cpp` so a tail can
persist and reuse its assigned tensor range across restarts, instead of
re-streaming the full shard over the network every time (currently
5-7+ minutes over WiFi — see LOG.md). This is genuinely committed, not a
maybe: it's a real robustness/speed improvement independent of any
hardware change. It's sequenced *second*, not deprioritized — a patch to
`rpc-server`'s wire protocol should land on top of an orchestration layer
that's already proven stable, not on top of today's manually-launched
moving target. (An incoming USB4/Thunderbolt link between the two Macs
will separately speed up shard transfer — that's a nice bonus, explicitly
not a substitute for this phase. See LOG.md for that discussion.)
Corresponds to GitHub issue #3, reframed.

## Language

**Phase 1: Python.** Matches the existing `slot_pin_proxy.py`, no build
step, cheapest to iterate while the design is still actively moving (it
changed multiple times in the course of one conversation tonight).

**Later: a compiled language, undecided.** Once Phase 1 has proven itself
in real daily use, port it — this is PLAN.md's stage 3 packaging/"real
app for other people" milestone, a distinct later decision, not something
Phase 1 needs to serve. The reasoning captured here and in LOG.md/GitHub
issues is the actual bridge to that port — not portable Python code.
Options, for whenever that decision comes up:

- **Go** — full cross-platform (macOS/Linux/Windows/BSD/more) with
  trivial cross-compilation (`GOOS`/`GOARCH`, no target SDK needed). Best
  choice if a non-Mac node ever joins the cluster.
- **Rust** — same platform breadth, the most battle-tested of the three
  for this class of networked infra tooling, more cross-compile setup
  overhead than Go.
- **Swift** — best native fit for today's all-Mac fleet: built-in
  Bonjour/Network framework (no third-party mDNS library needed), clean
  launchd/codesigning story. Real official Linux support, but less
  mature/common for this than Go or Rust; Windows support is the least
  mature of the three.

**Ruled out: Ruby / Rails.** Ruby is interpreted — a peer to Python, not
a compiled-language option. Rails specifically is a web application
framework (MVC, database-backed models, views, asset pipeline); Caravan's
agent is a networked daemon with no database and no web UI, so Rails'
actual machinery doesn't apply. Plain Ruby would be a fair comparison to
Python but has no advantage here — weaker stdlib/ecosystem for this kind
of systems/networking work, and, like Python, still needs a language
runtime installed on every node, so it doesn't get any closer to the
eventual compiled/dependency-free goal either.

## Open questions (not yet decided)

- Exact wire format for the head→tail "here's your shard" coordination
  message (today this is implicit in how `llama-server --rpc` connects
  out; whether the agent needs its own explicit message on top of that,
  or whether "tail has `rpc-server` reachable" is sufficient, is unresolved).
- What health-check surface each agent exposes for peer liveness checks
  (used by both the mDNS and Tailscale discovery paths).
- Whether a standalone stopgap fix for issue #1 (bootstrapping
  `llama-server` under launchd manually, using tonight's Tailscale-IP fix)
  is still worth doing before Phase 1 lands, or whether it's fine to wait
  for Phase 1 to fully absorb it.

## Relationship to existing GitHub issues

- **#1** (supervise processes with launchd) — absorbed by Phase 1; not
  closed yet pending the open question above.
- **#2** (system prompt size) — unrelated, still open on its own track.
- **#3** (shard-scoped cache) — becomes Phase 2 above; issue stays open
  as the tracking issue, reframed by this doc rather than superseded.
