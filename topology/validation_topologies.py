
from mininet.topo import Topo


class ValidationTopo(Topo):
    """Base class for independent topology validation networks."""

    def add_network(self, hosts, switches, links, configure_bandwidth=True):
        for host in hosts:
            self.addHost(host)

        for switch in switches:
            self.addSwitch(switch)

        for node1, node2 in links:
            if configure_bandwidth:
                self.addLink(node1, node2, bw=10)
            else:
                self.addLink(node1, node2)


class LinearTopo(ValidationTopo):
    """
    Three hosts connected through a linear chain of five switches.
    No alternate inter-switch path exists.
    """

    def build(self, configure_bandwidth=True):
        switches = [f"s{i}" for i in range(1, 6)]

        links = [
            ("h1", "s1"),
            ("h2", "s5"),
            ("h3", "s3"),
            ("s1", "s2"),
            ("s2", "s3"),
            ("s3", "s4"),
            ("s4", "s5"),
        ]

        self.add_network(
            ["h1", "h2", "h3"],
            switches,
            links,
            configure_bandwidth,
        )


class RingTopo(ValidationTopo):
    """
    Six-switch ring with three attached hosts.
    Two directions are available between switches.
    """

    def build(self, configure_bandwidth=True):
        switches = [f"s{i}" for i in range(1, 7)]

        links = [
            ("h1", "s1"),
            ("h2", "s4"),
            ("h3", "s6"),
            ("s1", "s2"),
            ("s2", "s3"),
            ("s3", "s4"),
            ("s4", "s5"),
            ("s5", "s6"),
            ("s6", "s1"),
        ]

        self.add_network(
            ["h1", "h2", "h3"],
            switches,
            links,
            configure_bandwidth,
        )


class MeshTopo(ValidationTopo):
    """
    Six-switch mesh-like topology with multiple redundant paths.
    """

    def build(self, configure_bandwidth=True):
        switches = [f"s{i}" for i in range(1, 7)]

        links = [
            ("h1", "s1"),
            ("h2", "s6"),
            ("h3", "s3"),

            # Horizontal links
            ("s1", "s2"),
            ("s2", "s3"),
            ("s4", "s5"),
            ("s5", "s6"),

            # Vertical links
            ("s1", "s4"),
            ("s2", "s5"),
            ("s3", "s6"),

            # Additional redundant links
            ("s1", "s5"),
            ("s2", "s6"),
        ]

        self.add_network(
            ["h1", "h2", "h3"],
            switches,
            links,
            configure_bandwidth,
        )


topos = {
    "linear": lambda: LinearTopo(),
    "ring": lambda: RingTopo(),
    "mesh": lambda: MeshTopo(),
}
