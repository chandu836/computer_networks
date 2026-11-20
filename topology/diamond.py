
from mininet.topo import Topo

from common.config import HOSTS, SWITCHES, LINKS


class DiamondTopo(Topo):
    """Two-host diamond topology with two alternative paths."""

    def build(self):
        for host in HOSTS:
            self.addHost(host)

        for switch in SWITCHES:
            self.addSwitch(switch)

        for node1, node2, bandwidth_mbps in LINKS:
            self.addLink(
                node1,
                node2,
                bw=bandwidth_mbps
            )


topos = {
    "diamond": lambda: DiamondTopo()
}
