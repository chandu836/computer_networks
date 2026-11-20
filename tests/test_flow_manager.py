from os_ken.ofproto import ofproto_v1_3

from recovery.flow_manager import (
    FlowManager,
    build_hops,
    link_key,
    path_links,
    switch_path,
)
from tests.recovery_helpers import (
    LOWER,
    UPPER,
    FakeDatapath,
    FakeTopologyManager,
    diamond_hops,
    is_add,
    is_delete,
    out_ports,
    tcp_match,
)


def make_manager():
    manager = FlowManager(idle_timeout=1)
    datapaths = {dpid: FakeDatapath(dpid) for dpid in (1, 2, 3, 4)}
    for datapath in datapaths.values():
        manager.register_datapath(datapath)
    return manager, datapaths


def test_switch_path_accepts_all_path_forms():
    assert switch_path(["h1", "s1", "s2", "s4", "h2"]) == [1, 2, 4]
    assert switch_path([1, 2, 4]) == [1, 2, 4]
    assert switch_path(diamond_hops(UPPER)) == [1, 2, 4]
    assert switch_path(None) == []


def test_path_links_are_undirected():
    assert path_links([4, 2, 1]) == [(2, 4), (1, 2)]
    assert link_key("s2", 1) == (1, 2)


def test_build_hops_uses_topology_ports_and_host_port():
    topology = FakeTopologyManager()

    assert build_hops(UPPER, topology.get_port, 3) == [
        {"dpid": 1, "out_port": 2},
        {"dpid": 2, "out_port": 2},
        {"dpid": 4, "out_port": 3},
    ]
    assert build_hops(["h1", "s1", "s3", "s4", "h2"], topology.get_port, 3) == [
        {"dpid": 1, "out_port": 3},
        {"dpid": 3, "out_port": 2},
        {"dpid": 4, "out_port": 3},
    ]
    # Hosts on the same switch: a single hop.
    assert build_hops([1], topology.get_port, 1) == [{"dpid": 1, "out_port": 1}]


def test_build_hops_fails_on_unknown_link():
    topology = FakeTopologyManager()
    topology.remove_link(1, 2)

    assert build_hops(UPPER, topology.get_port, 3) is None
    assert build_hops([], topology.get_port, 3) is None
    assert build_hops(UPPER, topology.get_port, None) is None


def test_install_path_writes_one_rule_per_switch():
    manager, datapaths = make_manager()

    assert manager.install_path("f1", tcp_match(), diamond_hops(UPPER))

    for dpid, port in ((1, 2), (2, 2), (4, 3)):
        (rule,) = datapaths[dpid].flow_mods()
        assert is_add(rule)
        assert out_ports(rule) == [port]
        assert rule.idle_timeout == 1
        assert rule.priority == manager.priority
        assert rule.match["tcp_dst"] == 5001
        assert rule.match["ipv4_src"] == "10.0.0.1"

    assert datapaths[3].flow_mods() == []
    assert manager.get_path("f1") == diamond_hops(UPPER)
    assert manager.rules_installed == 3


def test_only_first_switch_reports_flow_removal():
    manager, datapaths = make_manager()
    manager.install_path("f1", tcp_match(), diamond_hops(UPPER))

    flag = ofproto_v1_3.OFPFF_SEND_FLOW_REM
    assert datapaths[1].flow_mods()[0].flags & flag
    assert not datapaths[2].flow_mods()[0].flags & flag
    assert not datapaths[4].flow_mods()[0].flags & flag


def test_install_path_fails_without_a_connected_switch():
    manager, datapaths = make_manager()
    manager.unregister_datapath(2)

    assert not manager.install_path("f1", tcp_match(), diamond_hops(UPPER))
    assert not manager.install_path("f1", tcp_match(), [])
    assert not manager.has_flow("f1")
    assert all(dp.flow_mods() == [] for dp in datapaths.values())


def test_update_path_makes_before_it_breaks():
    manager, datapaths = make_manager()
    manager.install_path("f1", tcp_match(), diamond_hops(UPPER))
    for datapath in datapaths.values():
        datapath.clear()

    order = []
    for dpid, datapath in datapaths.items():
        original = datapath.send_msg

        def record(msg, dpid=dpid, original=original):
            order.append((dpid, msg.command))
            return original(msg)

        datapath.send_msg = record

    assert manager.update_path("f1", diamond_hops(LOWER))

    add = ofproto_v1_3.OFPFC_ADD
    delete = ofproto_v1_3.OFPFC_DELETE_STRICT
    # New rules from the last switch back to the first, then the cleanup.
    assert order == [(4, add), (3, add), (1, add), (2, delete)]

    assert out_ports(datapaths[1].flow_mods()[0]) == [3]
    assert out_ports(datapaths[3].flow_mods()[0]) == [2]
    assert is_delete(datapaths[2].flow_mods()[0])
    assert manager.get_path("f1") == diamond_hops(LOWER)
    assert manager.route_changes == 1


def test_install_path_on_existing_flow_updates_it():
    manager, datapaths = make_manager()
    manager.install_path("f1", tcp_match(), diamond_hops(UPPER))

    # Same path again: rules are refreshed, not counted as a route change.
    assert manager.install_path("f1", tcp_match(), diamond_hops(UPPER))
    assert manager.route_changes == 0

    assert manager.install_path("f1", tcp_match(), diamond_hops(LOWER))
    assert manager.route_changes == 1
    assert manager.get_path("f1") == diamond_hops(LOWER)
    assert is_delete(datapaths[2].flow_mods()[-1])


def test_update_path_requires_known_flow_and_switches():
    manager, _ = make_manager()

    assert not manager.update_path("missing", diamond_hops(LOWER))

    manager.install_path("f1", tcp_match(), diamond_hops(UPPER))
    manager.unregister_datapath(3)
    assert not manager.update_path("f1", diamond_hops(LOWER))
    assert manager.get_path("f1") == diamond_hops(UPPER)


def test_remove_path_deletes_rules_everywhere():
    manager, datapaths = make_manager()
    manager.install_path("f1", tcp_match(), diamond_hops(UPPER))
    for datapath in datapaths.values():
        datapath.clear()

    assert manager.remove_path("f1")

    for dpid in UPPER:
        (rule,) = datapaths[dpid].flow_mods()
        assert is_delete(rule)
        assert rule.priority == manager.priority
        assert rule.match["tcp_src"] == 40000

    assert not manager.has_flow("f1")
    assert not manager.remove_path("f1")
    assert manager.rules_removed == 3


def test_flow_removed_ends_flow_only_from_first_switch():
    manager, datapaths = make_manager()
    manager.install_path("f1", tcp_match(), diamond_hops(UPPER))
    cookie = datapaths[1].flow_mods()[0].cookie

    assert manager.handle_flow_removed(2, cookie) is None
    assert manager.handle_flow_removed(1, 9999) is None
    assert manager.has_flow("f1")

    assert manager.handle_flow_removed(1, cookie) == "f1"
    assert not manager.has_flow("f1")


def test_stale_flow_removed_does_not_end_reinstalled_flow():
    manager, datapaths = make_manager()
    manager.install_path("f1", tcp_match(), diamond_hops(UPPER))
    old_cookie = datapaths[1].flow_mods()[0].cookie

    # The rules are written again before the old timeout message arrives.
    manager.install_path("f1", tcp_match(), diamond_hops(UPPER))
    new_cookie = datapaths[1].flow_mods()[-1].cookie

    assert new_cookie != old_cookie
    assert manager.handle_flow_removed(1, old_cookie) is None
    assert manager.has_flow("f1")
    assert manager.handle_flow_removed(1, new_cookie) == "f1"


def test_repair_hop_rewrites_single_rule():
    manager, datapaths = make_manager()
    manager.install_path("f1", tcp_match(), diamond_hops(UPPER))
    for datapath in datapaths.values():
        datapath.clear()

    assert manager.repair_hop("f1", 2)
    (rule,) = datapaths[2].flow_mods()
    assert is_add(rule) and out_ports(rule) == [2]
    assert datapaths[1].flow_mods() == []

    assert not manager.repair_hop("f1", 3)        # not on the path
    assert not manager.repair_hop("missing", 2)


def test_install_drop_has_no_actions_and_expires():
    manager, datapaths = make_manager()

    assert manager.install_drop(1, tcp_match(), hard_timeout=1)

    (rule,) = datapaths[1].flow_mods()
    assert is_add(rule)
    assert rule.instructions == []
    assert rule.hard_timeout == 1 and rule.idle_timeout == 0
    assert not manager.has_flow("f1")
    assert not manager.install_drop(9, tcp_match())


def test_queries_by_link_and_switch():
    manager, _ = make_manager()
    manager.install_path("up", tcp_match(40000), diamond_hops(UPPER))
    manager.install_path("low", tcp_match(40001), diamond_hops(LOWER))
    manager.install_path("back", tcp_match(40002), diamond_hops([4, 2, 1], 1))

    assert sorted(manager.flows_using_link(1, 2)) == ["back", "up"]
    assert sorted(manager.flows_using_link("s2", "s1")) == ["back", "up"]
    assert manager.flows_using_link(3, 4) == ["low"]
    assert manager.flows_using_switch(3) == ["low"]
    assert set(manager.get_active_flows()) == {"up", "low", "back"}
    assert manager.get_match("up")["tcp_src"] == 40000


def test_unregister_datapath_forgets_flows_entering_there():
    manager, _ = make_manager()
    manager.install_path("up", tcp_match(40000), diamond_hops(UPPER))
    manager.install_path("back", tcp_match(40002), diamond_hops([4, 2, 1], 1))

    assert manager.unregister_datapath(1) == ["up"]
    assert not manager.has_flow("up")
    assert manager.has_flow("back")
    assert not manager.has_datapath(1)
