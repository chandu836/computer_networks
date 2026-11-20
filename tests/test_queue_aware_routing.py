import unittest
import networkx as nx

from baselines.ecmp import find_ecmp_paths, select_ecmp_path
from baselines.queue_aware import select_decision, select_path
from common.config import ALTERNATE_PATH, PRIMARY_PATH
from common.models import LinkMetrics, RouteDecision
from routing.queue_aware_routing import (
    PathSelector,
    QueueAwareRouter,
    calculate_path_cost,
    find_candidate_paths,
)
from topology.network_graph import create_network_graph


class _fake_monitor:
    """minimal mock of queue monitor to verify snapshot count."""

    def __init__(self, stats):
        self._stats = stats
        self.calls = 0

    def get_all_stats(self):
        self.calls += 1
        return dict(self._stats)


class test_queue_aware_routing(unittest.TestCase):

    def setUp(self):
        self.graph = create_network_graph()

    def test_diamond_paths(self):
        paths = find_candidate_paths(self.graph, "h1", "h2")

        self.assertEqual(len(paths), 2)
        self.assertIn(PRIMARY_PATH, paths)
        self.assertIn(ALTERNATE_PATH, paths)

    def test_failed_link_and_switch(self):
        link_down = find_candidate_paths(
            self.graph,
            "h1",
            "h2",
            failed_links=[("s1", "s2")],
        )
        self.assertEqual(link_down, [ALTERNATE_PATH])

        switch_down = find_candidate_paths(
            self.graph,
            "h1",
            "h2",
            failed_switches=["s2"],
        )
        self.assertEqual(switch_down, [ALTERNATE_PATH])

    def test_no_path(self):
        router = QueueAwareRouter()
        decision = router.select_path(
            self.graph,
            "h1",
            "h2",
            flow_id="f0",
            failed_links=[("s1", "s2"), ("s1", "s3")],
        )

        self.assertIsNone(decision)

    def test_path_cost(self):
        stats = {
            (1, 2): {"smoothed_delay_ms": 20.0, "utilization": 0.0, "valid": True},
            (2, 4): {"smoothed_delay_ms": 0.0, "utilization": 0.0, "valid": True},
            (1, 3): {"smoothed_delay_ms": 0.0, "utilization": 0.0, "valid": True},
            (3, 4): {"smoothed_delay_ms": 0.0, "utilization": 0.0, "valid": True},
        }

        # 4 hops * 0.05 = 0.2; upper path adds 20.0 ms queueing delay.
        self.assertAlmostEqual(calculate_path_cost(PRIMARY_PATH, stats), 20.2)
        self.assertAlmostEqual(calculate_path_cost(ALTERNATE_PATH, stats), 0.2)

    def test_all_input_formats(self):
        stats_dict = {
            (1, 2): {"smoothed_delay_ms": 20.0, "utilization": 0.5, "valid": True},
            (2, 4): {"smoothed_delay_ms": 5.0, "utilization": 0.2, "valid": True},
            (1, 3): {"smoothed_delay_ms": 1.0, "utilization": 0.1, "valid": True},
            (3, 4): {"smoothed_delay_ms": 1.0, "utilization": 0.1, "valid": True},
        }
        monitor = _fake_monitor(stats_dict)
        metrics_list = [
            LinkMetrics("s1", "s2", estimated_delay_s=0.020, utilization=0.5),
            LinkMetrics("s2", "s4", estimated_delay_s=0.005, utilization=0.2),
            LinkMetrics("s1", "s3", estimated_delay_s=0.001, utilization=0.1),
            LinkMetrics("s3", "s4", estimated_delay_s=0.001, utilization=0.1),
        ]

        cost_dict = calculate_path_cost(PRIMARY_PATH, stats_dict)
        cost_mon = calculate_path_cost(PRIMARY_PATH, monitor)
        cost_met = calculate_path_cost(PRIMARY_PATH, metrics_list)

        self.assertAlmostEqual(cost_dict, cost_mon)
        self.assertAlmostEqual(cost_dict, cost_met)

    def test_dpid_graph(self):
        # matches topology manager graph using integer dpids.
        dpid_graph = nx.Graph()
        dpid_graph.add_edges_from([(1, 2), (2, 4), (1, 3), (3, 4)])

        stats = {
            (1, 2): {"smoothed_delay_ms": 25.0, "utilization": 0.8, "valid": True},
            (2, 4): {"smoothed_delay_ms": 1.0, "utilization": 0.1, "valid": True},
            (1, 3): {"smoothed_delay_ms": 1.0, "utilization": 0.1, "valid": True},
            (3, 4): {"smoothed_delay_ms": 1.0, "utilization": 0.1, "valid": True},
        }

        router = QueueAwareRouter(queue_source=stats)
        decision = router.select_path(dpid_graph, 1, 4, flow_id="dpid-flow")

        self.assertIsNotNone(decision)
        self.assertEqual(decision.path, [1, 3, 4])

    def test_missing_stats_penalty(self):
        # invalid stats must get penalized instead of looking free.
        stats = {
            (1, 2): {"smoothed_delay_ms": None, "utilization": None, "valid": False},
            (2, 4): {"smoothed_delay_ms": 0.0, "utilization": 0.0, "valid": True},
            (1, 3): {"smoothed_delay_ms": 2.0, "utilization": 0.1, "valid": True},
            (3, 4): {"smoothed_delay_ms": 0.0, "utilization": 0.0, "valid": True},
        }

        router = QueueAwareRouter(queue_source=stats, missing_delay_ms=10.0)
        decision = router.select_path(self.graph, "h1", "h2", flow_id="blind")

        self.assertEqual(decision.path, ALTERNATE_PATH)

    def test_single_snapshot(self):
        stats = {
            (1, 2): {"smoothed_delay_ms": 1.0, "utilization": 0.1, "valid": True},
            (2, 4): {"smoothed_delay_ms": 1.0, "utilization": 0.1, "valid": True},
            (1, 3): {"smoothed_delay_ms": 1.0, "utilization": 0.1, "valid": True},
            (3, 4): {"smoothed_delay_ms": 1.0, "utilization": 0.1, "valid": True},
        }
        monitor = _fake_monitor(stats)
        router = QueueAwareRouter(queue_source=monitor)

        router.select_path(self.graph, "h1", "h2", flow_id="snap")

        # read monitor once per decision, not once per hop.
        self.assertEqual(monitor.calls, 1)

    def test_idle_split(self):
        idle = {
            (1, 2): {"smoothed_delay_ms": 0.0, "utilization": 0.0, "valid": True},
            (2, 4): {"smoothed_delay_ms": 0.0, "utilization": 0.0, "valid": True},
            (1, 3): {"smoothed_delay_ms": 0.0, "utilization": 0.0, "valid": True},
            (3, 4): {"smoothed_delay_ms": 0.0, "utilization": 0.0, "valid": True},
        }
        router = QueueAwareRouter(queue_source=idle)

        upper = 0
        lower = 0

        for i in range(1000):
            decision = router.select_path(self.graph, "h1", "h2", flow_id=f"f-{i}")
            if decision.path == PRIMARY_PATH:
                upper += 1
            elif decision.path == ALTERNATE_PATH:
                lower += 1

        self.assertEqual(upper + lower, 1000)
        self.assertGreater(upper, 400)
        self.assertGreater(lower, 400)

    def test_vs_ecmp(self):
        congested = {
            (1, 2): {"smoothed_delay_ms": 30.0, "utilization": 1.0, "valid": True},
            (2, 4): {"smoothed_delay_ms": 0.0, "utilization": 0.2, "valid": True},
            (1, 3): {"smoothed_delay_ms": 0.0, "utilization": 0.1, "valid": True},
            (3, 4): {"smoothed_delay_ms": 0.0, "utilization": 0.1, "valid": True},
        }

        ecmp_paths = find_ecmp_paths(self.graph, "h1", "h2")
        router = QueueAwareRouter(queue_source=congested)

        ecmp_upper = 0
        qa_upper = 0

        for i in range(1000):
            fid = f"f-{i}"
            if select_ecmp_path(ecmp_paths, fid) == PRIMARY_PATH:
                ecmp_upper += 1
            if router.select_path(self.graph, "h1", "h2", flow_id=fid).path == PRIMARY_PATH:
                qa_upper += 1

        # ecmp sends ~50% to congested upper path; queue-aware sends 0%.
        self.assertGreater(ecmp_upper, 400)
        self.assertEqual(qa_upper, 0)

    def test_flowlet_pinning(self):
        light = {
            (1, 2): {"smoothed_delay_ms": 1.0, "utilization": 0.1, "valid": True},
            (2, 4): {"smoothed_delay_ms": 0.0, "utilization": 0.1, "valid": True},
            (1, 3): {"smoothed_delay_ms": 20.0, "utilization": 0.8, "valid": True},
            (3, 4): {"smoothed_delay_ms": 0.0, "utilization": 0.1, "valid": True},
        }
        router = QueueAwareRouter(flowlet_timeout_s=0.1, hysteresis_ms=2.0)

        d1 = router.select_path(
            self.graph, "h1", "h2", flow_id="f1", queue_source=light, now=0.0
        )
        self.assertEqual(d1.path, PRIMARY_PATH)

        flipped = {
            (1, 2): {"smoothed_delay_ms": 40.0, "utilization": 1.0, "valid": True},
            (2, 4): {"smoothed_delay_ms": 0.0, "utilization": 0.1, "valid": True},
            (1, 3): {"smoothed_delay_ms": 1.0, "utilization": 0.1, "valid": True},
            (3, 4): {"smoothed_delay_ms": 0.0, "utilization": 0.1, "valid": True},
        }

        # inside flowlet window -> stays pinned to avoid reordering.
        d2 = router.select_path(
            self.graph, "h1", "h2", flow_id="f1", queue_source=flipped, now=0.05
        )
        self.assertEqual(d2.path, PRIMARY_PATH)
        self.assertEqual(d2.reason, "flowlet_pinned")

        # after flowlet timeout -> reroutes to uncongested path.
        d3 = router.select_path(
            self.graph, "h1", "h2", flow_id="f1", queue_source=flipped, now=0.25
        )
        self.assertEqual(d3.path, ALTERNATE_PATH)
        self.assertEqual(d3.reason, "congestion_reroute")
        self.assertEqual(router.route_changes, 1)

    def test_hysteresis(self):
        initial = {
            (1, 2): {"smoothed_delay_ms": 5.0, "utilization": 0.2, "valid": True},
            (2, 4): {"smoothed_delay_ms": 0.0, "utilization": 0.0, "valid": True},
            (1, 3): {"smoothed_delay_ms": 15.0, "utilization": 0.2, "valid": True},
            (3, 4): {"smoothed_delay_ms": 0.0, "utilization": 0.0, "valid": True},
        }
        router = QueueAwareRouter(flowlet_timeout_s=0.1, hysteresis_ms=5.0)

        d1 = router.select_path(
            self.graph, "h1", "h2", flow_id="f2", queue_source=initial, now=0.0
        )
        self.assertEqual(d1.path, PRIMARY_PATH)

        # only 1 ms better (< hysteresis_ms of 5.0) -> keep current path.
        small_diff = {
            (1, 2): {"smoothed_delay_ms": 6.0, "utilization": 0.2, "valid": True},
            (2, 4): {"smoothed_delay_ms": 0.0, "utilization": 0.0, "valid": True},
            (1, 3): {"smoothed_delay_ms": 5.0, "utilization": 0.2, "valid": True},
            (3, 4): {"smoothed_delay_ms": 0.0, "utilization": 0.0, "valid": True},
        }
        d2 = router.select_path(
            self.graph, "h1", "h2", flow_id="f2", queue_source=small_diff, now=1.0
        )
        self.assertEqual(d2.path, PRIMARY_PATH)
        self.assertEqual(d2.reason, "hysteresis_kept")
        self.assertEqual(router.route_changes, 0)

    def test_failover(self):
        stats = {
            (1, 2): {"smoothed_delay_ms": 1.0, "utilization": 0.1, "valid": True},
            (2, 4): {"smoothed_delay_ms": 0.0, "utilization": 0.0, "valid": True},
            (1, 3): {"smoothed_delay_ms": 20.0, "utilization": 0.8, "valid": True},
            (3, 4): {"smoothed_delay_ms": 0.0, "utilization": 0.0, "valid": True},
        }
        router = QueueAwareRouter(flowlet_timeout_s=0.1, hysteresis_ms=5.0)

        d1 = router.select_path(
            self.graph, "h1", "h2", flow_id="f4", queue_source=stats, now=0.0
        )
        self.assertEqual(d1.path, PRIMARY_PATH)

        d2 = router.select_path(
            self.graph,
            "h1",
            "h2",
            flow_id="f4",
            queue_source=stats,
            failed_links=[("s1", "s2")],
            now=0.02,
        )
        self.assertEqual(d2.path, ALTERNATE_PATH)
        self.assertEqual(d2.reason, "failover_reroute")
        self.assertEqual(router.route_changes, 1)

    def test_path_selector(self):
        stats = {
            (1, 2): {"smoothed_delay_ms": 25.0, "utilization": 0.5, "valid": True},
            (2, 4): {"smoothed_delay_ms": 0.0, "utilization": 0.0, "valid": True},
            (1, 3): {"smoothed_delay_ms": 2.0, "utilization": 0.1, "valid": True},
            (3, 4): {"smoothed_delay_ms": 0.0, "utilization": 0.0, "valid": True},
        }
        selector = PathSelector()
        candidates = find_candidate_paths(self.graph, "h1", "h2")

        decision = selector.select_path(candidates, stats)

        self.assertIsInstance(decision, RouteDecision)
        self.assertEqual(decision.path, ALTERNATE_PATH)
        self.assertLess(
            selector.calculate_path_cost(ALTERNATE_PATH, stats),
            selector.calculate_path_cost(PRIMARY_PATH, stats),
        )

    def test_baseline(self):
        stats = {
            (1, 2): {"estimated_delay_ms": 15.0, "valid": True},
            (2, 4): {"estimated_delay_ms": 0.0, "valid": True},
            (1, 3): {"estimated_delay_ms": 1.0, "valid": True},
            (3, 4): {"estimated_delay_ms": 0.0, "valid": True},
        }

        path = select_path(self.graph, "h1", "h2", stats)
        decision = select_decision(self.graph, "h1", "h2", stats)

        self.assertEqual(path, ALTERNATE_PATH)
        self.assertAlmostEqual(decision.cost, 1.0)


if __name__ == "__main__":
    unittest.main()