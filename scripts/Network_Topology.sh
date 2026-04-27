#!/bin/bash
# Limpiar topología antigua
for i in 0 1 2 3; do sudo ip link del tap$i 2>/dev/null || true; done
sudo ip link del br-roce 2>/dev/null || true

# Bridges punto a punto (cables worker↔switch)
for i in 1 2 3; do
    sudo ip link add br-link$i type bridge
    sudo ip link set br-link$i up

    sudo ip tuntap add dev tap-w$i mode tap
    sudo ip link set tap-w$i master br-link$i
    sudo ip link set tap-w$i up

    sudo ip tuntap add dev tap-s$i mode tap
    sudo ip link set tap-s$i master br-link$i
    sudo ip link set tap-s$i up
done

# Bridge de gestión (host ↔ switch)
sudo ip link add br-mgmt type bridge
sudo ip link set br-mgmt up
sudo ip addr add 10.10.0.254/24 dev br-mgmt

sudo ip tuntap add dev tap-sm mode tap
sudo ip link set tap-sm master br-mgmt
sudo ip link set tap-sm up

# iptables
sudo sysctl -w net.bridge.bridge-nf-call-iptables=0 > /dev/null
sudo sysctl -w net.bridge.bridge-nf-call-ip6tables=0 > /dev/null
sudo iptables -I FORWARD -j ACCEPT
sudo sysctl -w net.ipv4.ip_forward=1 > /dev/null

echo "=== Topología lista ==="
for br in br-link1 br-link2 br-link3 br-mgmt; do
    echo "--- $br ---"
    bridge link show master $br
done
