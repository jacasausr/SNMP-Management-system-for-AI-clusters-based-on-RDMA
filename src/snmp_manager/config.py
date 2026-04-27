"""
Configuración del gestor SNMP.

Centraliza IPs, OIDs, intervalos y conexión a InfluxDB.
"""

# --- Topología ---

WORKERS = {
    "vm1": {"ip": "10.10.0.1", "ovs_port": 1},
    "vm2": {"ip": "10.10.0.2", "ovs_port": 2},
    "vm3": {"ip": "10.10.0.3", "ovs_port": 3},
}

SWITCH_IP = "10.10.0.10"

OVS_PORTS = {
    1: "vm1",
    2: "vm2",
    3: "vm3",
    4: "host",
}

# --- SNMP ---

SNMP_COMMUNITY = "public"
SNMP_TIMEOUT = 2        # segundos
SNMP_RETRIES = 1
POLL_INTERVAL = 5        # segundos entre polls

# --- OIDs del worker (bajo .1.3.6.1.4.1.99999) ---

BASE_OID = "1.3.6.1.4.1.99999"

WORKER_OIDS = {
    "sent_pkts":               f"{BASE_OID}.1.1.0",
    "rcvd_pkts":               f"{BASE_OID}.1.2.0",
    "rdma_sends":              f"{BASE_OID}.1.3.0",
    "rdma_recvs":              f"{BASE_OID}.1.4.0",
    "rcvd_seq_err":            f"{BASE_OID}.1.5.0",
    "retry_exceeded_err":      f"{BASE_OID}.1.6.0",
    "rcvd_rnr_err":            f"{BASE_OID}.1.7.0",
    "send_rnr_err":            f"{BASE_OID}.1.8.0",
    "duplicate_request":       f"{BASE_OID}.1.9.0",
    "out_of_seq_request":      f"{BASE_OID}.1.10.0",
    "completer_retry_err":     f"{BASE_OID}.1.11.0",
    "ack_deferred":            f"{BASE_OID}.1.12.0",
    "send_err":                f"{BASE_OID}.1.13.0",
    "retry_rnr_exceeded_err":  f"{BASE_OID}.1.14.0",
    "link_downed":             f"{BASE_OID}.1.15.0",
    "lifespan":                f"{BASE_OID}.1.16.0",
    "in_ce_pkts":              f"{BASE_OID}.2.1.0",
    "in_ect0_pkts":            f"{BASE_OID}.2.2.0",
    "in_ect1_pkts":            f"{BASE_OID}.2.3.0",
    "in_noect_pkts":           f"{BASE_OID}.2.4.0",
}

# --- OIDs del switch (bajo .1.3.6.1.4.1.99999.3) ---

SWITCH_BASE_OID = f"{BASE_OID}.3"

# Métricas por puerto: OID = .3.METRIC.PORT
SWITCH_METRICS = {
    1: "rx_bytes",
    2: "tx_bytes",
    3: "rx_pkts",
    4: "tx_pkts",
    5: "rx_drops",
    6: "tx_drops",
    7: "rx_errors",
}

# --- InfluxDB ---

INFLUXDB_HOST = "localhost"
INFLUXDB_PORT = 8086
INFLUXDB_DATABASE = "roce_cluster"
INFLUXDB_RETENTION_POLICY = "one_day"
INFLUXDB_RETENTION_DURATION = "24h"
