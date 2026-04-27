#!/usr/bin/env python3
"""
Agente SNMP pass_persist para el switch OVS.

Expone la tabla ovsPortTable bajo .1.3.6.1.4.1.99999.3:
  .1.3.6.1.4.1.99999.3.METRIC.PORT

  METRIC:
    1 = rx_bytes
    2 = tx_bytes
    3 = rx_pkts
    4 = tx_pkts
    5 = rx_drops
    6 = tx_drops
    7 = rx_errors

  PORT: número de puerto OVS (1 = worker1, 2 = worker2, 3 = worker3, 4 = host mgmt)

Ejemplo: .1.3.6.1.4.1.99999.3.1.3 = rx_bytes del puerto 3 (Worker 3)

Instalación:
  1. Copiar a /usr/local/bin/ovs_agent.py
  2. chmod +x /usr/local/bin/ovs_agent.py
  3. Permitir que snmpd ejecute ovs-ofctl:
       echo 'Debian-snmp ALL=(root) NOPASSWD: /usr/bin/ovs-ofctl' | \
           sudo tee /etc/sudoers.d/snmpd-ovs
  4. Añadir a /etc/snmp/snmpd.conf:
       pass_persist .1.3.6.1.4.1.99999.3 /usr/local/bin/ovs_agent.py
  5. sudo systemctl restart snmpd
"""

import sys
import os
import subprocess
import re

BASE_OID = ".1.3.6.1.4.1.99999.3"
OVS_BRIDGE = "br0"

# Puertos físicos a monitorizar (excluye LOCAL y mirror0)
# El número de puerto OVS se detecta dinámicamente del output de dump-ports
MONITORED_PORTS = {1, 2, 3, 4}

METRIC_NAMES = {
    1: "rx_bytes",
    2: "tx_bytes",
    3: "rx_pkts",
    4: "tx_pkts",
    5: "rx_drops",
    6: "tx_drops",
    7: "rx_errors",
}


def parse_dump_ports():
    """
    Parsea la salida de 'ovs-ofctl dump-ports br0'.

    Formato esperado:
      port  1: rx pkts=123, bytes=456, drop=0, errs=0, frame=0, over=0, crc=0
               tx pkts=789, bytes=012, drop=0, errs=0, coll=0
      port  2: rx pkts=...

    Devuelve: {port_num: {rx_bytes, tx_bytes, rx_pkts, tx_pkts, rx_drops, tx_drops, rx_errors}}
    """
    ports = {}

    try:
        output = subprocess.check_output(
            ["sudo", "/usr/bin/ovs-ofctl", "dump-ports", OVS_BRIDGE],
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).decode("utf-8")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return ports

    # Buscar bloques de puerto: "port  N: rx ... \n           tx ..."
    # Cada bloque tiene una línea rx y una línea tx
    port_pattern = re.compile(
        r"port\s+(\d+):\s*rx\s+pkts=(\d+),\s*bytes=(\d+),\s*drop=(\d+),\s*errs=(\d+)"
    )
    tx_pattern = re.compile(
        r"tx\s+pkts=(\d+),\s*bytes=(\d+),\s*drop=(\d+),\s*errs=(\d+)"
    )

    lines = output.splitlines()
    i = 0
    while i < len(lines):
        rx_match = port_pattern.search(lines[i])
        if rx_match:
            port_num = int(rx_match.group(1))

            if port_num in MONITORED_PORTS:
                rx_pkts = int(rx_match.group(2))
                rx_bytes = int(rx_match.group(3))
                rx_drops = int(rx_match.group(4))
                rx_errors = int(rx_match.group(5))

                tx_pkts = 0
                tx_bytes = 0
                tx_drops = 0

                # La línea tx está justo después
                if i + 1 < len(lines):
                    tx_match = tx_pattern.search(lines[i + 1])
                    if tx_match:
                        tx_pkts = int(tx_match.group(1))
                        tx_bytes = int(tx_match.group(2))
                        tx_drops = int(tx_match.group(3))
                        i += 1

                ports[port_num] = {
                    1: rx_bytes,
                    2: tx_bytes,
                    3: rx_pkts,
                    4: tx_pkts,
                    5: rx_drops,
                    6: tx_drops,
                    7: rx_errors,
                }
        i += 1

    return ports


def build_oid_map():
    """Construye el mapa OID → (tipo, valor) con datos actuales de todos los puertos."""
    oid_map = {}
    ports = parse_dump_ports()

    for port_num in sorted(MONITORED_PORTS):
        port_data = ports.get(port_num, {})
        for metric_id in sorted(METRIC_NAMES.keys()):
            oid = f"{BASE_OID}.{metric_id}.{port_num}"
            value = port_data.get(metric_id, 0)
            oid_map[oid] = ("counter64", str(value))

    return oid_map


def oid_sort_key(oid_str):
    """Ordena OIDs numéricamente por cada componente."""
    return [int(x) for x in oid_str.strip(".").split(".")]


def handle_get(oid):
    """Responde a una petición SNMP GET."""
    oid_map = build_oid_map()
    if oid in oid_map:
        snmp_type, value = oid_map[oid]
        print(oid)
        print(snmp_type)
        print(value)
    else:
        print("NONE")


def handle_getnext(oid):
    """Responde a una petición SNMP GETNEXT (para WALK)."""
    oid_map = build_oid_map()
    sorted_oids = sorted(oid_map.keys(), key=oid_sort_key)

    for candidate in sorted_oids:
        if oid_sort_key(candidate) > oid_sort_key(oid):
            snmp_type, value = oid_map[candidate]
            print(candidate)
            print(snmp_type)
            print(value)
            return

    print("NONE")


def main():
    sys.stdout = os.fdopen(sys.stdout.fileno(), "w", buffering=1)

    while True:
        line = sys.stdin.readline()
        if not line:
            break

        line = line.strip()

        if line == "PING":
            print("PONG")
        elif line == "get":
            oid = sys.stdin.readline().strip()
            handle_get(oid)
        elif line == "getnext":
            oid = sys.stdin.readline().strip()
            handle_getnext(oid)

        sys.stdout.flush()


if __name__ == "__main__":
    main()
