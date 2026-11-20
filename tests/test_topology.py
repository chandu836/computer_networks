from topology.diamond import DiamondTopo
from topology.network_graph import create_network_graph


def test_network_graph_nodes():
    graph = create_network_graph()

    assert set(graph.nodes) == {
        "h1", "h2", "s1", "s2", "s3", "s4"
    }


def test_network_graph_edges():
    graph = create_network_graph()

    assert graph.number_of_edges() == 6
    assert graph.has_edge("s1", "s2")
    assert graph.has_edge("s1", "s3")
    assert graph.has_edge("s2", "s4")
    assert graph.has_edge("s3", "s4")


def test_diamond_topology():
    topo = DiamondTopo()

    assert len(topo.hosts()) == 2
    assert len(topo.switches()) == 4
    assert len(topo.links()) == 6
