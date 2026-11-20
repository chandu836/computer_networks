
import networkx as nx



def simulate_link_failure(graph, failed_link, source, destination):

    # Create a copy so the original graph remains unchanged
    modified_graph = graph.copy()

    # Check whether the failed link exists
    u, v = failed_link

    if not modified_graph.has_edge(u, v):
        raise ValueError(f"Link {failed_link} does not exist")

    # Remove the failed link
    modified_graph.remove_edge(u, v)

    # Find an alternate route
    try:
        new_path = nx.shortest_path(
            modified_graph,
            source=source,
            target=destination
        )
        return modified_graph, new_path

    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return modified_graph, None



def set_mininet_link_state(net, node1, node2, state):

    """
    Bring a Mininet link up or down.

    state must be either 'up' or 'down'.
    """

    if state not in ("up", "down"):
        raise ValueError("State must be 'up' or 'down'")

    net.configLinkStatus(node1, node2, state)
