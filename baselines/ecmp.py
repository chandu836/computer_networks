
import networkx as nx
import hashlib


def find_ecmp_paths(graph, source, destination):

    try:
        paths = list(
            nx.all_shortest_paths(
                graph,
                source=source,
                target=destination
            )
        )
        return paths

    except nx.NetworkXNoPath:
        return []


def select_ecmp_path(paths, flow_id):

    if not paths:
        return None

    # Generate a deterministic hash for the flow
    flow_hash = hashlib.sha256(
        str(flow_id).encode()
    ).hexdigest()

    index = int(flow_hash, 16) % len(paths)

    return paths[index]
