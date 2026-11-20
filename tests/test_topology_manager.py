
from types import SimpleNamespace
from unittest.mock import MagicMock

from controller.topology_manager import TopologyManager


def make_switch(dpid):
    return SimpleNamespace(
        dp=SimpleNamespace(id=dpid)
    )


def make_link(src, src_port, dst, dst_port):
    return SimpleNamespace(
        src=SimpleNamespace(
            dpid=src,
            port_no=src_port,
        ),
        dst=SimpleNamespace(
            dpid=dst,
            port_no=dst_port,
        ),
    )


def diamond_links():
    return [
        make_link(1, 2, 2, 1),
        make_link(2, 1, 1, 2),
        make_link(2, 2, 4, 1),
        make_link(4, 1, 2, 2),
        make_link(1, 3, 3, 1),
        make_link(3, 1, 1, 3),
        make_link(3, 2, 4, 2),
        make_link(4, 2, 3, 2),
    ]


def test_discover_diamond_topology(monkeypatch):
    app = SimpleNamespace(logger=MagicMock())
    manager = TopologyManager(app)

    monkeypatch.setattr(
        "controller.topology_manager.api.get_switch",
        lambda app, dpid: [
            make_switch(1),
            make_switch(2),
            make_switch(3),
            make_switch(4),
        ],
    )

    monkeypatch.setattr(
        "controller.topology_manager.api.get_link",
        lambda app, dpid: diamond_links(),
    )

    graph = manager.discover_topology()

    assert set(graph.nodes()) == {1, 2, 3, 4}
    assert graph.number_of_edges() == 8

    assert manager.get_port(1, 2) == 2
    assert manager.get_port(1, 3) == 3
    assert manager.get_port(2, 4) == 2
    assert manager.get_port(3, 4) == 2

    assert manager.has_link(1, 2)
    assert manager.has_link(2, 1)


def test_discover_returns_two_paths(monkeypatch):
    app = SimpleNamespace(logger=MagicMock())
    manager = TopologyManager(app)

    monkeypatch.setattr(
        "controller.topology_manager.api.get_switch",
        lambda app, dpid: [
            make_switch(1),
            make_switch(2),
            make_switch(3),
            make_switch(4),
        ],
    )

    monkeypatch.setattr(
        "controller.topology_manager.api.get_link",
        lambda app, dpid: diamond_links(),
    )

    manager.discover_topology()

    paths = manager.get_paths(1, 4, k=2)

    assert len(paths) == 2
    assert [1, 2, 4] in paths
    assert [1, 3, 4] in paths


def test_empty_discovery_removes_old_links(monkeypatch):
    app = SimpleNamespace(logger=MagicMock())
    manager = TopologyManager(app)

    monkeypatch.setattr(
        "controller.topology_manager.api.get_switch",
        lambda app, dpid: [
            make_switch(1),
            make_switch(2),
            make_switch(3),
            make_switch(4),
        ],
    )

    current_links = diamond_links()

    monkeypatch.setattr(
        "controller.topology_manager.api.get_link",
        lambda app, dpid: current_links,
    )

    manager.discover_topology()

    assert manager.has_link(1, 2)

    # Simulate OS-Ken reporting that all inter-switch links disappeared.
    current_links = []

    manager.discover_topology()

    assert manager.get_links() == []
    assert manager.get_port(1, 2) is None
    assert manager.get_paths(1, 4) == []


def test_graph_snapshot_cannot_mutate_internal_graph(monkeypatch):
    app = SimpleNamespace(logger=MagicMock())
    manager = TopologyManager(app)

    monkeypatch.setattr(
        "controller.topology_manager.api.get_switch",
        lambda app, dpid: [
            make_switch(1),
            make_switch(2),
        ],
    )

    monkeypatch.setattr(
        "controller.topology_manager.api.get_link",
        lambda app, dpid: [
            make_link(1, 2, 2, 1),
        ],
    )

    manager.discover_topology()

    external_graph = manager.get_graph()
    external_graph.remove_edge(1, 2)

    assert manager.has_link(1, 2)


def test_failed_discovery_preserves_previous_snapshot(monkeypatch):
    app = SimpleNamespace(logger=MagicMock())
    manager = TopologyManager(app)

    monkeypatch.setattr(
        "controller.topology_manager.api.get_switch",
        lambda app, dpid: [
            make_switch(1),
            make_switch(2),
        ],
    )

    monkeypatch.setattr(
        "controller.topology_manager.api.get_link",
        lambda app, dpid: [
            make_link(1, 2, 2, 1),
        ],
    )

    manager.discover_topology()

    def raise_error(app, dpid):
        raise RuntimeError("OS-Ken topology API unavailable")

    monkeypatch.setattr(
        "controller.topology_manager.api.get_link",
        raise_error,
    )

    try:
        manager.discover_topology()
    except RuntimeError:
        pass
    else:
        raise AssertionError("Expected discovery to fail")

    assert manager.has_link(1, 2)
