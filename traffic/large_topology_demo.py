
"""
Live congestion-aware routing demonstration.

Topology:
    10 switches, 4 hosts, 19 links.

Experiment:
    1. Start the proposed SDN controller.
    2. Verify all host pairs.
    3. Start a high-rate UDP flow from h1 to h2.
    4. Discover its actual forwarding path.
    5. Apply a 5 Mbps bottleneck to its first inter-switch link.
    6. Measure real queue backlog and drops.
    7. Start a second UDP flow.
    8. Verify that the second flow uses a different path.
    9. Verify that the first flow remains on its original path.

Run:
    sudo -E .venv/bin/python -m traffic.large_topology_demo --user-datapath
"""

import argparse
import os
import re
import sys
import time
import tempfile
import subprocess
from functools import partial
from types import SimpleNamespace

from mininet.link import Link
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController

from topology.large_topology import LargeTopo
from traffic import recovery_demo as rd


def flow_output_port(net, switch_name, pattern):
    """Find the output port of the matching flow rule."""

    output = rd.dump_flows(net.get(switch_name))
    matches = []

    for line in output.splitlines():
        if not re.search(pattern, line):
            continue

        port_match = re.search(r"actions=.*?output:(\d+)", line)
        if not port_match:
            continue

        packet_match = re.search(r"n_packets=(\d+)", line)
        packets = int(packet_match.group(1)) if packet_match else 0

        matches.append((packets, int(port_match.group(1))))

    if not matches:
        return None

    return max(matches)[1]


def peer_for_port(net, switch_name, port):
    """Find the neighbor connected to a switch output port."""

    switch = net.get(switch_name)
    intf = switch.intfs.get(port)

    if intf is None or intf.link is None:
        return None

    link = intf.link

    if link.intf1.node == switch:
        peer = link.intf2.node
    else:
        peer = link.intf1.node

    return peer.name


def trace_flow_path(net, pattern, source="s1", max_hops=12):
    """
    Reconstruct the forwarding path from the installed OpenFlow rules.

    Returns a path such as:
        ['s1', 's2', 's3', 's4', 's10']
    """

    current = source
    path = [current]
    visited = set()

    for _ in range(max_hops):
        if current in visited:
            return None

        visited.add(current)

        port = flow_output_port(net, current, pattern)

        if port is None:
            return None

        peer = peer_for_port(net, current, port)

        if peer is None:
            return None

        if peer.startswith("h"):
            return path + [peer]

        if not peer.startswith("s"):
            return None

        path.append(peer)
        current = peer

    return None


def start_server(host, port):
    return host.popen(
        ["iperf", "-s", "-u", "-p", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def start_client(host, destination, port, duration, rate):
    return host.popen(
        [
            "iperf", "-c", destination.IP(),
            "-u", "-p", str(port),
            "-b", rate, "-t", str(duration),
            "-l", "1400",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_for_path(net, pattern, timeout=12):
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        path = trace_flow_path(net, pattern)

        if path:
            return path

        time.sleep(0.5)

    return None

def wait_for_controller_path(log_dir, src_ip, dst_ip, port, timeout=12):
    log_file = os.path.join(log_dir, "controller.log")
    deadline = time.monotonic() + timeout

    pattern = re.compile(
        rf"Flow \('{re.escape(src_ip)}', "
        rf"'{re.escape(dst_ip)}', 17, \d+, {port}\) "
        rf"routed over \[([0-9, ]+)\]"
    )

    while time.monotonic() < deadline:
        try:
            with open(log_file, "r") as f:
                content = f.read()

            matches = pattern.findall(content)
            if matches:
                switches = [
                    f"s{int(x.strip())}"
                    for x in matches[-1].split(",")
                ]
                return ["h1"] + switches + ["h2"]

        except (FileNotFoundError, PermissionError):
            pass

        time.sleep(0.5)

    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-datapath", action="store_true")
    parser.add_argument("--controller-port", type=int, default=6653)
    parser.add_argument("--log-dir", default=None)
    args = parser.parse_args()

    log_dir = args.log_dir or tempfile.mkdtemp(
        prefix="large_congestion_demo_"
    )
    os.makedirs(log_dir, exist_ok=True)

    print("Logs:", log_dir)

    if rd.port_in_use(args.controller_port):
        raise SystemExit(
            f"Controller port {args.controller_port} is already in use."
        )

    controller_args = SimpleNamespace(
        routing_mode="proposed",
        recovery="auto",
        stability_interval=3.0,
        controller_port=args.controller_port,
    )

    controller = None
    controller_log = None
    net = None
    servers = []
    clients = []
    ok = False

    try:
        print("\n===== STARTING CONTROLLER =====")

        controller, controller_log = rd.start_controller(
            controller_args, log_dir
        )

        time.sleep(3)

        if controller.poll() is not None:
            raise RuntimeError(
                f"Controller failed. Check {log_dir}/controller.log"
            )

        print("[PASS] Controller started")

        setLogLevel("warning")

        switch = partial(
            OVSSwitch,
            protocols="OpenFlow13",
            **(
                {"datapath": "user"}
                if args.user_datapath else {}
            ),
        )

        net = Mininet(
            topo=LargeTopo(
                configure_bandwidth=not args.user_datapath
            ),
            switch=switch,
            controller=partial(
                RemoteController,
                ip="127.0.0.1",
                port=args.controller_port,
            ),
            link=Link if args.user_datapath else
                __import__(
                    "mininet.link", fromlist=["TCLink"]
                ).TCLink,
            autoSetMacs=True,
        )

        print("\n===== STARTING TOPOLOGY =====")
        net.start()

        h1, h2 = net.get("h1", "h2")

        if args.user_datapath:
            for host in net.hosts:
                host.cmd(
                    "ethtool", "-K",
                    host.defaultIntf().name, "tx", "off"
                )

        print(
            f"Hosts: {len(net.hosts)}, "
            f"Switches: {len(net.switches)}, "
            f"Links: {len(net.links)}"
        )

        if not rd.wait_for_connectivity(h1, h2, 20):
            raise RuntimeError("Initial h1-to-h2 connectivity failed")

        print("[PASS] Initial h1-to-h2 connectivity")

        # Verify all six host pairs.
        print("\n===== HOST CONNECTIVITY =====")

        pairs = [
            ("h1", "h2"),
            ("h1", "h3"),
            ("h1", "h4"),
            ("h2", "h3"),
            ("h2", "h4"),
            ("h3", "h4"),
        ]

        for src_name, dst_name in pairs:
            reachable = rd.wait_for_connectivity(
                net.get(src_name), net.get(dst_name), 15
            )

            print(
                f"[{'PASS' if reachable else 'FAIL'}] "
                f"{src_name} -> {dst_name}"
            )

            if not reachable:
                raise RuntimeError(
                    f"Host connectivity failed: {src_name} -> {dst_name}"
                )

        # First high-rate UDP flow.
        print("\n===== FIRST UDP FLOW =====")

        first_port = 5001

        first_pattern = (
            rf"udp.*"
            rf"(?=.*nw_src={re.escape(h1.IP())})"
            rf"(?=.*nw_dst={re.escape(h2.IP())})"
            rf"(?=.*tp_dst={first_port})"
        )

        servers.append(start_server(h2, first_port))
        time.sleep(1)

        clients.append(
            start_client(h1, h2, first_port, 22, "12M")
        )

        first_path = wait_for_controller_path(
            log_dir, h1.IP(), h2.IP(), first_port
        )

        if not first_path or len(first_path) < 3:
            raise RuntimeError(
                "Could not discover the first flow's complete path."
            )

        print("[PASS] First flow forwarding path discovered")
        print("First flow path:", " -> ".join(first_path))

        # The first link after s1 is the actual link used by this flow.
        first_hop = first_path[2]

        first_switch = net.get("s1")
        first_interface = None

        for port, intf in first_switch.intfs.items():
            if intf.link is None:
                continue

            peer = (
                intf.link.intf2.node
                if intf.link.intf1.node == first_switch
                else intf.link.intf1.node
            )

            if peer.name == first_hop:
                first_interface = intf.name
                break

        if first_interface is None:
            raise RuntimeError("Could not identify the first-hop interface.")

        interface = first_interface

        # Apply a real Linux traffic-control bottleneck.
        print("\n===== APPLYING CONGESTION =====")

        shape = net.get("s1").cmd(
            "tc", "qdisc", "replace",
            "dev", interface,
            "root", "tbf",
            "rate", "5mbit",
            "burst", "32kbit",
            "latency", "400ms",
        )

        print(
            f"[INFO] Applied 5 Mbps bottleneck "
            f"on s1 -> {first_hop} ({interface})"
        )

        if shape.strip():
            print("[INFO] tc:", shape.strip())

        # Allow queue measurements to register congestion.
        time.sleep(5)

        qdisc = net.get("s1").cmd(
            "tc", "-s", "qdisc", "show",
            "dev", interface
        )

        print("\n===== REAL QUEUE MEASUREMENTS =====")
        print(qdisc.strip())

        backlog_match = re.search(
            r"backlog\s+(\d+)([KMG]?b)", qdisc
        )

        drops_match = re.search(r"dropped\s+(\d+)", qdisc)

        backlog = int(backlog_match.group(1)) if backlog_match else 0
        drops = int(drops_match.group(1)) if drops_match else 0

        congested = backlog > 0 or drops > 0

        print("Queue backlog:", backlog_match.group(0) if backlog_match else "0 B")
        print("Queue drops:", drops)

        if not congested:
            raise RuntimeError(
                "No queue congestion observed. "
                "Inspect the qdisc output before continuing."
            )

        print("[PASS] Real queue congestion observed")

        # Second flow, with a different destination port.
        print("\n===== SECOND UDP FLOW =====")

        second_port = 5002

        second_pattern = (
            rf"udp.*"
            rf"(?=.*nw_src={re.escape(h1.IP())})"
            rf"(?=.*nw_dst={re.escape(h2.IP())})"
            rf"(?=.*tp_dst={second_port})"
        )

        servers.append(start_server(h2, second_port))
        time.sleep(1)

        clients.append(
            start_client(h1, h2, second_port, 10, "2M")
        )

        second_path = wait_for_controller_path(
            log_dir, h1.IP(), h2.IP(), second_port
        )

        if not second_path:
            raise RuntimeError(
                "Could not discover the second flow's path."
            )

        print("\n===== ROUTING RESULT =====")
        print("First flow :", " -> ".join(first_path))
        print("Second flow:", " -> ".join(second_path))

        alternate_selected = second_path != first_path

        print(
            f"[{'PASS' if alternate_selected else 'FAIL'}] "
            "Second flow selected a different path"
        )

        # Verify that the original flow did not migrate.
        time.sleep(1)

        still_first = wait_for_controller_path(
            log_dir, h1.IP(), h2.IP(), first_port, timeout=3
        )

        stable = still_first == first_path

        print(
            f"[{'PASS' if stable else 'FAIL'}] "
            "Original flow remains on its initial path"
        )

        ok = congested and alternate_selected and stable

        # Print controller decisions.
        print("\n===== CONTROLLER ROUTING DECISIONS =====")

        controller_log.flush()

        with open(os.path.join(log_dir, "controller.log")) as handle:
            for line in handle:
                if re.search(
                    r"routed over|queue|congestion",
                    line,
                    re.I,
                ):
                    print(line.rstrip())

    except Exception as exc:
        print(f"\n[ERROR] {type(exc).__name__}: {exc}")
        ok = False

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

        if controller is not None and controller.poll() is None:
            controller.terminate()
            controller.wait()

        if controller_log is not None:
            controller_log.close()

    print("\nLogs saved in:", log_dir)
    print("RESULT:", "ALL CHECKS PASSED" if ok else "CHECKS FAILED")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
