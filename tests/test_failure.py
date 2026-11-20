import pytest

from topology.network_graph import create_network_graph
from traffic.failure_scenarios import simulate_link_failure


def test_single_link_failure():
    graph = create_network_graph()

    modified, path = simulate_link_failure(
        graph, ("s1", "s2"), "h1", "h2"
    )

    assert path is not None
    assert "s3" in path
    assert not modified.has_edge("s1", "s2")


def test_original_graph_unchanged():
    graph = create_network_graph()

    simulate_link_failure(
        graph, ("s1", "s2"), "h1", "h2"
    )

    assert graph.has_edge("s1", "s2")


def test_both_paths_failed():
    graph = create_network_graph()

    graph.remove_edge("s1", "s2")

    modified, path = simulate_link_failure(
        graph, ("s1", "s3"), "h1", "h2"
    )

    assert path is None


def test_invalid_link():
    graph = create_network_graph()

    with pytest.raises(ValueError):
        simulate_link_failure(
            graph, ("s1", "s9"), "h1", "h2"
        )
