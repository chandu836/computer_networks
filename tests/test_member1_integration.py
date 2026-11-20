from common.config import PRIMARY_PATH, ALTERNATE_PATH
from topology.network_graph import create_network_graph
from baselines.shortest_path import find_shortest_path
from baselines.ecmp import find_ecmp_paths, select_ecmp_path
from traffic.failure_scenarios import simulate_link_failure


def test_member1_topology_routing_and_recovery_integration():
    # 1. Build the complete diamond network graph
    graph = create_network_graph()

    # 2. Verify the primary shortest path
    primary = find_shortest_path(graph, "h1", "h2")
    assert primary == PRIMARY_PATH

    # 3. Verify both equal-cost paths
    paths = find_ecmp_paths(graph, "h1", "h2")
    assert len(paths) == 2
    assert PRIMARY_PATH in paths
    assert ALTERNATE_PATH in paths

    # 4. Verify deterministic ECMP selection
    selected1 = select_ecmp_path(paths, "flow-1")
    selected2 = select_ecmp_path(paths, "flow-1")
    assert selected1 == selected2
    assert selected1 in paths

    # 5. Simulate failure of the primary path's first switch link
    failed_graph, recovery_path = simulate_link_failure(
        graph,
        ("s1", "s2"),
        "h1",
        "h2"
    )

    # 6. Verify that routing selects the alternate path
    assert recovery_path == ALTERNATE_PATH

    # 7. Verify that the original graph remains unchanged
    assert graph.has_edge("s1", "s2")
    assert not failed_graph.has_edge("s1", "s2")

    print("Primary path:", primary)
    print("ECMP paths:", paths)
    print("Selected ECMP path:", selected1)
    print("Path after failure:", recovery_path)
    print("Member 1 integration test passed.")


if __name__ == "__main__":
    test_member1_topology_routing_and_recovery_integration()
