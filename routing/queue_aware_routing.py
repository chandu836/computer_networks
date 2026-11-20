import hashlib
import time
import networkx as nx
from common.models import LinkMetrics, RouteDecision


def _to_dpid(node):
    """
    Convert a switch identifier like 's1' or '1' into integer DPID 1.
    Returns None for host nodes (e.g. 'h1', 'h2').
    """

    if isinstance(node, int):
        return node

    text = str(node).strip().lower()

    if text.startswith("s") and text[1:].isdigit():
        return int(text[1:])

    if text.isdigit():
        return int(text)

    return None


def _snapshot_queue_source(queue_source):
    """
    Take a single consistent snapshot from a QueueMonitor object so
    all hops in one routing decision use the same telemetry sample.
    """

    if queue_source is not None and hasattr(queue_source, "get_all_stats"):
        return queue_source.get_all_stats() or {}

    return queue_source


def _active_subgraph(graph, failed_links=None, failed_switches=None):
    """
    Return a filtered view of the graph containing only active
    switches and operational links.
    """

    failed_nodes = set(failed_switches or [])

    # Store both directions because the topology graph is undirected.
    failed_edges = set()
    for node1, node2 in (failed_links or []):
        failed_edges.add((node1, node2))
        failed_edges.add((node2, node1))

    def filter_node(node):
        return (
            node not in failed_nodes
            and graph.nodes[node].get("is_up", True)
        )

    def filter_edge(node1, node2):
        if (node1, node2) in failed_edges:
            return False

        return graph.edges[node1, node2].get("is_up", True)

    return nx.subgraph_view(
        graph,
        filter_node=filter_node,
        filter_edge=filter_edge,
    )


def find_candidate_paths(
    graph,
    source,
    destination,
    max_paths=8,
    max_hops=None,
    failed_links=None,
    failed_switches=None,
):
    """
    Find loop-free candidate paths ordered by hop count (shortest first).

    Returns:
        list: Candidate paths as lists of node names.
        []: If either node is missing or no operational path exists.
    """

    active_graph = _active_subgraph(
        graph,
        failed_links=failed_links,
        failed_switches=failed_switches,
    )

    if source not in active_graph or destination not in active_graph:
        return []

    try:
        # Yields simple paths in non-decreasing hop order.
        paths_iter = nx.shortest_simple_paths(
            active_graph,
            source=source,
            target=destination,
        )

        candidates = []

        for path in paths_iter:
            hops = len(path) - 1

            if max_hops is not None and hops > max_hops:
                break

            candidates.append(list(path))

            if max_paths is not None and len(candidates) >= max_paths:
                break

        return candidates

    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return []


def _get_link_stats(
    src,
    dst,
    queue_source,
    use_smoothed=True,
    missing_delay_ms=10.0,
):
    """
    Read (delay_ms, utilization) for the directed link src -> dst.

    Host-facing links are not monitored by QueueMonitor and always
    return (0.0, 0.0). Unreadable/missing inter-switch links receive
    missing_delay_ms so blind links do not look free.
    """

    if queue_source is None:
        return 0.0, 0.0

    src_id, dst_id = _to_dpid(src), _to_dpid(dst)
    is_switch_link = (src_id is not None and dst_id is not None)

    # Host-facing links (e.g. h1 -> s1, s4 -> h2) are not monitored.
    if not is_switch_link:
        return 0.0, 0.0

    fallback_delay = float(missing_delay_ms) if missing_delay_ms is not None else 0.0

    # 1. Raw dictionary (from QueueMonitor.get_all_stats() snapshot or tests).
    if isinstance(queue_source, dict):
        entry = queue_source.get((src, dst)) or queue_source.get((src_id, dst_id))

        if not entry:
            return fallback_delay, 0.0

        if isinstance(entry, (int, float)):
            return float(entry), 0.0

        if isinstance(entry, dict):
            # Penalize unreadable/invalid tc stats instead of treating as 0 ms.
            if entry.get("valid") is False:
                return fallback_delay, 0.0

            delay_key = (
                "smoothed_delay_ms" if use_smoothed else "estimated_delay_ms"
            )
            delay_val = entry.get(delay_key)

            if delay_val is None:
                delay_val = entry.get("estimated_delay_ms")

            if delay_val is None:
                return fallback_delay, 0.0

            util_val = entry.get("utilization")

            return float(delay_val), float(util_val or 0.0)

    # 2. List of common.models.LinkMetrics (delay is in seconds here).
    if isinstance(queue_source, list):
        for metric in queue_source:
            if (
                isinstance(metric, LinkMetrics)
                and metric.source == src
                and metric.destination == dst
            ):
                if metric.estimated_delay_s is None:
                    return fallback_delay, 0.0

                delay_ms = metric.estimated_delay_s * 1000.0
                utilization = (
                    float(metric.utilization)
                    if metric.utilization is not None
                    else 0.0
                )
                return delay_ms, utilization

        return fallback_delay, 0.0

    # 3. Fallback for custom monitor objects exposing only get_link_delay().
    if hasattr(queue_source, "get_link_delay"):
        delay = queue_source.get_link_delay(
            src,
            dst,
            smoothed=use_smoothed,
            default=None,
        )
        if delay is None:
            return fallback_delay, 0.0

        return float(delay), 0.0

    return fallback_delay, 0.0


def calculate_path_cost(
    path,
    queue_source,
    use_smoothed=True,
    utilization_weight=0.5,
    hop_weight=0.05,
    missing_delay_ms=10.0,
):
    """
    Calculate the queue-aware cost of a path (in ms-equivalent units):
        C(P) = sum(D_q(u, v)) + utilization_weight * sum(util) + hop_weight * hops
    """

    if len(path) < 2:
        return 0.0

    snapshot = _snapshot_queue_source(queue_source)

    total_delay_ms = 0.0
    total_utilization = 0.0

    # Queue delay is directional (u -> v), so walk consecutive hops in order.
    for node1, node2 in zip(path[:-1], path[1:]):
        delay_ms, utilization = _get_link_stats(
            node1,
            node2,
            snapshot,
            use_smoothed=use_smoothed,
            missing_delay_ms=missing_delay_ms,
        )
        total_delay_ms += delay_ms
        total_utilization += utilization

    hops = len(path) - 1

    cost = (
        total_delay_ms
        + (utilization_weight * total_utilization)
        + (hop_weight * hops)
    )

    return round(cost, 6)


def rank_candidate_paths(
    paths,
    queue_source,
    use_smoothed=True,
    utilization_weight=0.5,
    hop_weight=0.05,
    missing_delay_ms=10.0,
):
    """
    Score and sort candidate paths from lowest cost to highest cost.
    """

    snapshot = _snapshot_queue_source(queue_source)

    scored = [
        (
            path,
            calculate_path_cost(
                path,
                snapshot,
                use_smoothed=use_smoothed,
                utilization_weight=utilization_weight,
                hop_weight=hop_weight,
                missing_delay_ms=missing_delay_ms,
            ),
        )
        for path in paths
    ]

    # Deterministic tie-breaking: cost first, then fewer hops, then node order.
    scored.sort(key=lambda item: (item[1], len(item[0]), item[0]))

    return scored


class _FlowletEntry:
    """Tracks the active path and last packet timestamp for one flow."""

    def __init__(self, path, last_seen, last_cost):
        self.path = path
        self.last_seen = last_seen
        self.last_cost = last_cost


class QueueAwareRouter:
    """
    QDAPS-inspired queue-aware multipath router.

    Uses flowlet pinning to avoid packet reordering and hysteresis
    to prevent unnecessary route flapping.
    """

    def __init__(
        self,
        queue_source=None,
        flowlet_timeout_s=0.1,
        hysteresis_ms=2.0,
        load_balance_tolerance_ms=1.0,
        use_smoothed=True,
        utilization_weight=0.5,
        hop_weight=0.05,
        missing_delay_ms=10.0,
        max_paths=8,
    ):
        self.queue_source = queue_source
        self.flowlet_timeout_s = flowlet_timeout_s
        self.hysteresis_ms = hysteresis_ms
        self.load_balance_tolerance_ms = load_balance_tolerance_ms
        self.use_smoothed = use_smoothed
        self.utilization_weight = utilization_weight
        self.hop_weight = hop_weight
        self.missing_delay_ms = missing_delay_ms
        self.max_paths = max_paths

        # Maps flow_id -> _FlowletEntry.
        self._flow_table = {}
        self.route_changes = 0

    def _select_multipath(self, ranked_paths, flow_id):
        """
        Distribute flows across paths whose cost is within
        load_balance_tolerance_ms of the best path.
        """

        best_path, best_cost = ranked_paths[0]

        eligible = [
            (path, cost)
            for path, cost in ranked_paths
            if (cost - best_cost) <= self.load_balance_tolerance_ms
        ]

        if len(eligible) == 1 or flow_id is None:
            return best_path, best_cost

        # Inverse-cost weights: lower-cost paths receive a larger share.
        weights = [1.0 / (cost + 1.0) for _, cost in eligible]
        total_weight = sum(weights)

        # Deterministic SHA-256 hash (matches Member 1's ECMP hashing style).
        flow_hash = hashlib.sha256(
            str(flow_id).encode("utf-8")
        ).hexdigest()

        pick = (int(flow_hash, 16) % 10000) / 10000.0 * total_weight

        cumulative = 0.0
        for (path, cost), weight in zip(eligible, weights):
            cumulative += weight
            if pick <= cumulative:
                return path, cost

        return eligible[-1]

    def select_path(
        self,
        graph,
        source,
        destination,
        flow_id=None,
        queue_source=None,
        failed_links=None,
        failed_switches=None,
        now=None,
    ):
        """
        Select the best queue-aware path and return a RouteDecision.
        Returns None if no operational path exists.
        """

        raw_source = queue_source if queue_source is not None else self.queue_source
        # Single snapshot per routing decision for speed and consistency.
        q_snapshot = _snapshot_queue_source(raw_source)
        timestamp = time.monotonic() if now is None else now

        candidates = find_candidate_paths(
            graph,
            source=source,
            destination=destination,
            max_paths=self.max_paths,
            failed_links=failed_links,
            failed_switches=failed_switches,
        )

        if not candidates:
            if flow_id is not None:
                self._flow_table.pop(flow_id, None)
            return None

        ranked = rank_candidate_paths(
            candidates,
            q_snapshot,
            use_smoothed=self.use_smoothed,
            utilization_weight=self.utilization_weight,
            hop_weight=self.hop_weight,
            missing_delay_ms=self.missing_delay_ms,
        )

        cost_by_path = {tuple(path): cost for path, cost in ranked}

        # Check whether this flow already has an active pinned path.
        if flow_id is not None and flow_id in self._flow_table:
            entry = self._flow_table[flow_id]
            current_key = tuple(entry.path)

            if current_key in cost_by_path:
                current_cost = cost_by_path[current_key]
                elapsed = timestamp - entry.last_seen

                # 1. Active flowlet: stay pinned to prevent packet reordering.
                if elapsed < self.flowlet_timeout_s:
                    entry.last_seen = timestamp
                    entry.last_cost = current_cost
                    return RouteDecision(
                        path=list(entry.path),
                        cost=current_cost,
                        reason="flowlet_pinned",
                    )

                # 2. Flowlet gap expired: only switch if improvement beats hysteresis.
                best_path, best_cost = ranked[0]
                improvement = current_cost - best_cost

                if best_path == entry.path or improvement <= self.hysteresis_ms:
                    entry.last_seen = timestamp
                    entry.last_cost = current_cost
                    return RouteDecision(
                        path=list(entry.path),
                        cost=current_cost,
                        reason="hysteresis_kept",
                    )

                # 3. Congestion is significantly worse: reroute to a better path.
                chosen_path, chosen_cost = self._select_multipath(ranked, flow_id)

                if chosen_path != entry.path:
                    self.route_changes += 1

                self._flow_table[flow_id] = _FlowletEntry(
                    path=list(chosen_path),
                    last_seen=timestamp,
                    last_cost=chosen_cost,
                )

                return RouteDecision(
                    path=list(chosen_path),
                    cost=chosen_cost,
                    reason="congestion_reroute",
                )

            # Previous path is no longer operational: fail over immediately.
            chosen_path, chosen_cost = self._select_multipath(ranked, flow_id)
            self.route_changes += 1

            self._flow_table[flow_id] = _FlowletEntry(
                path=list(chosen_path),
                last_seen=timestamp,
                last_cost=chosen_cost,
            )

            return RouteDecision(
                path=list(chosen_path),
                cost=chosen_cost,
                reason="failover_reroute",
            )

        # New flow (or stateless call when flow_id is None).
        chosen_path, chosen_cost = self._select_multipath(ranked, flow_id)

        if flow_id is not None:
            self._flow_table[flow_id] = _FlowletEntry(
                path=list(chosen_path),
                last_seen=timestamp,
                last_cost=chosen_cost,
            )

        return RouteDecision(
            path=list(chosen_path),
            cost=chosen_cost,
            reason="initial_selection",
        )

    def clear_flow_state(self, flow_id=None):
        """Clear cached flowlet state for one flow or all flows."""

        if flow_id is None:
            self._flow_table.clear()
        else:
            self._flow_table.pop(flow_id, None)


class PathSelector:
    """
    Lightweight wrapper matching the proposal's PathSelector interface
    for callers that already have candidate_paths and queue_stats.
    """

    def __init__(
        self,
        use_smoothed=True,
        utilization_weight=0.5,
        hop_weight=0.05,
        missing_delay_ms=10.0,
    ):
        self.use_smoothed = use_smoothed
        self.utilization_weight = utilization_weight
        self.hop_weight = hop_weight
        self.missing_delay_ms = missing_delay_ms

    def calculate_path_cost(self, path, queue_stats):
        return calculate_path_cost(
            path,
            queue_stats,
            use_smoothed=self.use_smoothed,
            utilization_weight=self.utilization_weight,
            hop_weight=self.hop_weight,
            missing_delay_ms=self.missing_delay_ms,
        )

    def select_path(self, candidate_paths, queue_stats):
        if not candidate_paths:
            return None

        ranked = rank_candidate_paths(
            candidate_paths,
            queue_stats,
            use_smoothed=self.use_smoothed,
            utilization_weight=self.utilization_weight,
            hop_weight=self.hop_weight,
            missing_delay_ms=self.missing_delay_ms,
        )

        best_path, best_cost = ranked[0]

        return RouteDecision(
            path=list(best_path),
            cost=best_cost,
            reason="lowest_cost",
        )