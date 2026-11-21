
"""Generic scalability validation for the SDN routing controller."""

import argparse
import csv
import json
import os
import re
import sys
import tempfile
import time
import signal
import subprocess
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import networkx as nx

from traffic import recovery_demo as rd
from topology.diamond import DiamondTopo
from topology.large_topology import LargeTopo
from topology.validation_topologies import (
    LinearTopo,
    RingTopo,
    MeshTopo,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULT_DIR = PROJECT_ROOT / "results" / "scalability"

TOPOLOGIES = {
    "linear": (LinearTopo, False),
    "ring": (RingTopo, True),
    "mesh": (MeshTopo, True),
    "diamond": (DiamondTopo, True),
    "large": (LargeTopo, True),
}


def wait_for_controller_state(log_path, expected, timeout=30):
    """Wait until the controller reports the expected topology state."""

    deadline = time.monotonic() + timeout

    pattern = re.compile(
        r"Controller state: (\d+) switches, "
        r"(\d+) directed links, (\d+) monitored links"
    )

    while time.monotonic() < deadline:
        try:
            content = Path(log_path).read_text(errors="replace")
            matches = pattern.findall(content)

            if matches:
                switches, links, monitored = map(int, matches[-1])

                if (switches, links, monitored) == expected:
                    return True, (switches, links, monitored)

        except OSError:
            pass

        time.sleep(0.5)

    return False, None


def inspect_topology(topo):
    """Calculate expected topology counts and structural redundancy."""

    hosts = list(topo.hosts())
    switches = list(topo.switches())
    switch_set = set(switches)
    host_set = set(hosts)

    graph = nx.Graph()
    graph.add_nodes_from(hosts)
    graph.add_nodes_from(switches)

    switch_graph = nx.Graph()
    switch_graph.add_nodes_from(switches)

    host_attachment = {}

    for link in topo.links():
        a, b = link[0], link[1]
        graph.add_edge(a, b)

        if a in switch_set and b in switch_set:
            switch_graph.add_edge(a, b)

        if a in host_set and b in switch_set:
            host_attachment[a] = b
        elif b in host_set and a in switch_set:
            host_attachment[b] = a

    switch_links = switch_graph.number_of_edges()

    # OS-Ken discovers both directions of each inter-switch link.
    expected = (
        len(switches),
        2 * switch_links,
        2 * switch_links,
    )

    host_pairs = [
        (hosts[i], hosts[j])
        for i in range(len(hosts))
        for j in range(i + 1, len(hosts))
    ]

    alternate_pairs = 0

    for src, dst in host_pairs:
        a = host_attachment.get(src)
        b = host_attachment.get(dst)

        if a is None or b is None or a == b:
            continue

        try:
            if nx.edge_connectivity(switch_graph, a, b) >= 2:
                alternate_pairs += 1
        except nx.NetworkXError:
            pass

    return {
        "hosts": hosts,
        "switches": switches,
        "physical_links": len(topo.links()),
        "switch_links": switch_links,
        "expected_controller_state": expected,
        "host_pairs": host_pairs,
        "alternate_pairs": alternate_pairs,
        "switch_graph": switch_graph,
        "host_attachment": host_attachment,
    }


def check_connectivity(net, host_pairs):
    """Test communication in both directions for every host pair."""

    failures = []

    for src_name, dst_name in host_pairs:
        src = net.get(src_name)
        dst = net.get(dst_name)

        for a, b in ((src, dst), (dst, src)):
            result = a.cmd(
                "ping -c 1 -W 2",
                b.IP(),
                ">/dev/null 2>&1; echo $?"
            ).strip()

            if result != "0":
                failures.append((a.name, b.name))

    return failures


def run_link_failure_test(net, info, log_dir, timeout=15):
    """Fail an active-path link and verify proactive flow recovery."""

    h1, h2 = net.get("h1", "h2")
    graph = info["switch_graph"]
    attachments = info["host_attachment"]

    src = attachments["h1"]
    dst = attachments["h2"]

    pattern = rf"icmp,nw_src={h1.IP()},nw_dst={h2.IP()}"

    ping_path = os.path.join(log_dir, "failure_ping.log")
    ping_output = open(ping_path, "w")

    ping = h1.popen(
        ["ping", "-D", "-i", "0.02", h2.IP()],
        stdout=ping_output,
        stderr=subprocess.STDOUT,
    )

    failed_link = None
    injected_at = None
    result = {
        "success": False,
        "link": "",
        "flows_rerouted": 0,
        "recovery_time_ms": None,
        "details": "",
    }

    try:
        # Establish the flow and allow OpenFlow counters to increase.
        time.sleep(2)

        before = {
            sw: rd.flow_packets(net, sw, pattern)
            for sw in info["switches"]
        }

        time.sleep(0.5)

        after = {
            sw: rd.flow_packets(net, sw, pattern)
            for sw in info["switches"]
        }

        active = {
            sw for sw in info["switches"]
            if before[sw] is not None
            and after[sw] is not None
            and after[sw] > before[sw]
        }

        active.update((src, dst))

        active_graph = graph.subgraph(active).copy()

        if not nx.has_path(active_graph, src, dst):
            raise RuntimeError(
                f"Could not identify active path; active switches={active}"
            )

        active_path = nx.shortest_path(active_graph, src, dst)

        # Select an edge on the actual active path whose removal
        # still leaves a path between the endpoints.
        for a, b in zip(active_path[:-1], active_path[1:]):
            remaining = graph.copy()
            remaining.remove_edge(a, b)

            if nx.has_path(remaining, src, dst):
                failed_link = (a, b)
                break

        if failed_link is None:
            raise RuntimeError(
                f"No redundant link found on active path {active_path}"
            )

        a, b = failed_link
        result["link"] = f"{a}-{b}"

        print(f"[INFO] Active ICMP path: {active_path}")
        print(f"[INFO] Injecting failure on {a}-{b}")

        injected_at = time.time()
        net.configLinkStatus(a, b, "down")

        deadline = time.monotonic() + timeout
        recovery_record = None

        while time.monotonic() < deadline:
            rows = rd.read_csv(
                os.path.join(log_dir, "recovery_events.csv")
            )

            for row in rows:
                if (
                    row.get("event") == "failure"
                    and set(row.get("link", "").split("-")) == {a, b}
                    and float(row.get("detection_time", 0)) >= injected_at
                ):
                    recovery_record = row
                    break

            if recovery_record is not None:
                break

            time.sleep(0.5)

        if recovery_record is None:
            raise RuntimeError(
                f"No recovery record found for failed link {a}-{b}"
            )

        rerouted = int(recovery_record["flows_rerouted"])
        recovery_ms = (
            float(recovery_record["recovery_time_s"]) * 1000
        )

        result["flows_rerouted"] = rerouted
        result["recovery_time_ms"] = round(recovery_ms, 2)

        if rerouted <= 0:
            raise RuntimeError(
                "Controller detected the failure but rerouted zero flows"
            )

        if not rd.wait_for_connectivity(h1, h2, 8):
            raise RuntimeError(
                "Host connectivity did not return while the link was down"
            )

        result["success"] = True
        result["details"] = "Flow rerouted and connectivity recovered"

        print(
            f"[PASS] Failure detected and {rerouted} flow(s) rerouted"
        )
        print(f"[PASS] Controller recovery time: {recovery_ms:.2f} ms")
        print("[PASS] Connectivity recovered with failed link still down")

    except Exception as exc:
        result["details"] = f"{type(exc).__name__}: {exc}"
        print(f"[FAIL] Link-failure recovery: {result['details']}")

    finally:
        if failed_link is not None:
            net.configLinkStatus(*failed_link, "up")
            time.sleep(2)

        if ping.poll() is None:
            ping.send_signal(signal.SIGINT)
            ping.wait()

        ping_output.close()

    return result



def run_topology(name, topo_class, require_alternate, args):
    """Start one topology and validate its controller integration."""

    from mininet.link import Link, TCLink
    from mininet.log import setLogLevel
    from mininet.net import Mininet
    from mininet.node import OVSSwitch, RemoteController

    print("\n" + "=" * 65)
    print(f"VALIDATING TOPOLOGY: {name.upper()}")
    print("=" * 65)

    log_dir = tempfile.mkdtemp(prefix=f"scalability_{name}_")
    controller = None
    controller_log = None
    net = None

    result = {
        "topology": name,
        "status": "FAIL",
        "hosts": 0,
        "switches": 0,
        "physical_links": 0,
        "switch_links": 0,
        "discovered_switches": 0,
        "discovered_directed_links": 0,
        "monitored_links": 0,
        "connected_host_pairs": 0,
        "total_host_pairs": 0,
        "alternate_host_pairs": 0,
        "details": "",
        "log_dir": log_dir,
        "failure_test": "N/A",
        "failed_link": "",
        "flows_rerouted": 0,
        "recovery_time_ms": None,
    }

    try:
        topo = topo_class(configure_bandwidth=not args.user_datapath)
        info = inspect_topology(topo)

        result.update({
            "hosts": len(info["hosts"]),
            "switches": len(info["switches"]),
            "physical_links": info["physical_links"],
            "switch_links": info["switch_links"],
            "alternate_host_pairs": info["alternate_pairs"],
            "total_host_pairs": len(info["host_pairs"]),
        })

        if require_alternate and info["alternate_pairs"] == 0:
            raise RuntimeError(
                "Topology was expected to have alternate paths, "
                "but none were found."
            )

        if not require_alternate and info["alternate_pairs"] > 0:
            raise RuntimeError(
                "Linear topology unexpectedly contains alternate paths."
            )

        if rd.port_in_use(args.controller_port):
            raise RuntimeError(
                f"Controller port {args.controller_port} is already in use."
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

        time.sleep(2)

        if controller.poll() is not None:
            raise RuntimeError("Controller exited during startup.")

        setLogLevel("warning")

        switch = partial(
            OVSSwitch,
            protocols="OpenFlow13",
            **({"datapath": "user"} if args.user_datapath else {}),
        )

        net = Mininet(
            topo=topo,
            switch=switch,
            controller=partial(
                RemoteController,
                ip="127.0.0.1",
                port=args.controller_port,
            ),
            link=Link if args.user_datapath else TCLink,
            autoSetMacs=True,
        )

        net.start()

        if args.user_datapath:
            for host in net.hosts:
                host.cmd(
                    "ethtool", "-K",
                    host.defaultIntf().name, "tx", "off"
                )

        expected = info["expected_controller_state"]

        discovered, state = wait_for_controller_state(
            os.path.join(log_dir, "controller.log"),
            expected,
            timeout=args.discovery_timeout,
        )

        if not discovered:
            raise RuntimeError(
                f"Controller did not report expected state {expected}."
            )

        result["discovered_switches"] = state[0]
        result["discovered_directed_links"] = state[1]
        result["monitored_links"] = state[2]

        print(
            f"[PASS] Controller discovery: "
            f"{state[0]} switches, {state[1]} directed links"
        )
        print(f"[PASS] Queue monitor: {state[2]} monitored links")

        failures = check_connectivity(net, info["host_pairs"])

        result["connected_host_pairs"] = (
            len(info["host_pairs"]) - len({
                tuple(sorted(pair)) for pair in failures
            })
        )

        if failures:
            raise RuntimeError(f"Connectivity failures: {failures}")

        print(
            f"[PASS] All {len(info['host_pairs'])} "
            "host pairs communicate in both directions"
        )

        if require_alternate:
            print(
                f"[PASS] Alternate paths exist for "
                f"{info['alternate_pairs']} host pairs"
            )
        else:
            print("[PASS] Linear topology has no alternate paths")

        if require_alternate:
            recovery = run_link_failure_test(
                net,
                info,
                log_dir,
                timeout=20,
            )

            result["failure_test"] = (
                "PASS" if recovery["success"] else "FAIL"
            )
            result["failed_link"] = recovery["link"]
            result["flows_rerouted"] = recovery["flows_rerouted"]
            result["recovery_time_ms"] = recovery["recovery_time_ms"]

            if not recovery["success"]:
                raise RuntimeError(recovery["details"])
        else:
            result["failure_test"] = "N/A (no alternate path)"

        result["status"] = "PASS"
        result["details"] = "All validation checks passed"

    except Exception as exc:
        result["details"] = f"{type(exc).__name__}: {exc}"
        print(f"[FAIL] {result['details']}")

    finally:
        if net is not None:
            net.stop()

        if controller is not None:
            if controller.poll() is None:
                controller.terminate()
                try:
                    controller.wait(timeout=5)
                except Exception:
                    controller.kill()
                    controller.wait()

        if controller_log is not None:
            controller_log.close()

    print(f"RESULT: {result['status']}")
    print(f"Controller log: {log_dir}/controller.log")

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Generic SDN topology scalability validation"
    )
    parser.add_argument("--user-datapath", action="store_true")
    parser.add_argument("--controller-port", type=int, default=6653)
    parser.add_argument("--discovery-timeout", type=int, default=30)
    parser.add_argument(
        "--only",
        nargs="+",
        choices=list(TOPOLOGIES),
        default=list(TOPOLOGIES),
    )
    args = parser.parse_args()

    RESULT_DIR.mkdir(parents=True, exist_ok=True)

    results = []

    for name in args.only:
        topo_class, require_alternate = TOPOLOGIES[name]

        result = run_topology(
            name,
            topo_class,
            require_alternate,
            args,
        )

        results.append(result)

        # Leave a moment for Mininet/OVS cleanup before next controller.
        time.sleep(2)

    json_path = RESULT_DIR / "validation_summary.json"
    csv_path = RESULT_DIR / "validation_summary.csv"

    with json_path.open("w") as handle:
        json.dump(results, handle, indent=2)

    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(results[0].keys()),
        )
        writer.writeheader()
        writer.writerows(results)

    print("\n" + "=" * 65)
    print("SCALABILITY VALIDATION SUMMARY")
    print("=" * 65)

    for result in results:
        print(
            f"{result['topology']:10} "
            f"{result['status']:5} "
            f"hosts={result['hosts']:2} "
            f"switches={result['switches']:2} "
            f"links={result['physical_links']:2} "
            f"monitored={result['monitored_links']:2}"
        )

    passed = sum(r["status"] == "PASS" for r in results)

    print(f"\nPassed: {passed}/{len(results)}")
    print(f"JSON: {json_path}")
    print(f"CSV : {csv_path}")

    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
