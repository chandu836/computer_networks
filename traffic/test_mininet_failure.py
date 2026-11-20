
from functools import partial

from mininet.net import Mininet
from mininet.node import OVSSwitch
from mininet.link import Link

from topology.diamond import DiamondTopo
from traffic.failure_scenarios import set_mininet_link_state


def link_is_up(net, node1, node2):

    links = net.linksBetween(
        net.get(node1),
        net.get(node2)
    )

    if not links:
        raise RuntimeError(
            f"No link found between {node1} and {node2}"
        )

    return all(
        link.intf1.isUp() and link.intf2.isUp()
        for link in links
    )


def main():

    net = Mininet(
        topo=DiamondTopo(),
        switch=partial(OVSSwitch, datapath="user"),
        controller=None,
        link=Link,
        autoSetMacs=True
    )

    try:
        print("\nStarting diamond topology...")
        net.start()

        print("\nTEST 1: Initial link status")
        assert link_is_up(net, "s1", "s2")
        print("PASS: s1-s2 is initially UP")

        print("\nTEST 2: Disable link s1-s2")
        set_mininet_link_state(
            net, "s1", "s2", "down"
        )

        assert not link_is_up(net, "s1", "s2")
        print("PASS: s1-s2 is DOWN")

        print("\nTEST 3: Restore link s1-s2")
        set_mininet_link_state(
            net, "s1", "s2", "up"
        )

        assert link_is_up(net, "s1", "s2")
        print("PASS: s1-s2 is UP again")

        print("\nAll physical link-control tests passed.")

    finally:
        print("\nStopping Mininet...")
        net.stop()


if __name__ == "__main__":
    main()
