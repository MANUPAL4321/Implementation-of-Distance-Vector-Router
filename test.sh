#!/bin/bash

# Cleanup old containers and networks if they exist
echo "Cleaning up old lab environment..."
docker rm -f router_a router_b router_c >/dev/null 2>&1
docker network rm net_ab net_bc net_ac >/dev/null 2>&1

echo "Building Router Image..."
docker build -t my-router .

echo "Creating Networks..."
docker network create --subnet=10.0.1.0/24 --gateway=10.0.1.254 net_ab
docker network create --subnet=10.0.2.0/24 --gateway=10.0.2.254 net_bc
docker network create --subnet=10.0.3.0/24 --gateway=10.0.3.254 net_ac

echo "Starting Router A..."
docker run -d --name router_a --privileged \
  --network net_ab --ip 10.0.1.1 \
  -e ROUTER_ID=router_a -e MY_IP=10.0.1.1 -e NEIGHBORS=10.0.1.2,10.0.3.2 \
  my-router
docker network connect net_ac router_a --ip 10.0.3.1

echo "Starting Router B..."
docker run -d --name router_b --privileged \
  --network net_ab --ip 10.0.1.2 \
  -e ROUTER_ID=router_b -e MY_IP=10.0.1.2 -e NEIGHBORS=10.0.1.1,10.0.2.2 \
  my-router
docker network connect net_bc router_b --ip 10.0.2.1

echo "Starting Router C..."
docker run -d --name router_c --privileged \
  --network net_bc --ip 10.0.2.2 \
  -e ROUTER_ID=router_c -e MY_IP=10.0.2.2 -e NEIGHBORS=10.0.2.1,10.0.3.1 \
  my-router
docker network connect net_ac router_c --ip 10.0.3.2

echo "Lab Topology is running!"
echo "To view logs for Router A, run: docker logs -f router_a"
echo "To test failure, run: docker stop router_c"
