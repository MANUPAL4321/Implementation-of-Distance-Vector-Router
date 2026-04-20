# Implementation-of-Distance-Vector-Router
Implementation of a Distance Vector Routing Protocol (similar to RIP) using Python and Docker. The project simulates multiple routers communicating via UDP to dynamically discover network topology and compute shortest paths using the Bellman-Ford algorithm.

# Distance Vector Router (Python + Docker)

This project implements a custom Distance Vector Routing Protocol (similar to RIP) from scratch using Python. It simulates routers using Docker containers that communicate via UDP to dynamically discover network topology and compute shortest paths.

---

## 🚀 Features

- Implementation of Bellman-Ford Algorithm
- UDP-based router communication
- Dynamic routing table updates
- Docker-based virtual network topology
- Handling of routing loops using Split Horizon
- Simulation of network failures and recovery

---

## 🧠 Concepts Used

- Distance Vector Routing
- Bellman-Ford Algorithm
- UDP Sockets in Python
- Linux Routing Table (`ip route`)
- Docker Networking

---

## 🧪 Project Topology

Triangle topology with 3 routers:

A
/
B---C


Each router connects to two networks.

---

## ⚙️ Setup Instructions

### 1. Install dependencies

- Python 3
- Docker

---

### 2. Build Docker Image

```bash
docker build -t my-router .

---

### 3. Create Network

docker network create --subnet=10.0.1.0/24 net_ab
docker network create --subnet=10.0.2.0/24 net_bc
docker network create --subnet=10.0.3.0/24 net_ac

---

### 4. Run Routers

Example:

docker run -d --name router_a --privileged \
  --network net_ab --ip 10.0.1.1 \
  -e MY_IP=10.0.1.1 -e NEIGHBORS=10.0.1.2,10.0.3.2 \
  my-router

---

### 📡 Packet Format (DV-JSON)
{
  "router_id": "10.0.1.1",
  "version": 1.0,
  "routes": [
    {
      "subnet": "10.0.1.0/24",
      "distance": 0
    }
  ]
}

---

### 📊 Learning Outcomes
Hands-on implementation of routing protocols
Understanding dynamic routing behavior
Experience with containerized networking using Docker

---

---

# 🚀 How to push to GitHub

Inside your folder:

```bash
git init
git add .
git commit -m "Initial commit - Distance Vector Router"

---

git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/distance-vector-router.git
git push -u origin main
