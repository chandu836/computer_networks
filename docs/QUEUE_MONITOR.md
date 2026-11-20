# Queue Monitor (Member 2)

File: `controller/queue_monitor.py` (also importable as `monitoring.queue_monitor`).

## What it measures

For every directed inter-switch link it reads the egress qdisc of the
sending switch's interface with one `tc -s qdisc show` call and computes:

| Field | Meaning | Unit |
| --- | --- | --- |
| `queue_bytes` | tc backlog (bytes waiting) | bytes |
| `queue_packets` | tc backlog | packets |
| `estimated_delay_ms` | `8 * queue_bytes / capacity_bps` | ms |
| `smoothed_delay_ms` | EWMA of the delay (alpha 0.3) | ms |
| `utilization` | `8 * Δtx_bytes / (capacity_bps * Δt)`, capped at 1.0 | fraction 0–1 |
| `throughput_bps` | `8 * Δtx_bytes / Δt` (not capped) | bits/s |
| `tx_bytes`, `tx_packets` | cumulative transmitted (tc "Sent") | bytes / packets |
| `dropped_packets` | cumulative drops | packets |
| `drops_delta` | drops since previous sample | packets |
| `capacity_bps` | configured link rate used in the formulas | bits/s |
| `interface`, `qdisc` | e.g. `s1-eth2`, `htb` | |
| `valid`, `error` | `False` + reason when stats are unavailable | |
| `timestamp` | wall-clock time of the sample | Unix s |

`utilization`, `throughput_bps` and `drops_delta` are `None` on the first
sample (they need two samples), and after a counter reset.

## Link identifiers

Key = `(src_dpid, dst_dpid)` with integers. `(1, 2)` = traffic leaving s1
towards s2. Methods also accept `"s1"`/`"1"`.

By default the map is derived from `LINKS` in `common/config.py`, the same
list `topology/diamond.py` passes to `addLink()`, so it follows topology
changes automatically. For the diamond this gives:

| Link | Egress interface | Rate |
| --- | --- | --- |
| 1 → 2 | `s1-eth2` | 10 Mbps |
| 2 → 1 | `s2-eth1` | 10 Mbps |
| 2 → 4 | `s2-eth2` | 10 Mbps |
| 4 → 2 | `s4-eth1` | 10 Mbps |
| 1 → 3 | `s1-eth3` | 10 Mbps |
| 3 → 1 | `s3-eth1` | 10 Mbps |
| 3 → 4 | `s3-eth2` | 10 Mbps |
| 4 → 3 | `s4-eth2` | 10 Mbps |

Host-facing links (`s1-eth1`, `s4-eth3`) are not monitored, because they are
not routing choices.

## Interface for Member 3 (Path Selector)

```python
from controller.queue_monitor import QueueMonitor

monitor = QueueMonitor()            # diamond defaults
monitor.collect_stats()             # refresh (or monitor.start(1.0) / hub.spawn(monitor.run, 1.0))

queue_stats = monitor.get_all_stats()
# {(1, 2): {"queue_bytes": 12000, "estimated_delay_ms": 9.6, "utilization": 0.65, ...}, ...}

monitor.get_link_delay(1, 2)                  # ms, or None if unknown
monitor.get_link_delay("s1", "s2", default=0) # choose your own fallback
monitor.get_link_delay(1, 2, smoothed=True)   # EWMA, less route flapping
monitor.get_link_metrics()                    # list[common.models.LinkMetrics] (seconds, "s1" names)
```

Rules the Path Selector can rely on:
- Monitor methods never raise because of missing statistics. Check `valid`,
  or pass `default=` to `get_link_delay`.
- Delay is per directed link, so path delay = sum over consecutive switch pairs.
- High utilization with zero backlog is normal (the link is busy but keeping up).

## Using it in the controller (`controller/ryu_app.py`, OS-Ken)

Member 1's controller uses OS-Ken (the maintained Ryu fork), so the green
thread comes from `os_ken.lib.hub`:

```python
from os_ken.lib import hub
from controller.queue_monitor import QueueMonitor

# in SDNController.__init__
self.queue_monitor = QueueMonitor()          # links from common/config.py
self.queue_thread = hub.spawn(self.queue_monitor.run, 1.0)

# in topology_event_handler, after self.topology_manager.discover_topology()
self.queue_monitor.sync_with_topology_manager(self.topology_manager)

# when routing
queue_stats = self.queue_monitor.get_all_stats()
```

`sync_with_topology_manager` turns the discovered port of each link
(`TopologyManager.links[(src_dpid, dst_dpid)]`) into the interface
`s<src>-eth<port>`, so the monitor follows the live topology. Until
discovery finds links, the map from `common/config.py` is used.

Other ways to build the link map: `QueueMonitor.from_net(net)` (running
Mininet), `QueueMonitor.from_topo(DiamondTopo())`, or
`monitor.update_link_map({...})`.

## Queue configuration

Member 1's `TCLink` (`--link tc`) already creates `htb` shaping on every
interface, which gives the queue the monitor reads. `QueueConfigurator`
can add `htb` + bounded `pfifo` where shaping is missing. It is dry-run by
default and skips interfaces that already have a shaping qdisc, so it does
not overwrite Member 1's configuration unless `replace_existing=True`.
Coordinate with Member 1 before applying it.

## Running

```bash
make test                                   # unit tests, no Mininet/root needed
python3 -m controller.queue_monitor --interval 1          # live table
python3 -m controller.queue_monitor --bw 5                # override capacity
python3 -m controller.queue_monitor --count 1 --json      # one JSON sample
sudo python3 -m traffic.queue_monitor_demo                # idle/normal/heavy demo with PASS/FAIL checks
```

## Known limitations

- Without `--link tc` (or under WSL2, where the README reports qdisc errors)
  veth interfaces usually have `noqueue`: tx counters work but backlog stays
  0, so delay is always 0 and utilization may exceed the nominal 10 Mbps
  (it is capped at 1.0; `throughput_bps` shows the real rate).
- Delay is an estimate of the egress queue wait only (no propagation or
  processing delay).
- The unit tests use recorded `tc` output; the live behaviour is confirmed
  by `traffic/queue_monitor_demo.py` on a Mininet machine.
