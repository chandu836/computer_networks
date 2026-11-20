import csv

from common.models import FailureEvent, RouteDecision
from controller.route_selector import make_router
from failure_detection.detector import FailureDetector
from recovery.flow_manager import FlowManager, switch_path
from recovery.recovery_manager import RecoveryManager
from routing.queue_aware_routing import QueueAwareRouter
from tests.recovery_helpers import (
    DIAMOND_LINKS,
    LOWER,
    UPPER,
    FakeDatapath,
    FakeTopologyManager,
    diamond_hops,
    is_delete,
    out_ports,
    tcp_match,
)


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class Setup:
    """Diamond network with a detector, flow manager and recovery manager."""

    def __init__(self, router=None, costs=None, **kwargs):
        self.clock = Clock()
        self.topology = FakeTopologyManager()
        self.costs = costs if costs is not None else {}

        self.detector = FailureDetector(clock=self.clock)
        for dpid in (1, 2, 3, 4):
            self.detector.on_switch_enter(dpid)
        for src, src_port, dst, _ in DIAMOND_LINKS:
            self.detector.on_link_add(src, src_port, dst)

        self.flows = FlowManager(idle_timeout=1)
        self.datapaths = {dpid: FakeDatapath(dpid) for dpid in (1, 2, 3, 4)}
        for datapath in self.datapaths.values():
            self.flows.register_datapath(datapath)

        self.router = router or make_router("shortest_path")
        self.recovery = RecoveryManager(
            topology_manager=self.topology,
            router=self.router,
            flow_manager=self.flows,
            failure_detector=self.detector,
            path_cost=lambda path: self.costs.get(tuple(path), 0.0),
            clock=self.clock,
            **kwargs,
        )
        self.detector.register_listener(self.recovery.on_failure_event)

    def install(self, flow_id, path, src_port=40000):
        assert self.flows.install_path(
            flow_id, tcp_match(src_port), diamond_hops(path)
        )

    def path(self, flow_id):
        hops = self.flows.get_path(flow_id)
        return switch_path(hops) if hops else None

    def link_down(self, a, port_a, b, port_b):
        self.detector.on_port_status(a, port_a, True)
        self.detector.on_port_status(b, port_b, True)

    def link_up(self, a, port_a, b, port_b):
        self.detector.on_port_status(a, port_a, False)
        self.detector.on_port_status(b, port_b, False)


def test_link_failure_reroutes_only_affected_flows():
    net = Setup()
    net.install("up", UPPER, 40000)
    net.install("low", LOWER, 40001)
    for datapath in net.datapaths.values():
        datapath.clear()

    net.link_down(1, 2, 2, 1)

    assert net.path("up") == LOWER
    assert net.path("low") == LOWER
    assert net.recovery.flows_rerouted == 1

    # s1 now forwards the flow to s3, and the old rule on s2 is gone.
    assert out_ports(net.datapaths[1].flow_mods()[-1]) == [3]
    assert is_delete(net.datapaths[2].flow_mods()[-1])
    # Only the affected flow was touched.
    assert all(
        msg.match["tcp_src"] == 40000
        for datapath in net.datapaths.values()
        for msg in datapath.flow_mods()
    )


def test_failure_on_second_hop_and_reverse_direction():
    net = Setup()
    net.install("up", UPPER)
    assert net.flows.install_path(
        "back", tcp_match(40002), diamond_hops([4, 2, 1], 1)
    )

    net.link_down(2, 2, 4, 1)

    assert net.path("up") == LOWER
    assert net.path("back") == [4, 3, 1]
    assert net.recovery.flows_rerouted == 2


def test_no_alternative_path_removes_rules():
    net = Setup()
    net.install("up", UPPER)

    net.link_down(1, 3, 3, 1)       # lower path first: nothing to do
    assert net.path("up") == UPPER

    for datapath in net.datapaths.values():
        datapath.clear()
    net.link_down(1, 2, 2, 1)       # now h1 is cut off

    assert net.path("up") is None
    assert net.recovery.flows_unrecoverable == 1
    assert net.recovery.get_displaced_flows() == {}
    for dpid in UPPER:
        assert is_delete(net.datapaths[dpid].flow_mods()[-1])


def test_switch_failure_reroutes_around_the_switch():
    net = Setup()
    net.install("up", UPPER)

    net.detector.on_switch_leave(2)

    assert net.path("up") == LOWER
    assert net.recovery.get_failed_switches() == [2]
    assert not net.recovery.is_path_healthy(UPPER)
    assert net.recovery.is_path_healthy(LOWER)


def test_is_path_healthy_accepts_all_path_forms():
    net = Setup()
    assert net.recovery.is_path_healthy(["h1", "s1", "s2", "s4", "h2"])
    assert net.recovery.is_path_healthy(diamond_hops(UPPER))
    assert not net.recovery.is_path_healthy([])

    net.recovery.handle_failure(("s2", "s1"))

    assert not net.recovery.is_path_healthy(["h1", "s1", "s2", "s4", "h2"])
    assert not net.recovery.is_path_healthy(diamond_hops([4, 2, 1], 1))
    assert net.recovery.is_path_healthy(LOWER)
    assert net.recovery.get_failed_links() == [(1, 2)]


def test_router_that_ignores_failures_is_not_trusted():
    class StubbornRouter:
        def select_path(self, graph, source, destination, **kwargs):
            return RouteDecision(path=list(UPPER))

    net = Setup(router=StubbornRouter())
    net.install("up", UPPER)

    net.link_down(1, 2, 2, 1)

    assert net.path("up") is None
    assert net.recovery.flows_unrecoverable == 1


def test_recovery_time_is_measured_from_detection():
    net = Setup()
    net.install("up", UPPER)

    original = net.flows.update_path

    def slow_update(flow_id, hops):
        net.clock.now += 0.004      # the reroute takes 4 ms
        return original(flow_id, hops)

    net.flows.update_path = slow_update

    net.clock.now = 1000.25         # failure injected at 1000.0
    net.link_down(1, 2, 2, 1)

    (record,) = net.recovery.recovery_log
    assert record["event"] == "failure"
    assert record["link"] == "s1-s2"
    assert record["flows_rerouted"] == 1
    assert abs(record["recovery_time_s"] - 0.004) < 1e-9
    assert abs(net.recovery.recovery_time_since(1000.0, 1, 2) - 0.254) < 1e-9
    assert net.recovery.recovery_time_since(1000.0, 3, 4) is None
    assert net.recovery.recovery_time_since(2000.0, 1, 2) is None


def test_restoration_waits_for_the_stability_interval():
    costs = {tuple(LOWER): 5.0, tuple(UPPER): 0.0}
    net = Setup(costs=costs, stability_interval_s=3.0, restore_hysteresis=0.5)
    net.install("up", UPPER)
    net.link_down(1, 2, 2, 1)
    assert net.path("up") == LOWER

    net.clock.now += 10.0
    net.link_up(1, 2, 2, 1)
    assert net.recovery.get_recovering_links() == [(1, 2)]

    net.clock.now += 2.9
    assert net.recovery.process_pending() == []
    assert net.path("up") == LOWER

    net.clock.now += 0.2
    assert net.recovery.process_pending() == ["up"]
    assert net.path("up") == UPPER
    assert net.recovery.flows_restored == 1
    assert net.recovery.get_recovering_links() == []
    assert net.recovery.get_displaced_flows() == {}

    # Nothing moves again afterwards.
    net.clock.now += 10.0
    assert net.recovery.process_pending() == []
    assert net.flows.route_changes == 2


def test_flapping_link_is_not_reused():
    costs = {tuple(LOWER): 5.0, tuple(UPPER): 0.0}
    net = Setup(costs=costs, stability_interval_s=3.0)
    net.install("up", UPPER)
    net.link_down(1, 2, 2, 1)

    for _ in range(3):
        net.clock.now += 2.0
        net.link_up(1, 2, 2, 1)
        net.clock.now += 2.0
        assert net.recovery.process_pending() == []
        net.link_down(1, 2, 2, 1)

    net.clock.now += 60.0
    assert net.recovery.process_pending() == []
    assert net.path("up") == LOWER
    assert net.flows.route_changes == 1


def test_restoration_needs_a_meaningful_improvement():
    costs = {tuple(LOWER): 0.4, tuple(UPPER): 0.0}
    net = Setup(costs=costs, stability_interval_s=3.0, restore_hysteresis=0.5)
    net.install("up", UPPER)
    net.link_down(1, 2, 2, 1)
    net.link_up(1, 2, 2, 1)

    net.clock.now += 5.0
    assert net.recovery.process_pending() == []
    assert net.path("up") == LOWER
    assert net.flows.route_changes == 1

    # The detour gets congested later: now the move pays off.
    costs[tuple(LOWER)] = 3.0
    net.clock.now += 1.0
    assert net.recovery.process_pending() == ["up"]
    assert net.path("up") == UPPER


def test_restoration_waits_until_topology_knows_the_link_again():
    costs = {tuple(LOWER): 5.0}
    net = Setup(costs=costs, stability_interval_s=1.0)
    net.install("up", UPPER)
    net.link_down(1, 2, 2, 1)
    net.topology.remove_link(1, 2)      # LLDP removed the link

    net.link_up(1, 2, 2, 1)
    net.clock.now += 2.0
    assert net.recovery.process_pending() == []

    net.topology = FakeTopologyManager()    # rediscovered
    net.recovery.topology_manager = net.topology
    assert net.recovery.process_pending() == ["up"]


def test_only_displaced_flows_are_restored():
    costs = {tuple(LOWER): 5.0, tuple(UPPER): 0.0}
    net = Setup(costs=costs, stability_interval_s=1.0)
    net.install("up", UPPER, 40000)
    net.install("low", LOWER, 40001)
    net.link_down(1, 2, 2, 1)
    net.link_up(1, 2, 2, 1)

    net.clock.now += 2.0
    assert net.recovery.process_pending() == ["up"]
    assert net.path("low") == LOWER


def test_ended_flow_is_forgotten():
    costs = {tuple(LOWER): 5.0}
    net = Setup(costs=costs, stability_interval_s=1.0)
    net.install("up", UPPER)
    net.install("other", UPPER, 40001)
    net.link_down(1, 2, 2, 1)
    assert set(net.recovery.get_displaced_flows()) == {"up", "other"}

    net.recovery.forget_flow("up")              # flow-removed handler
    net.flows.remove_path("other")              # ended without notice

    net.link_up(1, 2, 2, 1)
    net.clock.now += 2.0
    assert net.recovery.process_pending() == []
    assert net.recovery.get_displaced_flows() == {}


def test_queue_aware_router_fails_over_and_forgets_detour_on_restore():
    router = QueueAwareRouter(queue_source={}, flowlet_timeout_s=1.0)
    costs = {tuple(LOWER): 5.0}
    net = Setup(router=router, costs=costs, stability_interval_s=1.0)

    decision = router.select_path(
        net.topology.get_graph(), 1, 4, flow_id="f", now=0.0
    )
    net.install("f", decision.path)
    original = list(decision.path)
    detour = LOWER if original == UPPER else UPPER
    costs.clear()
    costs[tuple(detour)] = 5.0

    if original == UPPER:
        net.link_down(1, 2, 2, 1)
    else:
        net.link_down(1, 3, 3, 1)

    assert net.path("f") == detour
    assert router.route_changes == 1

    if original == UPPER:
        net.link_up(1, 2, 2, 1)
    else:
        net.link_up(1, 3, 3, 1)

    net.clock.now += 2.0
    assert net.recovery.process_pending() == ["f"]
    assert net.path("f") == original

    # The router no longer pins the flow to the detour.
    again = router.select_path(
        net.topology.get_graph(), 1, 4, flow_id="f", now=100.0
    )
    assert again.reason == "initial_selection"
    assert again.path == original


def test_handle_failure_accepts_failure_event_without_detector():
    topology = FakeTopologyManager()
    flows = FlowManager()
    for dpid in (1, 2, 3, 4):
        flows.register_datapath(FakeDatapath(dpid))
    recovery = RecoveryManager(topology, make_router("ecmp"), flows)
    flows.install_path("up", tcp_match(), diamond_hops(UPPER))

    record = recovery.handle_failure(
        FailureEvent("s1", "s2", True, detection_time=None)
    )

    assert record["flows_rerouted"] == 1
    assert switch_path(flows.get_path("up")) == LOWER
    assert recovery.get_failed_links() == [(1, 2)]

    recovery.handle_restoration(FailureEvent("s2", "s1", False))
    assert recovery.get_failed_links() == []
    assert recovery.get_stats()["recovering_links"] == [(1, 2)]


def test_save_log(tmp_path):
    costs = {tuple(LOWER): 5.0}
    net = Setup(costs=costs, stability_interval_s=1.0)
    net.install("up", UPPER)
    net.link_down(1, 2, 2, 1)
    net.link_up(1, 2, 2, 1)
    net.clock.now += 2.0
    net.recovery.process_pending()

    path = net.recovery.save_log(str(tmp_path / "recovery.csv"))
    with open(path) as handle:
        rows = list(csv.DictReader(handle))

    assert [row["event"] for row in rows] == ["failure", "restoration"]
    assert rows[0]["link"] == "s1-s2" and rows[0]["flows_rerouted"] == "1"
    assert rows[1]["flows_restored"] == "1"
