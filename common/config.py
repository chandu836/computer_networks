
# Shared network configuration

HOSTS = ["h1", "h2"]

SWITCHES = ["s1", "s2", "s3", "s4"]

DEFAULT_LINK_BW_MBPS = 10

# All physical links in the diamond topology.
# Each entry: (node1, node2, bandwidth_mbps)

LINKS = [
    ("h1", "s1", DEFAULT_LINK_BW_MBPS),
    ("s1", "s2", DEFAULT_LINK_BW_MBPS),
    ("s2", "s4", DEFAULT_LINK_BW_MBPS),
    ("s1", "s3", DEFAULT_LINK_BW_MBPS),
    ("s3", "s4", DEFAULT_LINK_BW_MBPS),
    ("s4", "h2", DEFAULT_LINK_BW_MBPS),
]

PRIMARY_PATH = ["h1", "s1", "s2", "s4", "h2"]

ALTERNATE_PATH = ["h1", "s1", "s3", "s4", "h2"]
