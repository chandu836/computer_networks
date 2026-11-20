
from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import (
    CONFIG_DISPATCHER,
    MAIN_DISPATCHER,
    set_ev_cls,
)
from os_ken.lib import hub
from os_ken.ofproto import ofproto_v1_3
from os_ken.topology import event

from controller.topology_manager import TopologyManager
from controller.queue_monitor import QueueMonitor
from failure_detection.detector import FailureDetector


class SDNController(app_manager.OSKenApp):

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    QUEUE_POLL_INTERVAL = 1.0

    def __init__(self, *args, **kwargs):
        super(SDNController, self).__init__(*args, **kwargs)

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

        # Use OS-Ken's cooperative green-thread scheduler.
        # Do not use QueueMonitor.start() inside the controller.
        self.queue_thread = hub.spawn(self._queue_poll_loop)

        self.logger.info("SDN Controller initialized")

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

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER])
    def switch_state_handler(self, ev):
        datapath = ev.datapath

        self.logger.info(
            "Switch entered MAIN_DISPATCHER: datapath ID=%s",
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

        try:
            self.failure_detector.on_port_status(
                msg.datapath.id, port.port_no, is_down
            )
        except Exception:
            self.logger.exception("Failure detector error")
