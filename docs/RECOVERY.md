# Recovery and Flow Management (Member 5)

Files:
- `recovery/flow_manager.py`: `FlowManager`, turns a path into OpenFlow rules and tracks them per flow.
- `recovery/recovery_manager.py`: `RecoveryManager`, reroutes flows after a failure and restores them after recovery.
- `controller/route_selector.py`: one `select_path()` interface for the four routing modes.
- `controller/ryu_app.py`: packet-in handling, host learning and the wiring of all modules.
- `traffic/recovery_demo.py`: end-to-end demonstration on Mininet with PASS/FAIL checks.

## How a packet is forwarded

1. A host sends a packet. The first switch has no rule and sends it to the controller (table miss).
2. The controller learns the sender's location `(dpid, port)` if the packet arrived on a host-facing port.
3. ARP and packets for unknown hosts are sent to every host-facing port except the one they came from. They never travel over switch links, so the loop in the diamond cannot cause a broadcast storm.
4. For an IPv4 packet to a known host the controller asks the router for a path, converts it to hops with `TopologyManager.get_port()` plus the learned host port, and `FlowManager.install_path()` writes one rule per switch.
5. The packet that reached the controller is sent out at the destination switch directly.

Flows are identified by the 5-tuple, as agreed with Member 3:

| Traffic | `flow_id` | OpenFlow match | Priority |
| --- | --- | --- | --- |
| TCP / UDP | `(src_ip, dst_ip, proto, src_port, dst_port)` | `eth_type, ipv4_src, ipv4_dst, ip_proto, tcp_src/udp_src, tcp_dst/udp_dst` | 10 |
| ICMP, other IP, fragments | `(src_ip, dst_ip, proto, None, None)` | `eth_type, ipv4_src, ipv4_dst, ip_proto` | 9 |

Each direction of a connection is its own flow and may take a different path.

Rules use `idle_timeout = 1 s`, equal to `QueueAwareRouter(flowlet_timeout_s=1.0)`. A flow reaches the controller again only after a 1 s gap, which is when the router is allowed to pick a new path.

## Path representation

```python
hops = [
    {"dpid": 1, "out_port": 2},
    {"dpid": 2, "out_port": 2},
    {"dpid": 4, "out_port": 3},   # last hop: port towards the host
]
```

`recovery.flow_manager.build_hops(path, topology_manager.get_port, host_port)` builds this from a router path. It accepts `[1, 2, 4]`, `["s1", "s2", "s4"]` and `["h1", "s1", "s2", "s4", "h2"]`.

## FlowManager

```python
from recovery.flow_manager import FlowManager

fm = FlowManager(idle_timeout=1)
fm.register_datapath(datapath)             # when a switch connects
fm.install_path(flow_id, match, hops)      # new flow
fm.update_path(flow_id, new_hops)          # replace the path
fm.remove_path(flow_id)                    # delete the rules
fm.flows_using_link(1, 2)                  # flows crossing s1-s2 (either direction)
fm.get_path(flow_id)                       # current hops or None
fm.route_changes                           # number of path changes so far
```

- `update_path()` is make-before-break: new rules are written from the last switch back to the first, then the rules on switches that left the path are deleted.
- Only the rule on the first switch asks for a flow-removed message. When it times out the flow is dropped from the table.
- The project document lists `install_path(src, dst, path)`. The signature here takes `flow_id` and `match` instead, because rules are per 5-tuple and not per host pair.

## RecoveryManager

```python
from recovery.recovery_manager import RecoveryManager

recovery = RecoveryManager(
    topology_manager=topology_manager,
    router=router,
    flow_manager=flow_manager,
    failure_detector=failure_detector,
    path_cost=lambda path: calculate_path_cost(path, queue_monitor),
    stability_interval_s=3.0,
    restore_hysteresis=0.5,
)
failure_detector.register_listener(recovery.on_failure_event)

recovery.process_pending()        # call periodically (the controller does, every 0.5 s)
```

| Method | Purpose |
| --- | --- |
| `handle_failure(event)` | Reroute every flow that crosses the failed link. |
| `handle_restoration(event)` | Start the stability interval for a recovered link. |
| `process_pending()` | Move displaced flows back when that is allowed and worth it. |
| `is_path_healthy(path)` | True if no link or switch of the path is failed. |
| `recovery_time_since(t, a, b)` | Seconds from failure injection to the rules being sent. |
| `get_stats()`, `save_log(path)` | Counters and the CSV event log. |

### Failure

1. The detector reports `FailureEvent(is_failed=True)`.
2. `flows_using_link()` gives the affected flows. Flows on healthy paths are not touched.
3. For each one the router selects a path with `failed_links` and `failed_switches` excluded, and `update_path()` installs it.
4. A flow without an alternative has its rules removed. New packets of such a flow get a 1 s drop rule at the first switch so they do not flood the controller.

A switch failure arrives as one link failure per link of that switch, and the switch itself is passed to the router as failed.

### Restoration

1. `FailureEvent(is_failed=False)` only starts a timer. If the link fails again inside the stability interval, the timer is cancelled.
2. After the interval, each flow that was displaced by the failure is compared: `path_cost(current) - path_cost(original)`.
3. The flow returns to its original path only if that difference is larger than `restore_hysteresis`. Otherwise it stays, and is checked again on the next call (for example when the detour becomes congested later).
4. A flow is moved back at most once. Flows that were not displaced are never moved by the recovery manager.

With the queue-aware cost (ms of queueing delay plus `0.5 * utilization` per link) the default margin of `0.5` means: an idle ping stays where it is, a flow that loads its detour goes back to the recovered path.

## Routing modes and settings

The controller reads its settings from environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `SDN_ROUTING_MODE` | `proposed` | `shortest_path`, `ecmp`, `queue_aware` or `proposed` |
| `SDN_RECOVERY` | `auto` | `on`, `off`, or `auto` (on only for `proposed`) |
| `SDN_STABILITY_INTERVAL` | `3.0` | seconds a recovered link must stay up |
| `SDN_RESTORE_HYSTERESIS` | `0.5` | minimum cost improvement for restoration |
| `SDN_LOG_DIR` | unset | if set, `failure_events.csv` and `recovery_events.csv` are written there |

These are the four configurations of the evaluation:

| Mode | Path selection | On failure |
| --- | --- | --- |
| `shortest_path` | `baselines/shortest_path.py` | rules keep pointing at the dead link until they time out |
| `ecmp` | `baselines/ecmp.py`, hashed on the 5-tuple | same |
| `queue_aware` | `baselines/queue_aware.py` | same |
| `proposed` | `QueueAwareRouter` (flowlets, hysteresis) | recovery manager reroutes at once and restores later |

In the baselines a flow that keeps sending never lets its rule time out, so it stays down until the link returns. `SDN_RECOVERY=on` adds the recovery manager to a baseline.

## Running

Controller (from the project root):

```bash
PYTHONPATH=. osken-manager --observe-links controller.ryu_app
SDN_ROUTING_MODE=ecmp SDN_LOG_DIR=results PYTHONPATH=. osken-manager --observe-links controller.ryu_app
```

`--observe-links` is required: topology discovery uses LLDP.

Topology, in a second terminal:

```bash
sudo PYTHONPATH=. mn --custom topology/diamond.py --topo diamond \
    --switch ovs,datapath=user --controller remote,ip=127.0.0.1,port=6653 --link tc
```

Demo (starts its own controller and topology):

```bash
sudo python3 -m traffic.recovery_demo                      # kernel datapath
sudo python3 -m traffic.recovery_demo --user-datapath      # WSL2 / Docker
sudo python3 -m traffic.recovery_demo --user-datapath --routing-mode ecmp
```

Unit tests (no Mininet or root needed):

```bash
python3 -m pytest -v tests/test_flow_manager.py tests/test_recovery_manager.py \
    tests/test_route_selector.py tests/test_controller_forwarding.py
```

## Measurements

`recovery_events.csv` has one row per handled event:

| Column | Meaning |
| --- | --- |
| `event` | `failure` or `restoration` |
| `link` | for example `s1-s2` |
| `detection_time` | detector timestamp (`time.time()`) |
| `completed_time` | when the last flow-mod of the reroute was sent |
| `recovery_time_s` | `completed_time - detection_time` |
| `flows_rerouted`, `flows_unrecoverable`, `flows_restored` | flow counts |

This recovery time is measured inside the controller. The end-to-end recovery time (traffic arriving again) has to be measured at the hosts; the demo does it with a 20 ms ping.

Demo results on the diamond, OVS userspace datapath inside a Docker container (one run each):

| | `proposed` | `shortest_path` / `ecmp` / `queue_aware` |
| --- | --- | --- |
| Detection time | 1.5 to 1.8 ms | 1.2 to 2.0 ms |
| Controller recovery time (injection to rules sent) | 2.1 to 2.6 ms | no reroute |
| Ping outage, link failure | 27 to 38 ms | about 4.6 s (until the link returned) |
| Ping outage, switch failure | 79 to 85 ms | about 4.6 s |
| UDP loss, 8 Mbit/s for 23 s with a 4 s link failure | 0.05 % | 20 % |

The ping outage is bounded below by the 20 ms ping interval.

## Known limitations

- With the OVS userspace datapath (`datapath=user`) TCP and UDP only work after `ethtool -K <host>-eth0 tx off` on the hosts. The userspace datapath does not fill in the checksums the hosts leave to offloading, so the receiver drops the packets while ping still works. The demo does this itself with `--user-datapath`.
- The live tests were run with the userspace datapath only. The kernel datapath was not available in the test container.
- Hosts are assumed not to move between switch ports.
- Recovery reacts to link and switch failures reported by the failure detector. A failed host-facing link is not rerouted (there is no other path to that host).
- All four diamond links have the same rate as the host links, so switch queues stay short and the queue-aware cost is driven mostly by utilization. A lower rate on one path (as in `traffic/queue_monitor_demo.py`) is needed to see queueing delay differences.
