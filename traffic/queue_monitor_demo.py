"""
Live demonstration of the Queue Monitor on the diamond topology (Phase 5).

Run from the project root on a Linux machine/VM with Mininet, OVS and iperf:

    sudo python3 -m traffic.queue_monitor_demo
    sudo python3 -m traffic.queue_monitor_demo --user-datapath   # like README

What it does
------------
1. Starts topology/diamond.py with TCLink (every link 10 Mbps).
2. Lowers the s1 -> s2 egress (s1-eth2) to --bottleneck-mbps (default 5)
   and limits its queue to --queue-limit packets (default 20), inside this
   demo network only. Without the lower rate, h1's own 10 Mbps link is the
   bottleneck and s1's queue stays empty (high utilization, low backlog).
   Without the queue limit there are no drops: h1's UDP socket buffer still
   owns packets queued at s1 (Linux keeps the socket reference across veth),
   so iperf is slowed down before s1's default 1000-packet queue overflows.
3. Installs static OpenFlow rules for the upper path h1-s1-s2-s4-h2, so no
   controller is needed and the loop in the diamond causes no flooding.
4. Measures s1->s2 with QueueMonitor while idle, under normal UDP load
   (below the bottleneck) and under heavy load (above it), then checks that
   backlog, delay, utilization and drops react as expected.

Member 1's topology file is not modified.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from functools import partial

from controller.queue_monitor import (
    MonitoredLink,
    QueueMonitor,
    build_link_map_from_net,
    default_diamond_link_map,
    format_stats_table,
)

UPPER_PATH_FLOWS = {
    # switch: [(in_port, out_port), ...]  (ports follow diamond.py addLink order)
    "s1": [(1, 2), (2, 1)],
    "s2": [(1, 2), (2, 1)],
    "s4": [(1, 3), (3, 1)],
}


def install_upper_path_flows(net):
    for name, rules in UPPER_PATH_FLOWS.items():
        switch = net.get(name)
        switch.dpctl("del-flows")
        for in_port, out_port in rules:
            switch.dpctl("add-flow", f"in_port={in_port},actions=output:{out_port}")


def sample_phase(monitor, label, seconds, interval=1.0, key=(1, 2)):
    """Collect for `seconds` and return averages for one link."""
    rows = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(interval)
        stats = monitor.collect_stats()
        rows.append(stats[key])
    print(f"\n=== {label} (last sample) ===")
    print(format_stats_table(stats))

    def avg(field):
        values = [r[field] for r in rows if r["valid"] and r[field] is not None]
        return sum(values) / len(values) if values else None

    return {
        "queue_bytes": avg("queue_bytes"),
        "estimated_delay_ms": avg("estimated_delay_ms"),
        "utilization": avg("utilization"),
        "drops": sum(r["drops_delta"] or 0 for r in rows),
        "valid": all(r["valid"] for r in rows),
    }


def run_iperf(h1, h2, rate_mbps, seconds):
    server = h2.popen(["iperf", "-u", "-s"])
    time.sleep(0.5)
    client = h1.popen(["iperf", "-u", "-c", h2.IP(), "-b", f"{rate_mbps}M",
                       "-t", str(seconds), "-l", "1400"])
    return server, client


def check(name, condition, details):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}: {details}")
    return condition


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bottleneck-mbps", type=float, default=5.0)
    parser.add_argument("--normal-mbps", type=float, default=3.0)
    parser.add_argument("--heavy-mbps", type=float, default=15.0)
    parser.add_argument("--queue-limit", type=int, default=20,
                        help="max packets queued at s1-eth2 (0 = Linux default)")
    parser.add_argument("--phase-seconds", type=int, default=6)
    parser.add_argument("--user-datapath", action="store_true",
                        help="use OVS userspace datapath (needed on some WSL2 setups)")
    args = parser.parse_args(argv)

    from mininet.link import TCLink
    from mininet.log import setLogLevel
    from mininet.net import Mininet
    from mininet.node import OVSSwitch

    from topology.diamond import DiamondTopo

    if shutil.which("iperf") is None:
        print("iperf is not installed (sudo apt install iperf)")
        return 2

    setLogLevel("warning")
    switch = partial(OVSSwitch, datapath="user") if args.user_datapath else OVSSwitch
    net = Mininet(topo=DiamondTopo(), link=TCLink, switch=switch,
                  controller=None, autoSetMacs=True, autoStaticArp=True)
    ok = True
    try:
        net.start()

        # 1. confirm the static map matches the interfaces Mininet created
        live = build_link_map_from_net(net)
        static = default_diamond_link_map()
        same = {k: v.interface for k, v in live.items()} == \
               {k: v.interface for k, v in static.items()}
        ok &= check("link map", same, "static diamond map matches running network"
                    if same else f"live map differs: {live}")

        # 2. bottleneck on s1 -> s2
        s1 = net.get("s1")
        shaping = {"bw": args.bottleneck_mbps}
        if args.queue_limit > 0:
            shaping["max_queue_size"] = args.queue_limit   # adds netem limit N
        s1.intf("s1-eth2").config(**shaping)
        link_map = dict(live)
        link_map[(1, 2)] = MonitoredLink(1, 2, "s1-eth2", args.bottleneck_mbps * 1e6)
        monitor = QueueMonitor(link_map=link_map)

        install_upper_path_flows(net)
        h1, h2 = net.get("h1"), net.get("h2")
        loss = net.ping([h1, h2], timeout=2)
        ok &= check("forwarding", loss == 0, f"h1 <-> h2 ping loss {loss}%")

        monitor.collect_stats()  # baseline for utilization
        idle = sample_phase(monitor, "IDLE", args.phase_seconds)

        procs = run_iperf(h1, h2, args.normal_mbps, args.phase_seconds + 2)
        time.sleep(1)
        normal = sample_phase(monitor, f"NORMAL {args.normal_mbps} Mbps", args.phase_seconds)
        for p in procs:
            p.terminate()
        time.sleep(2)  # let the queue drain

        procs = run_iperf(h1, h2, args.heavy_mbps, args.phase_seconds + 2)
        time.sleep(2)
        heavy = sample_phase(monitor, f"HEAVY {args.heavy_mbps} Mbps", args.phase_seconds)
        # raw kernel view while still congested, to cross-check the monitor
        print("\n=== raw tc -s qdisc show dev s1-eth2 (during heavy traffic) ===")
        print(s1.cmd("tc -s qdisc show dev s1-eth2").strip())
        for p in procs:
            p.terminate()

        print("\n=== s1 -> s2 averages ===")
        for label, r in (("idle", idle), ("normal", normal), ("heavy", heavy)):
            print(f"{label:<7} backlog={r['queue_bytes']} B  delay={r['estimated_delay_ms']} ms"
                  f"  util={r['utilization']}  drops={r['drops']}")
        print()

        expected_normal = args.normal_mbps / args.bottleneck_mbps
        ok &= check("stats readable", idle["valid"] and normal["valid"] and heavy["valid"],
                    "tc statistics available for s1-eth2 in every phase")
        ok &= check("idle", (idle["utilization"] or 0) < 0.05 and (idle["queue_bytes"] or 0) < 3000,
                    f"util={idle['utilization']}, backlog={idle['queue_bytes']}")
        ok &= check("normal utilization",
                    normal["utilization"] is not None
                    and abs(normal["utilization"] - expected_normal) < 0.2,
                    f"expected about {expected_normal:.2f}, got {normal['utilization']}")
        ok &= check("heavy utilization", (heavy["utilization"] or 0) > 0.85,
                    f"util={heavy['utilization']}")
        ok &= check("heavy backlog grows",
                    (heavy["queue_bytes"] or 0) > 10 * max(normal["queue_bytes"] or 0, 1500),
                    f"normal={normal['queue_bytes']} B, heavy={heavy['queue_bytes']} B")
        ok &= check("heavy delay", (heavy["estimated_delay_ms"] or 0) > 20,
                    f"{heavy['estimated_delay_ms']} ms")
        if args.queue_limit > 0:
            ok &= check("heavy drops", heavy["drops"] > 0, f"{heavy['drops']} packets dropped")
        else:
            print(f"[INFO] heavy drops: {heavy['drops']} (no queue limit, so 0 is expected)")
        print("\nAll checks passed." if ok else "\nSome checks failed (see above).")
    finally:
        net.stop()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
