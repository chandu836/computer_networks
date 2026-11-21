"""Live proof of congestion-aware routing on the diamond topology."""

import argparse
import os
import re
import subprocess
import tempfile
import time
from functools import partial
from types import SimpleNamespace

from mininet.link import Link
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController

from topology.diamond import DiamondTopo
from traffic import recovery_demo as rd


def middle_switch(net, pattern):
    """Find which diamond middle switch is forwarding this flow."""
    active = []
    for name in rd.PATH_LINKS:
        output = rd.dump_flows(net.get(name))
        if re.search(pattern, output):
            active.append(name)
    return active[0] if len(active) == 1 else None


def start_iperf_server(h2, port):
    return h2.popen(
        ["iperf", "-s", "-u", "-p", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def start_iperf_client(h1, h2, port, duration, rate):
    return h1.popen(
        ["iperf", "-c", h2.IP(), "-u", "-p", str(port),
         "-b", rate, "-t", str(duration)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-datapath", action="store_true")
    parser.add_argument("--controller-port", type=int, default=6653)
    parser.add_argument("--log-dir", default=None)
    args = parser.parse_args()

    log_dir = args.log_dir or tempfile.mkdtemp(prefix="queue_aware_demo_")
    os.makedirs(log_dir, exist_ok=True)
    print("Logs:", log_dir)

    if rd.port_in_use(args.controller_port):
        raise SystemExit(
            f"Controller port {args.controller_port} is already in use. "
            "Stop the existing controller first."
        )

    controller_args = SimpleNamespace(
        routing_mode="proposed",
        recovery="auto",
        stability_interval=3.0,
        controller_port=args.controller_port,
    )
    controller, controller_log = rd.start_controller(
        controller_args, log_dir
    )

    net = None
    servers = []
    clients = []
    ok = False

    try:
        time.sleep(3)
        if controller.poll() is not None:
            raise RuntimeError(
                f"Controller failed. Check {log_dir}/controller.log"
            )

        setLogLevel("warning")
        switch = partial(
            OVSSwitch,
            protocols="OpenFlow13",
            **({"datapath": "user"} if args.user_datapath else {}),
        )

        net = Mininet(
            topo=DiamondTopo(configure_bandwidth=not args.user_datapath),
            switch=switch,
            controller=partial(
                RemoteController,
                ip="127.0.0.1",
                port=args.controller_port,
            ),
            link=Link if args.user_datapath else __import__(
                "mininet.link", fromlist=["TCLink"]
            ).TCLink,
            autoSetMacs=True,
        )
        net.start()

        h1, h2 = net.get("h1", "h2")

        if args.user_datapath:
            for host in (h1, h2):
                host.cmd("ethtool", "-K", host.defaultIntf().name, "tx", "off")

        if not rd.wait_for_connectivity(h1, h2, 20):
            raise RuntimeError("Initial h1-to-h2 connectivity failed")

        print("[PASS] Initial packet forwarding works")

        # Start the first UDP flow. Its first packet makes the controller
        # select and install a route.
        servers.append(start_iperf_server(h2, 5001))
        time.sleep(1)
        first_pattern = (
            rf"udp.*nw_src={re.escape(h1.IP())}.*"
            rf"nw_dst={re.escape(h2.IP())}.*tp_dst=5001"
        )
        first = start_iperf_client(h1, h2, 5001, 18, "12M")
        clients.append(first)

        first_path = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            first_path = middle_switch(net, first_pattern)
            if first_path:
                break
            time.sleep(0.5)

        if not first_path:
            raise RuntimeError(
                "Could not identify the first UDP flow's forwarding path. "
                "Inspect the switch flow tables and controller log."
            )

        other_path = next(name for name in rd.PATH_LINKS if name != first_path)
        print(f"[INFO] First flow selected {first_path}; "
              f"alternate path is {other_path}")

        # Shape the first flow's actual egress link on s1 to 5 Mbps.
        # The 12 Mbps UDP sender should create a measurable queue.
        port = 2 if first_path == "s2" else 3
        interface = f"s1-eth{port}"
        shape = net.get("s1").cmd(
            "tc", "qdisc", "replace", "dev", interface,
            "root", "tbf", "rate", "5mbit",
            "burst", "32kbit", "latency", "400ms"
        )
        print(f"[INFO] Applied 5 Mbps bottleneck to {interface}")
        if shape.strip():
            print("[INFO] tc:", shape.strip())

        time.sleep(4)

        qdisc = net.get("s1").cmd("tc", "-s", "qdisc", "show", "dev", interface)
        print("\n=== Congestion measurement ===")
        print(qdisc.strip())

        backlog_match = re.search(r"backlog\s+(\d+)([KMG]?b)", qdisc)
        drops_match = re.search(r"dropped\s+(\d+)", qdisc)
        backlog = int(backlog_match.group(1)) if backlog_match else 0
        drops = int(drops_match.group(1)) if drops_match else 0

        congested = backlog > 0 or drops > 0
        print(
            f"[{'PASS' if congested else 'WARN'}] "
            f"Queue congestion observed: backlog={backlog_match.group(0) if backlog_match else '0 B'}, "
            f"drops={drops}"
        )

        # A distinct destination port creates a new flow, so the controller
        # makes a fresh path decision using the latest queue measurements.
        servers.append(start_iperf_server(h2, 5002))
        time.sleep(1)

        second_pattern = (
            rf"udp.*nw_src={re.escape(h1.IP())}.*"
            rf"nw_dst={re.escape(h2.IP())}.*tp_dst=5002"
        )
        second = start_iperf_client(h1, h2, 5002, 8, "2M")
        clients.append(second)

        second_path = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            second_path = middle_switch(net, second_pattern)
            if second_path:
                break
            time.sleep(0.5)

        print("\n=== Congestion-aware routing result ===")
        print("First flow path :", first_path)
        print("Second flow path:", second_path)

        rerouted = second_path == other_path
        print(
            f"[{'PASS' if rerouted else 'FAIL'}] "
            "New flow selected the uncongested alternate path"
        )

        # Confirm the first flow stayed on its original path.
        still_first = middle_switch(net, first_pattern)
        print(
            f"[{'PASS' if still_first == first_path else 'FAIL'}] "
            f"First flow remains on {first_path} "
            f"(observed: {still_first})"
        )

        ok = congested and rerouted and still_first == first_path

        print("\n=== Controller routing decisions ===")
        controller_log.flush()
        with open(os.path.join(log_dir, "controller.log")) as handle:
            for line in handle:
                if re.search(r"routed over|queue|congestion", line, re.I):
                    print(line.rstrip())

    finally:
        for client in clients:
            if client.poll() is None:
                client.terminate()
                client.wait()
        for server in servers:
            if server.poll() is None:
                server.terminate()
                server.wait()

        if net is not None:
            net.stop()

        if controller.poll() is None:
            controller.terminate()
            controller.wait()

        controller_log.close()

    print("\nLogs saved in:", log_dir)
    print("RESULT:", "ALL CHECKS PASSED" if ok else "CHECKS FAILED")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
