
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
