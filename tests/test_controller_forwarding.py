"""
Controller forwarding and recovery, driven with fake switches.

The real SDNController handlers run against FakeDatapath objects, so the
OpenFlow messages they send are built and serialized by OS-Ken. Packets
are not forwarded by a real switch here; traffic/recovery_demo.py covers
that on Mininet.
"""

from types import SimpleNamespace

import pytest
from os_ken.controller.handler import DEAD_DISPATCHER, MAIN_DISPATCHER
from os_ken.lib.packet import arp, ethernet, icmp, ipv4, packet, tcp, udp
from os_ken.ofproto import ofproto_v1_3
from os_ken.topology import event

from recovery.flow_manager import switch_path
from tests.recovery_helpers import (
    DIAMOND_LINKS,
    H1_PORT,
    H2_PORT,
    LOWER,
    UPPER,
    FakeDatapath,
    is_add,
    is_delete,
    out_ports,
)

H1 = SimpleNamespace(mac="00:00:00:00:00:01", ip="10.0.0.1", dpid=1, port=H1_PORT)
H2 = SimpleNamespace(mac="00:00:00:00:00:02", ip="10.0.0.2", dpid=4, port=H2_PORT)
BROADCAST = "ff:ff:ff:ff:ff:ff"


def make_link(src, src_port, dst, dst_port):
    return SimpleNamespace(
        src=SimpleNamespace(dpid=src, port_no=src_port),
        dst=SimpleNamespace(dpid=dst, port_no=dst_port),
    )


class Network:
    """SDNController connected to four fake diamond switches."""

    def __init__(self, monkeypatch, mode="proposed", recovery="auto"):
        monkeypatch.setenv("SDN_ROUTING_MODE", mode)
        monkeypatch.setenv("SDN_RECOVERY", recovery)
        monkeypatch.setenv("SDN_STABILITY_INTERVAL", "3")
        monkeypatch.delenv("SDN_LOG_DIR", raising=False)

        from controller.ryu_app import SDNController

        self.links = [make_link(*link) for link in DIAMOND_LINKS]
        self.datapaths = {dpid: FakeDatapath(dpid) for dpid in (1, 2, 3, 4)}

        ports = {1: [1, 2, 3], 2: [1, 2], 3: [1, 2], 4: [1, 2, 3]}
        switches = [
            SimpleNamespace(
                dp=SimpleNamespace(id=dpid),
                ports=[SimpleNamespace(port_no=port) for port in ports[dpid]],
            )
            for dpid in (1, 2, 3, 4)
        ]

        monkeypatch.setattr(
            "os_ken.topology.api.get_switch", lambda app, dpid: switches
        )
        monkeypatch.setattr(
            "os_ken.topology.api.get_link", lambda app, dpid: list(self.links)
        )

        self.app = SDNController()

        for datapath in self.datapaths.values():
            self.app.switch_state_handler(
                SimpleNamespace(datapath=datapath, state=MAIN_DISPATCHER)
            )
        for link in self.links:
            self.app.topology_event_handler(event.EventLinkAdd(link))

    # -- helpers ------------------------------------------------------
    def packet_in(self, dpid, in_port, data):
        self.app.packet_in_handler(SimpleNamespace(msg=SimpleNamespace(
            datapath=self.datapaths[dpid],
            match={"in_port": in_port},
            data=data,
        )))

    def port_status(self, dpid, port_no, down):
        desc = SimpleNamespace(
            port_no=port_no,
            state=ofproto_v1_3.OFPPS_LINK_DOWN if down else ofproto_v1_3.OFPPS_LIVE,
            config=ofproto_v1_3.OFPPC_PORT_DOWN if down else 0,
        )
        self.app.port_status_handler(SimpleNamespace(msg=SimpleNamespace(
            datapath=self.datapaths[dpid],
            desc=desc,
            reason=ofproto_v1_3.OFPPR_MODIFY,
        )))

    def link_down(self, a, b):
        ports = {(src, dst): port for src, port, dst, _ in DIAMOND_LINKS}
        self.port_status(a, ports[(a, b)], True)
        self.port_status(b, ports[(b, a)], True)

        for link in list(self.links):
            if {link.src.dpid, link.dst.dpid} == {a, b}:
                self.links.remove(link)
                self.app.topology_event_handler(event.EventLinkDelete(link))

    def link_up(self, a, b):
        ports = {(src, dst): port for src, port, dst, _ in DIAMOND_LINKS}
        self.port_status(a, ports[(a, b)], False)
        self.port_status(b, ports[(b, a)], False)

        for spec in DIAMOND_LINKS:
            if {spec[0], spec[2]} == {a, b}:
                link = make_link(*spec)
                self.links.append(link)
                self.app.topology_event_handler(event.EventLinkAdd(link))

    def learn_hosts(self):
        self.packet_in(H1.dpid, H1.port, arp_request(H1, H2))
        self.packet_in(H2.dpid, H2.port, arp_reply(H2, H1))
        self.clear()

    def clear(self):
        for datapath in self.datapaths.values():
            datapath.clear()

    def path(self, flow_id):
        hops = self.app.flow_manager.get_path(flow_id)
        return switch_path(hops) if hops else None

    def flow_mods(self, dpid):
        return self.datapaths[dpid].flow_mods()

    def packet_outs(self, dpid):
        return self.datapaths[dpid].packet_outs()


def build(*protocols):
    pkt = packet.Packet()
    for protocol in protocols:
        pkt.add_protocol(protocol)
    pkt.serialize()
    return bytes(pkt.data)


def arp_request(src, dst):
    return build(
        ethernet.ethernet(dst=BROADCAST, src=src.mac, ethertype=0x0806),
        arp.arp(opcode=arp.ARP_REQUEST, src_mac=src.mac, src_ip=src.ip,
                dst_ip=dst.ip),
    )


def arp_reply(src, dst):
    return build(
        ethernet.ethernet(dst=dst.mac, src=src.mac, ethertype=0x0806),
        arp.arp(opcode=arp.ARP_REPLY, src_mac=src.mac, src_ip=src.ip,
                dst_mac=dst.mac, dst_ip=dst.ip),
    )


def tcp_packet(src, dst, src_port=40000, dst_port=5001):
    return build(
        ethernet.ethernet(dst=dst.mac, src=src.mac, ethertype=0x0800),
        ipv4.ipv4(src=src.ip, dst=dst.ip, proto=6),
        tcp.tcp(src_port=src_port, dst_port=dst_port),
    )


def udp_packet(src, dst, src_port=40000, dst_port=5001):
    return build(
        ethernet.ethernet(dst=dst.mac, src=src.mac, ethertype=0x0800),
        ipv4.ipv4(src=src.ip, dst=dst.ip, proto=17),
        udp.udp(src_port=src_port, dst_port=dst_port),
    )


def icmp_packet(src, dst):
    return build(
        ethernet.ethernet(dst=dst.mac, src=src.mac, ethertype=0x0800),
        ipv4.ipv4(src=src.ip, dst=dst.ip, proto=1),
        icmp.icmp(type_=icmp.ICMP_ECHO_REQUEST, data=icmp.echo(id_=1, seq=1)),
    )


def tcp_flow(src_port=40000, dst_port=5001):
    return (H1.ip, H2.ip, 6, src_port, dst_port)


@pytest.fixture
def net(monkeypatch):
    return Network(monkeypatch)


def test_arp_request_is_flooded_to_host_ports_only(net):
    net.packet_in(H1.dpid, H1.port, arp_request(H1, H2))

    assert net.app.hosts[H1.mac] == (1, 1)
    # Only s4 has another host-facing port. Nothing is sent on switch links.
    (flood,) = net.packet_outs(4)
    assert out_ports(flood) == [H2_PORT]
    for dpid in (1, 2, 3):
        assert net.packet_outs(dpid) == []


def test_arp_reply_goes_straight_to_the_known_host(net):
    net.packet_in(H1.dpid, H1.port, arp_request(H1, H2))
    net.clear()

    net.packet_in(H2.dpid, H2.port, arp_reply(H2, H1))

    assert net.app.hosts[H2.mac] == (4, 3)
    (reply,) = net.packet_outs(1)
    assert out_ports(reply) == [H1_PORT]
    assert all(net.flow_mods(dpid) == [] for dpid in (1, 2, 3, 4))


def test_packets_from_switch_links_do_not_teach_host_locations(net):
    # A broadcast that shows up on a switch link is dropped, not flooded.
    net.packet_in(2, 1, arp_request(H1, H2))

    assert H1.mac not in net.app.hosts
    assert all(net.packet_outs(dpid) == [] for dpid in (1, 2, 3, 4))


def test_known_host_is_not_relearned_somewhere_else(net):
    net.app._link_ports.discard((2, 1))     # link not discovered yet
    net.packet_in(H1.dpid, H1.port, arp_request(H1, H2))
    net.clear()

    net.packet_in(2, 1, arp_request(H1, H2))

    assert net.app.hosts[H1.mac] == (1, 1)
    assert all(net.packet_outs(dpid) == [] for dpid in (1, 2, 3, 4))


def test_host_on_a_discovered_switch_link_is_forgotten(net):
    net.app.hosts["00:00:00:00:00:09"] = (2, 1)

    net.app.topology_event_handler(event.EventLinkAdd(net.links[0]))

    assert "00:00:00:00:00:09" not in net.app.hosts


def test_tcp_flow_gets_five_tuple_rules_along_one_path(net):
    net.learn_hosts()

    net.packet_in(H1.dpid, H1.port, tcp_packet(H1, H2))

    path = net.path(tcp_flow())
    assert path in (UPPER, LOWER)
    middle = path[1]
    unused = 3 if middle == 2 else 2

    for dpid in path:
        (rule,) = net.flow_mods(dpid)
        assert is_add(rule)
        assert rule.priority == net.app.FLOW_PRIORITY
        assert rule.idle_timeout == net.app.FLOW_IDLE_TIMEOUT
        assert dict(rule.match.items()) == {
            "eth_type": 0x0800,
            "ipv4_src": H1.ip,
            "ipv4_dst": H2.ip,
            "ip_proto": 6,
            "tcp_src": 40000,
            "tcp_dst": 5001,
        }

    assert out_ports(net.flow_mods(1)[0]) == [middle]     # port 2 -> s2, 3 -> s3
    assert out_ports(net.flow_mods(middle)[0]) == [2]
    assert out_ports(net.flow_mods(4)[0]) == [H2_PORT]
    assert net.flow_mods(unused) == []

    # The first packet itself is delivered at the destination switch.
    (delivery,) = net.packet_outs(4)
    assert out_ports(delivery) == [H2_PORT]


def test_udp_and_icmp_matches(net):
    net.learn_hosts()

    net.packet_in(H1.dpid, H1.port, udp_packet(H1, H2, 1111, 2222))
    net.packet_in(H1.dpid, H1.port, icmp_packet(H1, H2))

    udp_rule, icmp_rule = net.flow_mods(1)
    assert udp_rule.match["udp_src"] == 1111
    assert udp_rule.match["udp_dst"] == 2222
    assert udp_rule.priority == net.app.FLOW_PRIORITY

    assert dict(icmp_rule.match.items()) == {
        "eth_type": 0x0800,
        "ipv4_src": H1.ip,
        "ipv4_dst": H2.ip,
        "ip_proto": 1,
    }
    assert icmp_rule.priority == net.app.ADDRESS_ONLY_PRIORITY
    assert net.path((H1.ip, H2.ip, 1, None, None)) in (UPPER, LOWER)


def test_fragments_are_matched_without_ports(net):
    net.learn_hosts()
    fragment = build(
        ethernet.ethernet(dst=H2.mac, src=H1.mac, ethertype=0x0800),
        ipv4.ipv4(src=H1.ip, dst=H2.ip, proto=17, flags=1),
        udp.udp(src_port=1111, dst_port=2222),
    )

    net.packet_in(H1.dpid, H1.port, fragment)

    (rule,) = net.flow_mods(1)
    assert "udp_src" not in rule.match
    assert rule.priority == net.app.ADDRESS_ONLY_PRIORITY


def test_both_directions_are_separate_flows(net):
    net.learn_hosts()

    net.packet_in(H1.dpid, H1.port, tcp_packet(H1, H2, 40000, 5001))
    net.packet_in(H2.dpid, H2.port, tcp_packet(H2, H1, 5001, 40000))

    forward = net.path((H1.ip, H2.ip, 6, 40000, 5001))
    reverse = net.path((H2.ip, H1.ip, 6, 5001, 40000))
    assert forward[0] == 1 and forward[-1] == 4
    assert reverse[0] == 4 and reverse[-1] == 1


def test_flows_are_spread_over_both_paths(net):
    net.learn_hosts()

    for src_port in range(40000, 40040):
        net.packet_in(H1.dpid, H1.port, tcp_packet(H1, H2, src_port))

    paths = [net.path(tcp_flow(src_port)) for src_port in range(40000, 40040)]
    assert UPPER in paths and LOWER in paths


def test_unknown_destination_is_flooded_without_rules(net):
    net.packet_in(H1.dpid, H1.port, tcp_packet(H1, H2))

    assert all(net.flow_mods(dpid) == [] for dpid in (1, 2, 3, 4))
    assert out_ports(net.packet_outs(4)[0]) == [H2_PORT]


def test_lldp_and_ipv6_are_ignored(net):
    lldp = build(ethernet.ethernet(dst="01:80:c2:00:00:0e", src=H1.mac,
                                   ethertype=0x88cc))
    ipv6 = build(ethernet.ethernet(dst="33:33:00:00:00:02", src=H1.mac,
                                   ethertype=0x86dd))

    net.packet_in(1, 2, lldp)
    net.packet_in(H1.dpid, H1.port, ipv6)

    assert net.app.hosts == {}
    assert all(net.datapaths[dpid].sent == [] for dpid in (1, 2, 3, 4))


def test_table_miss_in_the_middle_repairs_the_rule(net):
    net.learn_hosts()
    net.packet_in(H1.dpid, H1.port, tcp_packet(H1, H2))
    path = net.path(tcp_flow())
    middle = path[1]
    net.clear()

    net.packet_in(middle, 1, tcp_packet(H1, H2))

    (rule,) = net.flow_mods(middle)
    assert is_add(rule) and out_ports(rule) == [2]
    assert net.flow_mods(1) == [] and net.flow_mods(4) == []
    assert out_ports(net.packet_outs(4)[0]) == [H2_PORT]
    assert net.path(tcp_flow()) == path


def test_link_failure_reroutes_active_flows(net):
    net.learn_hosts()
    for src_port in range(40000, 40020):
        net.packet_in(H1.dpid, H1.port, tcp_packet(H1, H2, src_port))

    before = {p: net.path(tcp_flow(p)) for p in range(40000, 40020)}
    on_upper = [p for p, path in before.items() if path == UPPER]
    assert on_upper
    net.clear()

    net.link_down(1, 2)

    assert all(net.path(tcp_flow(p)) == LOWER for p in range(40000, 40020))
    assert net.app.recovery_manager.flows_rerouted == len(on_upper)

    # s1 was rewritten once per moved flow, s2 lost those rules.
    assert len(net.flow_mods(1)) == len(on_upper)
    assert all(out_ports(rule) == [3] for rule in net.flow_mods(1))
    assert len(net.flow_mods(2)) == len(on_upper)
    assert all(is_delete(rule) for rule in net.flow_mods(2))

    # New flows avoid the failed link as well.
    net.packet_in(H1.dpid, H1.port, tcp_packet(H1, H2, 50000))
    assert net.path(tcp_flow(50000)) == LOWER


def test_no_path_installs_a_short_drop_rule(net):
    net.learn_hosts()
    net.link_down(1, 2)
    net.link_down(1, 3)
    net.clear()

    net.packet_in(H1.dpid, H1.port, tcp_packet(H1, H2))

    (rule,) = net.flow_mods(1)
    assert rule.instructions == []
    assert rule.hard_timeout == net.app.NO_ROUTE_DROP_TIMEOUT
    assert net.path(tcp_flow()) is None
    assert net.packet_outs(4) == []


def test_restoration_after_stability_interval(net):
    net.learn_hosts()
    for src_port in range(40000, 40020):
        net.packet_in(H1.dpid, H1.port, tcp_packet(H1, H2, src_port))
    on_upper = [p for p in range(40000, 40020) if net.path(tcp_flow(p)) == UPPER]

    net.link_down(1, 2)
    net.link_up(1, 2)

    recovery = net.app.recovery_manager
    # The detour is loaded, the recovered path is idle.
    recovery.path_cost = lambda path: 5.0 if list(path) == LOWER else 0.0
    start = recovery._clock()

    assert recovery.process_pending(now=start + 1.0) == []
    restored = recovery.process_pending(now=start + 4.0)

    assert sorted(restored) == sorted(tcp_flow(p) for p in on_upper)
    assert all(net.path(tcp_flow(p)) == UPPER for p in on_upper)


def test_flow_removed_ends_the_flow(net):
    net.learn_hosts()
    net.packet_in(H1.dpid, H1.port, tcp_packet(H1, H2))
    cookie = net.flow_mods(1)[0].cookie

    def removed(dpid, reason):
        net.app.flow_removed_handler(SimpleNamespace(msg=SimpleNamespace(
            datapath=net.datapaths[dpid], cookie=cookie, reason=reason,
        )))

    removed(1, ofproto_v1_3.OFPRR_DELETE)
    assert net.path(tcp_flow()) is not None

    removed(1, ofproto_v1_3.OFPRR_IDLE_TIMEOUT)
    assert net.path(tcp_flow()) is None


def test_disconnected_switch_is_dropped_from_flow_state(net):
    net.learn_hosts()
    net.packet_in(H1.dpid, H1.port, tcp_packet(H1, H2))

    old = net.datapaths[1]
    net.app.switch_state_handler(
        SimpleNamespace(datapath=old, state=DEAD_DISPATCHER)
    )

    assert not net.app.flow_manager.has_datapath(1)
    assert net.path(tcp_flow()) is None

    # A stale DEAD event for an old connection must not remove the new one.
    new = FakeDatapath(1)
    net.app.switch_state_handler(
        SimpleNamespace(datapath=new, state=MAIN_DISPATCHER)
    )
    net.app.switch_state_handler(
        SimpleNamespace(datapath=old, state=DEAD_DISPATCHER)
    )
    assert net.app.flow_manager.has_datapath(1)


@pytest.mark.parametrize("mode", ["shortest_path", "ecmp", "queue_aware"])
def test_baselines_route_but_do_not_recover_proactively(monkeypatch, mode):
    net = Network(monkeypatch, mode=mode)
    assert not net.app.recovery_enabled
    net.learn_hosts()
    for src_port in range(40000, 40010):
        net.packet_in(H1.dpid, H1.port, tcp_packet(H1, H2, src_port))

    before = {p: net.path(tcp_flow(p)) for p in range(40000, 40010)}
    net.link_down(1, 2)

    # Existing rules are left alone ...
    assert {p: net.path(tcp_flow(p)) for p in before} == before
    # ... but the next flowlet is routed around the failure.
    net.packet_in(H1.dpid, H1.port, tcp_packet(H1, H2, 40000))
    assert net.path(tcp_flow(40000)) == LOWER


def test_recovery_can_be_forced_on_for_a_baseline(monkeypatch):
    net = Network(monkeypatch, mode="shortest_path", recovery="on")
    net.learn_hosts()
    net.packet_in(H1.dpid, H1.port, tcp_packet(H1, H2))
    assert net.path(tcp_flow()) == UPPER

    net.link_down(1, 2)

    assert net.path(tcp_flow()) == LOWER


def test_invalid_routing_mode_is_rejected(monkeypatch):
    with pytest.raises(ValueError):
        Network(monkeypatch, mode="fastest")
