import pytest

from controller.route_selector import (
    ROUTING_MODES,
    healthy_graph,
    make_router,
)
from routing.queue_aware_routing import QueueAwareRouter
from tests.recovery_helpers import LOWER, UPPER, FakeTopologyManager


def diamond():
    return FakeTopologyManager().get_graph()


def congested_upper():
    """Queue statistics with a long queue on s1 -> s2."""
    stats = {
        link: {"estimated_delay_ms": 0.0, "smoothed_delay_ms": 0.0,
               "utilization": 0.0, "valid": True}
        for link in [(1, 2), (2, 4), (1, 3), (3, 4)]
    }
    stats[(1, 2)]["estimated_delay_ms"] = 40.0
    stats[(1, 2)]["smoothed_delay_ms"] = 40.0
    return stats


@pytest.mark.parametrize("mode", ROUTING_MODES)
def test_every_mode_finds_a_path_and_avoids_failed_links(mode):
    router = make_router(mode, queue_source={})

    decision = router.select_path(diamond(), 1, 4, flow_id="f")
    assert decision.path in (UPPER, LOWER)

    decision = router.select_path(
        diamond(), 1, 4, flow_id="f", failed_links=[(1, 2)]
    )
    assert decision.path == LOWER

    # The failed link may be given in either direction.
    decision = router.select_path(
        diamond(), 1, 4, flow_id="g", failed_links=[(4, 3)]
    )
    assert decision.path == UPPER


@pytest.mark.parametrize("mode", ROUTING_MODES)
def test_every_mode_avoids_failed_switches(mode):
    router = make_router(mode, queue_source={})

    decision = router.select_path(
        diamond(), 1, 4, flow_id="f", failed_switches=[3]
    )
    assert decision.path == UPPER

    assert router.select_path(
        diamond(), 1, 4, flow_id="f", failed_switches=[2, 3]
    ) is None
    assert router.select_path(
        diamond(), 1, 4, flow_id="f", failed_switches=[4]
    ) is None


@pytest.mark.parametrize("mode", ROUTING_MODES)
def test_every_mode_returns_none_without_a_path(mode):
    router = make_router(mode, queue_source={})

    assert router.select_path(
        diamond(), 1, 4, flow_id="f", failed_links=[(1, 2), (3, 4)]
    ) is None
    assert router.select_path(diamond(), 1, 99, flow_id="f") is None


def test_shortest_path_ignores_flow_id_and_queues():
    router = make_router("shortest_path", queue_source=congested_upper())

    paths = {
        tuple(router.select_path(diamond(), 1, 4, flow_id=i).path)
        for i in range(20)
    }
    assert len(paths) == 1


def test_ecmp_spreads_flows_and_is_deterministic():
    router = make_router("ecmp")

    first = [router.select_path(diamond(), 1, 4, flow_id=i).path
             for i in range(40)]
    second = [router.select_path(diamond(), 1, 4, flow_id=i).path
              for i in range(40)]

    assert first == second
    assert UPPER in first and LOWER in first


def test_queue_aware_modes_avoid_the_congested_path():
    for mode in ("queue_aware", "proposed"):
        router = make_router(mode, queue_source=congested_upper())
        for flow_id in range(10):
            decision = router.select_path(diamond(), 1, 4, flow_id=flow_id)
            assert decision.path == LOWER


def test_proposed_mode_uses_the_flowlet_router():
    router = make_router("proposed", flowlet_timeout_s=1.0)

    assert isinstance(router, QueueAwareRouter)
    assert router.flowlet_timeout_s == 1.0


def test_baselines_accept_clear_flow_state():
    for mode in ("shortest_path", "ecmp", "queue_aware"):
        make_router(mode).clear_flow_state("f")


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        make_router("fastest")


def test_healthy_graph_leaves_the_original_untouched():
    graph = diamond()

    pruned = healthy_graph(graph, failed_links=[(2, 1)], failed_switches=[3])

    assert not pruned.has_edge(1, 2) and not pruned.has_edge(2, 1)
    assert 3 not in pruned
    assert graph.has_edge(1, 2) and 3 in graph
