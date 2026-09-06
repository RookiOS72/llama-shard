"""LAN peer discovery via Bonjour/mDNS, using macOS's built-in `dns-sd`
CLI rather than a third-party library (`zeroconf` et al.) -- keeps this
project dependency-free, matching proxy/slot_pin_proxy.py's approach, and
`dns-sd` talks to the same system mDNSResponder daemon a native Swift
`NWBrowser` would use (relevant if this ever gets ported per
docs/ARCHITECTURE.md's language section).

macOS-only as written. A Linux port would need the equivalent
avahi-browse/avahi-publish commands -- not attempted here, out of scope
for phase 1 (see ARCHITECTURE.md, still an all-Mac fleet).

`dns-sd -B`/`-L` are long-running/streaming commands with no built-in
"give me one result and exit" mode, so every function here follows the
same pattern: start it, let it run for a fixed window, kill it, parse
whatever it printed. Output formats below were captured from a real
`dns-sd` run on this machine, not guessed -- see docs/LOG.md.
"""
import re
import subprocess
import time
from dataclasses import dataclass

from config import MDNS_DOMAIN, MDNS_SERVICE_TYPE

_ADD_LINE_RE = re.compile(
    r"^\s*[\d:.]+\s+Add\s+\d+\s+\d+\s+\S+\s+\S+\s+(?P<instance>.+?)\s*$"
)
_RESOLVE_LINE_RE = re.compile(
    r"can be reached at (?P<host>\S+?)\.?:(?P<port>\d+)"
)


@dataclass
class MdnsPeer:
    instance_name: str
    hostname: str
    port: int


def _run_timeboxed(args: list[str], duration: float) -> str:
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        proc.wait(timeout=duration)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
    output = proc.stdout.read() if proc.stdout else ""
    return output


def advertise_start(instance_name: str, port: int) -> subprocess.Popen:
    """Registers this node's Caravan agent as discoverable. Long-running
    -- caller owns the returned process and must terminate it on
    shutdown (dns-sd deregisters on process exit)."""
    return subprocess.Popen(
        ["dns-sd", "-R", instance_name, MDNS_SERVICE_TYPE, MDNS_DOMAIN, str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def browse(duration: float = 3.0) -> list[str]:
    """Instance names of every Caravan agent advertising on the LAN right
    now, found by listening for `duration` seconds."""
    output = _run_timeboxed(["dns-sd", "-B", MDNS_SERVICE_TYPE, MDNS_DOMAIN], duration)
    names = set()
    for line in output.splitlines():
        m = _ADD_LINE_RE.match(line)
        if m:
            names.add(m.group("instance").strip())
    return sorted(names)


def resolve(instance_name: str, duration: float = 2.0) -> tuple[str, int] | None:
    """Hostname/port for one previously-browsed instance name."""
    output = _run_timeboxed(
        ["dns-sd", "-L", instance_name, MDNS_SERVICE_TYPE, MDNS_DOMAIN], duration
    )
    for line in output.splitlines():
        m = _RESOLVE_LINE_RE.search(line)
        if m:
            return m.group("host"), int(m.group("port"))
    return None


def discover_peers(browse_duration: float = 3.0, resolve_duration: float = 2.0) -> list[MdnsPeer]:
    """Browse, then resolve each found instance in turn. Slow-ish
    (roughly browse_duration + N * resolve_duration seconds) since
    dns-sd offers no batch-resolve mode -- fine for a periodic
    discovery pass, not meant for a tight loop."""
    peers = []
    for name in browse():
        resolved = resolve(name, duration=resolve_duration)
        if resolved is not None:
            hostname, port = resolved
            peers.append(MdnsPeer(instance_name=name, hostname=hostname, port=port))
    return peers
