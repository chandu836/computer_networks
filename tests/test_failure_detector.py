from failure_detection.detector import FailureDetector, link_key


def make_diamond():
    d = FailureDetector()
    for s in (1, 2, 3, 4):
        d.on_switch_enter(s)
    # (src, src_port, dst) both directions, ports as in the diamond topology
    for src, port, dst in [(1, 2, 2), (2, 1, 1), (2, 2, 4), (4, 1, 2),
                           (1, 3, 3), (3, 1, 1), (3, 2, 4), (4, 2, 3)]:
        d.on_link_add(src, port, dst)
    return d


def test_initial_discovery_is_not_a_failure():
    d = make_diamond()
    assert d.get_failed_links() == []
    assert d.event_log == []


def test_port_down_reports_failure_once():
    d = make_diamond()
    got = []
    d.register_listener(got.append)
    d.on_port_status(1, 2, True)      # s1 side
    d.on_port_status(2, 1, True)      # s2 side (duplicate)
    assert d.get_failed_links() == [(1, 2)]
    assert len(got) == 1
    assert got[0].is_failed and {got[0].source, got[0].destination} == {"s1", "s2"}
    assert got[0].detection_time is not None


def test_host_port_ignored():
    d = make_diamond()
    assert d.on_port_status(1, 1, True) is None
    assert d.get_failed_links() == []


def test_recovery_event():
    d = make_diamond()
    d.on_port_status(1, 2, True)
    ev = d.on_port_status(1, 2, False)
    assert ev is not None and not ev.is_failed
    assert d.get_failed_links() == []


def test_lldp_delete_after_port_status_is_duplicate():
    d = make_diamond()
    d.on_port_status(1, 2, True)
    assert d.on_link_delete(1, 2) is None
    assert len(d.event_log) == 1


def test_switch_failure_marks_all_its_links():
    d = make_diamond()
    events = d.on_switch_leave(3)
    assert d.is_switch_failed(3)
    assert d.get_failed_links() == [(1, 3), (3, 4)]
    assert len(events) == 2
    assert d.is_link_failed("s1", "s3")
    assert not d.is_link_failed(1, 2)


def test_name_forms_and_symmetry():
    assert link_key("s2", 1) == (1, 2)
    d = make_diamond()
    d.on_port_status(2, 1, True)
    assert d.is_link_failed(1, 2) and d.is_link_failed(2, 1)


def test_bad_listener_does_not_break_others():
    d = make_diamond()
    got = []
    d.register_listener(lambda e: 1 / 0)
    d.register_listener(got.append)
    d.on_port_status(1, 2, True)
    assert len(got) == 1


def test_detection_time_measurement():
    t = [100.0]
    d = FailureDetector(clock=lambda: t[0])
    for s in (1, 2):
        d.on_switch_enter(s)
    d.on_link_add(1, 2, 2)
    d.on_link_add(2, 1, 1)
    t[0] = 100.25
    d.on_port_status(1, 2, True)
    assert abs(d.detection_time_since(100.0, 1, 2) - 0.25) < 1e-9
