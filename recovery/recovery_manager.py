"""
Member 5: Failure recovery.

Subscribes to Member 4's failure detector and keeps traffic flowing:

  * link or switch down: every flow that crosses the failed link gets a
    new path from the router (failed components excluded) and its rules
    are replaced. Flows without an alternative path have their rules
    removed.
  * link up again: nothing moves immediately. The link has to stay up for
    a stability interval, and a displaced flow only returns to its
    original path if that path is cheaper than the current one by more
    than a hysteresis margin.

Pure-Python module (no os_ken import), like the failure detector.

Wiring in the controller:

    recovery = RecoveryManager(
        topology_manager=topology_manager,        # get_graph(), get_port()
        router=router,                            # select_path(...)
        flow_manager=flow_manager,
        failure_detector=failure_detector,
        path_cost=lambda path: calculate_path_cost(path, queue_monitor),
    )
    failure_detector.register_listener(recovery.on_failure_event)

    # periodically (restoration is evaluated here):
    recovery.process_pending()

Recovery time, as reported here, is measured inside the controller: from
the detection timestamp of the FailureEvent to the moment the last
flow-mod of the reroute was sent. End-to-end recovery time (traffic
arriving again) has to be measured at the hosts.
"""

import csv
import logging
import threading
import time

from recovery.flow_manager import (
    build_hops,
    link_key,
    path_links,
    switch_path,
)

DEFAULT_STABILITY_INTERVAL_S = 3.0
DEFAULT_RESTORE_HYSTERESIS = 0.5


def _hop_count(path):
    return float(max(len(switch_path(path)) - 1, 0))


def _link_name(link):
    return "s%d-s%d" % link


class RecoveryManager:
    def __init__(
        self,
        topology_manager,
        router,
        flow_manager,
        failure_detector=None,
        path_cost=None,
        stability_interval_s=DEFAULT_STABILITY_INTERVAL_S,
        restore_hysteresis=DEFAULT_RESTORE_HYSTERESIS,
        clock=time.time,
        logger=None,
    ):
        """
        path_cost(switch_path) returns the cost used to judge restoration.
        With the queue-aware cost this is in ms, so restore_hysteresis is
        in ms as well. Without it, hop count is used.
        """
        self.topology_manager = topology_manager
        self.router = router
        self.flow_manager = flow_manager
        self.failure_detector = failure_detector
        self.path_cost = path_cost or _hop_count
        self.stability_interval_s = stability_interval_s
        self.restore_hysteresis = restore_hysteresis
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)
        self._lock = threading.RLock()

        self._failed_links = set()
        self._recovering = {}   # link -> time it came back up
        self._displaced = {}    # flow_id -> switch path before the failure

        self.recovery_log = []
        self.flows_rerouted = 0
        self.flows_unrecoverable = 0
        self.flows_restored = 0

    # ------------------------------------------------------------------
    # Failed components
    # ------------------------------------------------------------------
    @staticmethod
    def _link_of(component):
        """Accept a FailureEvent or a (node, node) pair."""
        if hasattr(component, "source") and hasattr(component, "destination"):
            return link_key(component.source, component.destination)
        first, second = component
        return link_key(first, second)

    def get_failed_links(self):
        with self._lock:
            links = set(self._failed_links)
        if self.failure_detector is not None:
            links.update(self.failure_detector.get_failed_links())
        return sorted(links)

    def get_failed_switches(self):
        if self.failure_detector is None:
            return []
        return list(self.failure_detector.get_failed_switches())

    def is_path_healthy(self, path):
        """Check whether all links and switches of a path are healthy."""
        dpids = switch_path(path)
        if not dpids:
            return False

        failed_switches = set(self.get_failed_switches())
        if any(dpid in failed_switches for dpid in dpids):
            return False

        failed_links = set(self.get_failed_links())
        return not any(link in failed_links for link in path_links(dpids))

    # ------------------------------------------------------------------
    # Failure detector callback
    # ------------------------------------------------------------------
    def on_failure_event(self, event):
        """Listener for FailureDetector.register_listener()."""
        if event.is_failed:
            return self.handle_failure(event)
        return self.handle_restoration(event)

    # ------------------------------------------------------------------
    # Failure
    # ------------------------------------------------------------------
    def handle_failure(self, failed_component):
        """Find and activate an alternative route for every affected flow."""
        link = self._link_of(failed_component)
        detected_at = getattr(failed_component, "detection_time", None)
        if detected_at is None:
            detected_at = self._clock()

        with self._lock:
            self._failed_links.add(link)
            # A link that fails again while it is being observed is not
            # stable: cancel its restoration.
            self._recovering.pop(link, None)

            rerouted = 0
            unrecoverable = 0

            for flow_id in self.flow_manager.flows_using_link(*link):
                if self._reroute(flow_id):
                    rerouted += 1
                else:
                    unrecoverable += 1

            self.flows_rerouted += rerouted
            self.flows_unrecoverable += unrecoverable

            completed_at = self._clock()
            record = {
                "event": "failure",
                "link": _link_name(link),
                "detection_time": detected_at,
                "completed_time": completed_at,
                "recovery_time_s": completed_at - detected_at,
                "flows_rerouted": rerouted,
                "flows_unrecoverable": unrecoverable,
                "flows_restored": 0,
            }
            self.recovery_log.append(record)

        self._logger.info(
            "RECOVERY %s down: %d flows rerouted, %d without a path, "
            "%.3f ms after detection",
            record["link"], rerouted, unrecoverable,
            record["recovery_time_s"] * 1000.0,
        )
        return record

    def _reroute(self, flow_id):
        """Move one flow to a healthy path. Returns False if none exists."""
        old_hops = self.flow_manager.get_path(flow_id)
        if not old_hops:
            return False

        old_path = switch_path(old_hops)
        new_hops = None

        decision = self.router.select_path(
            self.topology_manager.get_graph(),
            old_path[0],
            old_path[-1],
            flow_id=flow_id,
            failed_links=self.get_failed_links(),
            failed_switches=self.get_failed_switches(),
        )

        if decision is not None and self.is_path_healthy(decision.path):
            new_hops = build_hops(
                decision.path,
                self.topology_manager.get_port,
                old_hops[-1]["out_port"],
            )

        if new_hops and self.flow_manager.update_path(flow_id, new_hops):
            # Remember where the flow was before its first failure.
            self._displaced.setdefault(flow_id, old_path)
            self._logger.info(
                "Flow %s rerouted: %s -> %s",
                flow_id, old_path, switch_path(new_hops),
            )
            return True

        # No usable alternative: do not leave rules pointing at a dead link.
        self.flow_manager.remove_path(flow_id)
        self._displaced.pop(flow_id, None)
        self._logger.warning(
            "Flow %s has no alternative path, rules removed", flow_id
        )
        return False

    # ------------------------------------------------------------------
    # Restoration
    # ------------------------------------------------------------------
    def handle_restoration(self, recovered_component):
        """
        Note that a link is back. The decision whether to use it again is
        taken by process_pending() once the stability interval has passed.
        """
        link = self._link_of(recovered_component)

        with self._lock:
            self._failed_links.discard(link)
            self._recovering[link] = self._clock()

        self._logger.info(
            "RECOVERY %s up: waiting %.1f s before reusing it",
            _link_name(link), self.stability_interval_s,
        )
        return link

    def process_pending(self, now=None):
        """
        Evaluate restoration. Call this periodically.

        Returns the list of flow_ids moved back to their original path.
        """
        now = self._clock() if now is None else now
        restored = []
        stable_links = []

        with self._lock:
            for link, since in list(self._recovering.items()):
                if now - since >= self.stability_interval_s:
                    del self._recovering[link]
                    stable_links.append(link)

            for flow_id, original in list(self._displaced.items()):
                outcome = self._evaluate_restoration(flow_id, original)
                if outcome == "restored":
                    restored.append(flow_id)
                if outcome != "wait":
                    self._displaced.pop(flow_id, None)

            self.flows_restored += len(restored)

            if stable_links or restored:
                done = self._clock()
                self.recovery_log.append({
                    "event": "restoration",
                    "link": "+".join(_link_name(link) for link in stable_links),
                    "detection_time": now,
                    "completed_time": done,
                    "recovery_time_s": done - now,
                    "flows_rerouted": 0,
                    "flows_unrecoverable": 0,
                    "flows_restored": len(restored),
                })

        for link in stable_links:
            self._logger.info("RECOVERY %s is stable again", _link_name(link))

        return restored

    def _evaluate_restoration(self, flow_id, original):
        """
        Decide what to do with one displaced flow.

        Returns "restored", "done" (nothing left to do for this flow) or
        "wait" (look again at the next call).
        """
        current_hops = self.flow_manager.get_path(flow_id)
        if not current_hops:
            return "done"   # flow ended

        current = switch_path(current_hops)
        if current == original:
            return "done"   # already back (the router moved it)

        links = path_links(original)
        if any(link in self._recovering for link in links):
            return "wait"   # still inside the stability interval

        if not self.is_path_healthy(original):
            return "wait"   # original path is still broken

        original_hops = build_hops(
            original,
            self.topology_manager.get_port,
            current_hops[-1]["out_port"],
        )
        if original_hops is None:
            return "wait"   # link not rediscovered by the topology yet

        improvement = self.path_cost(current) - self.path_cost(original)
        if improvement <= self.restore_hysteresis:
            return "wait"   # not worth a route change (yet)

        if not self.flow_manager.update_path(flow_id, original_hops):
            return "wait"

        # The router pinned this flow to the detour. Drop that state so the
        # next flowlet is not sent back to it.
        clear = getattr(self.router, "clear_flow_state", None)
        if clear is not None:
            clear(flow_id)

        self._logger.info(
            "Flow %s restored: %s -> %s (cost improvement %.3f)",
            flow_id, current, original, improvement,
        )
        return "restored"

    # ------------------------------------------------------------------
    # Bookkeeping
    # ------------------------------------------------------------------
    def forget_flow(self, flow_id):
        """Call when a flow ends (flow-removed from its first switch)."""
        with self._lock:
            self._displaced.pop(flow_id, None)

    def get_displaced_flows(self):
        with self._lock:
            return {
                flow_id: list(path)
                for flow_id, path in self._displaced.items()
            }

    def get_recovering_links(self):
        with self._lock:
            return sorted(self._recovering)

    def recovery_time_since(self, injected_at, a, b):
        """
        Seconds from failure injection (time.time() taken just before the
        link is brought down) until the reroute for that link was sent.
        Returns None if no such failure was handled.
        """
        name = _link_name(link_key(a, b))
        with self._lock:
            for record in self.recovery_log:
                if (record["event"] == "failure"
                        and record["link"] == name
                        and record["detection_time"] >= injected_at):
                    return record["completed_time"] - injected_at
        return None

    def get_stats(self):
        with self._lock:
            return {
                "flows_rerouted": self.flows_rerouted,
                "flows_unrecoverable": self.flows_unrecoverable,
                "flows_restored": self.flows_restored,
                "route_changes": self.flow_manager.route_changes,
                "failed_links": self.get_failed_links(),
                "recovering_links": sorted(self._recovering),
            }

    def save_log(self, path="recovery_events.csv"):
        fields = [
            "event", "link", "detection_time", "completed_time",
            "recovery_time_s", "flows_rerouted", "flows_unrecoverable",
            "flows_restored",
        ]
        with self._lock:
            rows = list(self.recovery_log)
        with open(path, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return path
