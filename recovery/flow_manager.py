"""
Member 5: Flow management.

Turns a selected path into OpenFlow rules and keeps track of which rules
belong to which flow, so the recovery manager can find the flows that use
a failed link and move them.

The module does not import os_ken. It only uses the ``ofproto`` and
``ofproto_parser`` objects carried by each datapath, so it can be tested
with fake datapaths on any machine.

Path representation (section 5.2 of the project document):

    hops = [
        {"dpid": 1, "out_port": 2},
        {"dpid": 2, "out_port": 2},
        {"dpid": 4, "out_port": 3},   # last hop: port towards the host
    ]

A flow is identified by a hashable ``flow_id`` (the controller uses the
5-tuple) and an OpenFlow match given as a dict of OFPMatch fields:

    match = {"eth_type": 0x0800, "ipv4_src": "10.0.0.1",
             "ipv4_dst": "10.0.0.2", "ip_proto": 6,
             "tcp_src": 40000, "tcp_dst": 5001}

Usage:

    fm = FlowManager(idle_timeout=1)
    fm.register_datapath(datapath)                # on switch connect
    fm.install_path(flow_id, match, hops)         # new flow
    fm.update_path(flow_id, new_hops)             # reroute
    fm.remove_path(flow_id)                       # delete its rules
"""

import logging
import threading

DEFAULT_PRIORITY = 10
DEFAULT_IDLE_TIMEOUT = 1


def to_dpid(node):
    """Return the integer datapath id for 1, "1" or "s1", else None."""
    if isinstance(node, bool):
        return None
    if isinstance(node, int):
        return node
    text = str(node).strip().lower()
    if text.startswith("s"):
        text = text[1:]
    return int(text) if text.isdigit() else None


def link_key(a, b):
    """Canonical undirected link id (same form as the failure detector)."""
    a, b = to_dpid(a), to_dpid(b)
    return (a, b) if a <= b else (b, a)


def switch_path(path):
    """
    Reduce a path to its switch datapath ids.

    Accepts a node list from the router (["h1", "s1", "s2", "s4", "h2"] or
    [1, 2, 4]) or a hop list ([{"dpid": 1, "out_port": 2}, ...]).
    Host nodes are dropped.
    """
    dpids = []
    for node in path or []:
        if isinstance(node, dict):
            node = node.get("dpid")
        dpid = to_dpid(node)
        if dpid is not None:
            dpids.append(dpid)
    return dpids


def path_links(path):
    """Return the canonical links used by a path."""
    dpids = switch_path(path)
    return [link_key(a, b) for a, b in zip(dpids[:-1], dpids[1:])]


def build_hops(path, get_port, host_port):
    """
    Convert a switch path into hops.

    get_port(current, next) must return the egress port between two
    switches (TopologyManager.get_port). host_port is the port of the
    last switch towards the destination host, which the topology manager
    does not know.

    Returns None if the path is empty or a port is unknown.
    """
    dpids = switch_path(path)
    if not dpids or host_port is None:
        return None

    hops = []
    for current, following in zip(dpids[:-1], dpids[1:]):
        out_port = get_port(current, following)
        if out_port is None:
            return None
        hops.append({"dpid": current, "out_port": out_port})

    hops.append({"dpid": dpids[-1], "out_port": host_port})
    return hops


class _FlowRecord:
    """Rules currently installed for one flow."""

    def __init__(self, flow_id, match, hops, priority, cookie):
        self.flow_id = flow_id
        self.match = dict(match)
        self.hops = [dict(hop) for hop in hops]
        self.priority = priority
        self.cookie = cookie


class FlowManager:
    """Installs, updates and removes the forwarding rules of each flow."""

    def __init__(self, idle_timeout=DEFAULT_IDLE_TIMEOUT, hard_timeout=0,
                 priority=DEFAULT_PRIORITY, logger=None):
        self.idle_timeout = idle_timeout
        self.hard_timeout = hard_timeout
        self.priority = priority
        self._logger = logger or logging.getLogger(__name__)
        self._lock = threading.RLock()

        self._datapaths = {}
        self._flows = {}        # flow_id -> _FlowRecord
        self._by_cookie = {}    # cookie -> flow_id
        self._next_cookie = 1

        # Counters for the evaluation.
        self.rules_installed = 0
        self.rules_removed = 0
        self.route_changes = 0

    # ------------------------------------------------------------------
    # Datapaths
    # ------------------------------------------------------------------
    def register_datapath(self, datapath):
        with self._lock:
            self._datapaths[datapath.id] = datapath

    def unregister_datapath(self, dpid):
        """
        Forget a disconnected switch.

        Flows that enter the network at that switch are dropped from the
        table as well: their source can no longer send, and no flow-removed
        message will ever arrive for them.
        """
        with self._lock:
            self._datapaths.pop(dpid, None)
            stale = [
                flow_id for flow_id, record in self._flows.items()
                if record.hops[0]["dpid"] == dpid
            ]
            for flow_id in stale:
                self._forget(flow_id)
        return stale

    def has_datapath(self, dpid):
        with self._lock:
            return dpid in self._datapaths

    # ------------------------------------------------------------------
    # OpenFlow messages
    # ------------------------------------------------------------------
    def _send_add(self, datapath, match, out_port, priority, cookie=0,
                  notify_removal=False, idle_timeout=None, hard_timeout=None):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        if out_port is None:
            instructions = []   # no actions: drop
        else:
            instructions = [
                parser.OFPInstructionActions(
                    ofproto.OFPIT_APPLY_ACTIONS,
                    [parser.OFPActionOutput(out_port)],
                )
            ]

        flow_mod = parser.OFPFlowMod(
            datapath=datapath,
            cookie=cookie,
            command=ofproto.OFPFC_ADD,
            idle_timeout=(
                self.idle_timeout if idle_timeout is None else idle_timeout
            ),
            hard_timeout=(
                self.hard_timeout if hard_timeout is None else hard_timeout
            ),
            priority=priority,
            flags=ofproto.OFPFF_SEND_FLOW_REM if notify_removal else 0,
            match=parser.OFPMatch(**match),
            instructions=instructions,
        )
        datapath.send_msg(flow_mod)
        self.rules_installed += 1

    def _send_delete(self, datapath, match, priority):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        flow_mod = parser.OFPFlowMod(
            datapath=datapath,
            command=ofproto.OFPFC_DELETE_STRICT,
            out_port=ofproto.OFPP_ANY,
            out_group=ofproto.OFPG_ANY,
            priority=priority,
            match=parser.OFPMatch(**match),
        )
        datapath.send_msg(flow_mod)
        self.rules_removed += 1

    def _write_hops(self, record, hops):
        """
        Send the rules of a path, last switch first.

        The switch where the flow enters is written last, so traffic is
        only steered onto the path once every later switch has its rule.
        Only that first rule asks for a flow-removed message.

        Every write uses a fresh cookie. A flow-removed message that was
        already on its way for the previous rules then no longer matches
        and cannot end the flow by mistake.
        """
        self._by_cookie.pop(record.cookie, None)
        record.cookie = self._next_cookie
        self._next_cookie += 1
        self._by_cookie[record.cookie] = record.flow_id

        for index in range(len(hops) - 1, -1, -1):
            hop = hops[index]
            self._send_add(
                self._datapaths[hop["dpid"]],
                record.match,
                hop["out_port"],
                record.priority,
                cookie=record.cookie,
                notify_removal=(index == 0),
            )

    def _usable(self, hops):
        if not hops:
            return False
        return all(hop["dpid"] in self._datapaths for hop in hops)

    def _forget(self, flow_id):
        record = self._flows.pop(flow_id, None)
        if record is not None:
            self._by_cookie.pop(record.cookie, None)
        return record

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------
    def install_path(self, flow_id, match, hops, priority=None):
        """
        Install forwarding rules along a path.

        If the flow already has a different path, this behaves like
        update_path(). Returns False (and sends nothing) when a switch of
        the path is not connected.
        """
        with self._lock:
            if flow_id in self._flows:
                return self.update_path(flow_id, hops)

            if not self._usable(hops):
                return False

            record = _FlowRecord(
                flow_id,
                match,
                hops,
                self.priority if priority is None else priority,
                cookie=0,
            )

            self._write_hops(record, record.hops)
            self._flows[flow_id] = record
            return True

    def update_path(self, flow_id, new_hops):
        """
        Replace the current forwarding path of a flow.

        The new rules are written first (make before break). Rules on
        switches that are no longer on the path are deleted afterwards.
        """
        with self._lock:
            record = self._flows.get(flow_id)
            if record is None or not self._usable(new_hops):
                return False

            new_hops = [dict(hop) for hop in new_hops]
            old_hops = record.hops
            changed = old_hops != new_hops

            self._write_hops(record, new_hops)

            kept = {hop["dpid"] for hop in new_hops}
            for hop in old_hops:
                datapath = self._datapaths.get(hop["dpid"])
                if hop["dpid"] not in kept and datapath is not None:
                    self._send_delete(datapath, record.match, record.priority)

            record.hops = new_hops
            if changed:
                self.route_changes += 1
            return True

    def remove_path(self, flow_id):
        """Remove the forwarding rules of a flow from every switch."""
        with self._lock:
            record = self._forget(flow_id)
            if record is None:
                return False

            for hop in record.hops:
                datapath = self._datapaths.get(hop["dpid"])
                if datapath is not None:
                    self._send_delete(datapath, record.match, record.priority)
            return True

    def repair_hop(self, flow_id, dpid):
        """
        Write the rule of one switch again.

        Used when a switch in the middle of a path reports a table miss
        for a flow that is still active (its rule timed out earlier than
        the rule of the first switch).
        """
        with self._lock:
            record = self._flows.get(flow_id)
            datapath = self._datapaths.get(dpid)
            if record is None or datapath is None:
                return False

            for index, hop in enumerate(record.hops):
                if hop["dpid"] == dpid:
                    self._send_add(
                        datapath,
                        record.match,
                        hop["out_port"],
                        record.priority,
                        cookie=record.cookie,
                        notify_removal=(index == 0),
                    )
                    return True
            return False

    def install_drop(self, dpid, match, priority=None, hard_timeout=1):
        """
        Drop a flow at one switch for a short time.

        Used when no path exists, so the switch does not send every packet
        of the flow to the controller. The rule is not tracked: it expires
        by itself, or is overwritten by the next install_path().
        """
        with self._lock:
            datapath = self._datapaths.get(dpid)
            if datapath is None:
                return False

            self._send_add(
                datapath,
                match,
                None,
                self.priority if priority is None else priority,
                idle_timeout=0,
                hard_timeout=hard_timeout,
            )
            return True

    def handle_flow_removed(self, dpid, cookie):
        """
        Process a flow-removed (timeout) message.

        Returns the flow_id that ended, or None when the message does not
        belong to the first switch of a known flow.
        """
        with self._lock:
            flow_id = self._by_cookie.get(cookie)
            if flow_id is None:
                return None

            if self._flows[flow_id].hops[0]["dpid"] != dpid:
                return None

            self._forget(flow_id)
            return flow_id

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def has_flow(self, flow_id):
        with self._lock:
            return flow_id in self._flows

    def get_path(self, flow_id):
        """Return a copy of the hops of a flow, or None."""
        with self._lock:
            record = self._flows.get(flow_id)
            if record is None:
                return None
            return [dict(hop) for hop in record.hops]

    def get_match(self, flow_id):
        with self._lock:
            record = self._flows.get(flow_id)
            return dict(record.match) if record is not None else None

    def get_active_flows(self):
        """Return {flow_id: hops} for every installed flow."""
        with self._lock:
            return {
                flow_id: [dict(hop) for hop in record.hops]
                for flow_id, record in self._flows.items()
            }

    def flows_using_link(self, a, b):
        """Return the flows whose path crosses link a-b in either direction."""
        key = link_key(a, b)
        with self._lock:
            return [
                flow_id for flow_id, record in self._flows.items()
                if key in path_links(record.hops)
            ]

    def flows_using_switch(self, dpid):
        dpid = to_dpid(dpid)
        with self._lock:
            return [
                flow_id for flow_id, record in self._flows.items()
                if dpid in switch_path(record.hops)
            ]
