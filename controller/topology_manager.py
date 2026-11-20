
import networkx as nx
from os_ken.topology import api


class TopologyManager:

    def __init__(self, app):
        self.app = app
        self.graph = nx.DiGraph()
        self.links = {}

    def discover_topology(self):
        """Discover switches and links from OS-Ken."""

        switches = api.get_switch(self.app, None)
        links = api.get_link(self.app, None)

        self.graph.clear()
        self.links.clear()

        for switch in switches:
            dpid = switch.dp.id
            self.graph.add_node(dpid, type="switch")

        for link in links:
            src = link.src
            dst = link.dst

            self.graph.add_edge(
                src.dpid,
                dst.dpid,
                src_port=src.port_no,
                dst_port=dst.port_no
            )

            self.links[(src.dpid, dst.dpid)] = src.port_no
            self.app.logger.info(
                "Port mapping: switch %s -> switch %s uses port %s",
                src.dpid,
                dst.dpid,
                src.port_no
            )

        self.app.logger.info(
            "Topology updated: %d switches, %d directed links",
            self.graph.number_of_nodes(),
            self.graph.number_of_edges()
        )

    def get_graph(self):
        return self.graph

    def get_paths(self, src, dst, k=2):
        try:
            paths = nx.shortest_simple_paths(self.graph, src, dst)
            return [
                path for _, path in zip(range(k), paths)
            ]
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return []

    def get_port(self, current_switch, next_switch):
        return self.links.get((current_switch, next_switch))
