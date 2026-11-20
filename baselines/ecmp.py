
import hashlib
import networkx as nx


def find_ecmp_paths(graph, source, destination):
    """
    Find all equal-cost shortest paths using hop count.

    Returns:
        list: All shortest paths.
        []: If either node is missing or no path exists.
    """

    try:
        return list(
            nx.all_shortest_paths(
                graph,
                source=source,
                target=destination,
            )
        )

    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return []


def select_ecmp_path(paths, flow_id):
    """
    Select a deterministic path for a flow using SHA-256 hashing.
    """

    if not paths:
        return None

    flow_hash = hashlib.sha256(
        str(flow_id).encode("utf-8")
    ).hexdigest()

    index = int(flow_hash, 16) % len(paths)

    return paths[index]
