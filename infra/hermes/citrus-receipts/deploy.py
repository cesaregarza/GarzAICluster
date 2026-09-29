#!/usr/bin/env python3
"""Fail-closed, fixed-purpose native Docker deployment for the receipts broker."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import shlex
import stat
import subprocess
import sys
import time
import tempfile
from pathlib import Path


IMAGE = "registry.digitalocean.com/sendouq/agent-workloads-citrus-receipts-broker@sha256:db0ca13ee0c7b9552c63e441d56a99a6fd58d0a8b8c1af74fb5ea92a84eb80e9"
SOURCE_COMMIT = "1e9db230b40842df1a052ec440669a80cdbe423c"
NAME = "mandate-receipts-broker"
NETWORK = "mandate-receipts"
BRIDGE = "br-receipts"
SUBNET = "172.30.94.0/29"
GATEWAY = "172.30.94.1"
CONTAINER_IP = "172.30.94.2"
HOST_IP = "10.108.0.8"
DEPLOY_DIR = Path("/var/lib/mandate-receipts-deploy-1e9db23")
CONFIG_DIR = Path("/var/lib/mandate-receipts/config")
STATE_DIR = Path("/var/lib/mandate-receipts/state")
SOCKET_DIR = Path("/run/mandate-receipt-reader")
SOCKET = SOCKET_DIR / "reader.sock"
XTABLES_LOCK = Path("/run/xtables.lock")
UNIT_SOURCE = Path(__file__).with_name("mandate-receipts-broker.service")
ARTIFACT = DEPLOY_DIR / "citrus-receipts-broker.json"
UNIT_DEST = Path("/etc/systemd/system/mandate-receipts-broker.service")
CHAIN = "MANDATE-RECEIPTS"
LABEL_KEY = "mandate.receipts.managed"

CHAIN_RULES = [
    ["-m", "conntrack", "--ctstate", "INVALID", "-j", "DROP"],
    ["-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
    ["-i", BRIDGE, "-s", f"{CONTAINER_IP}/32", "-d", "143.244.222.41/32", "-p", "tcp", "--dport", "443", "-j", "ACCEPT"],
    ["-o", BRIDGE, "-d", f"{CONTAINER_IP}/32", "-s", "10.108.0.9/32", "-p", "tcp", "--dport", "8443", "-j", "ACCEPT"],
    ["-o", BRIDGE, "-d", f"{CONTAINER_IP}/32", "-s", "10.108.0.11/32", "-p", "tcp", "--dport", "8443", "-j", "ACCEPT"],
    ["-o", BRIDGE, "-d", f"{CONTAINER_IP}/32", "-s", "10.244.0.0/16", "-p", "tcp", "--dport", "8443", "-j", "ACCEPT"],
    ["-j", "DROP"],
]
DOCKER_HOOKS = [
    ["-i", BRIDGE, "-j", CHAIN],
    ["-o", BRIDGE, "-j", CHAIN],
]
INPUT_HOOK = ["-i", BRIDGE, "-j", "DROP"]


class DeploymentError(RuntimeError):
    pass


def fail(message: str) -> None:
    raise DeploymentError(message)


def run(argv: list[str], *, check: bool = True, capture: bool = True, timeout: float = 30.0) -> str:
    try:
        result = subprocess.run(
            argv,
            check=False,
            text=True,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        fail(f"{argv[0]} timed out")
    except OSError as exc:
        fail(f"could not run {argv[0]} ({exc.__class__.__name__})")
    if check and result.returncode:
        fail(f"{argv[0]} failed with status {result.returncode}")
    return result.stdout.strip() if capture else ""


def require_root() -> None:
    if os.geteuid() != 0:
        fail("this operation must run as root")


def ensure_xtables_lock(*, create: bool = False) -> None:
    check_ancestors(XTABLES_LOCK)
    try:
        st = XTABLES_LOCK.lstat()
    except FileNotFoundError:
        if not create:
            fail("/run/xtables.lock must exist before the protected broker unit runs")
        try:
            fd = os.open(str(XTABLES_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            st = XTABLES_LOCK.lstat()
        except OSError as exc:
            fail(f"could not safely create /run/xtables.lock ({exc.__class__.__name__})")
        else:
            os.close(fd)
            st = XTABLES_LOCK.lstat()
    except OSError:
        fail("could not inspect /run/xtables.lock")
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_uid != 0:
        fail("/run/xtables.lock must be a root-owned regular non-symlink file")


def check_artifact() -> None:
    check_regular(ARTIFACT, 0, 0, 0o644)
    try:
        artifact = json.loads(ARTIFACT.read_text())
    except (OSError, json.JSONDecodeError):
        fail("release artifact is missing or invalid")
    image = artifact.get("image", {})
    if artifact.get("source_commit") != SOURCE_COMMIT or image.get("reference") != IMAGE:
        fail("release artifact does not match the fixed source and image")


def check_ancestors(path: Path) -> None:
    for ancestor in reversed(path.absolute().parents):
        try:
            st = ancestor.lstat()
        except OSError:
            fail(f"path ancestor is missing: {ancestor}")
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            fail(f"path ancestor is not a real directory: {ancestor}")


def check_regular(path: Path, uid: int, gid: int, mode: int) -> os.stat_result:
    check_ancestors(path)
    try:
        st = path.lstat()
    except OSError:
        fail(f"required path is missing: {path}")
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        fail(f"required path is not a regular non-symlink file: {path}")
    if (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) != (uid, gid, mode):
        fail(f"unsafe owner, group, or mode on {path}")
    return st


def check_directory(path: Path, uid: int, gid: int, mode: int) -> os.stat_result:
    check_ancestors(path)
    try:
        st = path.lstat()
    except OSError:
        fail(f"required directory is missing: {path}")
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        fail(f"required path is not a directory: {path}")
    if (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) != (uid, gid, mode):
        fail(f"unsafe owner, group, or mode on directory {path}")
    return st


def check_socket() -> None:
    check_directory(SOCKET_DIR, 997, 986, 0o750)
    try:
        st = SOCKET.lstat()
    except OSError:
        fail("reader socket is missing")
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISSOCK(st.st_mode):
        fail("reader socket path is not a socket")
    if (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) != (997, 986, 0o660):
        fail("reader socket has unsafe owner, group, or mode")


def check_runtime_files() -> None:
    check_directory(DEPLOY_DIR, 0, 0, 0o700)
    check_directory(CONFIG_DIR, 994, 986, 0o500)
    for filename in ("server.crt", "server.key", "client-ca.crt", "token"):
        check_regular(CONFIG_DIR / filename, 994, 986, 0o400)
    check_directory(STATE_DIR, 994, 986, 0o700)
    check_socket()
    check_artifact()
    # Parent stages the verified digest locally. This check never pulls or contacts a registry.
    digests = run(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", IMAGE])
    try:
        if IMAGE not in json.loads(digests):
            fail("the exact release image digest is not staged locally")
    except json.JSONDecodeError:
        fail("Docker returned invalid local image metadata")


def docker_networks() -> list[dict]:
    ids = run(["docker", "network", "ls", "-q"]).splitlines()
    if not ids:
        return []
    return json.loads(run(["docker", "network", "inspect", *ids]))


def verify_network(net: dict) -> None:
    ipam = net.get("IPAM", {}).get("Config", [])
    labels = net.get("Labels") or {}
    options = net.get("Options") or {}
    exact = (
        net.get("Name") == NETWORK
        and net.get("Driver") == "bridge"
        and net.get("EnableIPv6") is False
        and net.get("Internal") is False
        and len(ipam) == 1
        and ipam[0].get("Subnet") == SUBNET
        and ipam[0].get("Gateway") == GATEWAY
        and options.get("com.docker.network.bridge.name") == BRIDGE
        and labels.get(LABEL_KEY) == "true"
    )
    if not exact:
        fail("existing mandate-receipts network is foreign or differs from the fixed configuration")


def check_route_conflicts(networks: list[dict]) -> None:
    target = ipaddress.ip_network(SUBNET)
    for net in networks:
        if net.get("Name") == NETWORK:
            continue
        for config in ((net.get("IPAM") or {}).get("Config") or []):
            subnet = config.get("Subnet")
            if not subnet:
                continue
            try:
                other = ipaddress.ip_network(subnet, strict=False)
            except ValueError:
                fail("Docker has an invalid network route configuration")
            if target.overlaps(other):
                fail(f"Docker network {net.get('Name', '<unknown>')} overlaps the broker subnet")
    try:
        routes = json.loads(run(["ip", "-j", "-4", "route", "show", "table", "all"]))
    except json.JSONDecodeError:
        fail("ip returned invalid route metadata")
    for route in routes:
        destination = route.get("dst")
        if not destination or destination == "default":
            continue
        try:
            other = ipaddress.ip_network(destination, strict=False)
        except ValueError:
            fail("host has an invalid IPv4 route")
        if target.overlaps(other) and route.get("dev") != BRIDGE:
            fail("host IPv4 route overlaps the broker subnet")
    link = run(["ip", "-o", "link", "show", "dev", BRIDGE], check=False)
    matching = [net for net in networks if net.get("Name") == NETWORK]
    if link and not matching:
        fail("bridge interface name br-receipts is already occupied")
    if matching and not link:
        fail("managed Docker network exists without its expected bridge interface")


def ensure_network() -> None:
    networks = docker_networks()
    check_route_conflicts(networks)
    matches = [net for net in networks if net.get("Name") == NETWORK]
    if matches:
        if len(matches) != 1:
            fail("multiple networks have the reserved mandate-receipts name")
        verify_network(matches[0])
        return
    run([
        "docker", "network", "create", "--driver", "bridge",
        "--subnet", SUBNET, "--gateway", GATEWAY,
        "--ipv6=false", "--opt", f"com.docker.network.bridge.name={BRIDGE}",
        "--label", f"{LABEL_KEY}=true", NETWORK,
    ])
    matches = [net for net in docker_networks() if net.get("Name") == NETWORK]
    if len(matches) != 1:
        fail("created network could not be verified")
    verify_network(matches[0])


def iptables(*args: str, check: bool = True) -> str:
    return run(["iptables", *args], check=check)


def chain_lines(chain: str) -> list[list[str]]:
    output = iptables("-S", chain)
    rules = []
    for line in output.splitlines():
        words = shlex.split(line)
        if words and words[0] == "-A":
            if len(words) < 3 or words[1] != chain:
                fail(f"unexpected rule output for {chain}")
            rules.append(words[2:])
    return rules


def normalize_owned_rule(tokens: list[str]) -> tuple[str | None, ...]:
    """Parse only known owned-chain predicates; reject every other iptables match."""
    values: dict[str, str] = {}
    modules: set[str] = set()
    takes_value = {"-i": "in", "-o": "out", "-s": "source", "-d": "dest", "-p": "proto", "--ctstate": "ctstate", "--dport": "dport", "-j": "target"}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "-m":
            index += 1
            if index >= len(tokens) or tokens[index] not in ("conntrack", "tcp") or tokens[index] in modules:
                fail("owned firewall rule contains an unknown or duplicate match module")
            modules.add(tokens[index])
        elif token in takes_value:
            key = takes_value[token]
            index += 1
            if index >= len(tokens) or key in values:
                fail("owned firewall rule contains a missing or duplicate predicate")
            values[key] = tokens[index]
        else:
            fail("owned firewall rule contains an unsupported match or option")
        index += 1
    if "target" not in values:
        fail("owned firewall rule has no target")
    if ("ctstate" in values) != ("conntrack" in modules):
        fail("owned firewall conntrack predicate is malformed")
    if "ctstate" in values:
        states = values["ctstate"].split(",")
        if len(states) != len(set(states)) or set(states) not in ({"INVALID"}, {"ESTABLISHED", "RELATED"}):
            fail("owned firewall conntrack state is not permitted")
        values["ctstate"] = ",".join(sorted(states))
    if "tcp" in modules and values.get("proto") != "tcp":
        fail("owned firewall tcp matcher lacks a tcp protocol")
    if "dport" in values and values.get("proto") != "tcp":
        fail("owned firewall destination port lacks a tcp protocol")
    return tuple(values.get(key) for key in ("in", "out", "source", "dest", "proto", "ctstate", "dport", "target"))


def validate_owned_rules(rules: list[list[str]]) -> None:
    actual = [normalize_owned_rule(rule) for rule in rules]
    expected = [normalize_owned_rule(rule) for rule in CHAIN_RULES]
    if actual != expected:
        fail("owned firewall chain is partial or differs from the fixed rules; operator repair is required")


def check_rule_normalizer() -> None:
    """Offline deterministic checks for the iptables -S spellings accepted here."""
    canonical = CHAIN_RULES[3]
    reordered = ["-s", "10.108.0.9/32", "-p", "tcp", "-m", "tcp", "--dport", "8443", "-d", "172.30.94.2/32", "-o", BRIDGE, "-j", "ACCEPT"]
    validate_owned_rules(CHAIN_RULES[:3] + [reordered] + CHAIN_RULES[4:])
    reversed_states = ["-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"]
    validate_owned_rules(CHAIN_RULES[:1] + [reversed_states] + CHAIN_RULES[2:])
    if normalize_owned_rule(canonical) != normalize_owned_rule(reordered):
        fail("firewall normalization self-check failed for reordered tcp rule")
    cases = []
    broadened_source = list(canonical)
    broadened_source[broadened_source.index("10.108.0.9/32")] = "10.108.0.0/16"
    cases.append(CHAIN_RULES[:3] + [broadened_source] + CHAIN_RULES[4:])
    broadened_port = list(canonical)
    broadened_port[broadened_port.index("8443")] = "8442"
    cases.append(CHAIN_RULES[:3] + [broadened_port] + CHAIN_RULES[4:])
    unknown_match = list(canonical) + ["-m", "owner", "--uid-owner", "0"]
    cases.append(CHAIN_RULES[:3] + [unknown_match] + CHAIN_RULES[4:])
    for case in cases:
        try:
            validate_owned_rules(case)
        except DeploymentError:
            continue
        fail("firewall normalization self-check accepted an unsafe synthetic rule")


def ensure_owned_chain() -> None:
    """Create only an absent owned chain; an existing mismatch is never flushed."""
    if iptables("-S", CHAIN, check=False):
        validate_owned_rules(chain_lines(CHAIN))
        return
    iptables("-N", CHAIN)
    for rule in CHAIN_RULES:
        iptables("-A", CHAIN, *rule)
    validate_owned_rules(chain_lines(CHAIN))


def ensure_first_hook(parent: str, expected: list[str], position: int) -> None:
    rules = chain_lines(parent)
    occurrences = sum(1 for rule in rules if rule == expected)
    if occurrences:
        if occurrences != 1 or len(rules) < position or rules[position - 1] != expected:
            fail(f"firewall hook in {parent} is duplicated or not at the required position")
        return
    iptables("-I", parent, str(position), *expected)
    rules = chain_lines(parent)
    if len(rules) < position or rules[position - 1] != expected:
        fail(f"firewall hook in {parent} did not verify")


def install_firewall() -> None:
    ensure_owned_chain()
    ensure_first_hook("DOCKER-USER", DOCKER_HOOKS[0], 1)
    ensure_first_hook("DOCKER-USER", DOCKER_HOOKS[1], 2)
    ensure_first_hook("INPUT", INPUT_HOOK, 1)


def firewall_preflight() -> None:
    run(["iptables", "-S", "DOCKER-USER"])
    run(["iptables", "-S", "INPUT"])
    install_firewall()
    validate_owned_rules(chain_lines(CHAIN))
    if chain_lines("DOCKER-USER")[:2] != DOCKER_HOOKS:
        fail("DOCKER-USER broker hooks are not first")
    if not chain_lines("INPUT") or chain_lines("INPUT")[0] != INPUT_HOOK:
        fail("host INPUT broker drop is not first")


def inspect_container() -> dict | None:
    output = run(["docker", "container", "inspect", NAME], check=False)
    if not output:
        return None
    try:
        containers = json.loads(output)
        return containers[0] if containers else None
    except (json.JSONDecodeError, IndexError):
        fail("Docker returned invalid container metadata")


def verify_owned_container(info: dict) -> None:
    config = info.get("Config") or {}
    labels = config.get("Labels") or {}
    if labels.get(LABEL_KEY) != "true" or config.get("Image") != IMAGE:
        fail("reserved container name is occupied by an unexpected container")


def preflight() -> None:
    require_root()
    ensure_xtables_lock()
    check_runtime_files()
    ensure_network()
    # Firewall is installed and verified before any container can be started.
    firewall_preflight()
    existing = inspect_container()
    if existing:
        verify_owned_container(existing)


def docker_run_argv() -> list[str]:
    env = {
        "CITRUS_RECEIPTS_BROKER_HOST": "0.0.0.0",
        "CITRUS_RECEIPTS_BROKER_PORT": "8443",
        "CITRUS_RECEIPTS_SERVER_CERT_FILE": "/run/mandate-receipts-config/server.crt",
        "CITRUS_RECEIPTS_SERVER_KEY_FILE": "/run/mandate-receipts-config/server.key",
        "CITRUS_RECEIPTS_CLIENT_CA_FILE": "/run/mandate-receipts-config/client-ca.crt",
        "CITRUS_RECEIPTS_CLIENT_URI_SAN": "spiffe://mandate.garz.ai/core/citrus-dev-receipts",
        "CITRUS_RECEIPTS_API_ORIGIN": "https://dev.citrus-grace.com",
        "CITRUS_RECEIPTS_TOKEN_FILE": "/run/mandate-receipts-config/token",
        "CITRUS_RECEIPTS_SERVICE_IDENTITY": "mandate-citrus-dev-receipts",
        "CITRUS_RECEIPTS_READER_SOCKET": str(SOCKET),
        "CITRUS_RECEIPTS_READER_UID": "997",
        "CITRUS_RECEIPTS_STATE_DIR": str(STATE_DIR),
    }
    argv = [
        "docker", "run", "--detach", "--rm", "--name", NAME,
        "--label", f"{LABEL_KEY}=true",
        "--network", NETWORK, "--ip", CONTAINER_IP,
        "--publish", f"{HOST_IP}:8444:8443/tcp",
        "--user", "994:986", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
        "--memory", "256m", "--memory-swap", "256m", "--cpus", "0.5", "--pids-limit", "32",
        "--ulimit", "nofile=512:512", "--log-driver", "json-file",
        "--log-opt", "max-size=5m", "--log-opt", "max-file=2",
        "--mount", f"type=bind,src={SOCKET_DIR},dst={SOCKET_DIR},readonly",
        "--mount", f"type=bind,src={CONFIG_DIR},dst=/run/mandate-receipts-config,readonly",
        "--mount", f"type=bind,src={STATE_DIR},dst={STATE_DIR}",
    ]
    for key, value in env.items():
        argv.extend(("--env", f"{key}={value}"))
    argv.append(IMAGE)
    return argv


def start() -> None:
    require_root()
    # Repeat all gates immediately before creation; a failed rule check means no start.
    preflight()
    existing = inspect_container()
    if existing:
        verify_owned_container(existing)
        if (existing.get("State") or {}).get("Running"):
            fail("broker container already exists; refusing duplicate activation")
        fail("stopped broker container remains; operator inspection is required")
    run(docker_run_argv())
    info = inspect_container()
    if not info:
        fail("broker container disappeared immediately after start")
    verify_owned_container(info)
    if not (info.get("State") or {}).get("Running"):
        fail("broker container did not remain running")
    while True:
        time.sleep(3)
        info = inspect_container()
        if not info or not (info.get("State") or {}).get("Running"):
            fail("broker container exited; systemd will apply bounded restart policy")


def stop() -> None:
    require_root()
    info = inspect_container()
    if not info:
        return
    verify_owned_container(info)
    run(["docker", "stop", "--time", "10", NAME])


def status() -> None:
    info = inspect_container()
    if not info:
        print("broker container: absent")
        return
    verify_owned_container(info)
    state = (info.get("State") or {}).get("Status", "unknown")
    print(f"broker container: {state}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("preflight", "install", "prestart", "start", "stop", "status", "self-check"))
    args = parser.parse_args()
    try:
        if args.phase == "status":
            status()
            return 0
        if args.phase == "self-check":
            check_rule_normalizer()
            print("offline firewall normalization checks passed")
            return 0
        require_root()
        if args.phase == "install":
            ensure_xtables_lock(create=True)
            check_runtime_files()
            ensure_network()
            check_ancestors(UNIT_DEST)
            source = UNIT_SOURCE.read_bytes()
            if UNIT_DEST.exists() or UNIT_DEST.is_symlink():
                st = UNIT_DEST.lstat()
                if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                    fail("existing broker unit path is not a regular file")
                if (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) != (0, 0, 0o644):
                    fail("existing broker unit has unsafe ownership or mode")
                if UNIT_DEST.read_bytes() != source:
                    fail("existing broker unit differs; operator inspection is required")
            else:
                fd, temporary_name = tempfile.mkstemp(prefix=".mandate-receipts-broker.", suffix=".new", dir=str(UNIT_DEST.parent))
                temporary = Path(temporary_name)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(source)
                    os.chmod(temporary, 0o644)
                    os.chown(temporary, 0, 0)
                    os.replace(temporary, UNIT_DEST)
                finally:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass
            run(["systemctl", "daemon-reload"])
            print("installed broker unit and verified dedicated network")
        elif args.phase in ("preflight", "prestart"):
            ensure_xtables_lock()
            preflight()
            print("broker prerequisites, network, and firewall verified")
        elif args.phase == "start":
            start()
        elif args.phase == "stop":
            stop()
        return 0
    except DeploymentError as exc:
        print(f"deploy: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
