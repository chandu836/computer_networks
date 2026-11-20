# Project Integration Contract

## Project
Queue-Aware Multipath Routing with Proactive Failure
Detection and Recovery in Software-Defined Networks.

## Shared Infrastructure

- Mininet topology: topology/diamond.py
- Network graph: topology/network_graph.py
- Shared data structures: common/models.py
- Shared configuration: common/config.py

Do not create duplicate network topologies.

## Member Responsibilities

### Member 1: Topology and Baselines
- Create and maintain the Mininet topology.
- Maintain the NetworkX graph.
- Implement shortest-path and ECMP baselines.
- Provide link failure testing support.

### Member 2: Queue Monitoring
- Collect queue occupancy and backlog.
- Collect relevant link statistics.
- Estimate queueing delay.
- Provide LinkMetrics objects.
- Implemented in controller/queue_monitor.py; interface, units and link
  identifiers are documented in docs/QUEUE_MONITOR.md.

### Member 3: Queue-Aware Routing
- Use queue metrics to calculate path costs.
- Select appropriate paths.
- Implement multipath traffic distribution.

### Member 4: Failure Detection
- Detect link and switch failures.
- Generate FailureEvent objects.
- Notify the controller of failures.

### Member 5: Recovery
- Exclude failed links from routing decisions.
- Install updated forwarding rules.
- Restore recovered paths safely.

### Member 6: Experiments and Evaluation
- Generate network traffic.
- Measure throughput, delay, packet loss and recovery time.
- Compare baseline and proposed routing approaches.

## Controller Integration

The project will use one central Ryu controller.
Individual modules should expose reusable functions or classes
that can be imported by the controller.

## Shared Data Models

See common/models.py for LinkMetrics, FailureEvent
and RouteDecision.

Function signatures and module interfaces must be agreed
upon before final controller integration.

## Environment Limitations

Bandwidth enforcement and end-to-end OVS packet forwarding
have not yet been verified in the current WSL2 environment.
