
from mininet.topo import Topo


class LargeTopo(Topo):
    """
    Larger SDN topology:
    10 switches, 4 hosts, and redundant inter-switch paths.
    """

    def build(self, configure_bandwidth=True):

        # Four hosts
        for host in ["h1", "h2", "h3", "h4"]:
            self.addHost(host)

        # Ten switches
        for i in range(1, 11):
            self.addSwitch(f"s{i}")

        # Host-switch connections
        host_links = [
            ("h1", "s1"),
            ("h2", "s10"),
            ("h3", "s3"),
            ("h4", "s8"),
        ]

        # Horizontal links: upper row
        upper_links = [
            ("s1", "s2"),
            ("s2", "s3"),
            ("s3", "s4"),
            ("s4", "s5"),
        ]

        # Horizontal links: lower row
        lower_links = [
            ("s6", "s7"),
            ("s7", "s8"),
            ("s8", "s9"),
            ("s9", "s10"),
        ]

        # Vertical connections between rows
        vertical_links = [
            ("s1", "s6"),
            ("s2", "s7"),
            ("s3", "s8"),
            ("s4", "s9"),
            ("s5", "s10"),
        ]

        # Additional redundant connections
        extra_links = [
            ("s2", "s6"),
            ("s4", "s10"),
        ]

        all_links = (
            host_links
            + upper_links
            + lower_links
            + vertical_links
            + extra_links
        )

        for node1, node2 in all_links:
            if configure_bandwidth:
                self.addLink(node1, node2, bw=10)
            else:
                self.addLink(node1, node2)


topos = {
    "large": lambda: LargeTopo()
}
