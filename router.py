import socket
import json
import threading
import time
import os
import subprocess

# Configuration (to be adjusted per container)
# Environment variables used to facilitate deployment with Docker
MY_IP = os.getenv("MY_IP", "127.0.0.1")
NEIGHBORS = [n for n in os.getenv("NEIGHBORS", "").split(",") if n]
PORT = 5000

# Constants
BROADCAST_INTERVAL = 5
TIMEOUT_INTERVAL = 15
INFINITY = 16

# Initial Table: { Subnet: [Distance, Next_Hop, Timestamp] }
# Example: {"10.0.1.0/24": [0, "0.0.0.0", <time>]}
routing_table = {}
table_lock = threading.Lock()

def initialize_routes():
    """Discover directly connected subnets via the Linux routing table."""
    try:
        output = subprocess.getoutput("ip -4 route show proto kernel")
        for line in output.split('\n'):
            if line.strip():
                parts = line.split()
                if len(parts) > 0:
                    subnet = parts[0]
                    # Distance 0, next_hop 0.0.0.0, current timestamp
                    with table_lock:
                        routing_table[subnet] = [0, "0.0.0.0", time.time()]
                        print(f"[*] Discovered direct subnet: {subnet}")
    except Exception as e:
        print(f"Error reading initial routes: {e}")

def broadcast_updates():
    udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Allows sending to network if needed
    udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    while True:
        with table_lock:
            for neighbor in NEIGHBORS:
                routes_to_send = []
                for subnet, ds in routing_table.items():
                    distance = ds[0]
                    next_hop = ds[1]
                    
                    # Split Horizon: Do not advertise a route back to the neighbor 
                    # from which we learned it to avoid "Count to Infinity"
                    if next_hop == neighbor:
                        continue
                    
                    routes_to_send.append({
                        "subnet": subnet,
                        "distance": distance
                    })

                packet = {
                    "router_id": MY_IP,
                    "version": 1.0,
                    "routes": routes_to_send
                }
                
                try:
                    udp_socket.sendto(json.dumps(packet).encode('utf-8'), (neighbor, PORT))
                except Exception as e:
                    pass # Neighbor might not be up yet
        
        time.sleep(BROADCAST_INTERVAL)

def update_logic(neighbor_ip, routes_from_neighbor):
    changed = False
    current_time = time.time()
    
    with table_lock:
        for route in routes_from_neighbor:
            subnet = route.get("subnet")
            neighbor_dist = route.get("distance")
            new_dist = neighbor_dist + 1
            
            if new_dist >= INFINITY:
                continue

            if subnet not in routing_table:
                # 1. We found a completely new route
                routing_table[subnet] = [new_dist, neighbor_ip, current_time]
                changed = True
                print(f"[+] New route: {subnet} via {neighbor_ip} (Cost: {new_dist})")
                os.system(f"ip route replace {subnet} via {neighbor_ip}")
                
            else:
                current_dist = routing_table[subnet][0]
                current_next_hop = routing_table[subnet][1]
                
                # 2. We found a shorter path OR the existing path was updated by the same next hop
                if new_dist < current_dist or current_next_hop == neighbor_ip:
                    if new_dist != current_dist or current_next_hop != neighbor_ip:
                        print(f"[*] Updated route: {subnet} via {neighbor_ip} (Cost: {new_dist})")
                        os.system(f"ip route replace {subnet} via {neighbor_ip}")
                        changed = True
                    
                    # Refresh the distance and timestamp
                    routing_table[subnet] = [new_dist, neighbor_ip, current_time]
    
    if changed:
        print(f"Current routing table: {routing_table}")

def check_timeouts():
    """Periodically check for timed-out routes to remove dead neighbors"""
    while True:
        current_time = time.time()
        with table_lock:
            # Create a list of subnets to delete to avoid modifying dict while iterating
            subnets_to_delete = []
            for subnet, (dist, next_hop, timestamp) in routing_table.items():
                # Don't timeout directly connected routes (dist == 0)
                if dist > 0 and (current_time - timestamp > TIMEOUT_INTERVAL):
                    subnets_to_delete.append(subnet)
            
            for subnet in subnets_to_delete:
                next_hop = routing_table[subnet][1]
                print(f"[-] Route timeout: {subnet} via {next_hop}. Removing route.")
                os.system(f"ip route del {subnet} via {next_hop}")
                del routing_table[subnet]
                
        time.sleep(5)

def listen_for_updates():
    udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_socket.bind(("0.0.0.0", PORT))
    
    print(f"Listening for updates on UDP port {PORT}...")
    while True:
        data, addr = udp_socket.recvfrom(4096)
        try:
            packet = json.loads(data.decode('utf-8'))
            neighbor_ip = addr[0]
            if "routes" in packet:
                update_logic(neighbor_ip, packet["routes"])
        except json.JSONDecodeError:
            pass

if __name__ == "__main__":
    print("Router started at", MY_IP)
    initialize_routes()
    
    # Start threads
    threading.Thread(target=broadcast_updates, daemon=True).start()
    threading.Thread(target=check_timeouts, daemon=True).start()
    
    # Run server listening on main thread
    listen_for_updates()