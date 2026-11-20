"""
Unit tests for controller/queue_monitor.py.

These tests do not need Mininet, tc or root: tc output is supplied as
recorded text/JSON through a fake runner. The live check on a Mininet VM is
traffic/queue_monitor_demo.py.
"""

import json
import threading

import pytest

from controller.queue_monitor import (
    MonitoredLink,
    QueueConfigurator,
    QueueMonitor,
    TcError,
    TcStatsReader,
    build_link_map_from_config,
    build_link_map_from_net,
    build_link_map_from_topology_manager,
    build_link_map_from_topo,
    calculate_throughput_bps,
    calculate_utilization,
    default_diamond_link_map,
    estimate_queueing_delay_ms,
    estimate_queueing_delay_s,
    format_stats_table,
    link_key,
    main,
    normalize_link_map,
    parse_switch_id,
    parse_tc_json,
    parse_tc_output,
    parse_tc_text,
    summarize_interface,
)
from common.models import LinkMetrics


# ---------------------------------------------------------------------------
# Sample tc output (format of iproute2 `tc -s qdisc show` with Mininet TCLink)
# ---------------------------------------------------------------------------

TC_TEXT_HTB_NETEM = """\
qdisc htb 5: dev s1-eth2 root refcnt 2 r2q 10 default 0x1 direct_packets_stat 0 direct_qlen 1000
 Sent 7590060 bytes 5013 pkt (dropped 112, overlimits 9530 requeues 0)
 backlog 101026b 67p requeues 0
qdisc netem 10: dev s1-eth2 parent 5:1 limit 1000
 Sent 7590060 bytes 5013 pkt (dropped 112, overlimits 0 requeues 0)
 backlog 101026b 67p requeues 0
qdisc htb 5: dev s1-eth3 root refcnt 2 r2q 10 default 0x1 direct_packets_stat 0 direct_qlen 1000
 Sent 0 bytes 0 pkt (dropped 0, overlimits 0 requeues 0)
 backlog 0b 0p requeues 0
qdisc ingress ffff: dev s1-eth3 parent ffff:fff1 ----------------
 Sent 999 bytes 9 pkt (dropped 0, overlimits 0 requeues 0)
 backlog 0b 0p requeues 0
"""

TC_JSON = json.dumps([
    {"kind": "htb", "handle": "5:", "dev": "s1-eth2", "root": True, "refcnt": 2,
     "bytes": 1000, "packets": 10, "drops": 1, "overlimits": 3, "requeues": 0,
     "backlog": 12000, "qlen": 8},
    {"kind": "netem", "handle": "10:", "dev": "s1-eth2", "parent": "5:1",
     "bytes": 1000, "packets": 10, "drops": 1, "overlimits": 0, "requeues": 0,
     "backlog": 12000, "qlen": 8},
    {"kind": "noqueue", "handle": "0:", "dev": "lo", "root": True,
     "bytes": 0, "packets": 0, "drops": 0, "overlimits": 0, "requeues": 0,
     "backlog": 0, "qlen": 0},
])


def tc_json_for(devices):
    """Build tc -j output: devices = {intf: (sent_bytes, backlog_bytes, drops)}."""
    items = []
    for dev, (sent, backlog, drops) in devices.items():
        items.append({"kind": "htb", "handle": "5:", "dev": dev, "root": True,
                      "bytes": sent, "packets": sent // 1500, "drops": drops,
                      "overlimits": 0, "requeues": 0,
                      "backlog": backlog, "qlen": backlog // 1500})
    return json.dumps(items)


class FakeReader:
    """Stands in for TcStatsReader: returns queued outputs in order."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0

    def read(self):
        self.calls += 1
        item = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        if isinstance(item, Exception):
            raise item
        from controller.queue_monitor import group_by_device
        return group_by_device(parse_tc_output(item))


class FakeClock:
    def __init__(self, start=100.0):
        self.now = start

    def __call__(self):
        return self.now


# ---------------------------------------------------------------------------
# Identifiers and link maps
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [(1, 1), ("1", 1), ("s1", 1), ("S12", 12), (" s3 ", 3)])
def test_parse_switch_id(raw, expected):
    assert parse_switch_id(raw) == expected


@pytest.mark.parametrize("raw", ["h1", "s1-eth2", "", None, True])
def test_parse_switch_id_rejects_invalid(raw):
    with pytest.raises(ValueError):
        parse_switch_id(raw)


def test_link_key_mixed_styles():
    assert link_key("s1", 2) == (1, 2)


def test_default_diamond_map_matches_addlink_order():
    link_map = default_diamond_link_map()
    assert len(link_map) == 8  # 4 inter-switch links, both directions
    assert link_map[(1, 2)].interface == "s1-eth2"
    assert link_map[(2, 1)].interface == "s2-eth1"
    assert link_map[(1, 3)].interface == "s1-eth3"
    assert link_map[(3, 4)].interface == "s3-eth2"
    assert link_map[(4, 3)].interface == "s4-eth2"
    assert all(link.capacity_bps == 10_000_000 for link in link_map.values())


def test_default_map_agrees_with_network_graph():
    from topology.network_graph import create_network_graph

    graph = create_network_graph()
    switch_edges = {
        tuple(sorted((parse_switch_id(u), parse_switch_id(v))))
        for u, v in graph.edges if u.startswith("s") and v.startswith("s")
    }
    mapped = {tuple(sorted(key)) for key in default_diamond_link_map()}
    assert mapped == switch_edges


def test_config_link_map_matches_static_diamond_ports():
    """Member 1's diamond.py adds links in common.config.LINKS order."""
    from common.config import LINKS

    derived = build_link_map_from_config(LINKS)
    static = default_diamond_link_map()
    assert {k: v.interface for k, v in derived.items()} == \
           {k: v.interface for k, v in static.items()}
    assert all(v.capacity_bps == 10_000_000 for v in derived.values())


def test_config_link_map_follows_order_and_bandwidth_changes():
    links = [("h1", "s1", 10), ("s1", "s3", 20), ("s1", "s2", 5), ("s2", "s3")]
    link_map = build_link_map_from_config(links)
    assert link_map[(1, 3)].interface == "s1-eth2"   # second link on s1
    assert link_map[(1, 2)].interface == "s1-eth3"
    assert link_map[(3, 2)].interface == "s3-eth2"
    assert link_map[(1, 3)].capacity_bps == 20_000_000
    assert link_map[(2, 3)].capacity_bps == 10_000_000  # default bw


def test_config_link_map_falls_back_to_static_when_empty():
    assert build_link_map_from_config([]) == default_diamond_link_map()


def test_monitor_default_map_comes_from_config():
    monitor = QueueMonitor(reader=FakeReader([TC_JSON]))
    assert monitor.get_monitored_links() == build_link_map_from_config()


class FakeTopologyManager:
    """Shape of controller.topology_manager.TopologyManager after discovery."""

    def __init__(self, links):
        self.links = links


DISCOVERED_DIAMOND = {
    (1, 2): 2, (2, 1): 1, (2, 4): 2, (4, 2): 1,
    (1, 3): 3, (3, 1): 1, (3, 4): 2, (4, 3): 2,
}


def test_link_map_from_topology_manager_matches_static():
    link_map = build_link_map_from_topology_manager(FakeTopologyManager(DISCOVERED_DIAMOND))
    assert {k: v.interface for k, v in link_map.items()} == \
           {k: v.interface for k, v in default_diamond_link_map().items()}
    assert all(v.capacity_bps == 10_000_000 for v in link_map.values())


def test_link_map_from_topology_manager_bandwidth_override_and_bad_ports():
    tm = FakeTopologyManager({(1, 2): 2, ("s2", "s1"): 1, (1, 3): None})
    link_map = build_link_map_from_topology_manager(tm, bandwidths_mbps={("s1", "s2"): 5})
    assert set(link_map) == {(1, 2), (2, 1)}
    assert link_map[(1, 2)].capacity_bps == 5_000_000



def test_sync_with_topology_manager():
    monitor = QueueMonitor(reader=FakeReader([TC_JSON]))

    # The default monitor initially has the configured diamond links.
    assert len(monitor.get_monitored_links()) == 8

    # An empty discovered topology must clear the monitored links.
    monitor.sync_with_topology_manager(FakeTopologyManager({}))
    assert monitor.get_monitored_links() == {}

    # A discovered link must be added.
    monitor.sync_with_topology_manager(
        FakeTopologyManager({(1, 2): 2})
    )
    assert set(monitor.get_monitored_links()) == {(1, 2)}

    # The monitor must follow the complete discovered diamond.
    monitor.sync_with_topology_manager(
        FakeTopologyManager(DISCOVERED_DIAMOND)
    )
    assert len(monitor.get_monitored_links()) == 8

    tm_monitor = QueueMonitor.from_topology_manager(
        FakeTopologyManager(DISCOVERED_DIAMOND),
        reader=FakeReader([TC_JSON]),
    )

    stats = tm_monitor.collect_stats()
    assert stats[(1, 2)]["queue_bytes"] == 12000



def test_real_topology_manager_interface():
    pytest.importorskip("os_ken")
    from controller.topology_manager import TopologyManager

    tm = TopologyManager(app=None)
    assert hasattr(tm, "links")
    assert build_link_map_from_topology_manager(tm) == {}


class FakeTopo:
    """Mimics mininet.topo.Topo.links(withInfo=True) for the diamond."""

    def links(self, withInfo=False):
        data = [
            ("h1", "s1", {"port1": 0, "port2": 1, "bw": 10}),
            ("s1", "s2", {"port1": 2, "port2": 1, "bw": 10}),
            ("s2", "s4", {"port1": 2, "port2": 1, "bw": 10}),
            ("s1", "s3", {"port1": 3, "port2": 1, "bw": 5}),
            ("s3", "s4", {"port1": 2, "port2": 2}),
            ("s4", "h2", {"port1": 3, "port2": 0, "bw": 10}),
        ]
        return data if withInfo else [(a, b) for a, b, _ in data]


def test_build_link_map_from_topo_matches_static_map():
    link_map = build_link_map_from_topo(FakeTopo())
    static = default_diamond_link_map()
    assert {k: v.interface for k, v in link_map.items()} == \
           {k: v.interface for k, v in static.items()}
    assert link_map[(1, 3)].capacity_bps == 5_000_000
    assert link_map[(3, 4)].capacity_bps == 10_000_000  # default bw


def test_build_link_map_from_real_diamond_topo():
    pytest.importorskip("mininet")
    from topology.diamond import DiamondTopo

    link_map = build_link_map_from_topo(DiamondTopo())
    assert {k: v.interface for k, v in link_map.items()} == \
           {k: v.interface for k, v in default_diamond_link_map().items()}


class _Node:
    def __init__(self, name):
        self.name = name


class _Intf:
    def __init__(self, node, name, bw=None):
        self.node = _Node(node)
        self.name = name
        self.params = {"bw": bw} if bw else {}


class _Link:
    def __init__(self, a, b):
        self.intf1, self.intf2 = a, b


def test_build_link_map_from_net():
    class Net:
        links = [
            _Link(_Intf("h1", "h1-eth0"), _Intf("s1", "s1-eth1")),
            _Link(_Intf("s1", "s1-eth2", bw=20), _Intf("s2", "s2-eth1", bw=20)),
        ]

    link_map = build_link_map_from_net(Net())
    assert set(link_map) == {(1, 2), (2, 1)}
    assert link_map[(2, 1)].interface == "s2-eth1"
    assert link_map[(1, 2)].capacity_bps == 20_000_000


def test_normalize_link_map_accepts_several_shapes():
    link_map = normalize_link_map({
        ("s1", "s2"): "s1-eth2",
        (2, 4): ("s2-eth2", 5),
        (3, 4): {"interface": "s3-eth2", "bw_mbps": 1},
        (4, 3): MonitoredLink(9, 9, "s4-eth2", 100.0),
    })
    assert link_map[(1, 2)].capacity_bps == 10_000_000
    assert link_map[(2, 4)].capacity_bps == 5_000_000
    assert link_map[(3, 4)].capacity_bps == 1_000_000
    assert link_map[(4, 3)].key == (4, 3)  # key wins over stale src/dst


def test_normalize_link_map_rejects_garbage():
    with pytest.raises(ValueError):
        normalize_link_map({(1, 2): 42})


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parse_tc_text_htb_netem():
    qdiscs = parse_tc_text(TC_TEXT_HTB_NETEM)
    assert len(qdiscs) == 4
    root = qdiscs[0]
    assert (root.dev, root.kind, root.is_root) == ("s1-eth2", "htb", True)
    assert root.sent_bytes == 7590060
    assert root.sent_packets == 5013
    assert root.dropped == 112
    assert root.overlimits == 9530
    assert root.backlog_bytes == 101026
    assert root.backlog_packets == 67
    assert qdiscs[1].parent == "5:1" and not qdiscs[1].is_root
    assert qdiscs[3].kind == "ingress"


def test_parse_tc_text_size_suffixes():
    out = ("qdisc pfifo_fast 0: dev eth0 root refcnt 2 bands 3\n"
           " Sent 10 bytes 1 pkt (dropped 0, overlimits 0 requeues 0)\n"
           " backlog 98Kb 67p requeues 0\n"
           "qdisc pfifo_fast 0: dev eth1 root refcnt 2 bands 3\n"
           " Sent 10 bytes 1 pkt (dropped 0, overlimits 0 requeues 0)\n"
           " backlog 2Mb 1400p requeues 0\n")
    qdiscs = parse_tc_text(out)
    assert qdiscs[0].backlog_bytes == 98 * 1024
    assert qdiscs[1].backlog_bytes == 2 * 1024 * 1024


def test_parse_tc_text_without_stats_lines():
    qdiscs = parse_tc_text("qdisc noqueue 0: dev s1-eth2 root refcnt 2\n")
    assert len(qdiscs) == 1
    assert qdiscs[0].sent_bytes == 0 and qdiscs[0].backlog_bytes == 0


def test_parse_tc_json():
    qdiscs = parse_tc_json(TC_JSON)
    assert [q.dev for q in qdiscs] == ["s1-eth2", "s1-eth2", "lo"]
    assert qdiscs[0].backlog_bytes == 12000
    assert qdiscs[0].backlog_packets == 8
    assert qdiscs[0].dropped == 1
    assert qdiscs[0].is_root and not qdiscs[1].is_root


def test_parse_tc_json_nested_stats():
    out = json.dumps([{"kind": "htb", "dev": "s2-eth2", "root": True,
                       "stats64": {"bytes": 5, "packets": 1, "drops": 2,
                                   "backlog": 300, "qlen": 1}}])
    q = parse_tc_json(out)[0]
    assert (q.sent_bytes, q.dropped, q.backlog_bytes) == (5, 2, 300)


def test_parse_tc_output_detects_format():
    assert parse_tc_output(TC_JSON)[0].backlog_bytes == 12000
    assert parse_tc_output(TC_TEXT_HTB_NETEM)[0].backlog_bytes == 101026
    assert parse_tc_output("") == []


def test_summarize_interface_uses_root_without_double_counting():
    qdiscs = [q for q in parse_tc_text(TC_TEXT_HTB_NETEM) if q.dev == "s1-eth2"]
    summary = summarize_interface(qdiscs)
    assert summary["qdisc"] == "htb"
    assert summary["queue_bytes"] == 101026      # not 2 * 101026
    assert summary["dropped_packets"] == 112
    assert summary["tx_bytes"] == 7590060


def test_summarize_interface_ignores_ingress_and_empty():
    qdiscs = [q for q in parse_tc_text(TC_TEXT_HTB_NETEM) if q.dev == "s1-eth3"]
    assert summarize_interface(qdiscs)["tx_bytes"] == 0  # ingress's 999 ignored
    assert summarize_interface([]) is None
    ingress_only = [q for q in qdiscs if q.kind == "ingress"]
    assert summarize_interface(ingress_only) is None


def test_summarize_interface_child_backlog_when_root_does_not_aggregate():
    out = json.dumps([
        {"kind": "mq", "dev": "eth0", "root": True, "bytes": 100, "backlog": 0, "drops": 0},
        {"kind": "fq_codel", "dev": "eth0", "parent": ":1", "bytes": 60, "backlog": 300, "drops": 2},
        {"kind": "fq_codel", "dev": "eth0", "parent": ":2", "bytes": 40, "backlog": 200, "drops": 1},
    ])
    summary = summarize_interface(parse_tc_json(out))
    assert summary["queue_bytes"] == 500
    assert summary["dropped_packets"] == 3


# ---------------------------------------------------------------------------
# Formulas
# ---------------------------------------------------------------------------

def test_delay_formula_example_from_project_document():
    # 125,000 bytes on a 10 Mbps link -> 100 ms
    assert estimate_queueing_delay_s(125_000, 10_000_000) == pytest.approx(0.1)
    assert estimate_queueing_delay_ms(125_000, 10_000_000) == pytest.approx(100.0)


def test_delay_formula_edge_cases():
    assert estimate_queueing_delay_ms(0, 10e6) == 0.0
    assert estimate_queueing_delay_ms(1000, None) is None
    assert estimate_queueing_delay_ms(1000, 0) is None
    assert estimate_queueing_delay_ms(None, 10e6) is None
    assert estimate_queueing_delay_ms(-1, 10e6) is None


def test_utilization_formula():
    # 625,000 bytes in 1 s on 10 Mbps = 5 Mbps = 50 %
    assert calculate_utilization(625_000, 10e6, 1.0) == pytest.approx(0.5)
    assert calculate_throughput_bps(625_000, 1.0) == pytest.approx(5e6)
    assert calculate_utilization(2_500_000, 10e6, 1.0) == 1.0
    assert calculate_utilization(2_500_000, 10e6, 1.0, cap=False) == pytest.approx(2.0)


@pytest.mark.parametrize("args", [(100, 10e6, 0), (100, 10e6, -1), (-5, 10e6, 1),
                                  (100, None, 1), (None, 10e6, 1)])
def test_utilization_invalid_inputs(args):
    assert calculate_utilization(*args) is None


# ---------------------------------------------------------------------------
# TcStatsReader
# ---------------------------------------------------------------------------

def test_reader_uses_single_json_call():
    calls = []

    def runner(cmd, timeout):
        calls.append(cmd)
        return TC_JSON

    by_device = TcStatsReader(runner=runner).read()
    assert calls == [["tc", "-s", "-j", "qdisc", "show"]]
    assert set(by_device) == {"s1-eth2", "lo"}


def test_reader_falls_back_to_text_and_remembers():
    calls = []

    def runner(cmd, timeout):
        calls.append(cmd)
        if "-j" in cmd:
            raise TcError("Option \"-j\" is unknown")
        return TC_TEXT_HTB_NETEM

    reader = TcStatsReader(runner=runner)
    assert "s1-eth2" in reader.read()
    assert "s1-eth2" in reader.read()
    assert calls == [["tc", "-s", "-j", "qdisc", "show"],
                     ["tc", "-s", "qdisc", "show"],
                     ["tc", "-s", "qdisc", "show"]]


def test_reader_missing_tc_binary_raises_tc_error():
    reader = TcStatsReader(tc_path="/nonexistent/tc-binary")
    with pytest.raises(TcError):
        reader.read()


# ---------------------------------------------------------------------------
# QueueMonitor
# ---------------------------------------------------------------------------

def make_monitor(outputs, clock=None, **kwargs):
    clock = clock or FakeClock()
    monitor = QueueMonitor(reader=FakeReader(outputs), clock=clock,
                           wall_clock=lambda: 1_700_000_000.0, **kwargs)
    return monitor, clock


def test_get_all_stats_empty_before_first_collect():
    monitor, _ = make_monitor([TC_JSON])
    assert monitor.get_all_stats() == {}
    assert monitor.get_link_delay(1, 2) is None


def test_collect_stats_agreed_output_structure():
    monitor, _ = make_monitor([TC_JSON])
    stats = monitor.collect_stats()

    assert set(stats) == set(default_diamond_link_map())
    entry = stats[(1, 2)]
    for key in ("queue_bytes", "estimated_delay_ms", "utilization"):
        assert key in entry
    assert entry["valid"] is True
    assert entry["interface"] == "s1-eth2"
    assert entry["queue_bytes"] == 12000
    assert entry["estimated_delay_ms"] == pytest.approx(9.6)   # 8*12000/10e6 s
    assert entry["utilization"] is None                         # needs 2 samples
    assert entry["dropped_packets"] == 1


def test_interfaces_missing_from_tc_are_reported_not_raised():
    monitor, _ = make_monitor([TC_JSON])
    stats = monitor.collect_stats()
    missing = stats[(2, 4)]
    assert missing["valid"] is False
    assert "s2-eth2" in missing["error"]
    assert missing["estimated_delay_ms"] is None
    assert monitor.get_link_delay(2, 4) is None
    assert monitor.get_link_delay(2, 4, default=float("inf")) == float("inf")


def test_utilization_and_throughput_from_two_samples():
    first = tc_json_for({"s1-eth2": (1_000_000, 0, 0)})
    second = tc_json_for({"s1-eth2": (1_000_000 + 812_500, 15_000, 4)})
    monitor, clock = make_monitor([first, second])

    monitor.collect_stats()
    clock.now += 1.0
    stats = monitor.collect_stats()

    entry = stats[(1, 2)]
    assert entry["throughput_bps"] == pytest.approx(6.5e6)
    assert entry["utilization"] == pytest.approx(0.65)
    assert entry["estimated_delay_ms"] == pytest.approx(12.0)
    assert entry["drops_delta"] == 4


def test_high_utilization_with_low_backlog_is_possible():
    first = tc_json_for({"s1-eth2": (0, 0, 0)})
    second = tc_json_for({"s1-eth2": (1_225_000, 0, 0)})
    monitor, clock = make_monitor([first, second])
    monitor.collect_stats()
    clock.now += 1.0
    entry = monitor.collect_stats()[(1, 2)]
    assert entry["utilization"] == pytest.approx(0.98)
    assert entry["queue_bytes"] == 0
    assert entry["estimated_delay_ms"] == 0.0


def test_counter_reset_starts_new_baseline():
    outputs = [tc_json_for({"s1-eth2": (5_000_000, 0, 10)}),
               tc_json_for({"s1-eth2": (1000, 0, 0)}),        # qdisc replaced
               tc_json_for({"s1-eth2": (1000 + 125_000, 0, 1)})]
    monitor, clock = make_monitor(outputs)
    monitor.collect_stats()
    clock.now += 1.0
    after_reset = monitor.collect_stats()[(1, 2)]
    assert after_reset["utilization"] is None
    assert after_reset["drops_delta"] is None
    clock.now += 1.0
    entry = monitor.collect_stats()[(1, 2)]
    assert entry["utilization"] == pytest.approx(0.1)
    assert entry["drops_delta"] == 1


def test_tc_failure_marks_all_links_invalid_and_does_not_raise():
    monitor, _ = make_monitor([TcError("tc not found")])
    stats = monitor.collect_stats()
    assert all(not entry["valid"] for entry in stats.values())
    assert all("tc not found" in entry["error"] for entry in stats.values())
    assert monitor.last_error == "tc not found"


def test_unexpected_reader_exception_does_not_raise():
    monitor, _ = make_monitor([RuntimeError("boom")])
    stats = monitor.collect_stats()
    assert stats[(1, 2)]["valid"] is False


def test_recovery_after_outage_does_not_use_stale_baseline():
    outputs = [tc_json_for({"s1-eth2": (0, 0, 0)}),
               TcError("temporary"),
               tc_json_for({"s1-eth2": (10_000_000, 0, 0)}),
               tc_json_for({"s1-eth2": (10_125_000, 0, 0)})]
    monitor, clock = make_monitor(outputs)
    for _ in range(3):
        monitor.collect_stats()
        clock.now += 1.0
    # sample right after outage has no baseline -> no utilization
    assert monitor.get_link_stats(1, 2)["utilization"] is None
    monitor.collect_stats()
    assert monitor.get_link_stats(1, 2)["utilization"] == pytest.approx(0.1)


def test_get_link_delay_accepts_names_and_ids():
    monitor, _ = make_monitor([TC_JSON])
    monitor.collect_stats()
    assert monitor.get_link_delay(1, 2) == pytest.approx(9.6)
    assert monitor.get_link_delay("s1", "s2") == pytest.approx(9.6)
    assert monitor.get_link_delay("1", "2") == pytest.approx(9.6)
    assert monitor.get_link_delay(2, 1) is None          # reverse direction absent
    assert monitor.get_link_delay("h1", "s1") is None    # not a switch link
    assert monitor.get_link_delay(7, 8) is None          # unknown link


def test_smoothed_delay_ewma():
    outputs = [tc_json_for({"s1-eth2": (0, 0, 0)}),
               tc_json_for({"s1-eth2": (0, 12_500, 0)})]   # 10 ms
    monitor, _ = make_monitor(outputs, ewma_alpha=0.5)
    monitor.collect_stats()
    monitor.collect_stats()
    assert monitor.get_link_delay(1, 2) == pytest.approx(10.0)
    assert monitor.get_link_delay(1, 2, smoothed=True) == pytest.approx(5.0)


@pytest.mark.parametrize("alpha", [0, -0.1, 1.5])
def test_invalid_ewma_alpha(alpha):
    with pytest.raises(ValueError):
        QueueMonitor(reader=FakeReader([TC_JSON]), ewma_alpha=alpha)


def test_get_all_stats_returns_copy():
    monitor, _ = make_monitor([TC_JSON])
    monitor.collect_stats()
    snapshot = monitor.get_all_stats()
    snapshot[(1, 2)]["queue_bytes"] = -1
    assert monitor.get_all_stats()[(1, 2)]["queue_bytes"] == 12000


def test_get_link_metrics_uses_shared_model():
    monitor, _ = make_monitor([TC_JSON])
    monitor.collect_stats()
    metrics = {(m.source, m.destination): m for m in monitor.get_link_metrics()}
    m = metrics[("s1", "s2")]
    assert isinstance(m, LinkMetrics)
    assert m.queue_bytes == 12000
    assert m.bandwidth_bps == 10_000_000
    assert m.estimated_delay_s == pytest.approx(0.0096)
    assert metrics[("s2", "s4")].estimated_delay_s is None


def test_update_link_map_drops_removed_links():
    monitor, _ = make_monitor([TC_JSON])
    monitor.collect_stats()
    monitor.update_link_map({(1, 2): "s1-eth2"})
    assert set(monitor.get_all_stats()) == {(1, 2)}
    assert set(monitor.collect_stats()) == {(1, 2)}


def test_custom_capacity_changes_delay():
    monitor = QueueMonitor(link_map={(1, 2): ("s1-eth2", 100)},
                           reader=FakeReader([TC_JSON]))
    monitor.collect_stats()
    assert monitor.get_link_delay(1, 2) == pytest.approx(0.96)


def test_path_selector_can_sum_path_delay():
    """Sketch of how Member 3 consumes the monitor."""
    out = tc_json_for({"s1-eth2": (0, 25_000, 0), "s2-eth2": (0, 12_500, 0),
                       "s1-eth3": (0, 0, 0), "s3-eth2": (0, 1_250, 0)})
    monitor, _ = make_monitor([out])
    monitor.collect_stats()

    def path_delay(switches):
        return sum(monitor.get_link_delay(a, b, default=0.0)
                   for a, b in zip(switches, switches[1:]))

    upper = path_delay(["s1", "s2", "s4"])   # 20 + 10 ms
    lower = path_delay(["s1", "s3", "s4"])   # 0 + 1 ms
    assert upper == pytest.approx(30.0)
    assert lower == pytest.approx(1.0)
    assert lower < upper


# ---------------------------------------------------------------------------
# Background polling
# ---------------------------------------------------------------------------

def test_run_loop_stops_and_survives_callback_errors():
    monitor = QueueMonitor(reader=FakeReader([TC_JSON]))
    stop = threading.Event()
    seen = []

    def callback(stats):
        seen.append(len(stats))
        if len(seen) == 1:
            raise RuntimeError("consumer bug")
        if len(seen) >= 3:
            stop.set()

    monitor.run(interval=0.001, stop_event=stop, callback=callback)
    assert seen == [8, 8, 8]


def test_run_rejects_bad_interval():
    with pytest.raises(ValueError):
        QueueMonitor(reader=FakeReader([TC_JSON])).run(interval=0)


def test_start_and_stop_thread():
    reader = FakeReader([TC_JSON])
    monitor = QueueMonitor(reader=reader)
    collected = threading.Event()
    monitor.start(interval=0.01, callback=lambda stats: collected.set())
    try:
        assert collected.wait(2.0)
    finally:
        monitor.stop()
    calls = reader.calls
    assert calls >= 1
    assert monitor._thread is None


# ---------------------------------------------------------------------------
# QueueConfigurator
# ---------------------------------------------------------------------------

def test_configurator_commands():
    cmds = QueueConfigurator(runner=lambda c, t: "").build_commands("s1-eth2", 10, 50)
    assert cmds[0][:6] == ["tc", "qdisc", "replace", "dev", "s1-eth2", "root"]
    assert "10mbit" in cmds[1]
    assert cmds[2][-2:] == ["limit", "50"]


@pytest.mark.parametrize("rate, limit", [(0, 10), (10, 0)])
def test_configurator_rejects_invalid(rate, limit):
    with pytest.raises(ValueError):
        QueueConfigurator(runner=lambda c, t: "").build_commands("s1-eth2", rate, limit)


def test_configurator_dry_run_skips_already_shaped_interfaces():
    executed = []

    def runner(cmd, timeout):
        if cmd[1] == "-s":
            return TC_JSON                      # s1-eth2 already has htb
        executed.append(cmd)
        return ""

    links = default_diamond_link_map()
    result = QueueConfigurator(runner=runner).configure(
        [links[(1, 2)], links[(2, 4)]], dry_run=True)
    assert result["s1-eth2"]["action"] == "skipped"
    assert result["s2-eth2"]["action"] == "planned"
    assert executed == []


def test_configurator_apply_and_replace():
    executed = []

    def runner(cmd, timeout):
        if cmd[1] == "-s":
            return TC_JSON
        executed.append(cmd)
        return ""

    links = default_diamond_link_map()
    result = QueueConfigurator(runner=runner).configure(
        [links[(1, 2)]], dry_run=False, replace_existing=True)
    assert result["s1-eth2"]["action"] == "applied"
    assert len(executed) == 3


def test_configurator_reports_errors_and_unknown_state():
    def failing(cmd, timeout):
        raise TcError("Operation not permitted")

    link = default_diamond_link_map()[(1, 2)]
    cfg = QueueConfigurator(runner=failing)
    assert cfg.configure([link], dry_run=False)["s1-eth2"]["action"] == "skipped"
    result = cfg.configure([link], dry_run=False, replace_existing=True)
    assert result["s1-eth2"]["action"] == "error"
    unknown = MonitoredLink(1, 2, "s1-eth2", None)
    assert cfg.configure([unknown])["s1-eth2"]["reason"] == "link capacity unknown"


# ---------------------------------------------------------------------------
# Display / CLI
# ---------------------------------------------------------------------------

def test_format_stats_table():
    monitor, _ = make_monitor([TC_JSON])
    table = format_stats_table(monitor.collect_stats())
    assert "s1-eth2" in table and "9.60" in table
    assert "unavailable" in table


def test_cli_runs_without_tc(capsys):
    assert main(["--count", "1", "--tc-path", "/nonexistent/tc", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["1-2"]["valid"] is False


def test_monitoring_package_reexports():
    from monitoring.queue_monitor import QueueMonitor as Reexported
    assert Reexported is QueueMonitor
