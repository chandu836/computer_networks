"""
Queue Monitor (Member 2).

Collects per-link queue statistics from Linux traffic control (``tc``),
estimates queueing delay and link utilization, and exposes them to the
Path Selector (Member 3) in the agreed ``queue_stats`` format.

Measurements
------------
Estimated queueing delay (seconds)::

    D_q = 8 * B_q / R

    B_q = bytes currently queued (tc "backlog")
    R   = link capacity in bits/second

Link utilization over one sampling interval::

    U = 8 * dB / (R * dt)

    dB = increase in transmitted bytes (tc "Sent ... bytes")
    dt = elapsed time in seconds

Backlog bytes and transmitted bytes are different counters; backlog is only
used for delay, transmitted bytes only for utilization.

Link identifiers
----------------
A directed link is identified by ``(src_dpid, dst_dpid)`` with integer
datapath IDs, e.g. ``(1, 2)`` is traffic leaving s1 towards s2. Its queue is
the egress queue of s1's interface facing s2 (``s1-eth2`` in the diamond).

Output format (``get_all_stats()``)::

    {
        (1, 2): {
            "queue_bytes": 12000,
            "estimated_delay_ms": 9.6,
            "utilization": 0.65,
            ...extra fields, see QueueMonitor._build_entry...
        },
    }

Units: bytes, milliseconds, utilization as a fraction in [0, 1],
throughput and capacity in bits/second.

Quick use::

    from controller.queue_monitor import QueueMonitor

    monitor = QueueMonitor()          # diamond topology, 10 Mbps links
    monitor.collect_stats()
    monitor.get_link_delay(1, 2)      # -> ms, or None if unavailable
    monitor.get_all_stats()

Command line (run on the Mininet host while the topology is up)::

    python3 -m controller.queue_monitor --interval 1
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Tuple

try:
    from common.config import DEFAULT_LINK_BW_MBPS
except ImportError:  # pragma: no cover - allows running the file standalone
    DEFAULT_LINK_BW_MBPS = 10

try:
    from common.config import LINKS as CONFIG_LINKS
except ImportError:  # pragma: no cover - older config without LINKS
    CONFIG_LINKS = None

try:
    from common.models import LinkMetrics
except ImportError:  # pragma: no cover
    LinkMetrics = None


LOG = logging.getLogger(__name__)

LinkKey = Tuple[int, int]

DEFAULT_TC_TIMEOUT_S = 2.0
DEFAULT_EWMA_ALPHA = 0.3

# qdiscs that do not hold egress packets and must be ignored
_IGNORED_QDISC_KINDS = {"ingress", "clsact"}


# ---------------------------------------------------------------------------
# Link identifiers and link map
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MonitoredLink:
    """One directed link and the egress interface whose queue serves it."""

    src: int
    dst: int
    interface: str
    capacity_bps: Optional[float]

    @property
    def key(self) -> LinkKey:
        return (self.src, self.dst)


def parse_switch_id(node) -> int:
    """
    Convert a switch identifier to an integer dpid.

    Accepts 1, "1", "s1", "S1" and returns 1. Mininet assigns dpid N to
    switch sN by default, so names and dpids map one-to-one.
    """
    if isinstance(node, bool):
        raise ValueError(f"Invalid switch identifier: {node!r}")
    if isinstance(node, int):
        return node
    text = str(node).strip()
    match = re.fullmatch(r"[sS]?(\d+)", text)
    if not match:
        raise ValueError(f"Invalid switch identifier: {node!r}")
    return int(match.group(1))


def link_key(src, dst) -> LinkKey:
    """Return the canonical (src_dpid, dst_dpid) key for a directed link."""
    return (parse_switch_id(src), parse_switch_id(dst))


def _mbps_to_bps(bw_mbps) -> Optional[float]:
    if bw_mbps is None:
        return None
    bw = float(bw_mbps)
    return bw * 1_000_000 if bw > 0 else None


def default_diamond_link_map(bw_mbps=DEFAULT_LINK_BW_MBPS) -> Dict[LinkKey, MonitoredLink]:
    """
    Directed inter-switch links of topology/diamond.py.

    Mininet numbers a switch's ports in the order addLink() is called, so
    diamond.py produces:

        s1-eth1 h1   s1-eth2 s2   s1-eth3 s3
        s2-eth1 s1   s2-eth2 s4
        s3-eth1 s1   s3-eth2 s4
        s4-eth1 s2   s4-eth2 s3   s4-eth3 h2

    If the topology changes, prefer build_link_map_from_topo() or
    build_link_map_from_net(), which read the ports instead of assuming them.
    """
    capacity = _mbps_to_bps(bw_mbps)
    egress = {
        (1, 2): "s1-eth2",
        (2, 1): "s2-eth1",
        (2, 4): "s2-eth2",
        (4, 2): "s4-eth1",
        (1, 3): "s1-eth3",
        (3, 1): "s3-eth1",
        (3, 4): "s3-eth2",
        (4, 3): "s4-eth2",
    }
    return {
        key: MonitoredLink(key[0], key[1], intf, capacity)
        for key, intf in egress.items()
    }


def _is_switch_name(name: str) -> bool:
    return re.fullmatch(r"[sS]\d+", str(name)) is not None


def build_link_map_from_config(links=None, default_bw_mbps=DEFAULT_LINK_BW_MBPS
                               ) -> Dict[LinkKey, MonitoredLink]:
    """
    Build the link map from common.config.LINKS, the list Member 1's
    diamond.py feeds to addLink().

    Mininet numbers ports per node in addLink() order, starting at 1 for
    switches and 0 for hosts, so the egress interface of each link can be
    derived from the list order. Falls back to the static diamond map if
    the config has no LINKS.
    """
    links = CONFIG_LINKS if links is None else links
    if not links:
        return default_diamond_link_map(default_bw_mbps)

    next_port: Dict[str, int] = {}

    def allocate(node: str) -> int:
        if node not in next_port:
            next_port[node] = 1 if _is_switch_name(node) else 0
        port = next_port[node]
        next_port[node] += 1
        return port

    link_map: Dict[LinkKey, MonitoredLink] = {}
    for entry in links:
        node1, node2 = entry[0], entry[1]
        bw_mbps = entry[2] if len(entry) > 2 and entry[2] is not None else default_bw_mbps
        port1, port2 = allocate(node1), allocate(node2)
        if not (_is_switch_name(node1) and _is_switch_name(node2)):
            continue
        capacity = _mbps_to_bps(bw_mbps)
        a, b = parse_switch_id(node1), parse_switch_id(node2)
        link_map[(a, b)] = MonitoredLink(a, b, f"{node1}-eth{port1}", capacity)
        link_map[(b, a)] = MonitoredLink(b, a, f"{node2}-eth{port2}", capacity)
    return link_map


def _config_bandwidths(links=None) -> Dict[LinkKey, float]:
    """{(a, b): bw_mbps} in both directions, from common.config.LINKS."""
    result: Dict[LinkKey, float] = {}
    for entry in (CONFIG_LINKS if links is None else links) or []:
        node1, node2 = entry[0], entry[1]
        if _is_switch_name(node1) and _is_switch_name(node2) and len(entry) > 2:
            a, b = parse_switch_id(node1), parse_switch_id(node2)
            result[(a, b)] = result[(b, a)] = entry[2]
    return result


def build_link_map_from_topology_manager(topology_manager, bandwidths_mbps=None,
                                         default_bw_mbps=DEFAULT_LINK_BW_MBPS
                                         ) -> Dict[LinkKey, MonitoredLink]:
    """
    Build the link map from Member 1's controller.topology_manager.TopologyManager.

    Its ``links`` dict is {(src_dpid, dst_dpid): src_port}, discovered live
    through OpenFlow/LLDP, so the egress interface is ``s<src>-eth<port>``
    (Mininet's naming). Bandwidth comes from ``bandwidths_mbps``
    ({(src, dst): Mbps}), then common.config.LINKS, then the default.
    """
    bandwidths = _config_bandwidths()
    if bandwidths_mbps:
        bandwidths.update({link_key(*k): v for k, v in bandwidths_mbps.items()})
    link_map: Dict[LinkKey, MonitoredLink] = {}
    for (src, dst), port in dict(topology_manager.links).items():
        if port is None:
            continue
        key = link_key(src, dst)
        capacity = _mbps_to_bps(bandwidths.get(key, default_bw_mbps))
        link_map[key] = MonitoredLink(key[0], key[1], f"s{key[0]}-eth{int(port)}", capacity)
    return link_map


def build_link_map_from_topo(topo, default_bw_mbps=DEFAULT_LINK_BW_MBPS
                             ) -> Dict[LinkKey, MonitoredLink]:
    """
    Build the link map from a Mininet Topo object (e.g. DiamondTopo()).

    Only switch-to-switch links are included. Each undirected link produces
    two directed entries, one per egress interface. The bandwidth comes from
    the link's ``bw`` option, falling back to ``default_bw_mbps``.
    """
    link_map: Dict[LinkKey, MonitoredLink] = {}
    for node1, node2, info in topo.links(withInfo=True):
        if not (_is_switch_name(node1) and _is_switch_name(node2)):
            continue
        port1 = info.get("port1")
        port2 = info.get("port2")
        if port1 is None or port2 is None:
            port1, port2 = topo.port(node1, node2)
        capacity = _mbps_to_bps(info.get("bw", default_bw_mbps))
        a, b = parse_switch_id(node1), parse_switch_id(node2)
        link_map[(a, b)] = MonitoredLink(a, b, f"{node1}-eth{port1}", capacity)
        link_map[(b, a)] = MonitoredLink(b, a, f"{node2}-eth{port2}", capacity)
    return link_map


def build_link_map_from_net(net, default_bw_mbps=DEFAULT_LINK_BW_MBPS
                            ) -> Dict[LinkKey, MonitoredLink]:
    """
    Build the link map from a running Mininet network object.

    Uses the real interface names created by Mininet, so it stays correct
    even if port numbering differs from the static diamond map.
    """
    link_map: Dict[LinkKey, MonitoredLink] = {}
    for link in net.links:
        intf1, intf2 = link.intf1, link.intf2
        name1, name2 = intf1.node.name, intf2.node.name
        if not (_is_switch_name(name1) and _is_switch_name(name2)):
            continue
        params = getattr(intf1, "params", {}) or {}
        capacity = _mbps_to_bps(params.get("bw", default_bw_mbps))
        a, b = parse_switch_id(name1), parse_switch_id(name2)
        link_map[(a, b)] = MonitoredLink(a, b, intf1.name, capacity)
        link_map[(b, a)] = MonitoredLink(b, a, intf2.name, capacity)
    return link_map


def normalize_link_map(link_map) -> Dict[LinkKey, MonitoredLink]:
    """
    Accept several link-map shapes and return {key: MonitoredLink}.

    Accepted values per key (key may use "s1" or 1 style ids):
        MonitoredLink
        "s1-eth2"                                (capacity = default bw)
        ("s1-eth2", 10)                          (bw in Mbps)
        {"interface": "s1-eth2", "bw_mbps": 10}
    """
    default_capacity = _mbps_to_bps(DEFAULT_LINK_BW_MBPS)
    result: Dict[LinkKey, MonitoredLink] = {}
    for raw_key, value in link_map.items():
        key = link_key(*raw_key)
        if isinstance(value, MonitoredLink):
            link = MonitoredLink(key[0], key[1], value.interface, value.capacity_bps)
        elif isinstance(value, str):
            link = MonitoredLink(key[0], key[1], value, default_capacity)
        elif isinstance(value, dict):
            if "capacity_bps" in value:
                capacity = value["capacity_bps"]
            else:
                capacity = _mbps_to_bps(value.get("bw_mbps", DEFAULT_LINK_BW_MBPS))
            link = MonitoredLink(key[0], key[1], value["interface"], capacity)
        elif isinstance(value, (tuple, list)) and len(value) == 2:
            link = MonitoredLink(key[0], key[1], value[0], _mbps_to_bps(value[1]))
        else:
            raise ValueError(f"Unsupported link map entry for {raw_key!r}: {value!r}")
        result[key] = link
    return result


# ---------------------------------------------------------------------------
# tc output parsing
# ---------------------------------------------------------------------------

@dataclass
class QdiscStats:
    """Statistics of one qdisc as reported by ``tc -s qdisc show``."""

    dev: str
    kind: str
    handle: str = ""
    parent: Optional[str] = None
    is_root: bool = False
    sent_bytes: int = 0
    sent_packets: int = 0
    dropped: int = 0
    overlimits: int = 0
    requeues: int = 0
    backlog_bytes: int = 0
    backlog_packets: int = 0


_QDISC_HEADER_RE = re.compile(
    r"^qdisc\s+(?P<kind>\S+)\s+(?P<handle>\S+)\s+dev\s+(?P<dev>\S+)\s*(?P<rest>.*)$"
)
_SENT_RE = re.compile(
    r"Sent\s+(?P<bytes>\d+)\s+bytes\s+(?P<pkts>\d+)\s+pkts?"
    r"(?:\s*\(\s*dropped\s+(?P<dropped>\d+)"
    r"(?:\s*,\s*overlimits\s+(?P<overlimits>\d+))?"
    r"(?:\s+requeues\s+(?P<requeues>\d+))?\s*\))?"
)
_BACKLOG_RE = re.compile(
    r"backlog\s+(?P<size>\d+(?:\.\d+)?)(?P<unit>[KMG]?)b\s+(?P<pkts>\d+)p"
)
_UNIT_FACTOR = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}


def _parse_size(size: str, unit: str) -> int:
    """tc prints sizes as 1514b, 98Kb, 1Mb (binary multiples)."""
    return int(round(float(size) * _UNIT_FACTOR[unit]))


def parse_tc_text(output: str) -> List[QdiscStats]:
    """Parse the plain-text output of ``tc -s qdisc show [dev X]``."""
    qdiscs: List[QdiscStats] = []
    current: Optional[QdiscStats] = None

    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        header = _QDISC_HEADER_RE.match(stripped)
        if header:
            rest = header.group("rest")
            parent_match = re.search(r"\bparent\s+(\S+)", rest)
            current = QdiscStats(
                dev=header.group("dev"),
                kind=header.group("kind"),
                handle=header.group("handle"),
                parent=parent_match.group(1) if parent_match else None,
                is_root=re.search(r"\broot\b", rest) is not None,
            )
            qdiscs.append(current)
            continue

        if current is None:
            continue

        sent = _SENT_RE.search(stripped)
        if sent:
            current.sent_bytes = int(sent.group("bytes"))
            current.sent_packets = int(sent.group("pkts"))
            current.dropped = int(sent.group("dropped") or 0)
            current.overlimits = int(sent.group("overlimits") or 0)
            current.requeues = int(sent.group("requeues") or 0)
            continue

        backlog = _BACKLOG_RE.search(stripped)
        if backlog:
            current.backlog_bytes = _parse_size(backlog.group("size"), backlog.group("unit"))
            current.backlog_packets = int(backlog.group("pkts"))

    return qdiscs


def parse_tc_json(output: str) -> List[QdiscStats]:
    """Parse the output of ``tc -s -j qdisc show [dev X]``."""
    data = json.loads(output) if output.strip() else []
    if not isinstance(data, list):
        raise ValueError("Unexpected tc JSON output: expected a list")

    qdiscs: List[QdiscStats] = []
    for item in data:
        if not isinstance(item, dict) or "kind" not in item or "dev" not in item:
            continue
        # Some iproute2 versions nest counters under "stats"/"stats64".
        stats = item.get("stats64") or item.get("stats") or item
        qdiscs.append(QdiscStats(
            dev=item["dev"],
            kind=item["kind"],
            handle=str(item.get("handle", "")),
            parent=item.get("parent"),
            is_root=bool(item.get("root", False)),
            sent_bytes=int(stats.get("bytes", 0) or 0),
            sent_packets=int(stats.get("packets", 0) or 0),
            dropped=int(stats.get("drops", 0) or 0),
            overlimits=int(stats.get("overlimits", 0) or 0),
            requeues=int(stats.get("requeues", 0) or 0),
            backlog_bytes=int(stats.get("backlog", 0) or 0),
            backlog_packets=int(stats.get("qlen", 0) or 0),
        ))
    return qdiscs


def parse_tc_output(output: str) -> List[QdiscStats]:
    """Parse tc output in either JSON or plain-text form."""
    if output.lstrip().startswith("["):
        try:
            return parse_tc_json(output)
        except (ValueError, TypeError):
            LOG.debug("tc output looked like JSON but did not parse; trying text")
    return parse_tc_text(output)


def group_by_device(qdiscs: Iterable[QdiscStats]) -> Dict[str, List[QdiscStats]]:
    grouped: Dict[str, List[QdiscStats]] = {}
    for qdisc in qdiscs:
        grouped.setdefault(qdisc.dev, []).append(qdisc)
    return grouped


def summarize_interface(qdiscs: List[QdiscStats]) -> Optional[dict]:
    """
    Combine all egress qdiscs of one interface into a single summary.

    Linux classful qdiscs (htb, tbf, mq) already aggregate their children's
    counters at the root, so the root is used for transmitted bytes. For
    backlog and drops, the larger of the root value and the children's sum
    is taken, so a chain such as htb -> netem is not double counted and a
    root that does not aggregate is still covered.

    Returns None if the interface has no egress qdisc.
    """
    egress = [q for q in qdiscs if q.kind not in _IGNORED_QDISC_KINDS]
    if not egress:
        return None

    roots = [q for q in egress if q.is_root]
    root = roots[0] if roots else egress[0]
    children = [q for q in egress if q is not root]

    backlog_bytes = max(root.backlog_bytes, sum(c.backlog_bytes for c in children))
    backlog_packets = max(root.backlog_packets, sum(c.backlog_packets for c in children))
    dropped = max(root.dropped, sum(c.dropped for c in children))

    return {
        "qdisc": root.kind,
        "tx_bytes": root.sent_bytes,
        "tx_packets": root.sent_packets,
        "dropped_packets": dropped,
        "overlimits": root.overlimits,
        "requeues": root.requeues,
        "queue_bytes": backlog_bytes,
        "queue_packets": backlog_packets,
    }


# ---------------------------------------------------------------------------
# Formulas
# ---------------------------------------------------------------------------

def estimate_queueing_delay_s(queue_bytes, capacity_bps) -> Optional[float]:
    """D_q = 8 * B_q / R. Returns seconds, or None if inputs are unusable."""
    if queue_bytes is None or capacity_bps is None:
        return None
    if capacity_bps <= 0 or queue_bytes < 0:
        return None
    return 8.0 * queue_bytes / capacity_bps


def estimate_queueing_delay_ms(queue_bytes, capacity_bps) -> Optional[float]:
    delay_s = estimate_queueing_delay_s(queue_bytes, capacity_bps)
    return None if delay_s is None else delay_s * 1000.0


def calculate_throughput_bps(delta_bytes, delta_t) -> Optional[float]:
    if delta_bytes is None or delta_t is None or delta_t <= 0 or delta_bytes < 0:
        return None
    return 8.0 * delta_bytes / delta_t


def calculate_utilization(delta_bytes, capacity_bps, delta_t, cap=True) -> Optional[float]:
    """
    U = 8 * dB / (R * dt), a fraction of link capacity.

    With cap=True the result is limited to 1.0; small overshoots happen
    because sampling times are not exact, and larger ones mean the link is
    not actually shaped to the configured rate (e.g. TCLink not active).
    """
    throughput = calculate_throughput_bps(delta_bytes, delta_t)
    if throughput is None or capacity_bps is None or capacity_bps <= 0:
        return None
    utilization = throughput / capacity_bps
    return min(utilization, 1.0) if cap else utilization


# ---------------------------------------------------------------------------
# tc reader
# ---------------------------------------------------------------------------

class TcError(RuntimeError):
    """Raised when tc statistics cannot be read."""


Runner = Callable[[List[str], float], str]


def _default_runner(cmd: List[str], timeout: float) -> str:
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        raise TcError(f"'{cmd[0]}' not found; install iproute2") from exc
    except subprocess.TimeoutExpired as exc:
        raise TcError(f"'{' '.join(cmd)}' timed out after {timeout}s") from exc
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout).strip()
        raise TcError(f"'{' '.join(cmd)}' failed: {message}")
    return completed.stdout


class TcStatsReader:
    """
    Reads qdisc statistics for all interfaces with a single tc call.

    JSON output (``tc -j``) is tried first; if this iproute2 version does not
    support it, the reader switches to plain text permanently.
    Reading statistics does not change any configuration and normally does
    not need root.
    """

    def __init__(self, tc_path: str = "tc", use_json: bool = True,
                 timeout: float = DEFAULT_TC_TIMEOUT_S,
                 runner: Optional[Runner] = None):
        self.tc_path = tc_path
        self.use_json = use_json
        self.timeout = timeout
        self._runner = runner or _default_runner

    def _command(self, json_mode: bool) -> List[str]:
        cmd = [self.tc_path, "-s"]
        if json_mode:
            cmd.append("-j")
        cmd += ["qdisc", "show"]
        return cmd

    def read(self) -> Dict[str, List[QdiscStats]]:
        """Return {interface_name: [QdiscStats, ...]}. Raises TcError."""
        if self.use_json:
            try:
                output = self._runner(self._command(True), self.timeout)
                return group_by_device(parse_tc_json(output))
            except (TcError, ValueError, TypeError) as exc:
                LOG.info("tc JSON output unavailable (%s); using text output", exc)
                self.use_json = False
        output = self._runner(self._command(False), self.timeout)
        return group_by_device(parse_tc_text(output))


# ---------------------------------------------------------------------------
# Queue monitor
# ---------------------------------------------------------------------------

class QueueMonitor:
    """
    Collects queue and link statistics for every monitored directed link.

    Interface agreed with Member 3 (Path Selector):

        collect_stats()            -> dict, also refreshes the cache
        get_link_delay(src, dst)   -> estimated queueing delay in ms or None
        get_all_stats()            -> copy of the queue_stats dictionary

    Missing data never raises from these methods: an unavailable link is
    reported with ``valid = False`` and None metrics, and get_link_delay
    returns ``default`` (None unless given).
    """

    def __init__(self, link_map=None, reader: Optional[TcStatsReader] = None,
                 ewma_alpha: float = DEFAULT_EWMA_ALPHA,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time):
        if not 0.0 < ewma_alpha <= 1.0:
            raise ValueError("ewma_alpha must be in (0, 1]")
        self._links = normalize_link_map(
            link_map if link_map is not None else build_link_map_from_config()
        )
        self._reader = reader or TcStatsReader()
        self._alpha = ewma_alpha
        self._clock = clock
        self._wall_clock = wall_clock

        self._lock = threading.RLock()
        self._stats: Dict[LinkKey, dict] = {}
        # previous counters per link: (monotonic_time, tx_bytes, dropped)
        self._previous: Dict[LinkKey, Tuple[float, int, int]] = {}
        self._smoothed_delay: Dict[LinkKey, float] = {}
        self.last_error: Optional[str] = None
        self.last_collect_time: Optional[float] = None

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # -- configuration ------------------------------------------------------

    @classmethod
    def from_topo(cls, topo, **kwargs) -> "QueueMonitor":
        return cls(link_map=build_link_map_from_topo(topo), **kwargs)

    @classmethod
    def from_net(cls, net, **kwargs) -> "QueueMonitor":
        return cls(link_map=build_link_map_from_net(net), **kwargs)

    @classmethod
    def from_topology_manager(cls, topology_manager, **kwargs) -> "QueueMonitor":
        return cls(link_map=build_link_map_from_topology_manager(topology_manager), **kwargs)


    def sync_with_topology_manager(self, topology_manager) -> None:
        """
        Synchronize monitored links with the exact currently discovered
        topology, including an empty topology.

        An empty discovered map means no inter-switch links are currently
        available. Do not retain the previous map.
        """
        link_map = build_link_map_from_topology_manager(
            topology_manager
        )

        self.update_link_map(link_map)



    def update_link_map(self, link_map) -> None:
        """Replace monitored links and discard stale per-link measurements."""

        new_links = normalize_link_map(link_map)

        with self._lock:
            old_links = self._links

            for key, old_link in old_links.items():
                new_link = new_links.get(key)

                if new_link != old_link:
                    self._stats.pop(key, None)
                    self._previous.pop(key, None)
                    self._smoothed_delay.pop(key, None)

            self._links = new_links


    def get_monitored_links(self) -> Dict[LinkKey, MonitoredLink]:
        with self._lock:
            return dict(self._links)

    # -- collection ---------------------------------------------------------


    def collect_stats(self) -> Dict[LinkKey, dict]:
        """
        Read tc once and update statistics for the current topology.

        If the topology changes while tc is being read, discard this
        collection rather than publishing measurements for stale links.
        """

        with self._lock:
            links_snapshot = dict(self._links)

        now = self._clock()
        wall_now = self._wall_clock()

        try:
            by_device = self._reader.read()
            read_error = None

        except Exception as exc:
            LOG.warning("Queue statistics unavailable: %s", exc)
            by_device = {}
            read_error = str(exc)

        with self._lock:

            # A topology event may have occurred while tc was executing.
            if links_snapshot != self._links:
                return copy.deepcopy(self._stats)

            for key, link in links_snapshot.items():
                self._stats[key] = self._build_entry(
                    link,
                    by_device.get(link.interface),
                    now,
                    wall_now,
                    read_error,
                )

            # Remove entries that are no longer in the current topology.
            for key in list(self._stats):
                if key not in self._links:
                    self._stats.pop(key, None)

            self.last_error = read_error
            self.last_collect_time = wall_now

            return copy.deepcopy(self._stats)


    def _build_entry(self, link: MonitoredLink, qdiscs, now, wall_now,
                     read_error) -> dict:
        key = link.key
        entry = {
            "src": link.src,
            "dst": link.dst,
            "interface": link.interface,
            "capacity_bps": link.capacity_bps,
            "qdisc": None,
            "queue_bytes": None,
            "queue_packets": None,
            "estimated_delay_ms": None,
            "smoothed_delay_ms": None,
            "utilization": None,
            "throughput_bps": None,
            "tx_bytes": None,
            "tx_packets": None,
            "dropped_packets": None,
            "drops_delta": None,
            "overlimits": None,
            "timestamp": wall_now,
            "valid": False,
            "error": None,
        }

        summary = summarize_interface(qdiscs) if qdiscs else None
        if summary is None:
            entry["error"] = read_error or f"no qdisc statistics for {link.interface}"
            # forget counters so the next good sample is not compared with
            # a value from before the outage
            self._previous.pop(key, None)
            self._smoothed_delay.pop(key, None)
            return entry

        entry.update(summary)
        entry["valid"] = True

        delay_ms = estimate_queueing_delay_ms(summary["queue_bytes"], link.capacity_bps)
        entry["estimated_delay_ms"] = delay_ms
        if delay_ms is not None:
            previous_smoothed = self._smoothed_delay.get(key)
            smoothed = delay_ms if previous_smoothed is None else (
                self._alpha * delay_ms + (1 - self._alpha) * previous_smoothed
            )
            self._smoothed_delay[key] = smoothed
            entry["smoothed_delay_ms"] = smoothed

        previous = self._previous.get(key)
        tx_bytes = summary["tx_bytes"]
        dropped = summary["dropped_packets"]
        if previous is not None:
            prev_time, prev_bytes, prev_dropped = previous
            delta_t = now - prev_time
            delta_bytes = tx_bytes - prev_bytes
            if delta_bytes >= 0 and delta_t > 0:
                entry["throughput_bps"] = calculate_throughput_bps(delta_bytes, delta_t)
                entry["utilization"] = calculate_utilization(
                    delta_bytes, link.capacity_bps, delta_t
                )
            if dropped >= prev_dropped:
                entry["drops_delta"] = dropped - prev_dropped
            # negative deltas mean the qdisc was replaced and counters reset;
            # this sample becomes the new baseline
        self._previous[key] = (now, tx_bytes, dropped)
        return entry

    # -- queries ------------------------------------------------------------

    def get_link_delay(self, src, dst, default=None, smoothed: bool = False):
        """
        Estimated queueing delay of the directed link src -> dst in ms.

        src/dst may be dpids (1) or names ("s1"). Returns ``default`` when the
        link is unknown or has no valid measurement yet.
        """
        try:
            key = link_key(src, dst)
        except ValueError:
            return default
        with self._lock:
            entry = self._stats.get(key)
            if not entry or not entry["valid"]:
                return default
            value = entry["smoothed_delay_ms" if smoothed else "estimated_delay_ms"]
            return default if value is None else value

    def get_link_stats(self, src, dst) -> Optional[dict]:
        try:
            key = link_key(src, dst)
        except ValueError:
            return None
        with self._lock:
            entry = self._stats.get(key)
            return copy.deepcopy(entry) if entry else None

    def get_all_stats(self) -> Dict[LinkKey, dict]:
        """Return a copy of the latest queue_stats (empty before the first collect)."""
        with self._lock:
            return copy.deepcopy(self._stats)

    def get_link_metrics(self) -> list:
        """
        Latest statistics as common.models.LinkMetrics objects.

        Note LinkMetrics uses switch names ("s1") and seconds
        (estimated_delay_s), as defined in common/models.py.
        """
        if LinkMetrics is None:  # pragma: no cover
            raise RuntimeError("common.models.LinkMetrics is not importable")
        metrics = []
        with self._lock:
            for (src, dst), entry in sorted(self._stats.items()):
                delay_ms = entry["estimated_delay_ms"]
                capacity = entry["capacity_bps"]
                metrics.append(LinkMetrics(
                    source=f"s{src}",
                    destination=f"s{dst}",
                    queue_bytes=entry["queue_bytes"],
                    bandwidth_bps=int(capacity) if capacity is not None else None,
                    estimated_delay_s=None if delay_ms is None else delay_ms / 1000.0,
                    utilization=entry["utilization"],
                ))
        return metrics

    # -- background polling -------------------------------------------------

    def run(self, interval: float = 1.0, stop_event: Optional[threading.Event] = None,
            callback: Optional[Callable[[Dict[LinkKey, dict]], None]] = None) -> None:
        """
        Collect every ``interval`` seconds until ``stop_event`` is set.

        Inside Ryu, run this in a green thread::

            from ryu.lib import hub
            self.monitor_thread = hub.spawn(self.queue_monitor.run, 1.0)
        """
        if interval <= 0:
            raise ValueError("interval must be positive")
        stop_event = stop_event or self._stop_event
        while not stop_event.is_set():
            stats = self.collect_stats()
            if callback is not None:
                try:
                    callback(stats)
                except Exception:  # a faulty consumer must not stop polling
                    LOG.exception("Queue monitor callback failed")
            if stop_event.wait(interval):
                break

    def start(self, interval: float = 1.0, callback=None) -> None:
        """Start polling in a daemon thread (outside Ryu)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self.run, args=(interval, self._stop_event, callback),
            name="queue-monitor", daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: Optional[float] = 5.0) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout)
            self._thread = None


# ---------------------------------------------------------------------------
# Optional queue configuration
# ---------------------------------------------------------------------------

class QueueConfigurator:
    """
    Builds (and optionally applies) tc commands that shape an interface and
    give it a bounded FIFO queue:

        root htb 5:  ->  class 5:1 rate <R>  ->  leaf pfifo limit <N>

    Safety rules, because Member 1's TCLink may already configure the same
    interfaces:
      * dry_run=True by default: commands are only returned, never executed;
      * an interface that already has a shaping root qdisc is skipped
        unless replace_existing=True.
    Applying changes needs root (run the controller with sudo).
    """

    SHAPING_KINDS = {"htb", "tbf", "hfsc", "netem"}

    def __init__(self, tc_path: str = "tc", runner: Optional[Runner] = None,
                 reader: Optional[TcStatsReader] = None,
                 timeout: float = DEFAULT_TC_TIMEOUT_S):
        self.tc_path = tc_path
        self._runner = runner or _default_runner
        self._reader = reader or TcStatsReader(tc_path=tc_path, runner=runner)
        self.timeout = timeout

    def build_commands(self, interface: str, rate_mbit: float,
                       limit_packets: int = 100) -> List[List[str]]:
        if rate_mbit <= 0:
            raise ValueError("rate_mbit must be positive")
        if limit_packets <= 0:
            raise ValueError("limit_packets must be positive")
        tc = self.tc_path
        return [
            [tc, "qdisc", "replace", "dev", interface, "root", "handle", "5:", "htb", "default", "1"],
            [tc, "class", "replace", "dev", interface, "parent", "5:", "classid", "5:1",
             "htb", "rate", f"{rate_mbit:g}mbit", "burst", "15k"],
            [tc, "qdisc", "replace", "dev", interface, "parent", "5:1", "handle", "10:",
             "pfifo", "limit", str(int(limit_packets))],
        ]

    def has_shaping(self, interface: str, by_device=None) -> bool:
        by_device = by_device if by_device is not None else self._reader.read()
        return any(q.is_root and q.kind in self.SHAPING_KINDS
                   for q in by_device.get(interface, []))

    def configure(self, links: Iterable[MonitoredLink], limit_packets: int = 100,
                  dry_run: bool = True, replace_existing: bool = False) -> Dict[str, dict]:
        """
        Configure each link's egress interface at its capacity.

        Returns {interface: {"action": "planned"|"applied"|"skipped"|"error",
                             "commands": [...], "reason": ...}}.
        """
        results: Dict[str, dict] = {}
        try:
            by_device = self._reader.read()
        except TcError as exc:
            by_device = None
            read_error = str(exc)
        else:
            read_error = None

        for link in links:
            intf = link.interface
            if link.capacity_bps is None:
                results[intf] = {"action": "skipped", "commands": [],
                                 "reason": "link capacity unknown"}
                continue
            commands = self.build_commands(intf, link.capacity_bps / 1_000_000, limit_packets)
            if not replace_existing:
                if by_device is None:
                    results[intf] = {"action": "skipped", "commands": commands,
                                     "reason": f"cannot inspect existing qdiscs: {read_error}"}
                    continue
                if self.has_shaping(intf, by_device):
                    results[intf] = {"action": "skipped", "commands": commands,
                                     "reason": "interface already shaped (e.g. TCLink)"}
                    continue
            if dry_run:
                results[intf] = {"action": "planned", "commands": commands, "reason": None}
                continue
            try:
                for cmd in commands:
                    self._runner(cmd, self.timeout)
                results[intf] = {"action": "applied", "commands": commands, "reason": None}
            except TcError as exc:
                results[intf] = {"action": "error", "commands": commands, "reason": str(exc)}
        return results


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def format_stats_table(stats: Dict[LinkKey, dict]) -> str:
    header = (f"{'link':<8}{'intf':<10}{'qdisc':<8}{'backlog B':>10}{'pkts':>6}"
              f"{'delay ms':>10}{'util':>7}{'Mbps':>8}{'drops':>8}{'+drop':>7}")
    lines = [header, "-" * len(header)]

    def fmt(value, spec):
        return "-" if value is None else format(value, spec)

    for (src, dst), e in sorted(stats.items()):
        if not e["valid"]:
            lines.append(f"{src}->{dst:<5}{e['interface']:<10}unavailable: {e['error']}")
            continue
        mbps = None if e["throughput_bps"] is None else e["throughput_bps"] / 1e6
        lines.append(
            f"{f'{src}->{dst}':<8}{e['interface']:<10}{e['qdisc'] or '-':<8}"
            f"{fmt(e['queue_bytes'], 'd'):>10}{fmt(e['queue_packets'], 'd'):>6}"
            f"{fmt(e['estimated_delay_ms'], '.2f'):>10}{fmt(e['utilization'], '.2f'):>7}"
            f"{fmt(mbps, '.2f'):>8}{fmt(e['dropped_packets'], 'd'):>8}"
            f"{fmt(e['drops_delta'], 'd'):>7}"
        )
    return "\n".join(lines)


def _json_ready(stats: Dict[LinkKey, dict]) -> dict:
    return {f"{src}-{dst}": entry for (src, dst), entry in sorted(stats.items())}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Monitor egress queues of the diamond topology")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between samples")
    parser.add_argument("--count", type=int, default=0, help="samples to take (0 = forever)")
    parser.add_argument("--bw", type=float, default=None,
                        help="override every link's capacity in Mbps "
                             "(default: bandwidths from common/config.py)")
    parser.add_argument("--tc-path", default=shutil.which("tc") or "tc")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    link_map = build_link_map_from_config()
    if args.bw is not None:
        link_map = {k: MonitoredLink(v.src, v.dst, v.interface, _mbps_to_bps(args.bw))
                    for k, v in link_map.items()}
    monitor = QueueMonitor(link_map=link_map, reader=TcStatsReader(tc_path=args.tc_path))
    taken = 0
    try:
        while args.count == 0 or taken < args.count:
            stats = monitor.collect_stats()
            taken += 1
            if args.json:
                print(json.dumps(_json_ready(stats)), flush=True)
            else:
                print(time.strftime("%H:%M:%S"))
                print(format_stats_table(stats), flush=True)
                print()
            if args.count == 0 or taken < args.count:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
