
import networkx as nx

from common.config import HOSTS, SWITCHES, LINKS


def create_network_graph():
    """
    Create a NetworkX representation of the Mininet topology.

    The graph is undirected. Each edge stores its configured
    bandwidth and a default hop-count weight.

    Queue measurements and dynamic routing costs should be
    added by the corresponding monitoring/routing modules.
    """

    graph = nx.Graph()

    graph.add_nodes_from(HOSTS, node_type="host")
    graph.add_nodes_from(SWITCHES, node_type="switch")

    for node1, node2, bandwidth_mbps in LINKS:
        graph.add_edge(
            node1,
            node2,
            bandwidth_mbps=bandwidth_mbps,
            bandwidth_bps=bandwidth_mbps * 1_000_000,
            weight=1,
            is_up=True
        )

    return graph
