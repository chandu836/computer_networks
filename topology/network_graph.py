
import networkx as nx


def create_network_graph():

    G = nx.Graph()

    # Add network nodes
    G.add_nodes_from(["h1", "h2", "s1", "s2", "s3", "s4"])

    # Add links between nodes
    G.add_edges_from([
        ("h1", "s1"),
        ("s1", "s2"),
        ("s2", "s4"),
        ("s1", "s3"),
        ("s3", "s4"),
        ("s4", "h2")
    ])

    return G
