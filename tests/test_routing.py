from topology.network_graph import create_network_graph
from baselines.shortest_path import find_shortest_path
from baselines.ecmp import find_ecmp_paths, select_ecmp_path


def test_shortest_path():
    graph = create_network_graph()

    path = find_shortest_path(graph, "h1", "h2")

    assert path[0] == "h1"
    assert path[-1] == "h2"
    assert len(path) == 5


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
