from mininet.topo import Topo

from common.config import (
    HOSTS as CONFIG_HOSTS,
    SWITCHES as CONFIG_SWITCHES,
    LINKS as CONFIG_LINKS,
)


class DiamondTopo(Topo):
    """Two-host diamond topology with two alternative paths."""

    def build(self):
        for host in CONFIG_HOSTS:
            self.addHost(host)

        for switch in CONFIG_SWITCHES:
            self.addSwitch(switch)

        for node1, node2, bandwidth_mbps in CONFIG_LINKS:
            self.addLink(
                node1,
                node2,
                bw=bandwidth_mbps
            )


topos = {
    "diamond": lambda: DiamondTopo()
}
