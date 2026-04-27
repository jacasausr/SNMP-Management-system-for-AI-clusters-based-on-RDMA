"""
InfluxDBWriter: patrón Repository sobre InfluxDB 1.x.

Recibe data classes del dominio y las transforma en puntos
de InfluxDB. Abstrae completamente la capa de persistencia.
"""

import logging
from datetime import datetime

from influxdb import InfluxDBClient

from config import (
    INFLUXDB_DATABASE,
    INFLUXDB_HOST,
    INFLUXDB_PORT,
    INFLUXDB_RETENTION_DURATION,
    INFLUXDB_RETENTION_POLICY,
)
from models import (
    ClusterMetrics,
    SwitchPortDerivedMetrics,
    SwitchPortRawMetrics,
    WorkerDerivedMetrics,
    WorkerRawMetrics,
)

logger = logging.getLogger(__name__)


class InfluxDBWriter:
    """Escribe métricas del cluster en InfluxDB."""

    def __init__(self):
        self._client: InfluxDBClient | None = None

    def connect(self) -> None:
        """Conecta a InfluxDB y crea la database/retention si no existen."""
        self._client = InfluxDBClient(
            host=INFLUXDB_HOST,
            port=INFLUXDB_PORT,
            database=INFLUXDB_DATABASE,
        )

        databases = [db["name"] for db in self._client.get_list_database()]
        if INFLUXDB_DATABASE not in databases:
            logger.info("Creando database '%s'", INFLUXDB_DATABASE)
            self._client.create_database(INFLUXDB_DATABASE)

        self._client.create_retention_policy(
            name=INFLUXDB_RETENTION_POLICY,
            duration=INFLUXDB_RETENTION_DURATION,
            replication="1",
            database=INFLUXDB_DATABASE,
            default=True,
        )

        logger.info(
            "Conectado a InfluxDB en %s:%s, database='%s'",
            INFLUXDB_HOST,
            INFLUXDB_PORT,
            INFLUXDB_DATABASE,
        )

    def close(self) -> None:
        """Cierra la conexión."""
        if self._client:
            self._client.close()
            logger.info("Conexión a InfluxDB cerrada")

    def write_worker_raw(self, metrics: WorkerRawMetrics) -> None:
        """Escribe contadores crudos de un worker."""
        point = {
            "measurement": "roce_worker_raw",
            "tags": {"worker": metrics.worker_id},
            "time": self._ts(metrics.timestamp),
            "fields": {
                "sent_pkts": metrics.sent_pkts,
                "rcvd_pkts": metrics.rcvd_pkts,
                "rdma_sends": metrics.rdma_sends,
                "rdma_recvs": metrics.rdma_recvs,
                "rcvd_seq_err": metrics.rcvd_seq_err,
                "retry_exceeded_err": metrics.retry_exceeded_err,
                "rcvd_rnr_err": metrics.rcvd_rnr_err,
                "send_rnr_err": metrics.send_rnr_err,
                "duplicate_request": metrics.duplicate_request,
                "out_of_seq_request": metrics.out_of_seq_request,
                "completer_retry_err": metrics.completer_retry_err,
                "ack_deferred": metrics.ack_deferred,
                "send_err": metrics.send_err,
                "retry_rnr_exceeded_err": metrics.retry_rnr_exceeded_err,
                "link_downed": metrics.link_downed,
                "lifespan": metrics.lifespan,
                "in_ce_pkts": metrics.in_ce_pkts,
                "in_ect0_pkts": metrics.in_ect0_pkts,
                "in_ect1_pkts": metrics.in_ect1_pkts,
                "in_noect_pkts": metrics.in_noect_pkts,
            },
        }
        self._write([point])

    def write_worker_derived(self, metrics: WorkerDerivedMetrics) -> None:
        """Escribe métricas derivadas de un worker."""
        point = {
            "measurement": "roce_worker",
            "tags": {"worker": metrics.worker_id},
            "time": self._ts(metrics.timestamp),
            "fields": {
                "sent_pps": metrics.sent_pps,
                "rcvd_pps": metrics.rcvd_pps,
                "error_rate": metrics.error_rate,
                "retransmission_ratio": metrics.retransmission_ratio,
                "ecn_ratio": metrics.ecn_ratio,
                "rdma_vs_ovs_ratio": metrics.rdma_vs_ovs_ratio,
            },
        }
        self._write([point])

    def write_switch_port_raw(self, metrics: SwitchPortRawMetrics) -> None:
        """Escribe contadores crudos de un puerto OVS."""
        point = {
            "measurement": "ovs_port_raw",
            "tags": {
                "port": str(metrics.port_id),
                "connected_to": metrics.connected_to,
            },
            "time": self._ts(metrics.timestamp),
            "fields": {
                "rx_bytes": metrics.rx_bytes,
                "tx_bytes": metrics.tx_bytes,
                "rx_pkts": metrics.rx_pkts,
                "tx_pkts": metrics.tx_pkts,
                "rx_drops": metrics.rx_drops,
                "tx_drops": metrics.tx_drops,
                "rx_errors": metrics.rx_errors,
            },
        }
        self._write([point])

    def write_switch_port_derived(
        self, metrics: SwitchPortDerivedMetrics
    ) -> None:
        """Escribe métricas derivadas de un puerto OVS."""
        point = {
            "measurement": "ovs_port",
            "tags": {
                "port": str(metrics.port_id),
                "connected_to": metrics.connected_to,
            },
            "time": self._ts(metrics.timestamp),
            "fields": {
                "port_throughput_mbps": metrics.port_throughput_mbps,
                "port_drop_rate": metrics.port_drop_rate,
            },
        }
        self._write([point])

    def write_cluster(self, metrics: ClusterMetrics) -> None:
        """Escribe métricas globales del cluster."""
        point = {
            "measurement": "cluster",
            "time": self._ts(metrics.timestamp),
            "fields": {
                "asymmetry_index": metrics.asymmetry_index,
                "max_min_spread": metrics.max_min_spread,
                "straggler_id": metrics.straggler_id,
                "mean_pkt_rate": metrics.mean_pkt_rate,
            },
        }
        self._write([point])

    def write_worker_unreachable(self, timestamp: float, worker_id: str) -> None:
        """Registra que un worker no respondió al poll."""
        point = {
            "measurement": "roce_worker",
            "tags": {"worker": worker_id},
            "time": self._ts(timestamp),
            "fields": {
                "unreachable": True,
                "sent_pps": 0.0,
                "rcvd_pps": 0.0,
            },
        }
        self._write([point])

    # --- Internals ---

    def _write(self, points: list[dict]) -> None:
        """Escribe puntos a InfluxDB con manejo de errores."""
        if not self._client:
            logger.error("InfluxDB no conectado")
            return

        try:
            self._client.write_points(points)
        except Exception as e:
            logger.error("Error escribiendo a InfluxDB: %s", e)

    @staticmethod
    def _ts(epoch: float) -> str:
        """Convierte epoch float a ISO 8601 para InfluxDB."""
        return datetime.utcfromtimestamp(epoch).strftime("%Y-%m-%dT%H:%M:%SZ")
