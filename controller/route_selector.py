"""
Routing modes of the controller.

The evaluation compares four configurations (section 11 of the project
document). Each one is exposed through the same call the queue-aware
router already offers, so the controller and the recovery manager do not
care which mode is active:

    router = make_router("proposed", queue_source=queue_monitor)
    decision = router.select_path(graph, src, dst, flow_id=flow_id,
                                  failed_links=[(1, 2)])

Modes:
    shortest_path  baselines/shortest_path.py
    ecmp           baselines/ecmp.py
    queue_aware    baselines/queue_aware.py (stateless, lowest delay)
    proposed       routing/queue_aware_routing.py QueueAwareRouter
                   (flowlets and hysteresis), run together with the
                   recovery manager
"""

from baselines.ecmp import find_ecmp_paths, select_ecmp_path
from baselines.queue_aware import select_decision
from baselines.shortest_path import find_shortest_path
from common.models import RouteDecision
from routing.queue_aware_routing import QueueAwareRouter

ROUTING_MODES = ("shortest_path", "ecmp", "queue_aware", "proposed")
DEFAULT_ROUTING_MODE = "proposed"


def healthy_graph(graph, failed_links=None, failed_switches=None):
    """Return a copy of the graph without the failed links and switches."""
    pruned = graph.copy()

    pruned.remove_nodes_from(
        [node for node in (failed_switches or []) if node in pruned]
    )

    for node1, node2 in (failed_links or []):
        for edge in ((node1, node2), (node2, node1)):
            if pruned.has_edge(*edge):
                pruned.remove_edge(*edge)

    return pruned


class _StatelessRouter:
    """Shared plumbing of the baseline modes."""

    route_changes = 0

    def clear_flow_state(self, flow_id=None):
        """Baselines keep no per-flow state."""


class ShortestPathRouter(_StatelessRouter):
    def select_path(self, graph, source, destination, flow_id=None,
                    failed_links=None, failed_switches=None, **_ignored):
        path = find_shortest_path(
            healthy_graph(graph, failed_links, failed_switches),
            source,
            destination,
        )
        if path is None:
            return None

        return RouteDecision(
            path=list(path),
            cost=float(len(path) - 1),
            reason="shortest_path",
        )


class EcmpRouter(_StatelessRouter):
    def select_path(self, graph, source, destination, flow_id=None,
                    failed_links=None, failed_switches=None, **_ignored):
        paths = find_ecmp_paths(
            healthy_graph(graph, failed_links, failed_switches),
            source,
            destination,
        )
        path = select_ecmp_path(paths, flow_id)
        if path is None:
            return None

        return RouteDecision(
            path=list(path),
            cost=float(len(path) - 1),
            reason="ecmp",
        )


class PlainQueueAwareRouter(_StatelessRouter):
    def __init__(self, queue_source=None):
        self.queue_source = queue_source

    def select_path(self, graph, source, destination, flow_id=None,
                    failed_links=None, failed_switches=None, **_ignored):
        return select_decision(
            graph,
            source=source,
            destination=destination,
            queue_source=self.queue_source,
            failed_links=failed_links,
            failed_switches=failed_switches,
        )


def make_router(mode=DEFAULT_ROUTING_MODE, queue_source=None,
                flowlet_timeout_s=1.0):
    """
    Build the router for a routing mode.

    flowlet_timeout_s must equal the idle timeout of the installed
    OpenFlow rules (see docs/ROUTING.md).
    """
    if mode == "shortest_path":
        return ShortestPathRouter()

    if mode == "ecmp":
        return EcmpRouter()

    if mode == "queue_aware":
        return PlainQueueAwareRouter(queue_source)

    if mode == "proposed":
        return QueueAwareRouter(
            queue_source=queue_source,
            flowlet_timeout_s=flowlet_timeout_s,
        )

    raise ValueError(
        "Unknown routing mode %r (expected one of %s)"
        % (mode, ", ".join(ROUTING_MODES))
    )
