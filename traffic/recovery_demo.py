"""
Live demonstration of failure recovery on the diamond topology (Member 5).

Run from the project root on a Linux machine/VM with Mininet, OVS, iperf
and the Python requirements installed:

    sudo python3 -m traffic.recovery_demo
    sudo python3 -m traffic.recovery_demo --user-datapath      # WSL2 / Docker
    sudo python3 -m traffic.recovery_demo --routing-mode ecmp  # a baseline

What it does
------------
1. Starts the controller (controller/ryu_app.py) and the diamond topology.
2. Link failure with light traffic: pings h1 -> h2 every 20 ms, brings
   down the link the pings use, and measures
     - detection time (failure injection -> FailureEvent),
     - controller recovery time (injection -> new rules sent),
     - end-to-end outage (longest gap between two ping replies).
   Then it brings the link back and checks that the pings are not moved
   again: a light flow gains nothing from another route change.
3. Link failure under load (needs iperf): sends UDP h1 -> h2, fails the
   link that flow uses, checks it is rerouted, restores the link and
   checks that the loaded flow returns to its original path after the
   stability interval.

4. Switch failure: pings again, removes the middle switch the pings pass
   through from OVS, checks that traffic continues over the other
   switch, then starts the switch again.

The controller log and the CSV event logs are kept in --log-dir.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from functools import partial

from common.config import ALTERNATE_PATH, PRIMARY_PATH

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Middle switch of each path and the link the demo fails on that path.
PATH_LINKS = {
    PRIMARY_PATH[2]: (PRIMARY_PATH[1], PRIMARY_PATH[2]),
    ALTERNATE_PATH[2]: (ALTERNATE_PATH[1], ALTERNATE_PATH[2]),
}

PING_INTERVAL_S = 0.02
UDP_PORT = 5001


def check(name, condition, details, enforce=True):
    """
    Print a check. With enforce=False the result is only reported: the
    baselines have no proactive recovery, so they are expected to lose
    traffic until the failed component returns.
    """
    if not enforce:
        print(f"[INFO] {name}: {details}")
        return True

    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}: {details}")
    return bool(condition)


def is_proactive(args):
    return args.recovery == "on" or (
        args.recovery == "auto" and args.routing_mode == "proposed"
    )


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def start_controller(args, log_dir):
    env = dict(os.environ)
    env["PYTHONPATH"] = PROJECT_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["SDN_ROUTING_MODE"] = args.routing_mode
    env["SDN_RECOVERY"] = args.recovery
    env["SDN_STABILITY_INTERVAL"] = str(args.stability_interval)
    env["SDN_LOG_DIR"] = log_dir

    log = open(os.path.join(log_dir, "controller.log"), "w")
    process = subprocess.Popen(
        [sys.executable, "-m", "os_ken.cmd.manager", "--observe-links",
         "--ofp-tcp-listen-port", str(args.controller_port),
         "controller.ryu_app"],
        cwd=PROJECT_ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    return process, log


def dump_flows(switch):
    return switch.cmd("ovs-ofctl", "-O", "OpenFlow13", "dump-flows", switch.name)


def flow_packets(net, switch_name, pattern):
    """Packets counted by the rules of a switch whose text matches pattern."""
    total = None
    for line in dump_flows(net.get(switch_name)).splitlines():
        if re.search(pattern, line):
            found = re.search(r"n_packets=(\d+)", line)
            total = (total or 0) + (int(found.group(1)) if found else 0)
    return total


def active_middle_switch(net, pattern):
    """Return the middle switch whose rule for the flow is counting packets."""
    before = {name: flow_packets(net, name, pattern) for name in PATH_LINKS}
    time.sleep(0.5)
    after = {name: flow_packets(net, name, pattern) for name in PATH_LINKS}

    moving = [
        name for name in PATH_LINKS
        if after[name] is not None and after[name] > (before[name] or 0)
    ]
    return moving[0] if len(moving) == 1 else None


def wait_for_connectivity(h1, h2, timeout_s):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if "1 received" in h1.cmd("ping", "-c", "1", "-W", "1", h2.IP()):
            return True
        time.sleep(0.5)
    return False


def read_ping_replies(path):
    """Return [(timestamp, icmp_seq)] from `ping -D` output."""
    replies = []
    with open(path) as handle:
        for line in handle:
            found = re.match(r"\[(\d+\.\d+)\].*icmp_seq=(\d+)", line)
            if found:
                replies.append((float(found.group(1)), int(found.group(2))))
    return replies


def longest_gap(replies, start, end):
    """Longest time without a reply inside [start, end], in seconds."""
    times = [t for t, _ in replies if start - 1.0 <= t <= end]
    gaps = [b - a for a, b in zip(times[:-1], times[1:]) if b >= start]
    return max(gaps) if gaps else None


def lost_between(replies, start, end):
    seqs = sorted(seq for t, seq in replies if start <= t <= end)
    if len(seqs) < 2:
        return None
    return (seqs[-1] - seqs[0] + 1) - len(seqs)


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path) as handle:
        return list(csv.DictReader(handle))


def controller_times(log_dir, link, injected_at):
    """Detection and recovery time for a link, in seconds after injection."""
    names = {link[0], link[1]}
    detection = recovery = rerouted = None

    for row in read_csv(os.path.join(log_dir, "failure_events.csv")):
        if (row["is_failed"] == "True"
                and {row["source"], row["destination"]} == names
                and float(row["detection_time"]) >= injected_at):
            detection = float(row["detection_time"]) - injected_at
            break

    for row in read_csv(os.path.join(log_dir, "recovery_events.csv")):
        if (row["event"] == "failure"
                and set(row["link"].split("-")) == names
                and float(row["detection_time"]) >= injected_at):
            recovery = float(row["completed_time"]) - injected_at
            rerouted = int(row["flows_rerouted"])
            break

    return detection, recovery, rerouted


def ms(value):
    return "n/a" if value is None else f"{value * 1000.0:.1f} ms"


def light_traffic_phase(net, args, log_dir):
    h1, h2 = net.get("h1", "h2")
    proactive = is_proactive(args)
    echo = r"icmp,nw_src=%s,nw_dst=%s" % (h1.IP(), h2.IP())
    ping_log = os.path.join(log_dir, "ping.log")
    ok = True

    print("\n=== Link failure with light traffic (ping) ===")
    if not proactive:
        print("no proactive recovery in this configuration: traffic is "
              "expected to stay down until the link returns")
    ping_output = open(ping_log, "w")
    ping = h1.popen(
        ["ping", "-D", "-i", str(PING_INTERVAL_S), h2.IP()],
        stdout=ping_output, stderr=subprocess.STDOUT,
    )

    def stop_ping():
        # SIGINT lets ping flush its output before it exits.
        ping.send_signal(signal.SIGINT)
        ping.wait()
        ping_output.close()

    time.sleep(2.0)

    middle = active_middle_switch(net, echo)
    if middle is None:
        stop_ping()
        return check("ping path found", False, "no ICMP rule is counting packets")

    link = PATH_LINKS[middle]
    other = [name for name in PATH_LINKS if name != middle][0]
    print(f"pings use the path through {middle}; failing {link[0]}-{link[1]}")

    injected_at = time.time()
    net.configLinkStatus(link[0], link[1], "down")
    time.sleep(args.hold_seconds)

    moved_to = active_middle_switch(net, echo)
    ok &= check("traffic moved to the other path", moved_to == other,
                f"ICMP rule active on {moved_to}", enforce=proactive)

    restored_at = time.time()
    net.configLinkStatus(link[0], link[1], "up")
    time.sleep(args.stability_interval + 3.0)
    end = time.time()

    stays_on = active_middle_switch(net, echo)
    stop_ping()

    replies = read_ping_replies(ping_log)
    outage = longest_gap(replies, injected_at, end)
    detection, recovery, rerouted = controller_times(log_dir, link, injected_at)

    print(f"detection time            : {ms(detection)}")
    print(f"controller recovery time  : {ms(recovery)}  (rules sent)")
    print(f"end-to-end outage         : {ms(outage)}  "
          f"(ping every {PING_INTERVAL_S * 1000:.0f} ms)")
    print(f"pings lost by the failure : "
          f"{lost_between(replies, injected_at - 0.5, end)}")

    ok &= check("failure detected", detection is not None, ms(detection))
    ok &= check("flows rerouted by the recovery manager",
                bool(rerouted), f"{rerouted} flows", enforce=proactive)
    ok &= check("traffic resumed quickly", outage is not None
                and outage < args.max_outage, f"outage {ms(outage)}",
                enforce=proactive)

    lost_later = lost_between(replies, restored_at, end)
    ok &= check("no loss while the link was restored", lost_later == 0,
                f"{lost_later} pings lost", enforce=proactive)
    ok &= check("light flow was not moved again", stays_on == other,
                f"ICMP rule active on {stays_on}", enforce=proactive)
    return ok


def loaded_phase(net, args, log_dir):
    h1, h2 = net.get("h1", "h2")
    proactive = is_proactive(args)
    udp = r"udp,nw_src=%s,nw_dst=%s.*tp_dst=%d" % (h1.IP(), h2.IP(), UDP_PORT)
    duration = int(2 * args.hold_seconds + args.stability_interval + 12)
    ok = True

    print(f"\n=== Link failure under load (UDP {args.load_mbps} Mbit/s) ===")
    server = h2.popen(["iperf", "-u", "-s", "-p", str(UDP_PORT)],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    client = h1.popen(["iperf", "-u", "-c", h2.IP(), "-p", str(UDP_PORT),
                       "-b", f"{args.load_mbps}M", "-t", str(duration),
                       "-l", "1400"],
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(3.0)

    middle = active_middle_switch(net, udp)
    if middle is None:
        client.terminate()
        server.terminate()
        return check("UDP path found", False, "no UDP rule is counting packets")

    link = PATH_LINKS[middle]
    other = [name for name in PATH_LINKS if name != middle][0]
    print(f"UDP flow uses the path through {middle}; "
          f"failing {link[0]}-{link[1]}")

    injected_at = time.time()
    net.configLinkStatus(link[0], link[1], "down")
    time.sleep(args.hold_seconds)

    moved_to = active_middle_switch(net, udp)
    ok &= check("loaded flow rerouted", moved_to == other,
                f"UDP rule active on {moved_to}", enforce=proactive)

    net.configLinkStatus(link[0], link[1], "up")
    print(f"link restored; waiting {args.stability_interval:.0f} s "
          "stability interval")
    time.sleep(args.stability_interval + 5.0)

    back_on = active_middle_switch(net, udp)
    detection, recovery, _ = controller_times(log_dir, link, injected_at)
    print(f"detection time            : {ms(detection)}")
    print(f"controller recovery time  : {ms(recovery)}  (rules sent)")

    ok &= check("loaded flow returned to the recovered path",
                back_on == middle, f"UDP rule active on {back_on}",
                enforce=proactive and args.routing_mode == "proposed")

    output, _ = client.communicate()
    server.terminate()
    server.wait()

    # The client prints the server's report (lost/total datagrams) last.
    report = re.findall(
        r"(\d+)/\s*(\d+)\s+\(([\d.]+)%\)",
        output.decode(errors="replace"),
    )
    if report:
        lost, total, percent = report[-1]
        print(f"UDP datagrams lost        : {lost} of {total} ({percent}%)")

    ok &= check("UDP traffic was delivered",
                bool(report) and float(report[-1][2]) < args.max_loss,
                "server report received" if report else "no server report",
                enforce=proactive)
    return ok


def switch_failure_phase(net, args, log_dir):
    h1, h2 = net.get("h1", "h2")
    proactive = is_proactive(args)
    echo = r"icmp,nw_src=%s,nw_dst=%s" % (h1.IP(), h2.IP())
    ping_log = os.path.join(log_dir, "ping_switch.log")
    ok = True

    print("\n=== Switch failure (ping) ===")
    ping_output = open(ping_log, "w")
    ping = h1.popen(
        ["ping", "-D", "-i", str(PING_INTERVAL_S), h2.IP()],
        stdout=ping_output, stderr=subprocess.STDOUT,
    )
    time.sleep(2.0)

    middle = active_middle_switch(net, echo)
    if middle is None:
        ping.send_signal(signal.SIGINT)
        ping.wait()
        ping_output.close()
        return check("ping path found", False, "no ICMP rule is counting packets")

    other = [name for name in PATH_LINKS if name != middle][0]
    failed = net.get(middle)
    print(f"pings use the path through {middle}; stopping {middle}")

    injected_at = time.time()
    failed.cmd("ovs-vsctl", "del-br", middle)
    time.sleep(args.hold_seconds)

    moved_to = active_middle_switch(net, echo)
    ok &= check("traffic moved to the other switch", moved_to == other,
                f"ICMP rule active on {moved_to}", enforce=proactive)

    restarted_at = time.time()
    failed.start(net.controllers)
    time.sleep(args.stability_interval + 3.0)
    end = time.time()

    ping.send_signal(signal.SIGINT)
    ping.wait()
    ping_output.close()

    replies = read_ping_replies(ping_log)
    outage = longest_gap(replies, injected_at, end)
    print(f"end-to-end outage         : {ms(outage)}  "
          f"(ping every {PING_INTERVAL_S * 1000:.0f} ms)")
    print(f"pings lost by the failure : "
          f"{lost_between(replies, injected_at - 0.5, end)}")

    ok &= check("traffic resumed quickly", outage is not None
                and outage < args.max_outage, f"outage {ms(outage)}",
                enforce=proactive)
    lost_later = lost_between(replies, restarted_at, end)
    ok &= check("no loss while the switch came back", lost_later == 0,
                f"{lost_later} pings lost", enforce=proactive)
    # Shortest-path routing uses a single path, which need not be this one.
    ok &= check(f"{middle} forwards again after its restart",
                wait_for_path_through(net, middle, 15.0), "new flows",
                enforce=args.routing_mode != "shortest_path")
    return ok


def wait_for_path_through(net, middle, timeout_s):
    """
    Send short UDP flows until one of them is routed through `middle`.

    Flows are spread over both paths by their 5-tuple, so a handful of
    different source ports is enough once the switch is usable again.
    """
    h1, h2 = net.get("h1", "h2")
    pattern = r"udp,nw_src=%s,nw_dst=%s" % (h1.IP(), h2.IP())
    deadline = time.monotonic() + timeout_s

    while time.monotonic() < deadline:
        for _ in range(8):
            h1.cmd("bash", "-c",
                   f"'echo probe > /dev/udp/{h2.IP()}/{UDP_PORT + 1}'")
        # The rule is enough: the first packet of a flow is delivered by
        # the controller, so the counter of a one-packet flow stays at 0.
        if flow_packets(net, middle, pattern) is not None:
            return True
        time.sleep(0.5)
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--routing-mode", default="proposed",
                        choices=["shortest_path", "ecmp", "queue_aware",
                                 "proposed"])
    parser.add_argument("--recovery", default="auto",
                        choices=["auto", "on", "off"])
    parser.add_argument("--stability-interval", type=float, default=3.0)
    parser.add_argument("--hold-seconds", type=float, default=4.0,
                        help="how long the link stays down")
    parser.add_argument("--load-mbps", type=float, default=8.0)
    parser.add_argument("--max-outage", type=float, default=1.0,
                        help="largest accepted end-to-end outage in seconds")
    parser.add_argument("--max-loss", type=float, default=2.0,
                        help="largest accepted UDP loss in percent")
    parser.add_argument("--no-load", action="store_true",
                        help="skip the iperf phase")
    parser.add_argument("--no-switch-failure", action="store_true",
                        help="skip the switch failure phase")
    parser.add_argument("--user-datapath", action="store_true",
                        help="use the OVS userspace datapath")
    parser.add_argument("--controller-port", type=int, default=6653)
    parser.add_argument("--external-controller", action="store_true",
                        help="use a controller that is already running "
                             "(start it with SDN_LOG_DIR=<log dir>)")
    parser.add_argument("--log-dir", default=None)
    args = parser.parse_args(argv)

    from mininet.link import Link, TCLink
    from mininet.log import setLogLevel
    from mininet.net import Mininet
    from mininet.node import OVSSwitch, RemoteController

    from topology.diamond import DiamondTopo

    log_dir = args.log_dir or tempfile.mkdtemp(prefix="recovery_demo_")
    os.makedirs(log_dir, exist_ok=True)
    print("logs:", log_dir)

    controller = controller_log = None
    if not args.external_controller:
        if port_in_use(args.controller_port):
            print(f"port {args.controller_port} is already in use: stop the "
                  "running controller or pass --external-controller")
            return 2

        controller, controller_log = start_controller(args, log_dir)
        time.sleep(3.0)
        if controller.poll() is not None:
            print("controller failed to start, see",
                  os.path.join(log_dir, "controller.log"))
            return 2

    setLogLevel("warning")
    switch = partial(
        OVSSwitch,
        protocols="OpenFlow13",
        **({"datapath": "user"} if args.user_datapath else {}),
    )
    use_user_datapath = args.user_datapath

    net = Mininet(
        topo=DiamondTopo(configure_bandwidth=not use_user_datapath),
        switch=switch,
        controller=partial(RemoteController, ip="127.0.0.1",
                           port=args.controller_port),
        link=Link if use_user_datapath else TCLink,
        autoSetMacs=True,
    )

    ok = True
    try:
        net.start()
        h1, h2 = net.get("h1", "h2")

        if args.user_datapath:
            # The userspace datapath does not fill in checksums that the
            # hosts leave to "hardware", so TCP and UDP would be dropped
            # by the receiver. ICMP is not affected.
            for host in (h1, h2):
                host.cmd("ethtool", "-K", host.defaultIntf().name, "tx", "off")

        ok &= check("h1 reaches h2 through the controller",
                    wait_for_connectivity(h1, h2, 20.0), "ping")
        if ok:
            ok &= light_traffic_phase(net, args, log_dir)

            if args.no_load:
                pass
            elif shutil.which("iperf") is None:
                print("iperf is not installed, skipping the loaded phase")
            else:
                ok &= loaded_phase(net, args, log_dir)

            if not args.no_switch_failure:
                ok &= switch_failure_phase(net, args, log_dir)
    finally:
        net.stop()
        if controller is not None:
            controller.terminate()
            controller.wait()
            controller_log.close()

    log_path = os.path.join(log_dir, "controller.log")
    if os.path.exists(log_path):
        print("\ncontroller log (recovery lines):")
        with open(log_path) as handle:
            for line in handle:
                if re.search(r"RECOVERY|rerouted|restored|LINK ", line):
                    print("  " + line.rstrip())

    print("\nRESULT:", "all checks passed" if ok else "some checks FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
