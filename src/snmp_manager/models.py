"""
Data Transfer Objects para métricas del cluster.

Cada clase es un contenedor inmutable (frozen) que fluye entre capas
sin lógica de negocio. Equivalente a POJOs en Java.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerRawMetrics:
    """Lectura cruda de un worker vía SNMP (un ciclo de poll)."""

    timestamp: float
    worker_id: str

    # hw_counters — contadores RDMA reales (counters/ no existe en Soft-RoCE)
    sent_pkts: int
    rcvd_pkts: int
    rdma_sends: int
    rdma_recvs: int
    rcvd_seq_err: int
    retry_exceeded_err: int
    rcvd_rnr_err: int
    send_rnr_err: int
    duplicate_request: int
    out_of_seq_request: int
    completer_retry_err: int
    ack_deferred: int
    send_err: int
    retry_rnr_exceeded_err: int
    link_downed: int
    lifespan: int

    # roceEcnTable — contadores ECN
    in_ce_pkts: int
    in_ect0_pkts: int
    in_ect1_pkts: int
    in_noect_pkts: int


@dataclass(frozen=True)
class SwitchPortRawMetrics:
    """Lectura cruda de un puerto OVS vía SNMP."""

    timestamp: float
    port_id: int
    connected_to: str

    rx_bytes: int
    tx_bytes: int
    rx_pkts: int
    tx_pkts: int
    rx_drops: int
    tx_drops: int
    rx_errors: int


@dataclass(frozen=True)
class WorkerDerivedMetrics:
    """Métricas calculadas para un worker (requiere poll actual + anterior)."""

    timestamp: float
    worker_id: str

    sent_pps: float
    rcvd_pps: float
    error_rate: float
    retransmission_ratio: float
    ecn_ratio: float
    rdma_vs_ovs_ratio: float


@dataclass(frozen=True)
class SwitchPortDerivedMetrics:
    """Métricas calculadas para un puerto OVS."""

    timestamp: float
    port_id: int
    connected_to: str

    port_throughput_mbps: float
    port_drop_rate: float


@dataclass(frozen=True)
class ClusterMetrics:
    """Métricas de estado global del cluster."""

    timestamp: float
    asymmetry_index: float
    max_min_spread: float
    straggler_id: str
    mean_pkt_rate: float


@dataclass
class PollCycle:
    """Resultado completo de un ciclo de polling.

    No es frozen porque se construye progresivamente durante el poll.
    """

    timestamp: float
    workers: dict[str, WorkerRawMetrics | None]
    switch_ports: list[SwitchPortRawMetrics]
