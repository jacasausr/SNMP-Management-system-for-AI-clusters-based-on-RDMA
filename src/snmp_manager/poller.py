"""
SNMPPoller: abstrae pysnmp 7.x y devuelve data classes.

Usa la API nativa asyncio de pysnmp 7.x (v3arch.asyncio),
sin necesidad de thread pool.
"""

import asyncio
import logging
import time

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    get_cmd,
)

from config import (
    OVS_PORTS,
    SNMP_COMMUNITY,
    SNMP_RETRIES,
    SNMP_TIMEOUT,
    SWITCH_BASE_OID,
    SWITCH_IP,
    SWITCH_METRICS,
    WORKER_OIDS,
    WORKERS,
)
from models import PollCycle, SwitchPortRawMetrics, WorkerRawMetrics

logger = logging.getLogger(__name__)


class SNMPPoller:
    """Pollea agentes SNMP de workers y switch."""

    def __init__(self):
        self._engine = SnmpEngine()

    # --- API pública (async) ---

    async def poll_worker(self, worker_id: str) -> WorkerRawMetrics | None:
        """Pollea un worker. Devuelve None si hay timeout o error."""
        ip = WORKERS[worker_id]["ip"]
        oids = list(WORKER_OIDS.values())

        result = await self._snmp_get(ip, oids)

        if result is None:
            logger.warning("Worker %s (%s) no responde", worker_id, ip)
            return None

        return self._build_worker_metrics(worker_id, result)

    async def poll_switch(self) -> list[SwitchPortRawMetrics]:
        """Pollea el switch. Devuelve lista de métricas por puerto."""
        oids = []
        for metric_id in sorted(SWITCH_METRICS.keys()):
            for port_id in sorted(OVS_PORTS.keys()):
                oids.append(f"{SWITCH_BASE_OID}.{metric_id}.{port_id}")

        result = await self._snmp_get(SWITCH_IP, oids)

        if result is None:
            logger.warning("Switch (%s) no responde", SWITCH_IP)
            return []

        return self._build_switch_metrics(result)

    async def poll_all(self) -> PollCycle:
        """Pollea todas las entidades en paralelo con timestamp compartido."""
        ts = time.time()

        tasks = [self.poll_worker(wid) for wid in WORKERS]
        tasks.append(self.poll_switch())

        results = await asyncio.gather(*tasks, return_exceptions=True)

        workers = {}
        for wid, res in zip(WORKERS.keys(), results[:-1]):
            if isinstance(res, Exception):
                logger.error("Error polleando %s: %s", wid, res)
                workers[wid] = None
            else:
                workers[wid] = res

        switch_result = results[-1]
        if isinstance(switch_result, Exception):
            logger.error("Error polleando switch: %s", switch_result)
            switch_ports = []
        else:
            switch_ports = switch_result

        return PollCycle(timestamp=ts, workers=workers, switch_ports=switch_ports)

    # --- Internals (async nativo pysnmp 7.x) ---

    async def _snmp_get(self, ip: str, oids: list[str]) -> dict[str, int] | None:
        """SNMP GET asíncrono para múltiples OIDs."""
        object_types = [ObjectType(ObjectIdentity(oid)) for oid in oids]

        try:
            error_indication, error_status, error_index, var_binds = await get_cmd(
                self._engine,
                CommunityData(SNMP_COMMUNITY),
                await UdpTransportTarget.create(
                    (ip, 161),
                    timeout=SNMP_TIMEOUT,
                    retries=SNMP_RETRIES,
                ),
                ContextData(),
                *object_types,
            )
        except Exception as e:
            logger.error("Excepción SNMP hacia %s: %s", ip, e)
            return None

        if error_indication:
            logger.warning("SNMP error (indication) desde %s: %s", ip, error_indication)
            return None

        if error_status:
            logger.warning(
                "SNMP error (status) desde %s: %s at %s",
                ip,
                error_status.prettyPrint(),
                var_binds[int(error_index) - 1][0] if error_status else "?",
            )
            return None

        return {str(oid): int(val) for oid, val in var_binds}

    # --- Constructores de data classes ---

    def _build_worker_metrics(
        self, worker_id: str, data: dict[str, int]
    ) -> WorkerRawMetrics:
        """Construye WorkerRawMetrics a partir de la respuesta SNMP."""
        ts = time.time()

        def val(field_name: str) -> int:
            oid = WORKER_OIDS[field_name]
            return data.get(oid, 0)

        return WorkerRawMetrics(
            timestamp=ts,
            worker_id=worker_id,
            sent_pkts=val("sent_pkts"),
            rcvd_pkts=val("rcvd_pkts"),
            rdma_sends=val("rdma_sends"),
            rdma_recvs=val("rdma_recvs"),
            rcvd_seq_err=val("rcvd_seq_err"),
            retry_exceeded_err=val("retry_exceeded_err"),
            rcvd_rnr_err=val("rcvd_rnr_err"),
            send_rnr_err=val("send_rnr_err"),
            duplicate_request=val("duplicate_request"),
            out_of_seq_request=val("out_of_seq_request"),
            completer_retry_err=val("completer_retry_err"),
            ack_deferred=val("ack_deferred"),
            send_err=val("send_err"),
            retry_rnr_exceeded_err=val("retry_rnr_exceeded_err"),
            link_downed=val("link_downed"),
            lifespan=val("lifespan"),
            in_ce_pkts=val("in_ce_pkts"),
            in_ect0_pkts=val("in_ect0_pkts"),
            in_ect1_pkts=val("in_ect1_pkts"),
            in_noect_pkts=val("in_noect_pkts"),
        )

    def _build_switch_metrics(
        self, data: dict[str, int]
    ) -> list[SwitchPortRawMetrics]:
        """Construye lista de SwitchPortRawMetrics desde la respuesta SNMP."""
        ts = time.time()
        ports = []

        for port_id in sorted(OVS_PORTS.keys()):
            connected_to = OVS_PORTS[port_id]

            def val(metric_id: int, pid=port_id) -> int:
                oid = f"{SWITCH_BASE_OID}.{metric_id}.{pid}"
                return data.get(oid, 0)

            ports.append(
                SwitchPortRawMetrics(
                    timestamp=ts,
                    port_id=port_id,
                    connected_to=connected_to,
                    rx_bytes=val(1),
                    tx_bytes=val(2),
                    rx_pkts=val(3),
                    tx_pkts=val(4),
                    rx_drops=val(5),
                    tx_drops=val(6),
                    rx_errors=val(7),
                )
            )

        return ports
