#!/usr/bin/env python3
"""
Agente SNMP pass_persist para workers Soft-RoCE.

Expone dos tablas bajo .1.3.6.1.4.1.99999:
  .1.3.6.1.4.1.99999.1.X.0  -> rocePortTable  (contadores RDMA)
  .1.3.6.1.4.1.99999.2.X.0  -> roceEcnTable   (contadores ECN)

Instalación:
  1. Copiar a /usr/local/bin/roce_agent.py
  2. chmod +x /usr/local/bin/roce_agent.py
  3. Añadir a /etc/snmp/snmpd.conf:
       pass_persist .1.3.6.1.4.1.99999 /usr/local/bin/roce_agent.py
  4. sudo systemctl restart snmpd
"""

import sys
import os

BASE_OID = ".1.3.6.1.4.1.99999"
RXE_DEVICE = "rxe0"
RXE_PORT = "1"

# --- Definición de OIDs ---
# rocePortTable: .1.3.6.1.4.1.99999.1.X.0
# Contadores reales de hw_counters (el directorio counters/ no existe en Soft-RoCE)
HW_COUNTERS_DIR = f"/sys/class/infiniband/{RXE_DEVICE}/ports/{RXE_PORT}/hw_counters"

ROCE_PORT_TABLE = [
    # (sub-oid, tipo_snmp, fichero, directorio)
    ("1.1.0",  "counter64", "sent_pkts",              HW_COUNTERS_DIR),
    ("1.2.0",  "counter64", "rcvd_pkts",              HW_COUNTERS_DIR),
    ("1.3.0",  "counter64", "rdma_sends",             HW_COUNTERS_DIR),
    ("1.4.0",  "counter64", "rdma_recvs",             HW_COUNTERS_DIR),
    ("1.5.0",  "counter64", "rcvd_seq_err",           HW_COUNTERS_DIR),
    ("1.6.0",  "counter64", "retry_exceeded_err",     HW_COUNTERS_DIR),
    ("1.7.0",  "counter64", "rcvd_rnr_err",           HW_COUNTERS_DIR),
    ("1.8.0",  "counter64", "send_rnr_err",           HW_COUNTERS_DIR),
    ("1.9.0",  "counter64", "duplicate_request",      HW_COUNTERS_DIR),
    ("1.10.0", "counter64", "out_of_seq_request",     HW_COUNTERS_DIR),
    ("1.11.0", "counter64", "completer_retry_err",    HW_COUNTERS_DIR),
    ("1.12.0", "counter64", "ack_deferred",           HW_COUNTERS_DIR),
    ("1.13.0", "counter64", "send_err",               HW_COUNTERS_DIR),
    ("1.14.0", "counter64", "retry_rnr_exceeded_err", HW_COUNTERS_DIR),
    ("1.15.0", "counter64", "link_downed",            HW_COUNTERS_DIR),
    ("1.16.0", "integer",   "lifespan",               HW_COUNTERS_DIR),
]

# roceEcnTable: .1.3.6.1.4.1.99999.2.X.0
ECN_FIELDS = [
    ("2.1.0", "InCEPkts"),
    ("2.2.0", "InECT0Pkts"),
    ("2.3.0", "InECT1Pkts"),
    ("2.4.0", "InNoECTPkts"),
]


def read_sysfs(directory, filename):
    """Lee un valor entero de un fichero sysfs."""
    path = os.path.join(directory, filename)
    try:
        with open(path, "r") as f:
            return int(f.read().strip())
    except (IOError, ValueError):
        return 0


def read_ecn_counters():
    """Lee contadores ECN de /proc/net/netstat (línea IpExt)."""
    result = {}
    try:
        with open("/proc/net/netstat", "r") as f:
            lines = f.readlines()

        keys = None
        vals = None
        for i, line in enumerate(lines):
            if line.startswith("IpExt:") and keys is None:
                keys = line.strip().split()
                if i + 1 < len(lines):
                    vals = lines[i + 1].strip().split()
                break

        if keys and vals and len(keys) == len(vals):
            field_map = dict(zip(keys, vals))
            for _, field_name in ECN_FIELDS:
                result[field_name] = int(field_map.get(field_name, "0"))
    except (IOError, ValueError):
        pass

    return result


def build_oid_map():
    """Construye el mapa OID → (tipo, valor) con datos actuales."""
    oid_map = {}

    # rocePortTable
    for sub_oid, snmp_type, filename, directory in ROCE_PORT_TABLE:
        full_oid = f"{BASE_OID}.{sub_oid}"
        value = read_sysfs(directory, filename)
        oid_map[full_oid] = (snmp_type, str(value))

    # roceEcnTable
    ecn_data = read_ecn_counters()
    for sub_oid, field_name in ECN_FIELDS:
        full_oid = f"{BASE_OID}.{sub_oid}"
        value = ecn_data.get(field_name, 0)
        oid_map[full_oid] = ("counter64", str(value))

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
    # Forzar line-buffered para que snmpd reciba las respuestas inmediatamente
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
