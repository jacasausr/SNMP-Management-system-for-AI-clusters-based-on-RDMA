"""
Manager: orquesta el ciclo poll → calculate → write.

Coordina SNMPPoller, MetricsCalculator e InfluxDBWriter
en un bucle asyncio periódico.
"""

import asyncio
import logging
import time

from config import POLL_INTERVAL, WORKERS
from calculator import MetricsCalculator
from models import SwitchPortRawMetrics
from poller import SNMPPoller
from writer import InfluxDBWriter

logger = logging.getLogger(__name__)


class Manager:
    """Gestor principal del pipeline de métricas."""

    def __init__(
        self,
        poller: SNMPPoller,
        calculator: MetricsCalculator,
        writer: InfluxDBWriter,
        interval: int = POLL_INTERVAL,
    ):
        self._poller = poller
        self._calculator = calculator
        self._writer = writer
        self._interval = interval
        self._running = False

    async def run(self) -> None:
        """Bucle principal: poll → calculate → write."""
        self._running = True
        cycle_count = 0

        logger.info(
            "Manager iniciado — polling cada %ds a %d workers + switch",
            self._interval,
            len(WORKERS),
        )

        while self._running:
            cycle_start = time.time()
            cycle_count += 1

            try:
                await self._execute_cycle(cycle_count)
            except Exception as e:
                logger.error("Error en ciclo %d: %s", cycle_count, e)

            elapsed = time.time() - cycle_start
            sleep_time = max(0, self._interval - elapsed)

            if elapsed > self._interval:
                logger.warning(
                    "Ciclo %d tardó %.1fs (> %ds intervalo)",
                    cycle_count,
                    elapsed,
                    self._interval,
                )

            await asyncio.sleep(sleep_time)

    def stop(self) -> None:
        """Señala al bucle que debe terminar."""
        self._running = False
        logger.info("Manager detenido")

    async def _execute_cycle(self, cycle_count: int) -> None:
        """Ejecuta un ciclo completo de polling, cálculo y escritura."""

        # 1. Poll todas las entidades en paralelo
        poll = await self._poller.poll_all()

        workers_ok = sum(1 for w in poll.workers.values() if w is not None)
        logger.info(
            "Ciclo %d — workers: %d/%d, puertos OVS: %d",
            cycle_count,
            workers_ok,
            len(WORKERS),
            len(poll.switch_ports),
        )

        # Indexar puertos OVS por worker para cruce de datos
        ovs_by_worker = self._index_ovs_ports(poll.switch_ports)

        # 2. Procesar cada worker
        worker_derived_list = []

        for worker_id, w_raw in poll.workers.items():
            if w_raw is None:
                self._writer.write_worker_unreachable(poll.timestamp, worker_id)
                continue

            # Escribir métricas crudas
            self._writer.write_worker_raw(w_raw)

            # Calcular derivadas (cruzando con puerto OVS)
            ovs_port = ovs_by_worker.get(worker_id)
            w_derived = self._calculator.calculate_worker(w_raw, ovs_port)

            if w_derived is not None:
                self._writer.write_worker_derived(w_derived)
                worker_derived_list.append(w_derived)

                logger.debug(
                    "  %s: TX=%.0f pps, RX=%.0f pps, errors=%.1f/s, "
                    "OVS_ratio=%.2f",
                    worker_id,
                    w_derived.sent_pps,
                    w_derived.rcvd_pps,
                    w_derived.error_rate,
                    w_derived.rdma_vs_ovs_ratio,
                )

        # 3. Procesar puertos del switch
        for port_raw in poll.switch_ports:
            self._writer.write_switch_port_raw(port_raw)

            port_derived = self._calculator.calculate_switch_port(port_raw)
            if port_derived is not None:
                self._writer.write_switch_port_derived(port_derived)

        # 4. Métricas cluster-wide
        if len(worker_derived_list) >= 2:
            cluster = self._calculator.calculate_cluster(worker_derived_list)
            if cluster is not None:
                self._writer.write_cluster(cluster)

                logger.info(
                    "  Cluster: asymmetry=%.4f, spread=%.0f pps, "
                    "straggler=%s, mean=%.0f pps",
                    cluster.asymmetry_index,
                    cluster.max_min_spread,
                    cluster.straggler_id,
                    cluster.mean_pkt_rate,
                )

    @staticmethod
    def _index_ovs_ports(
        ports: list[SwitchPortRawMetrics],
    ) -> dict[str, SwitchPortRawMetrics]:
        """Indexa puertos OVS por el worker al que están conectados."""
        return {
            p.connected_to: p
            for p in ports
            if p.connected_to in WORKERS
        }
