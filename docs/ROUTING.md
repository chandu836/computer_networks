# Queue-Aware Multipath Routing (Member 3)

Files:
- `routing/queue_aware_routing.py` — full queue-aware multipath router (`QueueAwareRouter`) and `PathSelector` wrapper.
- `baselines/queue_aware.py` — plain stateless queue-aware baseline (`select_path`, `select_decision`) using lowest total queueing delay only (no flowlets, no hysteresis).

## Scope: QDAPS-Inspired (Not an Exact Reproduction)

This module is **QDAPS-inspired**, not an exact reproduction of the published QDAPS packet-spraying algorithm (Huang et al., IEEE ICNP 2018; IEEE/ACM ToN 2021).

- Published QDAPS performs per-packet spraying with fine-grained packet-level reordering mitigation.
- This implementation uses **flowlet-level multipath distribution** with hysteresis and EWMA-smoothed queueing delay from Member 2's `QueueMonitor`, which fits OpenFlow/Ryu flow-table forwarding cleanly without per-packet controller overhead.

## Path Cost Formula

For each candidate path `P`, cost is calculated in **ms-equivalent units**:

```text
C(P) = sum(D_q(u, v)) + utilization_weight * sum(utilization(u, v)) + hop_weight * hops
```

- `D_q(u, v)` is the directed egress queueing delay (ms) for inter-switch link `u -> v` from `QueueMonitor` (`smoothed_delay_ms` by default, or `estimated_delay_ms` if `use_smoothed=False`).
- `utilization(u, v)` is a `0.0–1.0` link utilization tie-breaker when queues have zero backlog.
- `hops` is a small tie-breaker favoring shorter paths when delays are equal.
- Host-facing links (`h1 -> s1`, `s4 -> h2`) are not monitored by `QueueMonitor` and contribute `0.0 ms`.
- Missing or invalid (`valid: False`) inter-switch link statistics receive `missing_delay_ms` (default `10.0 ms`) so unreadable links do not appear uncongested.

The plain baseline (`baselines/queue_aware.py`) sets `utilization_weight=0.0` and `hop_weight=0.0`, using pure queueing delay `sum(D_q(u, v))` with hop-count tie-breaking.

## Parameters (`QueueAwareRouter`)

| Parameter | Default | Meaning |
| --- | --- | --- |
| `flowlet_timeout_s` | `0.1` s | Inter-packet gap required before an existing flow can be re-evaluated (prevents packet reordering). |
| `hysteresis_ms` | `2.0` ms | Minimum cost improvement required to switch an existing flow after a flowlet gap (prevents route flapping). |
| `load_balance_tolerance_ms` | `1.0` ms | Paths within this margin of the best path are eligible for inverse-cost weighted multipath hashing. |
| `missing_delay_ms` | `10.0` ms | Penalty applied to inter-switch links with missing or `valid: False` telemetry. |
| `use_smoothed` | `True` | Use EWMA `smoothed_delay_ms` from `QueueMonitor` instead of instantaneous `estimated_delay_ms`. |
| `utilization_weight` | `0.5` | Weight on per-link utilization tie-breaker. |
| `hop_weight` | `0.05` | Weight per hop for tie-breaking when queues are empty. |
| `max_paths` | `8` | Maximum number of candidate simple paths evaluated per decision. |

## Usage

```python
from routing.queue_aware_routing import QueueAwareRouter, PathSelector
from baselines.queue_aware import select_path, select_decision

# Full router (flowlet pinning, hysteresis, and multipath load balancing)
router = QueueAwareRouter(queue_source=queue_monitor)
decision = router.select_path(graph, "h1", "h2", flow_id="flow-1")
# -> RouteDecision(path=["h1", "s1", "s3", "s4", "h2"], cost=2.25, reason="initial_selection")

# Proposal PathSelector wrapper (stateless, takes precomputed candidates + stats)
selector = PathSelector()
decision = selector.select_path(candidate_paths, queue_monitor.get_all_stats())

# Plain baseline (no flowlets, no hysteresis)
path = select_path(graph, "h1", "h2", queue_monitor)
decision = select_decision(graph, "h1", "h2", queue_monitor)
```

`QueueAwareRouter` accepts a live `QueueMonitor` (taking one `get_all_stats()` snapshot per routing decision), a raw `get_all_stats()` dict, or a `list[LinkMetrics]`, and supports both string node names (`"s1"`) and integer DPIDs (`1`).

## Controller Integration Notes (Members 1, 4 & 5)

1. **Path-to-port conversion:** `RouteDecision.path` returns a node list (e.g., `["s1", "s2", "s4"]`). Member 5's flow manager can convert hops to egress ports (`[{"dpid": 1, "out_port": 2}, ...]`) via `TopologyManager.get_port()`.
2. **Flowlet expiry in OpenFlow:** Because the controller only sees packets when no switch flow rule matches, install flow entries with an OpenFlow `idle_timeout` (e.g., `1` s) and initialize `QueueAwareRouter(flowlet_timeout_s=1.0)` to match. When a flow goes idle, the switch expires the rule and the next packet triggers a fresh `PacketIn`.
3. **Failure reporting:** `select_path()` supports both setting `is_up=False` on the NetworkX graph and passing explicit `failed_links` / `failed_switches` lists.

## Running Tests

```bash
python3 -m pytest -v tests/test_queue_aware_routing.py
python3 -m pytest -v tests/ --ignore=tests/test_topology.py
```