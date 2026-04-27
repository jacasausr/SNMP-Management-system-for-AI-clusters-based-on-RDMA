# Decisiones de arquitectura — Sistema de gestión RoCEv2

## Resumen ejecutivo

Sistema de gestión de red para un cluster simulado de entrenamiento IA sobre RoCEv2 (Soft-RoCE), con dos componentes: monitorización de métricas vía SNMP/InfluxDB/Grafana, y detección/respuesta a intrusiones vía Snort/Telegram.

---

## Topología

| Entidad | Implementación | IP (propuesta) |
|---------|---------------|----------------|
| Worker 1 (legítimo) | VM QEMU con Soft-RoCE | 10.10.0.1 |
| Worker 2 (legítimo) | VM QEMU con Soft-RoCE | 10.10.0.2 |
| Worker 3 (hackeado) | VM QEMU con Soft-RoCE | 10.10.0.3 |
| Switch | VM QEMU con Open vSwitch | 10.10.0.10 |
| Host gestión | WSL nativo (no VM) | 10.10.0.254 (bridge) |

**Switch: Open vSwitch (OVS)** en VM dedicada. Justificación:
- Port mirroring nativo: `ovs-vsctl -- set Bridge ... mirrors=...`
- Contadores por puerto: `ovs-ofctl dump-ports`
- Desconexión programática de puertos: `ovs-ofctl mod-port <bridge> <port> down`

**Cambio respecto al estado actual**: se pasa de 2 VMs + bridge Linux a 3 VMs + 1 VM-switch. El bridge Linux del host solo sirve para conectar los TAPs de QEMU; el switching L2 real lo hace OVS dentro de su VM.

---

## Componente 1 — Monitorización de métricas

### Métricas seleccionadas (solo pasivas)

| Métrica | Fuente | Entidades | Método de obtención |
|---------|--------|-----------|-------------------|
| Throughput (bytes TX/RX) | `/sys/class/infiniband/rxe0/ports/1/counters/port_xmit_data`, `port_rcv_data` | Workers | Polling + delta/tiempo |
| Tasa de paquetes | `port_xmit_packets`, `port_rcv_packets` | Workers | Polling + delta/tiempo |
| Errores de secuencia | `hw_counters/rcvd_seq_err` | Workers | Polling |
| Retransmisiones agotadas | `hw_counters/retry_exceeded_err` | Workers | Polling |
| Receiver Not Ready | `hw_counters/rcvd_rnr_err`, `send_rnr_err` | Workers | Polling |
| Requests duplicadas | `hw_counters/duplicate_request` | Workers | Polling |
| Requests fuera de secuencia | `hw_counters/out_of_seq_request` | Workers | Polling |
| Paquetes ECN (CE, ECT0, ECT1) | `/proc/net/netstat` línea `IpExt` | Workers | Polling |
| Contadores OVS por puerto | `ovs-ofctl dump-ports` | Switch | Polling |

**Decisión**: no se incluyen métricas activas (latencia, jitter). Si sobra tiempo, se puede añadir un wrapper sobre `ib_write_lat`.

**Métricas no disponibles (documentadas pero no pobladas)**: PFC por prioridad (802.1Qbb) y CNP (DCQCN) — Soft-RoCE no los implementa.

### Agentes SNMP

**Implementación**: `snmpd` (ya instalado en imagen base) + extensión `pass_persist` con script Python.

Cada entidad (3 workers + 1 switch) corre `snmpd` con un script Python registrado bajo un subárbol OID privado. El script lee contadores del filesystem (`/sys/class/infiniband/...`, `/proc/net/netstat`, o `ovs-ofctl`) y los expone vía el protocolo stdin/stdout de `pass_persist`.

Configuración en `/etc/snmp/snmpd.conf`:
```
pass_persist .1.3.6.1.4.1.99999 /usr/local/bin/roce_agent.py
```

### MIB

**Decisión**: MIB propia, inspirada en la estructura de MELLANOX-QOS-MIB pero adaptada a lo que Soft-RoCE + OVS pueden poblar realmente.

**OID raíz**: `.1.3.6.1.4.1.99999` (enterprise experimental)

**Tablas propuestas**:

1. **rocePortTable** — contadores RDMA por puerto (en workers):
   - `rocePortXmitData` (Counter64)
   - `rocePortRcvData` (Counter64)
   - `rocePortXmitPkts` (Counter64)
   - `rocePortRcvPkts` (Counter64)
   - `rocePortSeqErr` (Counter64)
   - `rocePortRetryExceeded` (Counter64)
   - `rocePortRnrErr` (Counter64)
   - `rocePortDuplicateReq` (Counter64)
   - `rocePortOutOfSeqReq` (Counter64)

2. **roceEcnTable** — contadores ECN del stack IP (en workers):
   - `roceEcnInCEPkts` (Counter64)
   - `roceEcnInECT0Pkts` (Counter64)
   - `roceEcnInECT1Pkts` (Counter64)
   - `roceEcnInNoECTPkts` (Counter64)

3. **ovsPortTable** — contadores OVS por puerto (en switch):
   - `ovsPortRxBytes` (Counter64)
   - `ovsPortTxBytes` (Counter64)
   - `ovsPortRxPkts` (Counter64)
   - `ovsPortTxPkts` (Counter64)
   - `ovsPortRxDrops` (Counter64)
   - `ovsPortTxDrops` (Counter64)
   - `ovsPortRxErrors` (Counter64)

**Referencia**: Las MIBs de Mellanox descargadas (MELLANOX-QOS-MIB, MELLANOX-MIB-4) se usan como referencia de diseño pero no se implementan directamente por:
- Dependencia rota (falta MELLANOX-SMI-MIB)
- >60% de OIDs no poblables en Soft-RoCE
- OID enterprise ajeno (33049 = Mellanox/NVIDIA)

### Gestor SNMP

**Ubicación**: WSL nativo (Python).

**Funcionalidad**:
- Polling periódico (cada N segundos) a las 4 entidades vía SNMP GET/WALK con `pysnmp`
- Cálculo de derivadas: throughput = delta_bytes / delta_tiempo
- Escritura a InfluxDB con `influxdb-client`
- Recepción de SNMP Traps (alertas de Snort) → reenvío a Telegram

### Pipeline de visualización

| Componente | Ubicación | Notas |
|-----------|-----------|-------|
| Gestor SNMP (Python) | WSL nativo | pysnmp + influxdb-client |
| InfluxDB | WSL nativo | Base de datos de series temporales |
| Grafana | WSL nativo | Dashboards accesibles desde navegador Windows |

**Dashboards propuestos**:
- Panel de throughput por worker (líneas temporales)
- Panel de errores/retransmisiones como indicadores de congestión
- Panel de contadores ECN
- Panel de estado de puertos OVS (bytes, drops por puerto)

---

## Componente 2 — Detección y respuesta a intrusiones

### Emulación del ataque (Worker 3)

**Dos fases**:

1. **Fase C2 (Command & Control)**: script que hace conexiones periódicas (beaconing) a un servidor falso — simula comunicación con un C2 server. Patrón: HTTP/HTTPS periódico cada N segundos al mismo destino.

2. **Fase criptominería**: tráfico hacia puertos conocidos de mining pools (3333, 4444, 8333) simulando protocolo Stratum.

### Snort IDS

**Ubicación**: dentro de la VM-switch (OVS).

**Port mirroring**: OVS duplica el tráfico del puerto del Worker 3 hacia una interfaz interna donde Snort escucha en modo promiscuo.

**Reglas**: ruleset de Emerging Threats para criptominería + reglas custom para detectar beaconing C2.

### Flujo de alertas

```
Snort (VM-switch) → alert log → script Python watcher → SNMP Trap → Gestor (WSL) → Bot Telegram
```

1. Snort detecta patrón → escribe en log de alertas
2. Script Python en la VM-switch monitoriza el log
3. Script envía SNMP Trap al gestor en WSL
4. Gestor procesa el trap y envía mensaje a Telegram:
   - Fase C2: "⚠️ Beaconing detectado en Worker 3"
   - Fase minería: "🚨 Criptominería detectada en Worker 3" + botón "Desconectar"

### Desconexión del worker

Cuando el usuario pulsa el botón en Telegram:
1. Bot Telegram notifica al gestor
2. Gestor hace SSH a la VM-switch
3. Ejecuta: `ovs-ofctl mod-port <bridge> <puerto_worker3> down`
4. Confirma por Telegram: "✅ Worker 3 desconectado"

---

## Consideraciones de recursos

- **RAM total**: 4 VMs × 2 GB = 8 GB + WSL overhead ≈ 10-11 GB necesarios
- **RAM recomendada host**: 16 GB (según README actual)
- **Disco**: ~15 GB para imágenes base + deltas
- **Red**: bridge Linux en WSL con 4 TAPs (uno por VM), OVS dentro de la VM-switch hace el switching L2 real

---

## Orden de implementación sugerido

1. **Topología**: crear VM-switch con OVS, añadir Worker 3, verificar conectividad RDMA entre los 3 workers
2. **Agentes SNMP**: implementar script `pass_persist` para workers, luego para switch OVS
3. **MIB**: definir MIB propia en formato ASN.1
4. **Gestor SNMP**: polling + escritura a InfluxDB
5. **Grafana**: dashboards de métricas
6. **Emulación ataque**: scripts de C2 + minería en Worker 3
7. **Snort**: reglas + port mirroring en OVS
8. **Alertas**: watcher → SNMP Trap → Telegram
9. **Desconexión**: bot Telegram → SSH → OVS port down
