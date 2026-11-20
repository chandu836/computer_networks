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
- Ryu SDN Controller (for controller integration)
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
│   ├── diamond.py
│   └── network_graph.py
│
├── baselines/
│   ├── shortest_path.py
│   └── ecmp.py
│
├── traffic/
│   ├── failure_scenarios.py
│   └── test_mininet_failure.py
│
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
from baselines.shortest_path import find_shortest_path

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

## 5. Setup and Dependencies

Install the required packages on Ubuntu:

```bash
sudo apt update
sudo apt install mininet openvswitch-switch python3-networkx -y
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

### Test A: Routing and graph-level failure simulation

Run from the project root:

```bash
python3 - <<'PY'
from topology.network_graph import create_network_graph
from baselines.shortest_path import find_shortest_path
from baselines.ecmp import find_ecmp_paths, select_ecmp_path
from traffic.failure_scenarios import simulate_link_failure

G = create_network_graph()

print("Shortest path:")
print(find_shortest_path(G, "h1", "h2"))

paths = find_ecmp_paths(G, "h1", "h2")
print("ECMP paths:", paths)

print("Selected path:", select_ecmp_path(paths, "flow_A"))

G2, path = simulate_link_failure(
    G, ("s1", "s2"), "h1", "h2"
)
print("Path after link failure:", path)
PY
```

### Test B: Physical Mininet link failure and recovery

Run:

```bash
sudo python3 -m traffic.test_mininet_failure
```

The script checks the initial link state, disables the link and restores it.

## 8. Current Implementation Scope

The following components are implemented:

- Diamond topology creation.
- NetworkX topology representation.
- Shortest-path routing baseline.
- ECMP path discovery and deterministic selection.
- Graph-level link-failure simulation.
- Mininet physical link-control function.
- Automated Mininet link down/up test script.

The following components require further implementation or integration:

- Queue monitoring and queue-delay estimation.
- Queue-aware multipath routing.
- Proactive failure detection.
- Automatic forwarding-rule installation.
- End-to-end traffic recovery.
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

The current routing and failure functions are software-level components. They must be integrated with the SDN controller before they can control actual packet forwarding.

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
