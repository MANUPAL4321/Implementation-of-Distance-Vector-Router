import ipaddress
import json
import os
import socket
import subprocess
import threading
import time
from typing import Any


PROTOCOL_VERSION = 1.0
PORT = int(os.getenv("PORT", "5000"))
INFINITY = int(os.getenv("INFINITY", "16"))
BROADCAST_INTERVAL = float(os.getenv("BROADCAST_INTERVAL", "5"))
NEIGHBOR_DEAD_INTERVAL = float(os.getenv("NEIGHBOR_DEAD_INTERVAL", "15"))

MY_IP = os.getenv("MY_IP", "127.0.0.1")
NEIGHBORS = [n.strip() for n in os.getenv("NEIGHBORS", "").split(",") if n.strip()]
DIRECT_SUBNETS_ENV = [s.strip() for s in os.getenv("DIRECT_SUBNETS", "").split(",") if s.strip()]

DIRECT_SOURCE = "direct"
NEIGHBOR_SOURCE = "neighbor"
DIRECT_NEXT_HOP = "0.0.0.0"

RouteEntry = dict[str, Any]
RoutingTable = dict[str, RouteEntry]
NeighborState = dict[str, Any]

routing_table: RoutingTable = {}
neighbor_tables: dict[str, NeighborState] = {}
state_lock = threading.Lock()


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def run_ip_route(args: list[str]) -> None:
    result = subprocess.run(
        ["ip", "route", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        error = result.stderr.strip() or result.stdout.strip() or "unknown error"
        log(f"ip route {' '.join(args)} failed: {error}")


def normalize_subnet(value: str) -> str | None:
    try:
        return str(ipaddress.ip_network(value, strict=False))
    except ValueError:
        return None


def make_route(distance: int, next_hop: str, source: str) -> RouteEntry:
    return {
        "distance": distance,
        "next_hop": next_hop,
        "source": source,
    }


def route_learned_from_neighbor(entry: RouteEntry | None) -> bool:
    return bool(entry and entry["source"] == NEIGHBOR_SOURCE)


def discover_direct_subnets() -> set[str]:
    discovered = set()

    try:
        output = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show", "scope", "global"],
            text=True,
        )
        for line in output.splitlines():
            parts = line.split()
            if "inet" not in parts:
                continue
            cidr = parts[parts.index("inet") + 1]
            network = ipaddress.ip_interface(cidr).network
            discovered.add(str(network))
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        log(f"Could not auto-discover subnets from interfaces: {exc}")

    for subnet in DIRECT_SUBNETS_ENV:
        normalized = normalize_subnet(subnet)
        if normalized is None:
            log(f"Ignoring invalid DIRECT_SUBNETS entry: {subnet}")
            continue
        discovered.add(normalized)

    return discovered


def direct_route_entries() -> RoutingTable:
    entries: RoutingTable = {}
    for subnet in sorted(discover_direct_subnets()):
        entries[subnet] = make_route(0, DIRECT_NEXT_HOP, DIRECT_SOURCE)
    return entries


def init_routing_table() -> None:
    direct_entries = direct_route_entries()
    if not direct_entries:
        log("No direct subnets discovered. Set DIRECT_SUBNETS if discovery fails.")

    with state_lock:
        routing_table.clear()
        routing_table.update(direct_entries)

    log(f"Router started with MY_IP={MY_IP}, neighbors={NEIGHBORS}")
    log(f"Direct subnets: {sorted(direct_entries)}")


def validate_packet(packet: dict[str, Any]) -> bool:
    return (
        isinstance(packet, dict)
        and packet.get("version") == PROTOCOL_VERSION
        and isinstance(packet.get("routes"), list)
    )


def parse_routes(routes: list[dict[str, Any]]) -> dict[str, int]:
    cleaned: dict[str, int] = {}

    for entry in routes:
        if not isinstance(entry, dict):
            continue

        subnet = entry.get("subnet")
        distance = entry.get("distance")

        if not isinstance(subnet, str):
            continue
        subnet = normalize_subnet(subnet)
        if subnet is None:
            continue

        try:
            distance = int(distance)
        except (ValueError, TypeError):
            continue

        cleaned[subnet] = max(0, min(distance, INFINITY))

    return cleaned


def build_packet(for_neighbor: str | None = None) -> dict[str, Any]:
    with state_lock:
        packet_routes = []
        for subnet, entry in sorted(routing_table.items()):
            advertised_distance = entry["distance"]

            if (
                for_neighbor
                and entry["source"] == NEIGHBOR_SOURCE
                and entry["next_hop"] == for_neighbor
            ):
                advertised_distance = INFINITY

            packet_routes.append(
                {
                    "subnet": subnet,
                    "distance": int(min(advertised_distance, INFINITY)),
                }
            )

    return {
        "router_id": MY_IP,
        "version": PROTOCOL_VERSION,
        "routes": packet_routes,
    }


def apply_kernel_route_changes(old_table: RoutingTable, new_table: RoutingTable) -> None:
    affected_subnets = set(old_table.keys()) | set(new_table.keys())

    for subnet in sorted(affected_subnets):
        old_entry = old_table.get(subnet)
        new_entry = new_table.get(subnet)

        old_is_dynamic = route_learned_from_neighbor(old_entry)
        new_is_dynamic = route_learned_from_neighbor(new_entry)

        if old_is_dynamic and not new_is_dynamic:
            run_ip_route(["del", subnet])
            log(f"Removed route {subnet}")
            continue

        if new_is_dynamic:
            should_replace = (
                not old_is_dynamic
                or old_entry["next_hop"] != new_entry["next_hop"]
                or old_entry["distance"] != new_entry["distance"]
            )
            if should_replace:
                run_ip_route(["replace", subnet, "via", new_entry["next_hop"]])
                log(
                    f"Route {subnet} via {new_entry['next_hop']} "
                    f"(distance {new_entry['distance']})"
                )


def recompute_routes_locked() -> None:
    old_table = dict(routing_table)
    new_table = direct_route_entries()
    now = time.time()

    expired_neighbors = [
        neighbor_ip
        for neighbor_ip, neighbor_state in neighbor_tables.items()
        if now - neighbor_state["last_seen"] > NEIGHBOR_DEAD_INTERVAL
    ]
    for neighbor_ip in expired_neighbors:
        del neighbor_tables[neighbor_ip]

    for neighbor_ip in list(neighbor_tables.keys()):
        neighbor_state = neighbor_tables.get(neighbor_ip)
        if not neighbor_state:
            continue

        for subnet, neighbor_distance in neighbor_state["routes"].items():
            if subnet in new_table:
                continue

            candidate = min(INFINITY, neighbor_distance + 1)
            if candidate >= INFINITY:
                continue

            current = new_table.get(subnet)
            if not current:
                new_table[subnet] = make_route(candidate, neighbor_ip, NEIGHBOR_SOURCE)
                continue

            better_distance = candidate < current["distance"]
            same_distance_better_tie = (
                candidate == current["distance"] and neighbor_ip < current["next_hop"]
            )
            if better_distance or same_distance_better_tie:
                new_table[subnet] = make_route(candidate, neighbor_ip, NEIGHBOR_SOURCE)

    apply_kernel_route_changes(old_table, new_table)
    routing_table.clear()
    routing_table.update(new_table)


def broadcast_updates() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    while True:
        for neighbor in NEIGHBORS:
            packet = build_packet(for_neighbor=neighbor)
            data = json.dumps(packet).encode("utf-8")
            try:
                sock.sendto(data, (neighbor, PORT))
            except OSError as exc:
                log(f"[ERROR] send to {neighbor} failed: {exc}")
        time.sleep(BROADCAST_INTERVAL)


def listen_for_updates() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", PORT))
    log(f"Listening for updates on UDP {PORT}")

    while True:
        data, addr = sock.recvfrom(65535)

        try:
            packet = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            continue

        if not validate_packet(packet):
            continue

        # 🔥 FIX: use router_id instead of addr[0]
        neighbor_id = addr[0] 
        if not neighbor_id:
            continue

        routes = parse_routes(packet["routes"])

        with state_lock:
            neighbor_tables[neighbor_id] = {
                "last_seen": time.time(),
                "routes": routes,
            }
            recompute_routes_locked()

def maintenance_loop() -> None:
    while True:
        with state_lock:
            recompute_routes_locked()
        time.sleep(1)


def format_routing_table() -> str:
    rows = []
    for subnet, entry in sorted(routing_table.items()):
        rows.append(
            f"{subnet:<18} dist={entry['distance']:<2} "
            f"next_hop={entry['next_hop']:<15} source={entry['source']}"
        )

    if not rows:
        return "Routing table: (empty)"

    return "Routing table:\n  " + "\n  ".join(rows)


def print_table_loop() -> None:
    while True:
        with state_lock:
            table_snapshot = format_routing_table()
        log(table_snapshot)
        time.sleep(5)


def main() -> None:
    init_routing_table()

    threading.Thread(target=broadcast_updates, daemon=True).start()
    threading.Thread(target=maintenance_loop, daemon=True).start()
    threading.Thread(target=print_table_loop, daemon=True).start()

    listen_for_updates()


if __name__ == "__main__":
    main()
