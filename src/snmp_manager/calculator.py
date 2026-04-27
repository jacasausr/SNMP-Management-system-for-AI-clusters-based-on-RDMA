"""
MetricsCalculator: calcula métricas derivadas a partir de lecturas crudas.

Módulo sin I/O — solo lógica de cálculo. Mantiene estado del poll
anterior para calcular deltas entre ciclos.
"""

import logging
import statistics

from models import (
    ClusterMetrics,
    SwitchPortDerivedMetrics,
    SwitchPortRawMetrics,
    WorkerDerivedMetrics,
    WorkerRawMetrics,
)

logger = logging.getLogger(__name__)


class MetricsCalculator:
    """Transforma métricas crudas en métricas derivadas."""

    def __init__(self):
        self._prev_workers: dict[str, WorkerRawMetrics] = {}
        self._prev_ports: dict[int, SwitchPortRawMetrics] = {}

    def calculate_worker(
        self,
        current: WorkerRawMetrics,
        ovs_port: SwitchPortRawMetrics | None,
    ) -> WorkerDerivedMetrics | None:
        """Calcula derivadas de un worker.

        Args:
            current: lectura cruda actual del worker.
            ovs_port: lectura cruda del puerto OVS correspondiente,
                      necesaria para calcular rdma_vs_ovs_ratio.

        Returns:
            WorkerDerivedMetrics o None si es el primer poll (sin delta).
        """
        prev = self._prev_workers.get(current.worker_id)
        self._prev_workers[current.worker_id] = current

        if prev is None:
            logger.info(
                "Primer poll de %s — almacenando base, sin derivadas",
                current.worker_id,
            )
            return None

        dt = current.timestamp - prev.timestamp
        if dt <= 0:
            return None

        # --- Derivadas de primer orden ---

        sent_pps = self._delta_rate(current.sent_pkts, prev.sent_pkts, dt)
        rcvd_pps = self._delta_rate(current.rcvd_pkts, prev.rcvd_pkts, dt)

        # Tasa de errores: todos los contadores de error RDMA disponibles
        _ERR_FIELDS = (
            "rcvd_seq_err", "retry_exceeded_err", "rcvd_rnr_err",
            "send_rnr_err", "completer_retry_err", "send_err", "retry_rnr_exceeded_err",
        )
        errors_now = sum(getattr(current, f) for f in _ERR_FIELDS)
        errors_prev = sum(getattr(prev, f) for f in _ERR_FIELDS)
        error_rate = self._delta_rate(errors_now, errors_prev, dt)

        # --- Ratios acumulativos ---

        retransmission_ratio = self._safe_ratio(
            current.retry_exceeded_err, current.sent_pkts
        )

        ecn_total = current.in_ce_pkts + current.in_noect_pkts
        ecn_ratio = self._safe_ratio(current.in_ce_pkts, ecn_total)

        # --- Cruce RDMA vs OVS ---

        rdma_vs_ovs_ratio = self._compute_rdma_vs_ovs(current, ovs_port)

        return WorkerDerivedMetrics(
            timestamp=current.timestamp,
            worker_id=current.worker_id,
            sent_pps=sent_pps,
            rcvd_pps=rcvd_pps,
            error_rate=error_rate,
            retransmission_ratio=retransmission_ratio,
            ecn_ratio=ecn_ratio,
            rdma_vs_ovs_ratio=rdma_vs_ovs_ratio,
        )

    def calculate_switch_port(
        self, current: SwitchPortRawMetrics
    ) -> SwitchPortDerivedMetrics | None:
        """Calcula derivadas de un puerto OVS."""
        prev = self._prev_ports.get(current.port_id)
        self._prev_ports[current.port_id] = current

        if prev is None:
            return None

        dt = current.timestamp - prev.timestamp
        if dt <= 0:
            return None

        total_bytes = (current.rx_bytes + current.tx_bytes) - (
            prev.rx_bytes + prev.tx_bytes
        )
        port_throughput_mbps = max(0.0, total_bytes * 8 / dt / 1e6)

        drops_now = current.rx_drops + current.tx_drops
        drops_prev = prev.rx_drops + prev.tx_drops
        port_drop_rate = self._delta_rate(drops_now, drops_prev, dt)

        return SwitchPortDerivedMetrics(
            timestamp=current.timestamp,
            port_id=current.port_id,
            connected_to=current.connected_to,
            port_throughput_mbps=port_throughput_mbps,
            port_drop_rate=port_drop_rate,
        )

    def calculate_cluster(
        self, workers: list[WorkerDerivedMetrics]
    ) -> ClusterMetrics | None:
        """Calcula métricas globales del cluster.

        Requiere al menos 2 workers con derivadas para que
        la asimetría tenga sentido.
        """
        if len(workers) < 2:
            return None

        rates = [w.sent_pps for w in workers]
        mean_rate = statistics.mean(rates)
        std_rate = statistics.stdev(rates) if len(rates) > 1 else 0.0

        asymmetry_index = std_rate / mean_rate if mean_rate > 0 else 0.0
        max_min_spread = max(rates) - min(rates)

        straggler = min(workers, key=lambda w: w.sent_pps)

        return ClusterMetrics(
            timestamp=workers[0].timestamp,
            asymmetry_index=round(asymmetry_index, 4),
            max_min_spread=round(max_min_spread, 4),
            straggler_id=straggler.worker_id,
            mean_pkt_rate=round(mean_rate, 4),
        )

    # --- Utilidades privadas ---

    @staticmethod
    def _delta_rate(
        current_val: int, prev_val: int, dt: float, scale: float = 1.0
    ) -> float:
        """Calcula (delta_valor / delta_tiempo) * scale.

        Protege contra counter wraps y valores negativos.
        """
        delta = current_val - prev_val
        if delta < 0:
            delta = 0  # counter wrap, ignorar este ciclo
        return round(delta * scale / dt, 4)

    @staticmethod
    def _safe_ratio(numerator: int, denominator: int) -> float:
        """División segura que devuelve 0.0 si el denominador es 0."""
        if denominator == 0:
            return 0.0
        return round(numerator / denominator, 6)

    @staticmethod
    def _compute_rdma_vs_ovs(
        worker: WorkerRawMetrics,
        ovs_port: SwitchPortRawMetrics | None,
    ) -> float:
        """Calcula el ratio tráfico_OVS / tráfico_RDMA.

        En condiciones normales ≈ 1.0 (todo el tráfico es RDMA).
        Si el worker genera tráfico no-RDMA (C2, minería), sube.
        Se calcula para TODOS los workers, no solo el sospechoso.
        """
        if ovs_port is None:
            return 0.0

        rdma_pkts = worker.sent_pkts + worker.rcvd_pkts
        ovs_pkts = ovs_port.rx_pkts + ovs_port.tx_pkts

        if ovs_pkts == 0:
            return 0.0

        # ratio ≈ 1.0 = todo el tráfico OVS es RDMA; < 1.0 = tráfico no-RDMA adicional
        return round(rdma_pkts / ovs_pkts, 4)
