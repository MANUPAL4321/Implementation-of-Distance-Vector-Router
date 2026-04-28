import socket
import json
import threading
import time
import os
import ipaddress
import subprocess

# Configuration (to be adjusted per container)
# Environment variables used to facilitate deployment with Docker
NODE_ADDR = os.getenv("MY_IP", "127.0.0.1")
PEER_LIST = [n for n in os.getenv("NEIGHBORS", "").split(",") if n]
LISTEN_PORT = 5000

PROTO_VERSION = 1.0
MAX_HOPS = 16
BROADCAST_INTERVAL = 5
ENTRY_EXPIRY = 15
CLEANUP_DELAY = 30

# Forwarding Table: { Subnet: {hops, gateway, last_seen, is_local} }
forwarding_table = {}
fw_lock = threading.Lock()
update_signal = threading.Event()


def fetch_local_networks():
    nets = []
    try:
        raw = subprocess.check_output(["ip", "-o", "-4", "addr", "show"], text=True)
    except Exception:
        return nets

    for line in raw.splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        iface_name = cols[1]
        if iface_name == "lo":
            continue
        cidr_str = cols[3]
        try:
            net = ipaddress.ip_interface(cidr_str).network
        except ValueError:
            continue
        nets.append(str(net))
    return nets


def resolve_iface_for_addr(dest_ip):
    try:
        raw = subprocess.check_output(["ip", "-o", "-4", "addr", "show"], text=True)
    except Exception:
        return None

    dest = ipaddress.ip_address(dest_ip)
    for line in raw.splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        iface_name = cols[1]
        if iface_name == "lo":
            continue
        cidr_str = cols[3]
        try:
            net = ipaddress.ip_interface(cidr_str).network
        except ValueError:
            continue
        if dest in net:
            return iface_name
    return None


def local_ip_towards_peer(peer_ip):
    try:
        raw = subprocess.check_output(["ip", "-o", "-4", "addr", "show"], text=True)
    except Exception:
        return NODE_ADDR

    dest = ipaddress.ip_address(peer_ip)
    for line in raw.splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        if cols[1] == "lo":
            continue
        try:
            iface_obj = ipaddress.ip_interface(cols[3])
        except ValueError:
            continue
        if dest in iface_obj.network:
            return str(iface_obj.ip)
    return NODE_ADDR


def install_route(prefix, via_ip):
    dev = resolve_iface_for_addr(via_ip)
    if dev:
        return os.system(f"ip route replace {prefix} via {via_ip} dev {dev} onlink")
    return os.system(f"ip route replace {prefix} via {via_ip} onlink")


def remove_route(prefix, via_ip):
    dev = resolve_iface_for_addr(via_ip)
    if dev:
        return os.system(f"ip route del {prefix} via {via_ip} dev {dev}")
    return os.system(f"ip route del {prefix} via {via_ip}")


def populate_forwarding_table():
    ts = time.time()
    for prefix in fetch_local_networks():
        forwarding_table[prefix] = {
            "hops": 0,
            "gateway": "0.0.0.0",
            "last_seen": ts,
            "is_local": True,
        }


def sync_local_networks():
    ts = time.time()
    live_prefixes = set(fetch_local_networks())
    for prefix in live_prefixes:
        existing = forwarding_table.get(prefix)
        if existing is None or not existing.get("is_local"):
            forwarding_table[prefix] = {
                "hops": 0,
                "gateway": "0.0.0.0",
                "last_seen": ts,
                "is_local": True,
            }
    stale = []
    for prefix, meta in list(forwarding_table.items()):
        if meta.get("is_local") and prefix not in live_prefixes:
            stale.append(prefix)
    modified = False
    for prefix in stale:
        del forwarding_table[prefix]
        os.system(f"ip route del {prefix}")
        print(f"[LOST] Direct subnet {prefix} removed (link down)")
        modified = True
    if modified:
        update_signal.set()


def prepare_advertisement(peer_ip):
    advert = []
    for prefix, meta in forwarding_table.items():
        hops = meta["hops"]
        if not meta["is_local"] and meta["gateway"] == peer_ip:
            hops = MAX_HOPS
        advert.append({"subnet": prefix, "distance": min(hops, MAX_HOPS)})
    return advert


def send_advertisements():
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    while True:
        update_signal.wait(timeout=BROADCAST_INTERVAL)
        update_signal.clear()

        with fw_lock:
            for peer in PEER_LIST:
                src_ip = local_ip_towards_peer(peer)
                payload = {
                    "router_id": src_ip,
                    "version": PROTO_VERSION,
                    "routes": prepare_advertisement(peer),
                }
                raw_bytes = json.dumps(payload).encode("utf-8")
                try:
                    udp_sock.sendto(raw_bytes, (peer, LISTEN_PORT))
                except OSError:
                    continue


def receive_advertisements():
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sock.bind(("0.0.0.0", LISTEN_PORT))
    while True:
        raw_bytes, origin = udp_sock.recvfrom(65535)
        try:
            payload = json.loads(raw_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue

        if payload.get("version") != PROTO_VERSION:
            continue
        entries = payload.get("routes")
        if not isinstance(entries, list):
            continue

        peer_ip = payload.get("router_id") or origin[0]
        process_advertisement(peer_ip, entries)


def process_advertisement(peer_ip, received_entries):
    modified = False
    ts = time.time()

    with fw_lock:
        for entry in received_entries:
            prefix = entry.get("subnet")
            hops = entry.get("distance")
            if not prefix or not isinstance(hops, (int, float)):
                continue
            hops = int(hops)
            candidate_hops = min(hops + 1, MAX_HOPS)
            existing = forwarding_table.get(prefix)

            if existing and existing.get("is_local"):
                continue

            if existing is None:
                if candidate_hops < MAX_HOPS:
                    forwarding_table[prefix] = {
                        "hops": candidate_hops,
                        "gateway": peer_ip,
                        "last_seen": ts,
                        "is_local": False,
                    }
                    install_route(prefix, peer_ip)
                    print(f"[ADD] {prefix} via {peer_ip} dist {candidate_hops}")
                    modified = True
                continue

            if existing["gateway"] == peer_ip:
                if candidate_hops >= MAX_HOPS:
                    if existing["hops"] < MAX_HOPS:
                        remove_route(prefix, peer_ip)
                    existing["hops"] = MAX_HOPS
                    existing["last_seen"] = ts
                    print(f"[DOWN] {prefix} via {peer_ip} dist {MAX_HOPS}")
                    modified = True
                else:
                    if candidate_hops != existing["hops"]:
                        install_route(prefix, peer_ip)
                        existing["hops"] = candidate_hops
                        existing["last_seen"] = ts
                        print(f"[UPD] {prefix} via {peer_ip} dist {candidate_hops}")
                        modified = True
                    else:
                        existing["last_seen"] = ts
            else:
                if candidate_hops < existing["hops"]:
                    forwarding_table[prefix] = {
                        "hops": candidate_hops,
                        "gateway": peer_ip,
                        "last_seen": ts,
                        "is_local": False,
                    }
                    install_route(prefix, peer_ip)
                    print(f"[BETTER] {prefix} via {peer_ip} dist {candidate_hops}")
                    modified = True

    if modified:
        update_signal.set()


def expiry_loop():
    while True:
        ts = time.time()
        modified = False
        with fw_lock:
            sync_local_networks()
            for prefix, meta in list(forwarding_table.items()):
                if meta["is_local"]:
                    continue

                elapsed = ts - meta["last_seen"]
                if meta["hops"] < MAX_HOPS and elapsed > ENTRY_EXPIRY:
                    remove_route(prefix, meta["gateway"])
                    meta["hops"] = MAX_HOPS
                    meta["last_seen"] = ts
                    print(f"[TIMEOUT] {prefix} via {meta['gateway']}")
                    modified = True
                    continue

                if meta["hops"] >= MAX_HOPS and elapsed > CLEANUP_DELAY:
                    print(f"[GC] {prefix}")
                    del forwarding_table[prefix]
                    modified = True

        if modified:
            update_signal.set()
        time.sleep(1)


if __name__ == "__main__":
    with fw_lock:
        populate_forwarding_table()
    print(f"[START] node_addr={NODE_ADDR} peers={PEER_LIST} local_nets={list(forwarding_table.keys())}")

    threading.Thread(target=send_advertisements, daemon=True).start()
    threading.Thread(target=expiry_loop, daemon=True).start()
    receive_advertisements()
