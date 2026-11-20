"""Shared fakes for the Member 5 tests (flow manager, recovery, controller)."""

import networkx as nx
from os_ken.ofproto import ofproto_v1_3, ofproto_v1_3_parser

# (src, src_port, dst, dst_port): ports as created by topology/diamond.py.
DIAMOND_LINKS = [
    (1, 2, 2, 1),
    (2, 1, 1, 2),
    (2, 2, 4, 1),
    (4, 1, 2, 2),
    (1, 3, 3, 1),
    (3, 1, 1, 3),
    (3, 2, 4, 2),
    (4, 2, 3, 2),
]

H1_PORT = 1     # s1 port towards h1
H2_PORT = 3     # s4 port towards h2

UPPER = [1, 2, 4]
LOWER = [1, 3, 4]


class FakeDatapath:
    """
    Stands in for an OS-Ken datapath.

    Messages are built with the real OpenFlow 1.3 parser and serialized,
    so a wrong match field or action fails the test.
    """

    ofproto = ofproto_v1_3
    ofproto_parser = ofproto_v1_3_parser

    def __init__(self, dpid):
        self.id = dpid
        self.sent = []
        self._xid = 0

    def send_msg(self, msg):
        self._xid += 1
        msg.set_xid(self._xid)
        msg.serialize()
        self.sent.append(msg)
        return True

    def flow_mods(self):
        return [
            msg for msg in self.sent
            if isinstance(msg, ofproto_v1_3_parser.OFPFlowMod)
        ]

    def packet_outs(self):
        return [
            msg for msg in self.sent
            if isinstance(msg, ofproto_v1_3_parser.OFPPacketOut)
        ]

    def clear(self):
        self.sent = []


def out_ports(msg):
    """Output ports of a flow-mod or packet-out."""
    if isinstance(msg, ofproto_v1_3_parser.OFPPacketOut):
        actions = msg.actions
    else:
        actions = [
            action
            for instruction in msg.instructions
            for action in instruction.actions
        ]
    return [action.port for action in actions]


def is_add(msg):
    return msg.command == ofproto_v1_3.OFPFC_ADD


def is_delete(msg):
    return msg.command == ofproto_v1_3.OFPFC_DELETE_STRICT


class FakeTopologyManager:
    """The part of TopologyManager used by the recovery manager."""

    def __init__(self, links=None):
        self._graph = nx.DiGraph()
        self._ports = {}
        for src, src_port, dst, dst_port in (links or DIAMOND_LINKS):
            self._graph.add_edge(src, dst, src_port=src_port, dst_port=dst_port)
            self._ports[(src, dst)] = src_port

    def get_graph(self):
        return self._graph.copy()

    def get_port(self, current_switch, next_switch):
        return self._ports.get((current_switch, next_switch))

    def remove_link(self, a, b):
        for edge in ((a, b), (b, a)):
            if self._graph.has_edge(*edge):
                self._graph.remove_edge(*edge)
            self._ports.pop(edge, None)


def diamond_hops(path, host_port=H2_PORT):
    """Hops of a diamond switch path, e.g. diamond_hops([1, 2, 4])."""
    ports = {(src, dst): port for src, port, dst, _ in DIAMOND_LINKS}
    hops = [
        {"dpid": src, "out_port": ports[(src, dst)]}
        for src, dst in zip(path[:-1], path[1:])
    ]
    hops.append({"dpid": path[-1], "out_port": host_port})
    return hops


def tcp_match(src_port=40000, dst_port=5001):
    return {
        "eth_type": 0x0800,
        "ipv4_src": "10.0.0.1",
        "ipv4_dst": "10.0.0.2",
        "ip_proto": 6,
        "tcp_src": src_port,
        "tcp_dst": dst_port,
    }
