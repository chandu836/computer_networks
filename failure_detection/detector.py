"""
Member 4: Failure detection.

Pure-Python module (no os_ken import) so it can be unit-tested anywhere.
The controller (controller/ryu_app.py) feeds it real events:

    detector.on_switch_enter(dpid)
    detector.on_switch_leave(dpid)
    detector.on_link_add(src_dpid, src_port, dst_dpid)
    detector.on_link_delete(src_dpid, dst_dpid)
    detector.on_port_status(dpid, port_no, is_down)   # fastest signal

and Member 5 (recovery) subscribes with:

    detector.register_listener(callback)   # callback(FailureEvent)

Conventions (match common/models.py and docs/QUEUE_MONITOR.md):
  * FailureEvent.source / .destination use names like "s1", "s2".
  * A failure event describes the PHYSICAL link, so it applies to BOTH
    directions (s1->s2 and s2->s1).
  * is_failed=True means link went down, is_failed=False means it is back.
  * A switch failure is reported as a link failure for every link of that
    switch, and is also visible through is_switch_failed().
  * detection_time is a time.time() timestamp taken when we detected it.
"""

import logging
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from common.models import FailureEvent

LinkKey = Tuple[int, int]

_logger = logging.getLogger(__name__)


def parse_switch_id(node) -> int:
    """Accept 1, "1" or "s1" and return the integer datapath id."""
    if isinstance(node, int):
        return node
    text = str(node).strip().lower()
    if text.startswith("s"):
        text = text[1:]
    return int(text)


def link_key(a, b) -> LinkKey:
    """Canonical (undirected) link id: sorted tuple of datapath ids."""
    a, b = parse_switch_id(a), parse_switch_id(b)
    return (a, b) if a <= b else (b, a)


def switch_name(dpid: int) -> str:
    return "s%d" % dpid


class FailureDetector:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.RLock()
        self._link_failed: Dict[LinkKey, bool] = {}   # known links only
        self._switch_failed: Dict[int, bool] = {}
        self._port_to_peer: Dict[Tuple[int, int], int] = {}
        self._listeners: List[Callable[[FailureEvent], None]] = []
        self.event_log: List[FailureEvent] = []

    # ------------------------------------------------------------------
    # Subscription
    # ------------------------------------------------------------------
    def register_listener(self, callback: Callable[[FailureEvent], None]):
        """callback(FailureEvent) is called on every failure/recovery."""
        self._listeners.append(callback)

    def _emit(self, a: int, b: int, is_failed: bool) -> FailureEvent:
        event = FailureEvent(
            source=switch_name(a),
            destination=switch_name(b),
            is_failed=is_failed,
            detection_time=self._clock(),
        )
        self.event_log.append(event)
        _logger.info(
            "%s: %s <-> %s",
            "LINK DOWN" if is_failed else "LINK UP",
            event.source, event.destination,
        )
        for callback in list(self._listeners):
            try:
                callback(event)
            except Exception:
                _logger.exception("failure listener raised")
        return event

    def _set_link(self, a: int, b: int, failed: bool) -> Optional[FailureEvent]:
        key = link_key(a, b)
        with self._lock:
            if self._link_failed.get(key) == failed:
                return None  # duplicate report: ignore
            self._link_failed[key] = failed
        return self._emit(key[0], key[1], failed)

    # ------------------------------------------------------------------
    # Inputs from the controller
    # ------------------------------------------------------------------
    def on_switch_enter(self, dpid) -> None:
        dpid = parse_switch_id(dpid)
        with self._lock:
            self._switch_failed[dpid] = False

    def on_switch_leave(self, dpid) -> List[FailureEvent]:
        """Switch disconnected: every link of that switch is down."""
        dpid = parse_switch_id(dpid)
        events = []
        with self._lock:
            self._switch_failed[dpid] = True
            affected = [k for k in self._link_failed if dpid in k]
        for a, b in sorted(affected):
            ev = self._set_link(a, b, True)
            if ev:
                events.append(ev)
        return events

    def on_link_add(self, src_dpid, src_port, dst_dpid) -> Optional[FailureEvent]:
        """LLDP discovered a link. Also learns port -> neighbour mapping."""
        src, dst = parse_switch_id(src_dpid), parse_switch_id(dst_dpid)
        with self._lock:
            if src_port is not None:
                self._port_to_peer[(src, src_port)] = dst
            self._switch_failed.setdefault(src, False)
            self._switch_failed.setdefault(dst, False)
            known = link_key(src, dst) in self._link_failed
        if not known:
            # First discovery: record as healthy, nothing to report.
            with self._lock:
                self._link_failed[link_key(src, dst)] = False
            return None
        return self._set_link(src, dst, False)

    def on_link_delete(self, src_dpid, dst_dpid) -> Optional[FailureEvent]:
        """LLDP timed out / link removed (slower backup signal)."""
        return self._set_link(parse_switch_id(src_dpid),
                              parse_switch_id(dst_dpid), True)

    def on_port_status(self, dpid, port_no, is_down: bool) -> Optional[FailureEvent]:
        """OpenFlow port-status message: the fastest failure signal."""
        dpid = parse_switch_id(dpid)
        peer = self._port_to_peer.get((dpid, port_no))
        if peer is None:
            return None  # host-facing or unknown port
        return self._set_link(dpid, peer, bool(is_down))

    # ------------------------------------------------------------------
    # Queries (for Member 5 and for evaluation)
    # ------------------------------------------------------------------
    def is_link_failed(self, a, b) -> bool:
        key = link_key(a, b)
        with self._lock:
            return (self._link_failed.get(key, False)
                    or self._switch_failed.get(key[0], False)
                    or self._switch_failed.get(key[1], False))

    def is_switch_failed(self, dpid) -> bool:
        with self._lock:
            return self._switch_failed.get(parse_switch_id(dpid), False)

    def get_failed_links(self) -> List[LinkKey]:
        with self._lock:
            return sorted(k for k, failed in self._link_failed.items() if failed)

    def get_failed_switches(self) -> List[int]:
        with self._lock:
            return sorted(d for d, failed in self._switch_failed.items() if failed)

    def detection_time_since(self, injected_at: float, a, b) -> Optional[float]:
        """Seconds from failure injection (time.time() taken just before the
        link is brought down) to our first FailureEvent for that link."""
        src, dst = switch_name(link_key(a, b)[0]), switch_name(link_key(a, b)[1])
        for ev in self.event_log:
            if (ev.is_failed and ev.detection_time is not None
                    and ev.detection_time >= injected_at
                    and {ev.source, ev.destination} == {src, dst}):
                return ev.detection_time - injected_at
        return None

    def save_log(self, path: str = "failure_events.csv") -> str:
        import csv
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["detection_time", "source", "destination", "is_failed"])
            for ev in self.event_log:
                writer.writerow([ev.detection_time, ev.source,
                                 ev.destination, ev.is_failed])
        return path
