# launchd services

Supervises the three plain background processes this project depends on, so
they survive a crash or reboot instead of needing to be started by hand.
See [docs/PLAN.md](../docs/PLAN.md) (stage 2) and
[GitHub issue #1](https://github.com/RookiOS72/caravan/issues/1).

*(The GitHub repo was renamed `llama-shard` → `caravan`; the plist labels,
paths, and log directory below still say `llama-shard` pending a fuller
rebrand.)*

| plist | machine | what |
|---|---|---|
| `com.llama-shard.llama-server.plist` | node-a | the sharded `llama-server` |
| `com.llama-shard.slot-pin-proxy.plist` | node-a | `proxy/slot_pin_proxy.py` |
| `com.llama-shard.rpc-server.plist` | node-b | `ggml-rpc-server` |

All are user-level LaunchAgents (not LaunchDaemons) -- they run in the
logged-in user's session, matching how openclaw's own gateway is set up.
Paths inside the plists are absolute and specific to this machine pair
(both currently have user `brenden` at `/Users/brenden`); there's no
templating since this project isn't at the "package it for other people"
stage yet (see PLAN.md stage 3).

Logs go to `~/Library/Logs/llama-shard/*.log` on each machine.

## Install (node-a: llama-server + slot-pin-proxy)

```sh
mkdir -p ~/Library/Logs/llama-shard
cp launchd/com.llama-shard.llama-server.plist launchd/com.llama-shard.slot-pin-proxy.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.llama-shard.llama-server.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.llama-shard.slot-pin-proxy.plist
```

## Install (node-b: rpc-server)

```sh
ssh brenden@<node-b-ip> 'mkdir -p ~/Library/Logs/llama-shard'
scp launchd/com.llama-shard.rpc-server.plist brenden@<node-b-ip>:~/Library/LaunchAgents/
ssh brenden@<node-b-ip> 'launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.llama-shard.rpc-server.plist'
```

## Check status / logs

```sh
launchctl list | grep llama-shard
tail -f ~/Library/Logs/llama-shard/llama-server.log
```

## Restart after editing a plist

```sh
launchctl bootout gui/$(id -u)/com.llama-shard.<name>
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.llama-shard.<name>.plist
```

## Uninstall

```sh
launchctl bootout gui/$(id -u)/com.llama-shard.<name>
rm ~/Library/LaunchAgents/com.llama-shard.<name>.plist
```
