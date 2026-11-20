# Queue-Aware Multipath Routing with Proactive Failure Detection and Recovery in SDN

## 1. Project Overview

This project implements a multipath routing framework for Software-Defined Networks (SDNs).

The objective is to distribute network traffic across multiple available paths while considering network congestion and proactively detecting link failures.

The network is initially represented using a diamond topology, which provides two alternative paths between the source and destination hosts.

### Technology Stack

- Python 3
- Mininet
- Open vSwitch (OVS)
- NetworkX
- OS-Ken SDN Controller
- Linux Traffic Control (`tc`)

## 2. Network Topology

The current topology consists of:

- 2 hosts: h1 and h2
- 4 switches: s1, s2, s3 and s4
- 6 links
- 2 alternative paths between the hosts

The two paths are:

**Upper path:**

`h1 → s1 → s2 → s4 → h2`

**Lower path:**

`h1 → s1 → s3 → s4 → h2`

Each link is configured with a nominal bandwidth of 10 Mbps in the topology definition.

## 3. Project Structure

```text
computer_networks/
│
├── topology/
│   ├── __init__.py
│   ├── diamond.py
│   └── network_graph.py
│
├── baselines/
│   ├── __init__.py
│   ├── shortest_path.py
│   └── ecmp.py
│
├── monitoring/
│   ├── __init__.py
│   └── queue_monitor.py
│
├── routing/
│   ├── __init__.py
│   └── queue_aware_routing.py
│
├── failure_detection/
│   ├── __init__.py
│   └── detector.py
│
├── recovery/
│   ├── __init__.py
│   ├── flow_manager.py
│   └── recovery_manager.py
│
├── controller/
│   ├── __init__.py
│   ├── queue_monitor.py
│   ├── route_selector.py
│   ├── topology_manager.py
│   └── ryu_app.py
│
├── traffic/
│   ├── __init__.py
│   ├── failure_scenarios.py
│   ├── queue_monitor_demo.py
│   ├── recovery_demo.py
│   └── test_mininet_failure.py
│
├── experiments/
│   ├── __init__.py
│   ├── traffic_generator.py
│   ├── metrics.py
│   └── run_experiments.py
│
├── common/
│   ├── __init__.py
│   ├── config.py
│   └── models.py
│
├── tests/
│   ├── __init__.py
│   ├── test_topology.py
│   ├── test_routing.py
│   ├── test_failure.py
│   ├── test_queue_monitor.py
│   ├── test_queue_aware_routing.py
│   ├── test_failure_detector.py
│   ├── test_flow_manager.py
│   ├── test_recovery_manager.py
│   ├── test_route_selector.py
│   └── test_controller_forwarding.py
│
├── docs/
│   ├── INTEGRATION.md
│   ├── QUEUE_MONITOR.md
│   ├── ROUTING.md
│   └── RECOVERY.md
│
├── .gitignore
├── requirements.txt
├── Makefile
└── README.md
```

## 4. Module Description

### 4.1 topology/diamond.py

Defines the Mininet diamond topology.

It creates two hosts, four switches and six links. Each link is assigned a bandwidth parameter of 10 Mbps.

### 4.2 topology/network_graph.py

Creates a NetworkX graph representing the same network topology.

```python
from topology.network_graph import create_network_graph

G = create_network_graph()
```

This graph is used by the routing algorithms and failure-simulation modules.

### 4.3 baselines/shortest_path.py

Implements shortest-path routing using NetworkX.

```python
from topology.network_graph import create_network_graph
from baselines.shortest_path import find_shortest_path

G = create_network_graph()
path = find_shortest_path(G, "h1", "h2")
```

Returns the shortest available path or `None` if no route exists.

### 4.4 baselines/ecmp.py

Implements Equal-Cost Multi-Path (ECMP) routing.

It identifies all shortest paths between the source and destination and selects one deterministically using a flow identifier.

```python
from baselines.ecmp import find_ecmp_paths, select_ecmp_path

paths = find_ecmp_paths(G, "h1", "h2")

selected_path = select_ecmp_path(paths, "flow_A")
```

Using the same flow identifier produces the same selected path.

This is a software-level ECMP baseline; it does not independently install OpenFlow forwarding rules.

### 4.5 traffic/failure_scenarios.py

Contains two functions.

**Graph-level failure simulation:**

```python
from traffic.failure_scenarios import simulate_link_failure

modified_graph, new_path = simulate_link_failure(
    G, ("s1", "s2"), "h1", "h2"
)
```

This removes a failed link from a copy of the graph and calculates an alternate route if one exists.

**Mininet physical link control:**

```python
from traffic.failure_scenarios import set_mininet_link_state

set_mininet_link_state(net, "s1", "s2", "down")
set_mininet_link_state(net, "s1", "s2", "up")
```

This changes the status of a link in a running Mininet network.

### 4.6 traffic/test_mininet_failure.py

An automated test script for physical Mininet link control.

It checks whether a link can be disabled and restored successfully.

### 4.7 common/models.py

Defines shared data structures used for communication between project modules.

The current data models include:

- `LinkMetrics`: Represents link queue and performance measurements.
- `FailureEvent`: Represents a detected network failure.
- `RouteDecision`: Represents a routing decision.

### 4.8 common/config.py

Contains shared network configuration, including host names, switch names, nominal link bandwidth and the two alternative paths.

### 4.9 controller/queue_monitor.py (Member 2)

Collects per-link queue backlog, transmitted bytes and drops from Linux `tc`,
and estimates queueing delay (`8 * backlog / rate`) and link utilization.

```python
from controller.queue_monitor import QueueMonitor

monitor = QueueMonitor()
monitor.collect_stats()
monitor.get_link_delay(1, 2)   # ms on the s1 -> s2 link
monitor.get_all_stats()        # {(1, 2): {"queue_bytes": ..., "estimated_delay_ms": ..., "utilization": ...}}
```

Live view: `python3 -m controller.queue_monitor --interval 1`.
Traffic demo: `sudo python3 -m traffic.queue_monitor_demo`.
Details: `docs/QUEUE_MONITOR.md`. (`monitoring.queue_monitor` re-exports the same module.)

### 4.10 recovery/ and controller/ryu_app.py (Member 5)

`recovery/flow_manager.py` installs, updates and removes the OpenFlow rules of each flow.
`recovery/recovery_manager.py` listens to the failure detector, reroutes the flows that cross a failed link, and moves them back after the link has been stable again for a few seconds, if that lowers their path cost.

`controller/ryu_app.py` is the central controller. It learns host locations, selects a path per flow (5-tuple) with the configured routing mode and connects topology discovery, queue monitoring, routing, failure detection and recovery.

```bash
# terminal 1: controller (routing mode: shortest_path, ecmp, queue_aware or proposed)
SDN_ROUTING_MODE=proposed PYTHONPATH=. osken-manager --observe-links controller.ryu_app

# terminal 2: topology
sudo PYTHONPATH=. mn --custom topology/diamond.py --topo diamond \
    --switch ovs,datapath=user --controller remote,ip=127.0.0.1,port=6653 --link tc
```

End-to-end demo with link failure, restoration and switch failure:

```bash
sudo python3 -m traffic.recovery_demo --user-datapath
```

Details, settings and measurements: `docs/RECOVERY.md`.

### 4.11 Other Project Modules

- `routing/`: Queue-aware multipath routing (Member 3), see `docs/ROUTING.md`.
- `failure_detection/`: Link and switch failure detection (Member 4).
- `experiments/`: Traffic generation and performance evaluation (pending).
- `tests/`: Automated tests for the implemented components.
- `docs/`: Project integration documentation.

## 5. Setup and Dependencies

Install the required packages on Ubuntu:

```bash
sudo apt update
sudo apt install mininet openvswitch-switch python3-networkx -y
```

Install the Python dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Verify Python and Mininet:

```bash
python3 --version
mn --version
```

Ensure that Open vSwitch is running before starting Mininet.

## 6. Running the Topology

From the project root:

```bash
sudo mn --custom topology/diamond.py \
--topo diamond \
--switch ovs,datapath=user \
--controller none
```

At the Mininet prompt, inspect the topology:

```text
net
links
```

Exit using:

```text
exit
```

To request traffic shaping through Mininet's TCLink:

```bash
sudo mn --custom topology/diamond.py \
--topo diamond \
--switch ovs,datapath=user \
--controller none \
--link tc
```

Note: The `bw=10` parameter is only enforced when using a traffic-controlled link implementation. Kernel support for the required queue disciplines is necessary.

## 7. Running the Tests

Run all Python syntax checks and automated tests from the project root:

```bash
make all
```

To run only the automated test suite:

```bash
make test
```

To run only Python syntax checks:

```bash
make syntax
```

The automated test suite covers:

- Network topology nodes and links.
- Shortest-path routing.
- ECMP path discovery and deterministic selection.
- Link-failure simulation and alternate-path selection.
- Handling of disconnected paths and invalid links.
- Preservation of the original graph during failure simulation.
- Queue monitoring, queue-aware routing and failure detection.
- Flow rule installation, rerouting after failures and path restoration.
- The controller's packet handling in every routing mode (with simulated switches).

### Physical Mininet Link Failure and Recovery

Run separately:

```bash
sudo python3 -m traffic.test_mininet_failure
```

This test checks whether a Mininet link can be disabled and restored successfully.

Note: The automated pytest suite does not start Mininet. The Mininet test above checks physical link-state control only. End-to-end forwarding and recovery through OVS are checked by:

```bash
sudo python3 -m traffic.recovery_demo --user-datapath
```

## 8. Current Implementation Scope

The following components are implemented:

- Diamond topology creation.
- NetworkX topology representation.
- Shortest-path routing baseline.
- ECMP path discovery and deterministic selection.
- Graph-level link-failure simulation.
- Mininet physical link-control function.
- Automated Mininet link down/up test script.
- Shared configuration and data models.
- Queue monitoring, queueing-delay and utilization estimation (Member 2).
- Queue-aware multipath routing (Member 3).
- Link and switch failure detection (Member 4).
- Forwarding-rule installation, failure recovery and path restoration (Member 5).
- Controller integration of all modules, with four routing modes.
- Automated Python test suite.

The following components require further implementation or integration:

- Performance evaluation under different traffic loads.

## 9. Notes for Team Integration

The other modules should reuse the existing topology and graph representation rather than creating separate versions of the network.

Recommended imports:

```python
from topology.network_graph import create_network_graph
from baselines.shortest_path import find_shortest_path
from baselines.ecmp import find_ecmp_paths, select_ecmp_path
from traffic.failure_scenarios import simulate_link_failure
```

The central SDN controller should eventually coordinate:

1. Queue monitoring.
2. Path selection.
3. Failure detection.
4. Recovery and forwarding-rule updates.
5. Traffic generation and evaluation.

The controller in `controller/ryu_app.py` coordinates the first four. Traffic generation and evaluation run outside the controller and can read its event logs (`SDN_LOG_DIR`, see `docs/RECOVERY.md`).

For module responsibilities and shared interfaces, refer to `docs/INTEGRATION.md`.

## 10. Known Environment Limitation

The initial development environment uses Ubuntu under WSL2.

The topology was successfully created, and all six links were reported as `OK`.

However, WSL2 reported:

```text
Specified qdisc kind is unknown.
RTNETLINK answers: No such file or directory
```

when attempting to configure TCLink bandwidth limits.

Therefore, actual 10 Mbps traffic shaping and end-to-end OVS packet forwarding have not yet been verified in this environment.

A native Ubuntu installation or a suitable Linux VM may be required for complete traffic-shaping and network-performance experiments.

With the OVS userspace datapath (`datapath=user`), TCP and UDP traffic between the hosts is dropped by the receiver unless checksum offloading is turned off on the hosts (ping is not affected):

```text
h1 ethtool -K h1-eth0 tx off
h2 ethtool -K h2-eth0 tx off
```
