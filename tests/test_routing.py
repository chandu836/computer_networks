
from topology.network_graph import create_network_graph
from baselines.shortest_path import find_shortest_path
from baselines.ecmp import find_ecmp_paths, select_ecmp_path


def test_shortest_path():
    graph = create_network_graph()

    path = find_shortest_path(graph, "h1", "h2")

    assert path[0] == "h1"
    assert path[-1] == "h2"
    assert len(path) == 5


def test_shortest_path_invalid_node():
    graph = create_network_graph()

    assert find_shortest_path(graph, "h1", "s9") is None


def test_shortest_path_unreachable():
    graph = create_network_graph()
    graph.remove_edge("s1", "s2")
    graph.remove_edge("s1", "s3")

    assert find_shortest_path(graph, "h1", "h2") is None


def test_shortest_path_same_node():
    graph = create_network_graph()

    assert find_shortest_path(graph, "h1", "h1") == ["h1"]


def test_ecmp_finds_two_paths():
    graph = create_network_graph()

    paths = find_ecmp_paths(graph, "h1", "h2")

    assert len(paths) == 2


def test_ecmp_selection_is_deterministic():
    graph = create_network_graph()
    paths = find_ecmp_paths(graph, "h1", "h2")

    first = select_ecmp_path(paths, "flow-1")
    second = select_ecmp_path(paths, "flow-1")

    assert first == second
    assert first in paths


def test_ecmp_invalid_node():
    graph = create_network_graph()

    assert find_ecmp_paths(graph, "h1", "s9") == []


def test_ecmp_unreachable():
    graph = create_network_graph()
    graph.remove_edge("s1", "s2")
    graph.remove_edge("s1", "s3")

    assert find_ecmp_paths(graph, "h1", "h2") == []


def test_ecmp_empty_paths():
    assert select_ecmp_path([], "flow-1") is None
