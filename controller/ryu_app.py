
import os

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import (
    CONFIG_DISPATCHER,
    DEAD_DISPATCHER,
    MAIN_DISPATCHER,
    set_ev_cls,
)
from os_ken.lib import hub
from os_ken.lib.packet import arp, ether_types, ethernet, ipv4, packet, tcp, udp
from os_ken.ofproto import ofproto_v1_3
from os_ken.topology import api, event

from controller.route_selector import (
    DEFAULT_ROUTING_MODE,
    ROUTING_MODES,
    make_router,
)
from controller.topology_manager import TopologyManager
from controller.queue_monitor import QueueMonitor
from failure_detection.detector import FailureDetector
from recovery.flow_manager import FlowManager, build_hops, switch_path
from recovery.recovery_manager import RecoveryManager
from routing.queue_aware_routing import calculate_path_cost


class SDNController(app_manager.OSKenApp):

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    QUEUE_POLL_INTERVAL = 1.0

    # Member 5: forwarding rules.
    # The idle timeout must equal the router's flowlet timeout, so that a
    # flow reaches the controller again only after a flowlet gap.
    FLOW_IDLE_TIMEOUT = 1
    FLOW_PRIORITY = 10
    # Rules without port numbers (ICMP, IP fragments) sit just below.
    ADDRESS_ONLY_PRIORITY = 9
    NO_ROUTE_DROP_TIMEOUT = 1
    RECOVERY_POLL_INTERVAL = 0.5

    def __init__(self, *args, **kwargs):
        super(SDNController, self).__init__(*args, **kwargs)

        # Settings come from the environment, see docs/RECOVERY.md.
        self.routing_mode = os.environ.get(
            "SDN_ROUTING_MODE", DEFAULT_ROUTING_MODE
        ).strip().lower()

        if self.routing_mode not in ROUTING_MODES:
            raise ValueError(
                "SDN_ROUTING_MODE=%r is not one of %s"
                % (self.routing_mode, ", ".join(ROUTING_MODES))
            )

        # Proactive recovery belongs to the proposed approach. The
        # baselines only recover when their rules time out.
        recovery_setting = os.environ.get(
            "SDN_RECOVERY", "auto"
        ).strip().lower()

        if recovery_setting == "auto":
            self.recovery_enabled = self.routing_mode == "proposed"
        else:
            self.recovery_enabled = recovery_setting in ("1", "on", "true", "yes")

        self.log_dir = os.environ.get("SDN_LOG_DIR")

        # Member 1: live topology and port mappings.
        self.topology_manager = TopologyManager(self)

        # Member 2: queue monitoring based on discovered topology.
        self.queue_monitor = QueueMonitor.from_topology_manager(
            self.topology_manager
        )

        # Member 4: link and switch failure detection.
        # Member 5 subscribes with:
        #     self.failure_detector.register_listener(callback)
        # where callback receives a common.models.FailureEvent.
        self.failure_detector = FailureDetector()

        # Member 3: path selection for the configured routing mode.
        self.router = make_router(
            self.routing_mode,
            queue_source=self.queue_monitor,
            flowlet_timeout_s=float(self.FLOW_IDLE_TIMEOUT),
        )

        # Member 5: forwarding rules and failure recovery.
        self.flow_manager = FlowManager(
            idle_timeout=self.FLOW_IDLE_TIMEOUT,
            priority=self.FLOW_PRIORITY,
            logger=self.logger,
        )

        self.recovery_manager = RecoveryManager(
            topology_manager=self.topology_manager,
            router=self.router,
            flow_manager=self.flow_manager,
            failure_detector=self.failure_detector,
            path_cost=self._path_cost,
            stability_interval_s=float(
                os.environ.get("SDN_STABILITY_INTERVAL", 3.0)
            ),
            restore_hysteresis=float(
                os.environ.get("SDN_RESTORE_HYSTERESIS", 0.5)
            ),
            logger=self.logger,
        )

        if self.recovery_enabled:
            self.failure_detector.register_listener(
                self.recovery_manager.on_failure_event
            )

        # Host MAC -> (dpid, port), learned from packets on edge ports.
        self.hosts = {}
        # (dpid, port) of every port ever seen on a switch-to-switch link.
        self._link_ports = set()
        # dpid -> [port numbers]
        self._switch_ports = {}
        self._datapaths = {}
        self._saved_log_sizes = (0, 0)

        # Use OS-Ken's cooperative green-thread scheduler.
        # Do not use QueueMonitor.start() inside the controller.
        self.queue_thread = hub.spawn(self._queue_poll_loop)
        self.recovery_thread = hub.spawn(self._recovery_loop)

        self.logger.info(
            "SDN Controller initialized: routing mode=%s, recovery=%s",
            self.routing_mode,
            "on" if self.recovery_enabled else "off",
        )

    def _queue_poll_loop(self):
        """Collect queue statistics periodically without a native thread."""

        while True:
            try:
                self.queue_monitor.collect_stats()

            except Exception:
                self.logger.exception(
                    "Unexpected error in queue-monitor polling"
                )

            hub.sleep(self.QUEUE_POLL_INTERVAL)

    def _recovery_loop(self):
        """Member 5: evaluate path restoration and write the event logs."""

        while True:
            try:
                if self.recovery_enabled:
                    self.recovery_manager.process_pending()

                self._save_logs()

            except Exception:
                self.logger.exception(
                    "Unexpected error in recovery processing"
                )

            hub.sleep(self.RECOVERY_POLL_INTERVAL)

    def _save_logs(self):
        """Write the failure and recovery logs when SDN_LOG_DIR is set."""

        if not self.log_dir:
            return

        sizes = (
            len(self.failure_detector.event_log),
            len(self.recovery_manager.recovery_log),
        )

        if sizes == self._saved_log_sizes:
            return

        os.makedirs(self.log_dir, exist_ok=True)
        self.failure_detector.save_log(
            os.path.join(self.log_dir, "failure_events.csv")
        )
        self.recovery_manager.save_log(
            os.path.join(self.log_dir, "recovery_events.csv")
        )
        self._saved_log_sizes = sizes

    def _path_cost(self, path):
        """Cost used to decide whether a recovered path is worth reusing."""

        if self.routing_mode in ("shortest_path", "ecmp"):
            return float(len(switch_path(path)) - 1)

        return calculate_path_cost(path, self.queue_monitor)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """
        Install a table-miss rule.

        OS-Ken's topology service installs its own higher-priority LLDP
        rule. This rule sends other unmatched packets to the controller.
        """

        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        match = parser.OFPMatch()

        actions = [
            parser.OFPActionOutput(
                ofproto.OFPP_CONTROLLER,
                ofproto.OFPCML_NO_BUFFER,
            )
        ]

        instructions = [
            parser.OFPInstructionActions(
                ofproto.OFPIT_APPLY_ACTIONS,
                actions,
            )
        ]

        flow_mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=0,
            match=match,
            instructions=instructions,
        )

        datapath.send_msg(flow_mod)

        self.logger.info(
            "Switch connected: datapath ID=%s",
            datapath.id,
        )

    @set_ev_cls(
        ofp_event.EventOFPStateChange,
        [MAIN_DISPATCHER, DEAD_DISPATCHER],
    )
    def switch_state_handler(self, ev):
        datapath = ev.datapath

        if datapath.id is None:
            return

        if ev.state == MAIN_DISPATCHER:
            self._datapaths[datapath.id] = datapath
            self.flow_manager.register_datapath(datapath)

            self.logger.info(
                "Switch entered MAIN_DISPATCHER: datapath ID=%s",
                datapath.id,
            )

        elif ev.state == DEAD_DISPATCHER:
            # Ignore the old connection of a switch that already came back.
            if self._datapaths.get(datapath.id) is not datapath:
                return

            del self._datapaths[datapath.id]

            for flow_id in self.flow_manager.unregister_datapath(datapath.id):
                self.recovery_manager.forget_flow(flow_id)

            self.logger.info(
                "Switch disconnected: datapath ID=%s",
                datapath.id,
            )

    @set_ev_cls(event.EventSwitchEnter)
    @set_ev_cls(event.EventSwitchLeave)
    @set_ev_cls(event.EventLinkAdd)
    @set_ev_cls(event.EventLinkDelete)
    def topology_event_handler(self, ev):
        """Synchronize topology and queue-monitor state after every event."""

        self.logger.info(
            "Topology event received: %s",
            type(ev).__name__,
        )

        # Member 4: feed the failure detector before anything else.
        try:
            if isinstance(ev, event.EventSwitchEnter):
                self.failure_detector.on_switch_enter(ev.switch.dp.id)
            elif isinstance(ev, event.EventSwitchLeave):
                self.failure_detector.on_switch_leave(ev.switch.dp.id)
            elif isinstance(ev, event.EventLinkAdd):
                self.failure_detector.on_link_add(
                    ev.link.src.dpid, ev.link.src.port_no, ev.link.dst.dpid
                )
            elif isinstance(ev, event.EventLinkDelete):
                self.failure_detector.on_link_delete(
                    ev.link.src.dpid, ev.link.dst.dpid
                )
        except Exception:
            self.logger.exception("Failure detector error")

        try:
            self.topology_manager.discover_topology()

            # Always synchronize, including when zero links are discovered.
            self.queue_monitor.sync_with_topology_manager(
                self.topology_manager
            )

            # Member 5: edge ports and host locations follow the topology.
            self._refresh_ports()

            self.logger.info(
                "Controller state: %d switches, %d directed links, "
                "%d monitored links",
                len(self.topology_manager.get_switches()),
                len(self.topology_manager.get_links()),
                len(self.queue_monitor.get_monitored_links()),
            )

        except Exception:
            self.logger.exception(
                "Failed to synchronize topology state"
            )

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        """Member 4: OpenFlow port-status = fastest link failure signal."""

        msg = ev.msg
        ofproto = msg.datapath.ofproto
        port = msg.desc

        is_down = bool(
            (port.state & ofproto.OFPPS_LINK_DOWN)
            or (port.config & ofproto.OFPPC_PORT_DOWN)
        )

        self.logger.debug(
            "Port status: switch %s port %s reason=%s state=%s config=%s",
            msg.datapath.id, port.port_no, msg.reason, port.state,
            port.config,
        )

        try:
            self.failure_detector.on_port_status(
                msg.datapath.id, port.port_no, is_down
            )
        except Exception:
            self.logger.exception("Failure detector error")

    # ------------------------------------------------------------------
    # Member 5: forwarding
    # ------------------------------------------------------------------
    def _refresh_ports(self):
        """Update the port lists used for host learning and flooding."""

        graph = self.topology_manager.get_graph()

        for src, dst, data in graph.edges(data=True):
            self._link_ports.add((src, data.get("src_port")))
            self._link_ports.add((dst, data.get("dst_port")))

        self._switch_ports = {
            switch.dp.id: [
                port.port_no for port in getattr(switch, "ports", [])
            ]
            for switch in api.get_switch(self, None)
        }

        # A port that turned out to be a switch link has no host on it.
        for mac, location in list(self.hosts.items()):
            if location in self._link_ports:
                del self.hosts[mac]

    def _send_packet(self, dpid, out_ports, data):
        """Send a packet out of one or more ports of a switch."""

        datapath = self._datapaths.get(dpid)

        if datapath is None or not out_ports:
            return False

        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        datapath.send_msg(
            parser.OFPPacketOut(
                datapath=datapath,
                buffer_id=ofproto.OFP_NO_BUFFER,
                in_port=ofproto.OFPP_CONTROLLER,
                actions=[
                    parser.OFPActionOutput(port) for port in out_ports
                ],
                data=data,
            )
        )
        return True

    def _flood_to_edge(self, data, ingress):
        """
        Deliver a packet to every host-facing port except the ingress.

        The packet never travels over switch links, so the loop in the
        diamond cannot cause a broadcast storm.
        """

        for dpid, ports in self._switch_ports.items():
            out_ports = [
                port for port in ports
                if (dpid, port) not in self._link_ports
                and (dpid, port) != ingress
            ]
            self._send_packet(dpid, out_ports, data)

    @staticmethod
    def _classify(ip_header, pkt):
        """
        Return (flow_id, match, has_ports) for an IPv4 packet.

        TCP and UDP flows are identified by the 5-tuple. Other protocols
        and IP fragments (which carry no usable port numbers) are
        identified by addresses and protocol only.
        """

        match = {
            "eth_type": ether_types.ETH_TYPE_IP,
            "ipv4_src": ip_header.src,
            "ipv4_dst": ip_header.dst,
            "ip_proto": ip_header.proto,
        }
        src_port = dst_port = None

        fragmented = bool(ip_header.flags & 0x1) or ip_header.offset != 0

        if not fragmented:
            tcp_header = pkt.get_protocol(tcp.tcp)
            udp_header = pkt.get_protocol(udp.udp)

            if tcp_header is not None:
                src_port = tcp_header.src_port
                dst_port = tcp_header.dst_port
                match["tcp_src"] = src_port
                match["tcp_dst"] = dst_port

            elif udp_header is not None:
                src_port = udp_header.src_port
                dst_port = udp_header.dst_port
                match["udp_src"] = src_port
                match["udp_dst"] = dst_port

        flow_id = (
            ip_header.src,
            ip_header.dst,
            ip_header.proto,
            src_port,
            dst_port,
        )

        return flow_id, match, src_port is not None

    def _route_flow(self, flow_id, match, priority, src_dpid,
                    dst_dpid, dst_port):
        """Select a path for a flow and install it. Returns True on success."""

        if src_dpid == dst_dpid:
            path = [src_dpid]

        else:
            decision = self.router.select_path(
                self.topology_manager.get_graph(),
                src_dpid,
                dst_dpid,
                flow_id=flow_id,
                failed_links=self.failure_detector.get_failed_links(),
                failed_switches=self.failure_detector.get_failed_switches(),
            )

            if decision is None:
                return False

            path = decision.path

        hops = build_hops(path, self.topology_manager.get_port, dst_port)

        if hops is None:
            return False

        previous = self.flow_manager.get_path(flow_id)

        if not self.flow_manager.install_path(
            flow_id, match, hops, priority=priority
        ):
            return False

        if previous != hops:
            self.logger.info(
                "Flow %s routed over %s",
                flow_id,
                switch_path(hops),
            )

        return True

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        """Member 5: learn hosts, select a path and install its rules."""

        msg = ev.msg
        dpid = msg.datapath.id
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        if eth is None:
            return

        arp_header = pkt.get_protocol(arp.arp)
        ip_header = pkt.get_protocol(ipv4.ipv4)

        # LLDP belongs to topology discovery; IPv6 is not routed.
        if arp_header is None and ip_header is None:
            return

        ingress = (dpid, in_port)

        # A packet enters the network here only if the port is not a
        # switch link and its sender is not already known somewhere else.
        from_edge = (
            ingress not in self._link_ports
            and self.hosts.get(eth.src, ingress) == ingress
        )

        if from_edge:
            self.hosts[eth.src] = ingress

        destination = self.hosts.get(eth.dst)

        if destination is None:
            # Broadcast (ARP request) or a host we have not seen yet.
            if from_edge:
                self._flood_to_edge(msg.data, ingress)
            return

        dst_dpid, dst_port = destination

        if ip_header is not None:
            flow_id, match, has_ports = self._classify(ip_header, pkt)
            priority = (
                self.FLOW_PRIORITY if has_ports else self.ADDRESS_ONLY_PRIORITY
            )

            if from_edge:
                if not self._route_flow(
                    flow_id, match, priority, dpid, dst_dpid, dst_port
                ):
                    # No path: drop the flow at this switch for a moment
                    # instead of receiving each of its packets.
                    self.flow_manager.install_drop(
                        dpid,
                        match,
                        priority=priority,
                        hard_timeout=self.NO_ROUTE_DROP_TIMEOUT,
                    )
                    return

            else:
                # Table miss in the middle of the network: put the rule
                # of this switch back if the flow is still active.
                self.flow_manager.repair_hop(flow_id, dpid)

        # The packet that reached the controller is delivered directly.
        self._send_packet(dst_dpid, [dst_port], msg.data)

    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def flow_removed_handler(self, ev):
        """Member 5: a flow timed out at its first switch, so it ended."""

        msg = ev.msg
        ofproto = msg.datapath.ofproto

        if msg.reason not in (
            ofproto.OFPRR_IDLE_TIMEOUT,
            ofproto.OFPRR_HARD_TIMEOUT,
        ):
            return

        flow_id = self.flow_manager.handle_flow_removed(
            msg.datapath.id,
            msg.cookie,
        )

        if flow_id is not None:
            self.recovery_manager.forget_flow(flow_id)
