
# Network configuration

HOSTS = ["h1", "h2"]

SWITCHES = ["s1", "s2", "s3", "s4"]

DEFAULT_LINK_BW_MBPS = 10

# Two alternative paths between h1 and h2

PRIMARY_PATH = ["h1", "s1", "s2", "s4", "h2"]

ALTERNATE_PATH = ["h1", "s1", "s3", "s4", "h2"]
