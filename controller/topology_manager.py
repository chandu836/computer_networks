
import logging
import threading

import networkx as nx
from os_ken.topology import api


class TopologyManager:
    """
    Maintains the controller's live directed switch topology.

    Each directed edge (u, v) stores:
        src_port: egress port on switch u
        dst_port: ingress port on switch v

    The graph and port map are replaced together after each discovery
    operation so consumers never observe a partially updated topology.
    """

    def __init__(self, app):
        self.app = app
        self._logger = getattr(app, "logger", logging.getLogger(__name__))
        self._lock = threading.RLock()

        self._graph = nx.DiGraph()
        self._links = {}

    @property
    def graph(self):
        """Return a snapshot; callers cannot mutate the internal graph."""
        return self.get_graph()

    @property
    def links(self):
        """Return {(src_dpid, dst_dpid): src_port} as a snapshot."""
        with self._lock:
            return dict(self._links)

    def discover_topology(self):
        """
        Fetch a complete topology snapshot from OS-Ken and replace the
        current graph and port map atomically.

        If the OS-Ken API raises an exception, preserve the last valid
        snapshot rather than replacing it with incomplete data.
        """

        switches = api.get_switch(self.app, None)
        discovered_links = api.get_link(self.app, None)

        new_graph = nx.DiGraph()
        new_links = {}

        for switch in switches:
            dpid = switch.dp.id
            new_graph.add_node(dpid, type="switch")

        for link in discovered_links:
            src = link.src
            dst = link.dst

            new_graph.add_edge(
                src.dpid,
                dst.dpid,
                src_port=src.port_no,
                dst_port=dst.port_no,
            )

            new_links[(src.dpid, dst.dpid)] = src.port_no

        # Publish both structures together.
        with self._lock:
            self._graph = new_graph
            self._links = new_links

            switch_count = self._graph.number_of_nodes()
            link_count = self._graph.number_of_edges()

        self._logger.info(
            "Topology updated: %d switches, %d directed links",
            switch_count,
            link_count,
        )

        for (src, dst), port in sorted(new_links.items()):
            self._logger.debug(
                "Port mapping: switch %s -> switch %s uses port %s",
                src,
                dst,
                port,
            )

        return self.get_graph()

    def get_graph(self):
        """Return a copy of the current directed topology."""
        with self._lock:
            return self._graph.copy()

    def get_paths(self, src, dst, k=2):
        """
        Return up to k shortest simple directed paths.

        Example:
            get_paths(1, 4, k=2)
            -> [[1, 2, 4], [1, 3, 4]]

        Returns [] if no path exists or either switch is unknown.
        """
        if k <= 0:
            return []

        with self._lock:
            graph_snapshot = self._graph.copy()

        try:
            paths = nx.shortest_simple_paths(
                graph_snapshot,
                source=src,
                target=dst,
            )
            result = []

            for path in paths:
                result.append(path)
                if len(result) >= k:
                    break

            return result

        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return []

    def get_port(self, current_switch, next_switch):
        """
        Return the egress port on current_switch towards next_switch.

        Example:
            get_port(1, 2) -> 2
        """
        with self._lock:
            return self._links.get((current_switch, next_switch))

    def get_link_info(self, src, dst):
        """Return a copy of the directed edge attributes, or None."""
        with self._lock:
            if not self._graph.has_edge(src, dst):
                return None

            return dict(self._graph[src][dst])

    def has_link(self, src, dst):
        with self._lock:
            return self._graph.has_edge(src, dst)

    def get_switches(self):
        with self._lock:
            return list(self._graph.nodes())

    def get_links(self):
        with self._lock:
            return list(self._graph.edges())
