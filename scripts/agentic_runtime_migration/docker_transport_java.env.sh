# Configuration-only BASH_ENV for the unchanged original Gradle/Java commands.
# The complete immutable file is checked by the real Docker lifecycle broker.
_reviewbench_docker_gateway="$(python3 - <<'PY'
import fcntl
import ipaddress
import os
import socket
import struct

assert os.environ.get("REVIEWBENCH_DOCKER_PROFILE") == "mfscreener133-135-original-testcontainers-v1"
port = int(os.environ["REVIEWBENCH_DOCKER_BROKER_PORT"])
assert 1024 <= port <= 65535
for flag in ("TESTCONTAINERS_RYUK_DISABLED", "TESTCONTAINERS_RYUK_PRIVILEGED", "DOCKER_TLS_VERIFY"):
    assert os.environ.get(flag, "").lower() not in {"true", "1"}
candidates = set()
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
    for _, name in socket.if_nameindex():
        if name == "lo":
            continue
        packed = struct.pack("256s", name.encode()[:15])
        try:
            address = socket.inet_ntoa(fcntl.ioctl(probe, 0x8915, packed)[20:24])
            mask = socket.inet_ntoa(fcntl.ioctl(probe, 0x891b, packed)[20:24])
        except OSError:
            continue
        network = ipaddress.IPv4Network(address + "/" + mask, strict=False)
        assert network.prefixlen <= 24 and not ipaddress.IPv4Address(address).is_loopback
        candidates.add(str(network.network_address + 1))
assert len(candidates) == 1, "expected exactly one internal Docker IPv4 bridge"
print(candidates.pop())
PY
)" || exit 78
export DOCKER_HOST="tcp://${_reviewbench_docker_gateway}:${REVIEWBENCH_DOCKER_BROKER_PORT}"
export TESTCONTAINERS_HOST_OVERRIDE="${_reviewbench_docker_gateway}"
export TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE=/var/run/docker.sock
if [ "${REVIEWBENCH_ORIGINAL_HTTPS_PROXY-}" = "1" ]; then
    # Reconstruct from the bound original options, rather than appending again
    # each time the original Gradle wrapper starts a nested noninteractive shell.
    export JAVA_TOOL_OPTIONS="${REVIEWBENCH_BASE_JAVA_TOOL_OPTIONS-} -Dhttps.proxyHost=${_reviewbench_docker_gateway} -Dhttps.proxyPort=${REVIEWBENCH_DOCKER_BROKER_PORT}"
fi
unset _reviewbench_docker_gateway
