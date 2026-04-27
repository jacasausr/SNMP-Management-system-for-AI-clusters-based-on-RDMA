# Guía de la Componente 1: Monitorización SNMP del Cluster RoCEv2

## Índice

1. Visión general
2. Agentes SNMP
3. Controlador de tráfico
4. Gestor SNMP (monitor)
5. InfluxDB
6. Métricas del sistema
7. Despliegue y operación
8. Notas técnicas relevantes

---

## 1. Visión general

Esta componente implementa el pipeline de monitorización del cluster: recolectar contadores de rendimiento de los workers y el switch, calcularles métricas derivadas, y almacenarlas en InfluxDB para su posterior visualización en Grafana.

El flujo de datos es unidireccional:

```
Workers (agente SNMP) ──┐
                        ├──→ Gestor SNMP (WSL) ──→ InfluxDB ──→ Grafana
Switch  (agente SNMP) ──┘
```

El gestor pollea las 4 entidades cada 5 segundos, calcula métricas derivadas (tasas, ratios, asimetrías) y escribe tanto los contadores crudos como las derivadas a InfluxDB. En paralelo, un controlador de tráfico en cada worker genera tráfico RDMA que simula entrenamiento distribuido.

---

## 2. Agentes SNMP

### Qué hacen

Cada entidad del cluster (3 workers + 1 switch) ejecuta `snmpd` con una extensión `pass_persist` que expone contadores específicos bajo el subárbol OID `.1.3.6.1.4.1.99999`.

El protocolo `pass_persist` funciona así: `snmpd` lanza el script Python como subproceso persistente. Cuando llega una petición SNMP GET o GETNEXT al subárbol configurado, `snmpd` escribe el comando y el OID por stdin del script. El script lee el contador correspondiente del sistema, y responde por stdout con tres líneas: OID, tipo SNMP y valor. El script queda vivo en un bucle infinito respondiendo peticiones. La comunicación es exclusivamente texto plano por stdin/stdout — todo el transporte UDP, comunidades y protocolo SNMP lo maneja `snmpd`.

La configuración de `snmpd` es mínima: se define la dirección de escucha (UDP:161), la community SNMP permitida, y la línea `pass_persist` que registra el script bajo el OID raíz.

### Agente de los workers (`roce_agent.py`)

Expone dos tablas:

La primera tabla (bajo `.1.3.6.1.4.1.99999.1`) contiene los contadores InfiniBand de Soft-RoCE. Todos se leen del directorio `/sys/class/infiniband/rxe0/ports/1/hw_counters/`. Los contadores principales son `sent_pkts` y `rcvd_pkts` (paquetes RDMA transmitidos y recibidos), junto con contadores de errores como `rcvd_seq_err`, `retry_exceeded_err`, `duplicate_request` y otros indicadores de salud del enlace RDMA.

La segunda tabla (bajo `.1.3.6.1.4.1.99999.2`) contiene los contadores ECN del stack IP, leídos de `/proc/net/netstat`. Expone `InCEPkts` (paquetes marcados con Congestion Experienced), `InECT0Pkts`, `InECT1Pkts` e `InNoECTPkts`.

**Nota sobre contadores**: Soft-RoCE (rdma_rxe) solo expone `hw_counters/`, no el directorio `counters/` que sí existe en hardware InfiniBand real (ej. Mellanox ConnectX). Esto implica que no hay contadores de bytes a nivel InfiniBand — solo de paquetes. El throughput se expresa en paquetes por segundo, no en Mbps. Es una limitación inherente a la implementación software.

### Agente del switch (`ovs_agent.py`)

Expone contadores OVS por puerto bajo `.1.3.6.1.4.1.99999.3`, con esquema `.3.METRICA.PUERTO`. Los contadores se obtienen parseando la salida de `ovs-ofctl dump-ports br0` e incluyen bytes TX/RX, paquetes TX/RX, drops TX/RX y errores RX por cada puerto.

Un detalle de implementación: `snmpd` ejecuta el script como el usuario `Debian-snmp`, que no tiene permisos para ejecutar `ovs-ofctl`. Se resuelve con una regla sudoers específica que permite a ese usuario ejecutar exclusivamente `/usr/bin/ovs-ofctl` sin contraseña.

---

## 3. Controlador de tráfico

### Qué simula

El controlador (`traffic_controller.py`) simula el patrón de tráfico de un cluster de entrenamiento IA distribuido. Cada iteración de entrenamiento tiene dos fases: una fase de computación (forward + backward pass) donde no hay tráfico de red, seguida de una fase de comunicación (all-reduce) donde los workers intercambian gradientes.

### Ring all-reduce

La comunicación entre workers sigue el patrón ring all-reduce: cada worker envía datos a su vecino siguiente y recibe del anterior formando un anillo (W1→W2→W3→W1). Cada fase de comunicación ejecuta dos ráfagas RDMA (reduce-scatter y all-gather), que es lo que haría NCCL en un entrenamiento real con 3 GPUs.

El tráfico se genera con `ib_write_bw` del paquete `perftest`. Cada worker mantiene un servidor `ib_write_bw` escuchando permanentemente en un puerto dedicado para recibir datos de su vecino anterior, y actúa como cliente cuando necesita enviar al vecino siguiente. Como `ib_write_bw` no soporta modo servidor persistente (no tiene flag `--run_infinitely`), el servidor se relanza automáticamente en un hilo tras cada conexión completada.

### Varianza y realismo

La fase de computación tiene duración aleatoria (entre 1 y 5 segundos) para simular la variabilidad natural entre iteraciones. Cada worker tiene varianza independiente, lo que provoca desincronización gradual entre ellos — exactamente lo que ocurre en un cluster real donde la velocidad de cada GPU varía ligeramente.

El Worker 3 (hackeado) añade un delay extra de ~0.5 segundos por iteración, simulando la degradación causada por la criptominería que consume parte de sus recursos. Esto lo convierte en el straggler más frecuente del cluster, detectable en las métricas de asimetría.

### Nota sobre perftest

En la versión de `perftest` instalada, el flag `-g` significa multicast group, no GID index. El GID index se especifica con `-x`. Esto difiere de `ibv_rc_pingpong` donde `-g` sí es GID index. Además, los flags `-q` (queue depth) y `--report_gbits` no son reconocidos en esta versión. Los comandos de `ib_write_bw` usan exclusivamente `-d rxe0 -x 1 -p <puerto> -s 65536 -D 2`.

---

## 4. Gestor SNMP (monitor)

### Arquitectura

El gestor es un paquete Python (`src/snmp_manager/`) con separación estricta de responsabilidades en 7 módulos. El diseño sigue el principio de que cada módulo es testeable de forma independiente: se puede verificar el cálculo de métricas sin VMs corriendo, o la generación de puntos InfluxDB sin conexión a la base de datos.

### Data Transfer Objects (`models.py`)

Todas las estructuras de datos que fluyen entre capas son dataclasses inmutables (`frozen=True`). Esto garantiza que un dato, una vez construido, no es modificado por ninguna capa — se crea uno nuevo. Los DTOs cubren 5 niveles: lectura cruda de worker, lectura cruda de puerto OVS, métricas derivadas de worker, métricas derivadas de puerto OVS, y métricas globales del cluster.

Existe además un `PollCycle` mutable que agrupa los resultados de un ciclo de polling completo. No es frozen porque se construye progresivamente durante el poll.

### Polling SNMP (`poller.py`)

Usa la API asyncio nativa de pysnmp 7.x (`pysnmp.hlapi.v3arch.asyncio`). Las peticiones a los 4 agentes se lanzan en paralelo con `asyncio.gather`, lo que reduce la latencia del ciclo. Un timestamp único capturado al inicio del ciclo se comparte entre todas las lecturas para garantizar coherencia temporal.

Si un agente no responde (timeout de 2 segundos), el poller devuelve `None` para esa entidad sin bloquear al resto. Esto es necesario para el caso en que el Worker 3 sea desconectado vía OVS — el gestor debe seguir funcionando con los workers restantes.

### Cálculo de métricas (`calculator.py`)

Módulo sin I/O, exclusivamente lógica de cálculo. Mantiene el estado del poll anterior para calcular deltas entre ciclos (necesarios para tasas). El primer ciclo solo almacena valores base y no produce derivadas.

El cálculo de `rdma_vs_ovs_ratio` requiere cruzar datos de dos fuentes distintas (contadores RDMA del worker y contadores de tráfico del puerto OVS correspondiente). Este cruce se hace en la capa del calculador: recibe ambos DTOs como parámetros y devuelve el ratio. Ninguna capa inferior sabe que el cruce existe.

Protege contra counter wraps (contadores que vuelven a 0 tras llegar al máximo) tratando deltas negativos como 0.

### Escritura a InfluxDB (`writer.py`)

Implementa el patrón Repository: abstrae el acceso a InfluxDB detrás de una interfaz que habla exclusivamente en objetos del dominio (DTOs). Cada método acepta un DTO y lo transforma internamente en line protocol de InfluxDB. Al arrancar, crea la database y la retention policy si no existen.

Registra explícitamente cuando un worker es inalcanzable, escribiendo un punto con `unreachable=True` para que Grafana pueda mostrar huecos o alertas visuales.

### Orquestador (`manager.py`)

Coordina el ciclo completo en un bucle asyncio: poll → calculate → write, cada 5 segundos. Si el ciclo tarda más que el intervalo (por timeouts SNMP), loguea un warning y continúa inmediatamente sin acumular retraso.

El indexado de puertos OVS por worker se hace una vez por ciclo para evitar búsquedas repetidas durante el cálculo de métricas cruzadas.

### Entry point (`main.py`)

Construye los 4 componentes, conecta a InfluxDB, y ejecuta el manager. Maneja señales SIGINT/SIGTERM para shutdown limpio (cierra la conexión a InfluxDB).

### Configuración (`config.py`)

Centraliza toda la configuración: IPs de la topología, mapeo worker↔puerto OVS, definición de OIDs, parámetros SNMP (community, timeout, retries), intervalo de polling, y conexión a InfluxDB.

---

## 5. InfluxDB

### Instalación

InfluxDB 1.x desde los repositorios de Ubuntu (`apt install influxdb`). El cliente CLI es un paquete separado (`apt install influxdb-client`). El servidor se gestiona con systemd (`sudo systemctl start influxdb`).

### Esquema de datos

La database `roce_cluster` contiene 5 measurements:

**`roce_worker_raw`**: contadores crudos por worker. Tags: `worker`. Fields: todos los contadores de `hw_counters` y ECN tal cual llegan del agente SNMP.

**`roce_worker`**: métricas derivadas por worker. Tags: `worker`. Fields: `sent_pps`, `rcvd_pps`, `error_rate`, `retransmission_ratio`, `ecn_ratio`, `rdma_vs_ovs_ratio`.

**`ovs_port_raw`**: contadores crudos por puerto OVS. Tags: `port`, `connected_to`. Fields: bytes, paquetes, drops, errores TX/RX.

**`ovs_port`**: métricas derivadas por puerto. Tags: `port`, `connected_to`. Fields: `port_throughput_mbps`, `port_drop_rate`.

**`cluster`**: métricas globales. Fields: `asymmetry_index`, `max_min_spread`, `straggler_id`, `mean_pkt_rate`.

Retention policy `one_day`: los datos se eliminan automáticamente tras 24 horas.

---

## 6. Métricas del sistema

### Por worker

**`sent_pps` / `rcvd_pps`**: tasa de paquetes RDMA por segundo. Es la métrica principal de actividad y la base para detectar asimetrías. Se calcula como delta de `sent_pkts` entre polls dividido por el intervalo.

**`error_rate`**: tasa de errores RDMA por segundo. Suma de deltas de `rcvd_seq_err`, `retry_exceeded_err`, `rcvd_rnr_err`, `send_rnr_err`, `completer_retry_err`, `send_err` y `retry_rnr_exceeded_err`. Un pico indica problemas en el enlace.

**`retransmission_ratio`**: proporción de reintentos agotados sobre el total de paquetes enviados. Un 0.01% es normal; un 1% indica enlace degradado.

**`ecn_ratio`**: proporción de paquetes marcados con Congestion Experienced sobre el total (CE + NoECT). Indica nivel de congestión en la red.

**`rdma_vs_ovs_ratio`**: ratio entre paquetes vistos por OVS en el puerto del worker y paquetes RDMA reportados por el worker. En condiciones normales es cercano a 1.0 (todo el tráfico es RDMA). Si un worker genera tráfico no-RDMA (C2, minería), el ratio baja porque OVS ve más paquetes de los que InfiniBand reporta. Se calcula para todos los workers, y la detección de anomalías sale de comparar los ratios entre sí.

### Por puerto OVS

**`port_throughput_mbps`**: caudal total del puerto en Mbps. A diferencia de los workers (solo paquetes), OVS sí tiene contadores de bytes.

**`port_drop_rate`**: tasa de paquetes descartados por segundo. Correlaciona con `retry_exceeded_err` en el worker conectado a ese puerto.

### Cluster-wide

**`asymmetry_index`**: coeficiente de variación del `sent_pps` entre workers (desviación estándar / media). Cercano a 0 = cluster equilibrado. Cercano a 1 = un worker es significativamente más lento. Esta es la métrica más valiosa para gestión de clusters de entrenamiento IA, donde el worker más lento frena a todos los demás.

**`max_min_spread`**: diferencia absoluta entre el worker más rápido y el más lento en paquetes por segundo.

**`straggler_id`**: identificador del worker con menor `sent_pps` en cada ciclo. En el sistema actual, vm3 debería ser el straggler más frecuente por la degradación simulada del malware.

**`mean_pkt_rate`**: media de `sent_pps` de todos los workers. Indica la actividad general del cluster.

---

## 7. Despliegue y operación

### Prerrequisitos

La topología debe estar funcionando (ver `README-RDMA-Virtualizacion_2.md`). Soft-RoCE (`rxe0`) debe estar activo en los 3 workers. InfluxDB debe estar corriendo en WSL.

### Orden de arranque

1. Topología de red: `./start-topology.sh`
2. VMs: `./start-vm.sh 1`, `./start-vm.sh 2`, `./start-vm.sh 3`, `./start-switch.sh`
3. Controladores de tráfico: `./start-traffic.sh`
4. Gestor SNMP: `cd src/snmp_manager/ && python3 main.py`

### Despliegue de agentes

Si se modifica un agente, redesplegar con `./deploy_agent.sh` desde el directorio del fichero. El script copia el agente a los 3 workers, reinicia `snmpd` en cada uno, y ejecuta un `snmpwalk` de verificación.

### Verificación

Comprobar que los agentes responden con valores no-cero (requiere tráfico activo):

```
snmpwalk -v2c -c public 10.10.0.1 .1.3.6.1.4.1.99999
snmpwalk -v2c -c public 10.10.0.10 .1.3.6.1.4.1.99999.3
```

Comprobar que InfluxDB recibe datos:

```
influx -database roce_cluster -execute "SELECT * FROM roce_worker ORDER BY time DESC LIMIT 5"
influx -database roce_cluster -execute "SELECT * FROM cluster ORDER BY time DESC LIMIT 5"
```

### Parada

Parar tráfico: `./start-traffic.sh stop`. Parar gestor: `Ctrl+C` (shutdown limpio).

---

## 8. Notas técnicas relevantes

### pysnmp 7.x

La versión 7 de pysnmp cambió la API respecto a versiones anteriores. Los imports son de `pysnmp.hlapi.v3arch.asyncio`. Todas las funciones usan snake_case (`get_cmd`, no `getCmd`). `UdpTransportTarget` se crea con una factory async (`await UdpTransportTarget.create(...)`). Las llamadas son nativamente asyncio, sin necesidad de thread pool.

### Persistencia tras reinicio de VMs

Las VMs pierden la configuración de Soft-RoCE (`rxe0`) al reiniciar si no se habilitó el servicio systemd `setup-roce.service` (documentado en el README de topología, sección 9). Sin `rxe0` activo, los contadores de `hw_counters` no existen y el agente devuelve todo a 0. Si los valores llegan a 0 con tráfico activo, verificar `rdma link` dentro de la VM.

La topología de red en WSL (bridges y TAPs) tampoco sobrevive a un reinicio de WSL. Ejecutar `start-topology.sh` antes de arrancar las VMs.

### Congestión simulada (pendiente de activación)

Se puede simular congestión ECN y pérdida de paquetes en el switch con `tc netem`:

- ECN al 1% en todos los puertos: marca paquetes como Congestion Experienced
- Drops al 0.1% en el puerto del Worker 3: simula enlace degradado

Esto haría que `ecn_ratio` suba en todos los workers y que `retry_exceeded_err` suba en el Worker 3. No se ha activado aún para no interferir con la verificación del pipeline base.
