
import networkx as nx


def find_shortest_path(graph, source, destination):
    """
    Find a shortest path using hop count.

    Returns:
        list: Shortest path from source to destination.
        None: If either node is missing or no path exists.
    """

    try:
        return nx.shortest_path(
            graph,
            source=source,
            target=destination,
        )

    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return None
